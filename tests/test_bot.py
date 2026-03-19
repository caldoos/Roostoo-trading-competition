from __future__ import annotations

import json

import requests

from roostoo_bot.bot import TrendBot
from roostoo_bot.models import AccountSnapshot, OrderInstruction, ScanDiagnostic, StrategySnapshot

from tests.conftest import build_settings, make_frame


def test_run_cycle_paper_mode_updates_state_and_logs(monkeypatch, tmp_path) -> None:
    settings = build_settings(tmp_path)
    bot = TrendBot(settings)
    frame = make_frame([100 + i for i in range(30)])
    signal_ts = frame.index[-1]
    candle_map = {"BTCUSDT": frame, "ETHUSDT": frame}

    monkeypatch.setattr(bot, "refresh_candles", lambda: candle_map)
    monkeypatch.setattr(bot, "_send_scan_summary", lambda eligible, actions, ts: None)
    monkeypatch.setattr(
        bot.strategy,
        "evaluate",
        lambda candle_map, state, account, signal_ts: StrategySnapshot(
            timestamp=signal_ts.isoformat(),
                ranked_symbols=["BTCUSDT"],
                eligible_symbols=["BTCUSDT"],
                trend_scores={"BTCUSDT": 1.5},
                actions=[
                OrderInstruction(
                    symbol="BTCUSDT",
                    side="buy",
                    quantity=1.0,
                    order_type="limit",
                    limit_price=100.0,
                    reason="trend_entry",
                    tranche_number=1,
                    target_notional=100.0,
                    stop_price=90.0,
                    trend_score=1.5,
                    breakout_high=99.0,
                    exit_low=90.0,
                    trend_ema=95.0,
                    trend_ema_slope=1.0,
                    reference_close=100.0,
                )
            ],
            diagnostics=[
                    ScanDiagnostic(
                        symbol="BTCUSDT",
                        close=100.0,
                        breakout_high=99.0,
                        exit_low=90.0,
                        trend_ema=95.0,
                        trend_ema_slope=1.0,
                        trend_score=1.5,
                        base_entry=True,
                        eligible=True,
                        in_position=False,
                        selected_for_entry=True,
                    ),
                    ScanDiagnostic(
                        symbol="ETHUSDT",
                        close=90.0,
                        breakout_high=99.0,
                        exit_low=85.0,
                        trend_ema=95.0,
                        trend_ema_slope=-1.0,
                        trend_score=-0.5,
                        base_entry=False,
                        eligible=False,
                        in_position=False,
                        failed_reasons=["close_below_breakout_high", "close_below_trend_ema", "trend_ema_slope_nonpositive"],
                    ),
                ],
            ),
        )

    processed = bot.run_cycle()

    assert processed is True
    assert bot.state.last_processed_candle_ts == signal_ts.isoformat()
    assert "BTCUSDT" in bot.state.positions
    assert bot.state.last_cash == 1_000_000.0 - 100.0

    event_lines = settings.event_log_path.read_text(encoding="utf-8").strip().splitlines()
    heartbeat_lines = settings.heartbeat_log_path.read_text(encoding="utf-8").strip().splitlines()
    diagnostic_lines = settings.scan_diagnostics_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(event_lines) == 1
    assert len(heartbeat_lines) == 1
    assert len(diagnostic_lines) == 2

    event = json.loads(event_lines[0])
    heartbeat = json.loads(heartbeat_lines[0])
    diagnostics = [json.loads(line) for line in diagnostic_lines]
    assert event["symbol"] == "BTCUSDT"
    assert event["action"] == "entry"
    assert heartbeat["status"] == "processed"
    assert any(item["symbol"] == "BTCUSDT" and item["selected_for_entry"] for item in diagnostics)


def test_run_cycle_sends_scan_log_summary_to_log_chat(monkeypatch, tmp_path) -> None:
    settings = build_settings(tmp_path, telegram_token="token", telegram_chat_id="123", telegram_log_id="999")
    bot = TrendBot(settings)
    frame = make_frame([100 + i for i in range(30)])
    signal_ts = frame.index[-1]
    candle_map = {"BTCUSDT": frame, "ETHUSDT": frame}
    sent_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(bot, "refresh_candles", lambda: candle_map)
    monkeypatch.setattr(bot.notifier, "send", lambda text: True)
    monkeypatch.setattr(bot.notifier, "send_to", lambda chat_id, text: sent_messages.append((str(chat_id), text)) or True)
    monkeypatch.setattr(
        bot.strategy,
        "evaluate",
        lambda candle_map, state, account, signal_ts: StrategySnapshot(
            timestamp=signal_ts.isoformat(),
            ranked_symbols=["BTCUSDT"],
            eligible_symbols=["BTCUSDT"],
            trend_scores={"BTCUSDT": 1.5},
            actions=[],
            diagnostics=[
                ScanDiagnostic(
                    symbol="BTCUSDT",
                    close=100.0,
                    breakout_high=99.0,
                    exit_low=90.0,
                    trend_ema=95.0,
                    trend_ema_slope=1.0,
                    trend_score=1.5,
                    base_entry=True,
                    eligible=True,
                    in_position=False,
                ),
                ScanDiagnostic(
                    symbol="ETHUSDT",
                    close=90.0,
                    breakout_high=99.0,
                    exit_low=85.0,
                    trend_ema=95.0,
                    trend_ema_slope=-1.0,
                    trend_score=-0.5,
                    base_entry=False,
                    eligible=False,
                    in_position=False,
                    failed_reasons=["close_below_trend_ema"],
                ),
            ],
        ),
    )

    processed = bot.run_cycle()

    assert processed is True
    assert any(chat_id == "999" and "Top ranked:" in text for chat_id, text in sent_messages)


def test_poll_telegram_commands_replies_to_help(monkeypatch, tmp_path) -> None:
    settings = build_settings(tmp_path, telegram_token="token", telegram_chat_id="123")
    bot = TrendBot(settings)
    sent_messages: list[tuple[str, str]] = []

    monkeypatch.setattr(
        bot.notifier,
        "get_updates",
        lambda offset=None, timeout=0: [
            {
                "update_id": 99,
                "message": {
                    "chat": {"id": 123},
                    "text": "/help",
                },
            }
        ],
    )
    monkeypatch.setattr(
        bot.notifier,
        "send_to",
        lambda chat_id, text: sent_messages.append((str(chat_id), text)),
    )

    bot.poll_telegram_commands()

    assert bot.telegram_offset == 100
    assert settings.telegram_offset_path.exists()
    assert sent_messages
    assert sent_messages[0][0] == "123"
    assert "/account" in sent_messages[0][1]


def test_poll_telegram_commands_ignores_telegram_http_errors(monkeypatch, tmp_path) -> None:
    settings = build_settings(tmp_path, telegram_token="token", telegram_chat_id="123")
    bot = TrendBot(settings)

    monkeypatch.setattr(
        bot.notifier.session,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.HTTPError("429 Too Many Requests")),
    )

    bot.poll_telegram_commands()

    assert bot.telegram_offset is None


def test_run_cycle_live_submitted_order_does_not_create_position(monkeypatch, tmp_path) -> None:
    settings = build_settings(tmp_path, live_trading=True)
    bot = TrendBot(settings)
    frame = make_frame([100 + i for i in range(30)])
    signal_ts = frame.index[-1]
    candle_map = {"BTCUSDT": frame, "ETHUSDT": frame}

    monkeypatch.setattr(bot, "refresh_candles", lambda: candle_map)
    monkeypatch.setattr(bot, "_send_scan_summary", lambda eligible, actions, ts: None)
    sent_messages: list[str] = []
    monkeypatch.setattr(bot.notifier, "send", lambda text: sent_messages.append(text))
    monkeypatch.setattr(
        bot.roostoo,
        "fetch_account_snapshot",
        lambda initial_equity: AccountSnapshot(
            timestamp=signal_ts.isoformat(),
            cash=50_000.0,
            equity=50_000.0,
            open_orders=[],
            raw={"mode": "live-test"},
        ),
    )
    monkeypatch.setattr(
        bot.strategy,
        "evaluate",
        lambda candle_map, state, account, signal_ts: StrategySnapshot(
            timestamp=signal_ts.isoformat(),
            ranked_symbols=["BTCUSDT"],
            eligible_symbols=["BTCUSDT"],
            trend_scores={"BTCUSDT": 1.2},
            actions=[
                OrderInstruction(
                    symbol="BTCUSDT",
                    side="buy",
                    quantity=1.0,
                    order_type="limit",
                    limit_price=100.0,
                    reason="trend_entry",
                    tranche_number=1,
                    target_notional=100.0,
                    stop_price=90.0,
                    trend_score=1.2,
                    breakout_high=99.0,
                    exit_low=90.0,
                    trend_ema=95.0,
                    trend_ema_slope=1.0,
                    reference_close=100.0,
                )
            ],
        ),
    )
    monkeypatch.setattr(
        bot.roostoo,
        "place_order",
        lambda **kwargs: {"Success": True, "OrderDetail": {"OrderID": "abc123"}},
    )

    processed = bot.run_cycle()

    assert processed is True
    assert "BTCUSDT" not in bot.state.positions
    event = json.loads(settings.event_log_path.read_text(encoding="utf-8").strip())
    assert event["order_status"] == "submitted"
    assert any("Order submitted" in message for message in sent_messages)
