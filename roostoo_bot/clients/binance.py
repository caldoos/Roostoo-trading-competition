from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import requests


class BinanceClient:
    def __init__(self, base_url: str, timeout_seconds: int = 30, market_type: str = "spot") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.market_type = market_type.lower().strip()
        self.session = requests.Session()

    def _get_json(self, path: str, params: dict[str, object]) -> list[list[object]]:
        response = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Unexpected Binance response format.")
        return payload

    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        params: dict[str, object] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        path = "/fapi/v1/klines" if self.market_type in {"usdtm", "futures", "binance_futures"} else "/api/v3/klines"
        rows = self._get_json(path, params)
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        frame = pd.DataFrame(
            rows,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_volume",
                "trade_count",
                "taker_buy_base",
                "taker_buy_quote",
                "ignore",
            ],
        )
        frame["timestamp"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
        numeric_cols = ["open", "high", "low", "close", "volume"]
        frame[numeric_cols] = frame[numeric_cols].astype(float)
        return frame.set_index("timestamp")[numeric_cols].sort_index()

    def fetch_recent_klines(self, symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
        return self.fetch_klines(symbol, interval, limit=limit)

    def fetch_usdt_m_symbols(
        self,
        *,
        limit: int = 0,
        excluded_symbols: list[str] | None = None,
    ) -> list[str]:
        excluded = {symbol.upper().strip() for symbol in (excluded_symbols or [])}
        info_response = self.session.get(
            f"{self.base_url}/fapi/v1/exchangeInfo",
            timeout=self.timeout_seconds,
        )
        info_response.raise_for_status()
        info_payload = info_response.json()
        tradable = {
            item["symbol"]
            for item in info_payload.get("symbols", [])
            if item.get("contractType") == "PERPETUAL"
            and item.get("quoteAsset") == "USDT"
            and item.get("status") == "TRADING"
            and item.get("symbol") not in excluded
        }
        ticker_response = self.session.get(
            f"{self.base_url}/fapi/v1/ticker/24hr",
            timeout=self.timeout_seconds,
        )
        ticker_response.raise_for_status()
        tickers = ticker_response.json()
        ranked = sorted(
            (
                (item["symbol"], float(item.get("quoteVolume", 0.0) or 0.0))
                for item in tickers
                if item.get("symbol") in tradable
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        symbols = [symbol for symbol, _ in ranked]
        return symbols[:limit] if limit > 0 else symbols

    def fetch_history_since(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        *,
        limit: int = 1000,
    ) -> pd.DataFrame:
        return self.fetch_klines(symbol, interval, start_time_ms=start_time_ms, limit=limit)

    @staticmethod
    def latest_closed_bar_time() -> datetime:
        return datetime.now(UTC)
