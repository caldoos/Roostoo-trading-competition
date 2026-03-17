from __future__ import annotations

from typing import Any

import pandas as pd

from roostoo_bot.config import Settings
from roostoo_bot.models import AccountSnapshot, BotState, OrderInstruction, StrategySnapshot


class TrendOnlyStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_feature_frames(self, candle_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        for symbol, frame in candle_map.items():
            if frame.empty:
                continue
            data = frame.copy().sort_index()
            data["ema20"] = data["close"].ewm(span=self.settings.ema_span, adjust=False).mean()
            data["ema20_slope"] = data["ema20"].diff()
            data["mom_6"] = data["close"].pct_change(self.settings.momentum_bars)
            data["breakout_high"] = data["high"].shift(1).rolling(self.settings.breakout_lookback).max()
            data["exit_low"] = data["low"].shift(1).rolling(self.settings.exit_lookback).min()
            data["base_entry"] = (
                (data["close"] > data["breakout_high"])
                & (data["close"] > data["ema20"])
                & (data["ema20_slope"] > 0)
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
            rows.append({"symbol": symbol, "mom_6": float(frame.at[signal_ts, "mom_6"])})
        score_frame = pd.DataFrame(rows)
        if score_frame.empty:
            return {}
        score_frame["mom_6"] = score_frame["mom_6"].fillna(0.0)
        std = float(score_frame["mom_6"].std(ddof=0))
        if std == 0:
            score_frame["trend_score"] = 0.0
        else:
            score_frame["trend_score"] = (score_frame["mom_6"] - score_frame["mom_6"].mean()) / std
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
            (btc.at[signal_ts, "close"] > btc.at[signal_ts, "ema20"])
            and (btc.at[signal_ts, "ema20_slope"] > 0)
        )

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
            should_exit = (
                (close_price < float(row["ema20"]))
                or (close_price < float(row["exit_low"]))
                or (close_price <= position.stop_price)
                or (close_price <= trailing_stop)
                or (position.hold_bars >= self.settings.max_hold_bars)
            )
            if not should_exit:
                continue
            exited_symbols.add(symbol)
            available_cash += position.units * close_price
            actions.append(
                OrderInstruction(
                    symbol=symbol,
                    side="sell",
                    quantity=position.units,
                    order_type=self.settings.default_order_type,
                    limit_price=close_price if self.settings.default_order_type == "limit" else None,
                    reason="trend_or_risk_exit",
                    target_notional=position.target_notional,
                    stop_price=position.stop_price,
                    trend_score=trend_scores.get(symbol),
                    breakout_high=float(row["breakout_high"]) if pd.notna(row["breakout_high"]) else None,
                    exit_low=float(row["exit_low"]) if pd.notna(row["exit_low"]) else None,
                    ema20=float(row["ema20"]) if pd.notna(row["ema20"]) else None,
                    ema20_slope=float(row["ema20_slope"]) if pd.notna(row["ema20_slope"]) else None,
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
                continue
            if position.bars_since_last_fill < self.settings.add_delay_bars:
                continue
            close_price = float(row["close"])
            if close_price < position.avg_entry:
                continue
            tranche_index = position.tranches_filled
            tranche_pct = self.settings.tranche_scheme[tranche_index]
            add_notional = min(position.target_notional * tranche_pct, available_cash)
            if add_notional <= 0:
                continue
            available_cash -= add_notional
            quantity = add_notional / close_price
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
                    ema20=float(row["ema20"]) if pd.notna(row["ema20"]) else None,
                    ema20_slope=float(row["ema20_slope"]) if pd.notna(row["ema20_slope"]) else None,
                    reference_close=close_price,
                )
            )

        if btc_ok:
            available_slots = self.settings.max_open_positions - len(remaining_positions)
            for symbol in ranked_eligible:
                if available_slots <= 0:
                    break
                if symbol in remaining_positions:
                    continue
                frame = feature_frames[symbol]
                row = frame.loc[signal_ts]
                close_price = float(row["close"])
                stop_price = float(row["exit_low"]) if pd.notna(row["exit_low"]) else None
                if stop_price is None or stop_price >= close_price:
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
                    continue
                available_cash -= tranche_notional
                quantity = tranche_notional / close_price
                remaining_positions.add(symbol)
                available_slots -= 1
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
                        ema20=float(row["ema20"]) if pd.notna(row["ema20"]) else None,
                        ema20_slope=float(row["ema20_slope"]) if pd.notna(row["ema20_slope"]) else None,
                        reference_close=close_price,
                    )
                )

        return StrategySnapshot(
            timestamp=signal_ts.isoformat(),
            ranked_symbols=ranked_eligible,
            eligible_symbols=ranked_eligible.copy(),
            trend_scores=trend_scores,
            actions=actions,
        )
