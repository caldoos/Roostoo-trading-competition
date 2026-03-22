from __future__ import annotations

import pandas as pd

from roostoo_bot.models import AccountSnapshot, BotState, PositionState
from roostoo_bot.strategy.trend_only import TrendOnlyStrategy

from tests.conftest import build_settings, make_frame


def test_strategy_generates_entry_for_eligible_symbol(tmp_path) -> None:
    settings = build_settings(tmp_path)
    strategy = TrendOnlyStrategy(settings)
    btc = make_frame([100 + i for i in range(30)])
    eth = make_frame([100] * 23 + [103, 106, 109, 112, 116, 120, 150], high_pad=2.0, low_pad=1.0)
    candle_map = {"BTCUSDT": btc, "ETHUSDT": eth}
    signal_ts = eth.index[-1]
    account = AccountSnapshot(timestamp=signal_ts.isoformat(), cash=1_000_000.0, equity=1_000_000.0)

    snapshot = strategy.evaluate(candle_map, BotState(), account, signal_ts)

    assert "ETHUSDT" in snapshot.eligible_symbols
    assert any(action.reason == "trend_entry" and action.symbol == "ETHUSDT" for action in snapshot.actions)


def test_strategy_generates_exit_for_existing_position(tmp_path) -> None:
    settings = build_settings(tmp_path)
    strategy = TrendOnlyStrategy(settings)
    eth = make_frame([100] * 24 + [104, 108, 112, 116, 120, 90], high_pad=2.0, low_pad=1.0)
    candle_map = {"ETHUSDT": eth}
    signal_ts = eth.index[-1]
    state = BotState(
        positions={
            "ETHUSDT": PositionState(
                symbol="ETHUSDT",
                units=10.0,
                target_notional=1000.0,
                tranches_filled=1,
                stop_price=100.0,
                peak_close=120.0,
                hold_bars=1,
                avg_entry=110.0,
                bars_since_last_fill=3,
            )
        }
    )
    account = AccountSnapshot(timestamp=signal_ts.isoformat(), cash=1000.0, equity=2000.0)

    snapshot = strategy.evaluate(candle_map, state, account, signal_ts)

    assert any(action.reason == "stop" and action.symbol == "ETHUSDT" for action in snapshot.actions)


def test_strategy_generates_add_after_delay(tmp_path) -> None:
    settings = build_settings(tmp_path)
    strategy = TrendOnlyStrategy(settings)
    eth = make_frame([100] * 23 + [103, 106, 109, 112, 116, 120, 150], high_pad=2.0, low_pad=1.0)
    candle_map = {"ETHUSDT": eth}
    signal_ts = eth.index[-1]
    state = BotState(
        positions={
            "ETHUSDT": PositionState(
                symbol="ETHUSDT",
                units=5.0,
                target_notional=10_000.0,
                tranches_filled=1,
                stop_price=110.0,
                peak_close=145.0,
                hold_bars=1,
                avg_entry=120.0,
                bars_since_last_fill=3,
            )
        }
    )
    account = AccountSnapshot(timestamp=signal_ts.isoformat(), cash=100_000.0, equity=100_000.0)

    snapshot = strategy.evaluate(candle_map, state, account, signal_ts)

    add_actions = [action for action in snapshot.actions if action.reason == "trend_add"]
    assert len(add_actions) == 1
    assert add_actions[0].tranche_number == 2


def test_strategy_respects_btc_filter(tmp_path) -> None:
    settings = build_settings(tmp_path, use_btc_filter=True)
    strategy = TrendOnlyStrategy(settings)
    btc = make_frame([120] * 23 + [118, 116, 114, 112, 110, 108, 106], high_pad=1.0, low_pad=1.0)
    eth = make_frame([100] * 23 + [103, 106, 109, 112, 116, 120, 150], high_pad=2.0, low_pad=1.0)
    candle_map = {"BTCUSDT": btc, "ETHUSDT": eth}
    signal_ts = eth.index[-1]
    account = AccountSnapshot(timestamp=signal_ts.isoformat(), cash=1_000_000.0, equity=1_000_000.0)

    snapshot = strategy.evaluate(candle_map, BotState(), account, signal_ts)

    assert "ETHUSDT" in snapshot.eligible_symbols
    assert not any(action.reason == "trend_entry" for action in snapshot.actions)


def test_strategy_exposes_failed_reasons_in_diagnostics(tmp_path) -> None:
    settings = build_settings(tmp_path)
    strategy = TrendOnlyStrategy(settings)
    btc = make_frame([100 + i for i in range(40)])
    eth = make_frame([100] * 39 + [99], high_pad=1.0, low_pad=1.0)
    candle_map = {"BTCUSDT": btc, "ETHUSDT": eth}
    signal_ts = eth.index[-1]
    account = AccountSnapshot(timestamp=signal_ts.isoformat(), cash=1_000_000.0, equity=1_000_000.0)

    snapshot = strategy.evaluate(candle_map, BotState(), account, signal_ts)

    eth_diag = next(diag for diag in snapshot.diagnostics if diag.symbol == "ETHUSDT")
    assert eth_diag.eligible is False
    assert "close_below_breakout_high" in eth_diag.failed_reasons
    assert "close_below_trend_ema" in eth_diag.failed_reasons


def test_strategy_generates_take_profit_and_moves_stop_to_breakeven(tmp_path) -> None:
    settings = build_settings(tmp_path)
    strategy = TrendOnlyStrategy(settings)
    eth = make_frame([100] * 24 + [102, 104, 106, 108, 110, 112], high_pad=2.0, low_pad=1.0)
    candle_map = {"ETHUSDT": eth}
    signal_ts = eth.index[-1]
    state = BotState(
        positions={
            "ETHUSDT": PositionState(
                symbol="ETHUSDT",
                units=9.0,
                target_notional=1000.0,
                tranches_filled=1,
                stop_price=95.0,
                peak_close=112.0,
                hold_bars=5,
                avg_entry=100.0,
                bars_since_last_fill=3,
                tp_base_units=9.0,
                tp1_taken=False,
                tp2_taken=False,
            )
        }
    )
    account = AccountSnapshot(timestamp=signal_ts.isoformat(), cash=1000.0, equity=2000.0)

    snapshot = strategy.evaluate(candle_map, state, account, signal_ts)

    tp_actions = [action for action in snapshot.actions if action.reason == "take_profit_1"]
    assert len(tp_actions) == 1
    assert tp_actions[0].quantity == 2.97
    assert tp_actions[0].stop_price == 100.0
