from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import requests

from roostoo_bot.config import Settings
from roostoo_bot.logging_utils import utc_now_iso
from roostoo_bot.models import AccountSnapshot


class BinanceFuturesClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.binance_futures_base_url.rstrip("/")
        self.api_key = settings.binance_api_key
        self.api_secret = settings.binance_api_secret
        self.timeout_seconds = settings.roostoo_timeout_seconds
        self.max_retries = max(settings.roostoo_max_retries, 1)
        self.backoff_seconds = max(settings.roostoo_backoff_seconds, 0.0)
        self.leverage = max(int(settings.binance_futures_leverage), 1)
        self.session = requests.Session()
        self._symbol_rules: dict[str, dict[str, Any]] | None = None
        self._leverage_set: set[str] = set()

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.api_secret)

    @staticmethod
    def _timestamp_ms() -> int:
        return int(time.time() * 1000)

    def _signature(self, encoded_params: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            encoded_params.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        retry_safe: bool = False,
    ) -> Any:
        if signed and not self.is_configured():
            raise RuntimeError("Binance futures client is not configured.")

        payload = dict(params or {})
        headers = {"Accept": "application/json"}
        if signed:
            payload.setdefault("timestamp", self._timestamp_ms())
            payload.setdefault("recvWindow", 5000)
            encoded = urlencode(payload, doseq=True)
            payload["signature"] = self._signature(encoded)
            headers["X-MBX-APIKEY"] = self.api_key

        request_kwargs: dict[str, Any] = {
            "method": method.upper(),
            "url": f"{self.base_url}{path}",
            "headers": headers,
            "timeout": self.timeout_seconds,
        }
        if method.upper() in {"GET", "DELETE"}:
            request_kwargs["params"] = payload
        else:
            request_kwargs["params"] = payload

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(**request_kwargs)
                response.raise_for_status()
                if not response.content:
                    return {}
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                should_retry = retry_safe and (
                    status_code in {408, 425, 429, 500, 502, 503, 504} or status_code is None
                )
                if not should_retry or attempt >= self.max_retries - 1:
                    raise
                time.sleep(self.backoff_seconds * (2**attempt))

        if last_error is not None:
            raise last_error
        raise RuntimeError("Binance futures request failed without a concrete exception.")

    def get_server_time(self) -> dict[str, Any]:
        result = self._request("GET", "/fapi/v1/time", retry_safe=True)
        return result if isinstance(result, dict) else {"raw": result}

    def get_symbols(self) -> dict[str, Any]:
        result = self._request("GET", "/fapi/v1/exchangeInfo", retry_safe=True)
        return result if isinstance(result, dict) else {"raw": result}

    def _load_symbol_rules(self) -> dict[str, dict[str, Any]]:
        if self._symbol_rules is not None:
            return self._symbol_rules
        payload = self.get_symbols()
        rules: dict[str, dict[str, Any]] = {}
        for item in payload.get("symbols", []):
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            filters = {flt.get("filterType"): flt for flt in item.get("filters", []) if isinstance(flt, dict)}
            rules[str(item["symbol"])] = {
                "status": item.get("status"),
                "contractType": item.get("contractType"),
                "quoteAsset": item.get("quoteAsset"),
                "price_filter": filters.get("PRICE_FILTER", {}),
                "lot_size": filters.get("LOT_SIZE", {}),
                "market_lot_size": filters.get("MARKET_LOT_SIZE", {}),
                "min_notional": filters.get("MIN_NOTIONAL", {}),
            }
        self._symbol_rules = rules
        return rules

    @staticmethod
    def _floor_to_step(value: float, step: str) -> float:
        decimal_value = Decimal(str(value))
        decimal_step = Decimal(str(step))
        if decimal_step <= 0:
            return float(decimal_value)
        return float((decimal_value / decimal_step).to_integral_value(rounding=ROUND_DOWN) * decimal_step)

    def normalize_order_params(
        self,
        *,
        symbol: str,
        quantity: float,
        price: float | None,
        order_type: str = "market",
    ) -> dict[str, Any]:
        symbol = symbol.upper().strip()
        rules = self._load_symbol_rules().get(symbol)
        if rules is None:
            return {"ok": False, "error": f"Symbol not found in Binance futures exchangeInfo: {symbol}"}
        if rules.get("status") != "TRADING":
            return {"ok": False, "error": f"Symbol is not trading on Binance futures: {symbol}"}

        lot_filter = rules["market_lot_size"] if order_type.lower() == "market" else rules["lot_size"]
        step_size = str(lot_filter.get("stepSize") or rules["lot_size"].get("stepSize") or "0.00000001")
        min_qty = float(lot_filter.get("minQty") or rules["lot_size"].get("minQty") or 0.0)
        normalized_qty = self._floor_to_step(quantity, step_size)
        if normalized_qty <= 0 or normalized_qty < min_qty:
            return {
                "ok": False,
                "error": f"Rounded quantity {normalized_qty} below minQty {min_qty} for {symbol}",
            }

        normalized_price = price
        if price is not None:
            tick_size = str(rules["price_filter"].get("tickSize") or "0.00000001")
            normalized_price = self._floor_to_step(price, tick_size)
            if normalized_price <= 0:
                return {"ok": False, "error": f"Rounded price {normalized_price} is invalid for {symbol}"}

        min_notional = float(rules["min_notional"].get("notional") or rules["min_notional"].get("minNotional") or 0.0)
        notional_price = normalized_price if normalized_price is not None else price
        if min_notional > 0 and notional_price is not None and normalized_qty * notional_price < min_notional:
            return {
                "ok": False,
                "error": f"Rounded notional {normalized_qty * notional_price} below minNotional {min_notional} for {symbol}",
            }

        return {
            "ok": True,
            "symbol": symbol,
            "quantity": normalized_qty,
            "price": normalized_price,
            "rules": rules,
        }

    def ensure_leverage(self, symbol: str) -> None:
        symbol = symbol.upper().strip()
        if self.leverage <= 0 or symbol in self._leverage_set:
            return
        self._request(
            "POST",
            "/fapi/v1/leverage",
            params={"symbol": symbol, "leverage": self.leverage},
            signed=True,
        )
        self._leverage_set.add(symbol)

    def get_balances(self) -> dict[str, Any]:
        balances = self._request("GET", "/fapi/v2/balance", signed=True, retry_safe=True)
        wallet: dict[str, dict[str, float]] = {}
        if isinstance(balances, list):
            for row in balances:
                if not isinstance(row, dict):
                    continue
                asset = str(row.get("asset", ""))
                if not asset:
                    continue
                wallet[asset] = {
                    "Free": float(row.get("availableBalance", 0.0) or 0.0),
                    "Lock": float(row.get("balance", 0.0) or 0.0)
                    - float(row.get("availableBalance", 0.0) or 0.0),
                    "Balance": float(row.get("balance", 0.0) or 0.0),
                }
        return {"Success": True, "FuturesWallet": wallet, "raw": balances}

    def query_orders(
        self,
        *,
        order_id: str | None = None,
        pair: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        pending_only: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if pair:
            params["symbol"] = pair.upper().replace("/", "")
        if order_id:
            params["orderId"] = order_id
        if pending_only:
            result = self._request("GET", "/fapi/v1/openOrders", params=params, signed=True, retry_safe=True)
        else:
            if "symbol" not in params:
                return {"Orders": []}
            if limit is not None:
                params["limit"] = limit
            result = self._request("GET", "/fapi/v1/allOrders", params=params, signed=True, retry_safe=True)
        return {"Orders": result if isinstance(result, list) else [result]}

    def get_pending_count(self) -> dict[str, Any]:
        payload = self.query_orders(pending_only=True)
        orders = payload.get("Orders", [])
        return {"Success": True, "TotalPending": len(orders), "Orders": orders}

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
    ) -> dict[str, Any]:
        order_type_normalized = order_type.upper()
        normalized = self.normalize_order_params(
            symbol=symbol,
            quantity=quantity,
            price=price,
            order_type=order_type,
        )
        if not normalized.get("ok"):
            return {"Success": False, "ErrMsg": normalized.get("error", "Order normalization failed")}

        symbol = str(normalized["symbol"])
        self.ensure_leverage(symbol)
        payload: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type_normalized,
            "quantity": normalized["quantity"],
            "newOrderRespType": "RESULT",
        }
        if side.lower() == "sell":
            payload["reduceOnly"] = "true"
        if order_type_normalized == "LIMIT":
            if normalized.get("price") is None:
                return {"Success": False, "ErrMsg": "Limit orders require a price."}
            payload["price"] = normalized["price"]
            payload["timeInForce"] = "GTC"
        result = self._request("POST", "/fapi/v1/order", params=payload, signed=True)
        if not isinstance(result, dict):
            return {"raw": result}
        return {
            **result,
            "Success": True,
            "order_id": result.get("orderId"),
            "status": str(result.get("status", "")).lower(),
            "filled_price": self._extract_avg_price(result, fallback=price),
            "filled_qty": float(result.get("executedQty", 0.0) or 0.0),
        }

    @staticmethod
    def _extract_avg_price(payload: dict[str, Any], fallback: float | None = None) -> float | None:
        avg_price = float(payload.get("avgPrice", 0.0) or 0.0)
        if avg_price > 0:
            return avg_price
        price = float(payload.get("price", 0.0) or 0.0)
        if price > 0:
            return price
        return fallback

    def cancel_order(self, order_id: str, symbol: str | None = None) -> dict[str, Any]:
        if symbol is None:
            raise ValueError("Binance futures cancel_order requires symbol.")
        result = self._request(
            "DELETE",
            "/fapi/v1/order",
            params={"symbol": symbol.upper(), "orderId": order_id},
            signed=True,
        )
        return result if isinstance(result, dict) else {"raw": result}

    @staticmethod
    def _extract_open_orders(payload: Any) -> list[dict[str, Any]]:
        return payload if isinstance(payload, list) else []

    def fetch_account_snapshot(self, initial_equity: float) -> AccountSnapshot:
        account = self._request("GET", "/fapi/v2/account", signed=True, retry_safe=True)
        if not isinstance(account, dict):
            raise ValueError("Unexpected Binance futures account payload.")

        cash = float(account.get("availableBalance", initial_equity) or initial_equity)
        equity = float(account.get("totalMarginBalance", account.get("totalWalletBalance", cash)) or cash)
        open_orders_payload = self.query_orders(pending_only=True)
        positions = [
            pos for pos in account.get("positions", [])
            if isinstance(pos, dict) and abs(float(pos.get("positionAmt", 0.0) or 0.0)) > 0
        ]
        return AccountSnapshot(
            timestamp=utc_now_iso(),
            cash=cash,
            equity=equity,
            open_orders=self._extract_open_orders(open_orders_payload.get("Orders", [])),
            raw={
                "exchange": "binance_futures",
                "account": account,
                "balances": self.get_balances(),
                "positions": positions,
                "open_orders": open_orders_payload,
            },
        )
