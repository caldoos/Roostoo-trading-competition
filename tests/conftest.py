from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roostoo_bot.config import Settings


def build_settings(tmp_path: Path, **overrides: object) -> Settings:
    base = Settings(
        bot_name="test-bot",
        polling_seconds=60,
        telegram_poll_seconds=5,
        candle_interval="4h",
        symbols=["BTCUSDT", "ETHUSDT"],
        initial_equity=1_000_000.0,
        risk_per_trade=0.015,
        max_open_positions=5,
        max_position_notional_pct=0.20,
        ema_span=20,
        momentum_bars=6,
        breakout_lookback=6,
        exit_lookback=6,
        max_hold_bars=9,
        trailing_stop_pct=0.06,
        tranche_scheme=(0.35, 0.35, 0.30),
        add_delay_bars=2,
        use_btc_filter=False,
        default_order_type="limit",
        live_trading=False,
        state_path=tmp_path / "bot_state.json",
        event_log_path=tmp_path / "events.jsonl",
        heartbeat_log_path=tmp_path / "heartbeat.jsonl",
        scan_diagnostics_path=tmp_path / "scan_diagnostics.jsonl",
        telegram_offset_path=tmp_path / "telegram_offset.json",
        candle_cache_dir=tmp_path / "candle_cache",
        telegram_token="",
        telegram_chat_id="",
        telegram_log_id="",
        binance_base_url="https://api.binance.com",
        roostoo_base_url="https://mock-api.roostoo.com",
        roostoo_api_key="test-key",
        roostoo_api_secret="test-secret",
        roostoo_timeout_seconds=30,
        roostoo_max_retries=3,
        roostoo_backoff_seconds=1.0,
        roostoo_endpoints={
            "server_time": "/v3/serverTime",
            "balances": "/v3/balance",
            "pending_count": "/v3/pending_count",
            "open_orders": "/v3/query_order",
            "place_order": "/v3/place_order",
            "cancel_order": "/v3/cancel_order",
            "symbols": "/v3/exchangeInfo",
            "ticker": "/v3/ticker",
        },
    )
    return replace(base, **overrides)


def make_frame(close_values: list[float], *, high_pad: float = 1.0, low_pad: float = 1.0) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(close_values), freq="4h", tz="UTC")
    close = pd.Series(close_values, index=index, dtype=float)
    frame = pd.DataFrame(index=index)
    frame["open"] = close.shift(1).fillna(close.iloc[0])
    frame["high"] = close + high_pad
    frame["low"] = close - low_pad
    frame["close"] = close
    frame["volume"] = 1000.0
    return frame


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)
