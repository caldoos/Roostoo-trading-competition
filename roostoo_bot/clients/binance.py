from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import requests


class BinanceClient:
    def __init__(self, base_url: str, timeout_seconds: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
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
        rows = self._get_json("/api/v3/klines", params)
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
