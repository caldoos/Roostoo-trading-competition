from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


def _split_csv(value: str, default: list[str]) -> list[str]:
    if not value.strip():
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_tranche_scheme(value: str) -> tuple[float, float, float]:
    parts = [float(x.strip()) for x in value.split(",") if x.strip()]
    if len(parts) != 3:
        raise ValueError("TRANCHE_SCHEME must contain exactly 3 comma-separated values.")
    if abs(sum(parts) - 1.0) > 1e-6:
        raise ValueError("TRANCHE_SCHEME must sum to 1.0.")
    return tuple(parts)  # type: ignore[return-value]


def _parse_json(value: str) -> dict[str, str]:
    if not value.strip():
        return {}
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("ROOSTOO_ENDPOINTS_JSON must be a JSON object.")
    return {str(k): str(v) for k, v in payload.items()}


@dataclass(frozen=True, slots=True)
class Settings:
    bot_name: str
    polling_seconds: int
    telegram_poll_seconds: int
    candle_interval: str
    symbols: list[str]
    initial_equity: float
    risk_per_trade: float
    max_open_positions: int
    max_position_notional_pct: float
    ema_span: int
    momentum_bars: int
    breakout_lookback: int
    exit_lookback: int
    max_hold_bars: int
    trailing_stop_pct: float
    tranche_scheme: tuple[float, float, float]
    add_delay_bars: int
    use_btc_filter: bool
    default_order_type: str
    live_trading: bool
    state_path: Path
    event_log_path: Path
    heartbeat_log_path: Path
    telegram_offset_path: Path
    candle_cache_dir: Path
    telegram_token: str
    telegram_chat_id: str
    binance_base_url: str
    roostoo_base_url: str
    roostoo_api_key: str
    roostoo_api_secret: str
    roostoo_timeout_seconds: int
    roostoo_endpoints: dict[str, str]


def load_settings() -> Settings:
    root = Path(__file__).resolve().parents[1]
    default_symbols = [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "BNBUSDT",
        "DOGEUSDT",
        "AVAXUSDT",
        "ADAUSDT",
        "LINKUSDT",
        "SUIUSDT",
        "FETUSDT",
    ]
    return Settings(
        bot_name=os.getenv("BOT_NAME", "roostoo-trend-only"),
        polling_seconds=int(os.getenv("POLLING_SECONDS", "60")),
        telegram_poll_seconds=int(os.getenv("TELEGRAM_POLL_SECONDS", "5")),
        candle_interval=os.getenv("CANDLE_INTERVAL", "1h"),
        symbols=_split_csv(os.getenv("SYMBOLS", ""), default_symbols),
        initial_equity=float(os.getenv("INITIAL_EQUITY", "1000000")),
        risk_per_trade=float(os.getenv("RISK_PER_TRADE", "0.015")),
        max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "5")),
        max_position_notional_pct=float(os.getenv("MAX_POSITION_NOTIONAL_PCT", "0.20")),
        ema_span=int(os.getenv("EMA_SPAN", "80")),
        momentum_bars=int(os.getenv("MOMENTUM_BARS", "24")),
        breakout_lookback=int(os.getenv("BREAKOUT_LOOKBACK", "16")),
        exit_lookback=int(os.getenv("EXIT_LOOKBACK", "24")),
        max_hold_bars=int(os.getenv("MAX_HOLD_BARS", "216")),
        trailing_stop_pct=float(os.getenv("TRAILING_STOP_PCT", "0.08")),
        tranche_scheme=_parse_tranche_scheme(os.getenv("TRANCHE_SCHEME", "0.35,0.35,0.30")),
        add_delay_bars=int(os.getenv("ADD_DELAY_BARS", "8")),
        use_btc_filter=os.getenv("USE_BTC_FILTER", "false").lower() == "true",
        default_order_type=os.getenv("DEFAULT_ORDER_TYPE", "limit"),
        live_trading=os.getenv("LIVE_TRADING", "false").lower() == "true",
        state_path=root / "outputs" / "bot_state.json",
        event_log_path=root / "outputs" / "events.jsonl",
        heartbeat_log_path=root / "outputs" / "heartbeat.jsonl",
        telegram_offset_path=root / "outputs" / "telegram_offset.json",
        candle_cache_dir=root / "outputs" / "candle_cache",
        telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        binance_base_url=os.getenv("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/"),
        roostoo_base_url=os.getenv("ROOSTOO_BASE_URL", "").rstrip("/"),
        roostoo_api_key=os.getenv("ROOSTOO_API_KEY", "").strip(),
        roostoo_api_secret=os.getenv("ROOSTOO_API_SECRET", "").strip(),
        roostoo_timeout_seconds=int(os.getenv("ROOSTOO_TIMEOUT_SECONDS", "30")),
        roostoo_endpoints=_parse_json(os.getenv("ROOSTOO_ENDPOINTS_JSON", "")),
    )
