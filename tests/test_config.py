from __future__ import annotations

from roostoo_bot.config import load_settings


def test_load_settings_uses_env_values(monkeypatch) -> None:
    monkeypatch.setenv("BOT_NAME", "env-bot")
    monkeypatch.setenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT")
    monkeypatch.setenv("MAX_HOLD_BARS", "9")
    monkeypatch.setenv("TRANCHE_SCHEME", "0.35,0.35,0.30")
    monkeypatch.setenv("USE_BTC_FILTER", "false")
    monkeypatch.setenv("ROOSTOO_ENDPOINTS_JSON", '{"balances":"/v3/balance","open_orders":"/v3/query_order"}')

    settings = load_settings()

    assert settings.bot_name == "env-bot"
    assert settings.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert settings.max_hold_bars == 9
    assert settings.tranche_scheme == (0.35, 0.35, 0.30)
    assert settings.use_btc_filter is False
    assert settings.roostoo_endpoints["balances"] == "/v3/balance"
    assert settings.roostoo_endpoints["open_orders"] == "/v3/query_order"
