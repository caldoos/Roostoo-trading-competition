from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import requests

from roostoo_bot.config import Settings
from roostoo_bot.logging_utils import utc_now_iso
from roostoo_bot.models import AccountSnapshot


class RoostooClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.roostoo_base_url.rstrip("/")
        self.api_key = settings.roostoo_api_key
        self.api_secret = settings.roostoo_api_secret
        self.timeout_seconds = settings.roostoo_timeout_seconds
        self.endpoints = settings.roostoo_endpoints
        self.session = requests.Session()

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.api_secret)

    def _endpoint(self, name: str, default: str) -> str:
        path = self.endpoints.get(name, default)
        if not path.startswith("/"):
            path = "/" + path
        return path

    @staticmethod
    def _timestamp_ms() -> str:
        return str(int(time.time() * 1000))

    @staticmethod
    def _normalize_pair(symbol: str) -> str:
        symbol = symbol.upper().strip()
        if "/" in symbol:
            return symbol
        if symbol.endswith("USDT"):
            return f"{symbol[:-4]}/USD"
        if symbol.endswith("USD"):
            return f"{symbol[:-3]}/USD"
        return symbol

    @staticmethod
    def _to_string_payload(payload: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in payload.items():
            if value is None:
                continue
            if isinstance(value, bool):
                out[key] = "TRUE" if value else "FALSE"
            else:
                out[key] = str(value)
        return out

    def _encode_params(self, params: dict[str, Any]) -> str:
        normalized = self._to_string_payload(params)
        ordered = dict(sorted(normalized.items(), key=lambda item: item[0]))
        return urlencode(ordered, safe="/")

    def _signature(self, encoded_params: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            encoded_params.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self, *, signed: bool, signature: str | None = None, form_encoded: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if form_encoded:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if signed:
            headers["RST-API-KEY"] = self.api_key
            if signature is None:
                raise ValueError("Signature is required for signed requests.")
            headers["MSG-SIGNATURE"] = signature
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        if signed and not self.is_configured():
            raise RuntimeError("Roostoo client is not configured.")

        payload = dict(params or {})
        if signed and "timestamp" not in payload:
            payload["timestamp"] = self._timestamp_ms()

        encoded_payload = self._encode_params(payload) if payload else ""
        signature = self._signature(encoded_payload) if signed else None

        request_kwargs: dict[str, Any] = {
            "method": method,
            "url": f"{self.base_url}{path}",
            "headers": self._headers(signed=signed, signature=signature, form_encoded=(method.upper() == "POST")),
            "timeout": self.timeout_seconds,
        }
        if method.upper() == "GET":
            request_kwargs["params"] = payload
        elif encoded_payload:
            request_kwargs["data"] = encoded_payload

        response = self.session.request(**request_kwargs)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    def get_server_time(self) -> dict[str, Any]:
        result = self._request("GET", self._endpoint("server_time", "/v3/serverTime"))
        return result if isinstance(result, dict) else {"raw": result}

    def get_symbols(self) -> dict[str, Any]:
        result = self._request("GET", self._endpoint("symbols", "/v3/exchangeInfo"))
        return result if isinstance(result, dict) else {"raw": result}

    def get_ticker(self, pair: str | None = None) -> dict[str, Any]:
        params = {"pair": pair} if pair else None
        result = self._request("GET", self._endpoint("ticker", "/v3/ticker"), params=params, signed=False)
        return result if isinstance(result, dict) else {"raw": result}

    def get_balances(self) -> dict[str, Any]:
        result = self._request("GET", self._endpoint("balances", "/v3/balance"), signed=True)
        return result if isinstance(result, dict) else {"raw": result}

    def get_pending_count(self) -> dict[str, Any]:
        result = self._request("GET", self._endpoint("pending_count", "/v3/pending_count"), signed=True)
        return result if isinstance(result, dict) else {"raw": result}

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
        if order_id:
            params["order_id"] = order_id
        else:
            if pair:
                params["pair"] = self._normalize_pair(pair)
            if limit is not None:
                params["limit"] = limit
            if offset is not None:
                params["offset"] = offset
            params["pending_only"] = pending_only
        result = self._request("POST", self._endpoint("open_orders", "/v3/query_order"), params=params, signed=True)
        return result if isinstance(result, dict) else {"raw": result}

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str,
        price: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pair": self._normalize_pair(symbol),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity,
        }
        if price is not None:
            payload["price"] = price
        result = self._request("POST", self._endpoint("place_order", "/v3/place_order"), params=payload, signed=True)
        return result if isinstance(result, dict) else {"raw": result}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            self._endpoint("cancel_order", "/v3/cancel_order"),
            params={"order_id": order_id},
            signed=True,
        )
        return result if isinstance(result, dict) else {"raw": result}

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _extract_open_orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("Orders", "OrderList", "Data", "OrderMatched"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        order_detail = payload.get("OrderDetail")
        if isinstance(order_detail, dict):
            return [order_detail]
        return []

    def fetch_account_snapshot(self, initial_equity: float) -> AccountSnapshot:
        balances = self.get_balances()
        pending_count = self.get_pending_count()
        total_pending = self._safe_float(pending_count.get("TotalPending"), 0.0)
        open_orders_payload = self.query_orders(pending_only=True) if total_pending > 0 else {"Orders": []}
        try:
            ticker_payload = self.get_ticker()
        except requests.HTTPError:
            ticker_payload = {"Data": {}}

        wallet = {}
        if isinstance(balances, dict):
            wallet = balances.get("SpotWallet") or balances.get("Wallet") or {}
        tickers = ticker_payload.get("Data", {}) if isinstance(ticker_payload, dict) else {}

        usd_wallet = wallet.get("USD", {}) if isinstance(wallet, dict) else {}
        cash = self._safe_float(usd_wallet.get("Free"), initial_equity)
        equity = cash + self._safe_float(usd_wallet.get("Lock"))

        if isinstance(wallet, dict):
            for asset, asset_wallet in wallet.items():
                if asset == "USD" or not isinstance(asset_wallet, dict):
                    continue
                amount = self._safe_float(asset_wallet.get("Free")) + self._safe_float(asset_wallet.get("Lock"))
                if amount <= 0:
                    continue
                pair = f"{asset}/USD"
                last_price = self._safe_float(tickers.get(pair, {}).get("LastPrice"))
                equity += amount * last_price

        return AccountSnapshot(
            timestamp=utc_now_iso(),
            cash=cash,
            equity=equity,
            open_orders=self._extract_open_orders(open_orders_payload),
            raw={
                "balances": balances,
                "open_orders": open_orders_payload,
                "tickers": ticker_payload,
            },
        )
