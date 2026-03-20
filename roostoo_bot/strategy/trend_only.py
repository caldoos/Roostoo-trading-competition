from __future__ import annotations

from typing import Any

import pandas as pd

from roostoo_bot.config import Settings
from roostoo_bot.models import AccountSnapshot, BotState, OrderInstruction, ScanDiagnostic, StrategySnapshot


class TrendOnlyStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_feature_frames(self, candle_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        for symbol, frame in candle_map.items():
            if frame.empty:
                continue
            data = frame.copy().sort_index()
            data["trend_ema"] = data["close"].ewm(span=self.settings.ema_span, adjust=False).mean()
            data["trend_ema_slope"] = data["trend_ema"].diff()
            data["momentum_return"] = data["close"].pct_change(self.settings.momentum_bars)
            data["breakout_high"] = data["high"].shift(1).rolling(self.settings.breakout_lookback).max()
            data["exit_low"] = data["low"].shift(1).rolling(self.settings.exit_lookback).min()
            data["base_entry"] = (
                (data["close"] > data["breakout_high"])
                & (data["close"] > data["trend_ema"])
                & (data["trend_ema_slope"] > 0)
            )
            frames[symbol] = data
        return frames

    def _score_symbols(
        self,
        feature_frames: dict[str, pd.DataFrame],
        signal_ts: pd.Timestamp,
    ) -> dict[str, float]:
        rows: list[dict[str, Any]] = []
        for symbol, frame in feature_frames.items():
            if signal_ts not in frame.index:
                continue
            rows.append({"symbol": symbol, "momentum_return": float(frame.at[signal_ts, "momentum_return"])})
        score_frame = pd.DataFrame(rows)
        if score_frame.empty:
            return {}
        score_frame["momentum_return"] = score_frame["momentum_return"].fillna(0.0)
        std = float(score_frame["momentum_return"].std(ddof=0))
        if std == 0:
            score_frame["trend_score"] = 0.0
        else:
            score_frame["trend_score"] = (
                score_frame["momentum_return"] - score_frame["momentum_return"].mean()
            ) / std
        return dict(zip(score_frame["symbol"], score_frame["trend_score"]))

    def _btc_filter_ok(
        self,
        feature_frames: dict[str, pd.DataFrame],
        signal_ts: pd.Timestamp,
    ) -> bool:
        if not self.settings.use_btc_filter:
            return True
        btc = feature_frames.get("BTCUSDT")
        if btc is None or signal_ts not in btc.index:
            return False
        return bool(
            (btc.at[signal_ts, "close"] > btc.at[signal_ts, "trend_ema"])
            and (btc.at[signal_ts, "trend_ema_slope"] > 0)
        )

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if pd.isna(value):
            return None
        return float(value)

    def _build_diagnostics(
        self,
        feature_frames: dict[str, pd.DataFrame],
        trend_scores: dict[str, float],
        signal_ts: pd.Timestamp,
        state: BotState,
        btc_ok: bool,
    ) -> dict[str, ScanDiagnostic]:
        diagnostics: dict[str, ScanDiagnostic] = {}
        for symbol, frame in feature_frames.items():
            if signal_ts not in frame.index:
                continue
            row = frame.loc[signal_ts]
            close_price = self._safe_float(row["close"])
            breakout_high = self._safe_float(row["breakout_high"])
            exit_low = self._safe_float(row["exit_low"])
            trend_ema = self._safe_float(row["trend_ema"])
            trend_ema_slope = self._safe_float(row["trend_ema_slope"])
            base_entry = bool(row["base_entry"])
            failed_reasons: list[str] = []

            if breakout_high is None:
                failed_reasons.append("breakout_window_unready")
            elif close_price is None or close_price <= breakout_high:
                failed_reasons.append("close_below_breakout_high")

            if trend_ema is None:
                failed_reasons.append("ema_window_unready")
            elif close_price is None or close_price <= trend_ema:
                failed_reasons.append("close_below_trend_ema")

            if trend_ema_slope is None:
                failed_reasons.append("ema_slope_unready")
            elif trend_ema_slope <= 0:
                failed_reasons.append("trend_ema_slope_nonpositive")

            if self.settings.use_btc_filter and not btc_ok:
                failed_reasons.append("btc_filter_blocked")

            diagnostics[symbol] = ScanDiagnostic(
                symbol=symbol,
                close=close_price,
                breakout_high=breakout_high,
                exit_low=exit_low,
                trend_ema=trend_ema,
                trend_ema_slope=trend_ema_slope,
                trend_score=trend_scores.get(symbol),
                base_entry=base_entry,
                eligible=base_entry,
                in_position=symbol in state.positions,
                failed_reasons=failed_reasons,
            )
        return diagnostics

    def evaluate(
        self,
        candle_map: dict[str, pd.DataFrame],
        state: BotState,
        account: AccountSnapshot,
        signal_ts: pd.Timestamp,
    ) -> StrategySnapshot:
        feature_frames = self.build_feature_frames(candle_map)
        trend_scores = self._score_symbols(feature_frames, signal_ts)
        btc_ok = self._btc_filter_ok(feature_frames, signal_ts)
        diagnostics = self._build_diagnostics(feature_frames, trend_scores, signal_ts, state, btc_ok)

        ranked_eligible: list[str] = []
        actions: list[OrderInstruction] = []
        available_cash = account.cash
        exited_symbols: set[str] = set()

        for symbol, position in state.positions.items():
            frame = feature_frames.get(symbol)
            if frame is None or signal_ts not in frame.index:
                continue
            row = frame.loc[signal_ts]
            close_price = float(row["close"])
            trailing_stop = position.peak_close * (1 - self.settings.trailing_stop_pct)
            exit_reason: str | None = None
            if close_price <= position.stop_price:
                exit_reason = "stop"
            elif close_price <= trailing_stop:
                exit_reason = "trailing_stop"
            elif close_price < float(row["exit_low"]):
                exit_reason = "exit_low_break"
            elif close_price < float(row["trend_ema"]):
                exit_reason = "trend_ema_break"
            elif position.hold_bars >= self.settings.max_hold_bars:
                exit_reason = "max_hold"
            if exit_reason is None:
                continue
            exited_symbols.add(symbol)
            if symbol in diagnostics:
                diagnostics[symbol].selected_for_exit = True
            available_cash += position.units * close_price
            actions.append(
                OrderInstruction(
                    symbol=symbol,
                    side="sell",
                    quantity=position.units,
                    order_type=self.settings.default_order_type,
                    limit_price=close_price if self.settings.default_order_type == "limit" else None,
                    reason=exit_reason,
                    target_notional=position.target_notional,
                    stop_price=position.stop_price,
                    trend_score=trend_scores.get(symbol),
                    breakout_high=float(row["breakout_high"]) if pd.notna(row["breakout_high"]) else None,
                    exit_low=float(row["exit_low"]) if pd.notna(row["exit_low"]) else None,
                    trend_ema=float(row["trend_ema"]) if pd.notna(row["trend_ema"]) else None,
                    trend_ema_slope=float(row["trend_ema_slope"]) if pd.notna(row["trend_ema_slope"]) else None,
                    reference_close=close_price,
                )
            )

        for symbol, frame in feature_frames.items():
            if signal_ts not in frame.index:
                continue
            row = frame.loc[signal_ts]
            if bool(row["base_entry"]):
                ranked_eligible.append(symbol)

        ranked_eligible.sort(key=lambda s: trend_scores.get(s, 0.0), reverse=True)
        remaining_positions = {symbol for symbol in state.positions if symbol not in exited_symbols}

        for symbol in ranked_eligible:
            if symbol not in remaining_positions:
                continue
            position = state.positions[symbol]
            frame = feature_frames[symbol]
            row = frame.loc[signal_ts]
            if position.tranches_filled >= 3:
                diagnostics[symbol].failed_reasons.append("max_tranches_reached")
                continue
            if position.bars_since_last_fill < self.settings.add_delay_bars:
                diagnostics[symbol].failed_reasons.append("add_delay_not_met")
                continue
            close_price = float(row["close"])
            if close_price < position.avg_entry:
                diagnostics[symbol].failed_reasons.append("close_below_avg_entry_for_add")
                continue
            tranche_index = position.tranches_filled
            tranche_pct = self.settings.tranche_scheme[tranche_index]
            add_notional = min(position.target_notional * tranche_pct, available_cash)
            if add_notional <= 0:
                diagnostics[symbol].failed_reasons.append("insufficient_cash_for_add")
                continue
            available_cash -= add_notional
            quantity = add_notional / close_price
            diagnostics[symbol].selected_for_add = True
            actions.append(
                OrderInstruction(
                    symbol=symbol,
                    side="buy",
                    quantity=quantity,
                    order_type=self.settings.default_order_type,
                    limit_price=close_price if self.settings.default_order_type == "limit" else None,
                    reason="trend_add",
                    tranche_number=position.tranches_filled + 1,
                    target_notional=position.target_notional,
                    stop_price=position.stop_price,
                    trend_score=trend_scores.get(symbol),
                    breakout_high=float(row["breakout_high"]) if pd.notna(row["breakout_high"]) else None,
                    exit_low=float(row["exit_low"]) if pd.notna(row["exit_low"]) else None,
                    trend_ema=float(row["trend_ema"]) if pd.notna(row["trend_ema"]) else None,
                    trend_ema_slope=float(row["trend_ema_slope"]) if pd.notna(row["trend_ema_slope"]) else None,
                    reference_close=close_price,
                )
            )

        available_slots = self.settings.max_open_positions - len(remaining_positions)
        for symbol in ranked_eligible:
            if symbol in remaining_positions:
                continue
            if not btc_ok:
                diagnostics[symbol].failed_reasons.append("btc_filter_blocked")
                continue
            if available_slots <= 0:
                diagnostics[symbol].failed_reasons.append("max_open_positions_reached")
                continue
            frame = feature_frames[symbol]
            row = frame.loc[signal_ts]
            close_price = float(row["close"])
            stop_price = float(row["exit_low"]) if pd.notna(row["exit_low"]) else None
            if stop_price is None or stop_price >= close_price:
                diagnostics[symbol].failed_reasons.append("invalid_stop_structure")
                continue
            stop_pct = max((close_price - stop_price) / close_price, 0.0001)
            risk_budget = account.equity * self.settings.risk_per_trade
            target_notional = min(
                risk_budget / stop_pct,
                account.equity * self.settings.max_position_notional_pct,
                available_cash,
            )
            tranche_notional = target_notional * self.settings.tranche_scheme[0]
            if tranche_notional <= 0:
                diagnostics[symbol].failed_reasons.append("insufficient_cash_for_entry")
                continue
            available_cash -= tranche_notional
            quantity = tranche_notional / close_price
            remaining_positions.add(symbol)
            available_slots -= 1
            diagnostics[symbol].selected_for_entry = True
            actions.append(
                OrderInstruction(
                    symbol=symbol,
                    side="buy",
                    quantity=quantity,
                    order_type=self.settings.default_order_type,
                    limit_price=close_price if self.settings.default_order_type == "limit" else None,
                    reason="trend_entry",
                    tranche_number=1,
                    target_notional=target_notional,
                    stop_price=stop_price,
                    trend_score=trend_scores.get(symbol),
                    breakout_high=float(row["breakout_high"]) if pd.notna(row["breakout_high"]) else None,
                    exit_low=float(row["exit_low"]) if pd.notna(row["exit_low"]) else None,
                    trend_ema=float(row["trend_ema"]) if pd.notna(row["trend_ema"]) else None,
                    trend_ema_slope=float(row["trend_ema_slope"]) if pd.notna(row["trend_ema_slope"]) else None,
                    reference_close=close_price,
                )
            )

        return StrategySnapshot(
            timestamp=signal_ts.isoformat(),
            ranked_symbols=ranked_eligible,
            eligible_symbols=ranked_eligible.copy(),
            trend_scores=trend_scores,
            actions=actions,
            diagnostics=list(diagnostics.values()),
        )
