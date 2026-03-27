from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from roostoo_bot.clients.binance import BinanceClient
from roostoo_bot.clients.roostoo import RoostooClient
from roostoo_bot.config import Settings
from roostoo_bot.logging_utils import append_jsonl, get_logger, utc_now_iso
from roostoo_bot.models import AccountSnapshot, OrderInstruction, PositionState, ScanDiagnostic
from roostoo_bot.notifications.telegram import TelegramNotifier
from roostoo_bot.storage.candle_store import CandleStore
from roostoo_bot.storage.state_store import StateStore
from roostoo_bot.strategy.trend_only import TrendOnlyStrategy


class TrendBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = get_logger(
            "roostoo_bot",
            Path(__file__).resolve().parents[1] / "logs" / "bot.log",
        )
        self.notifier = TelegramNotifier(settings.telegram_token, settings.telegram_chat_id)
        self.state_store = StateStore(settings.state_path)
        self.candle_store = CandleStore(settings.candle_cache_dir)
        self.binance = BinanceClient(settings.binance_base_url)
        self.roostoo = RoostooClient(settings)
        self.strategy = TrendOnlyStrategy(settings)
        self.state = self.state_store.load()
        self.telegram_offset = self._load_telegram_offset()
        if self.state.last_cash == 0.0 and not self.state.positions:
            self.state.last_cash = settings.initial_equity
            self.state.last_equity = settings.initial_equity

    @staticmethod
    def _format_price(value: float | None) -> str:
        if value is None:
            return "n/a"
        abs_value = abs(value)
        if abs_value >= 1000:
            return f"{value:.2f}"
        if abs_value >= 1:
            return f"{value:.4f}"
        if abs_value >= 0.01:
            return f"{value:.5f}"
        if abs_value >= 0.0001:
            return f"{value:.6f}"
        if abs_value == 0:
            return "0"
        return f"{value:.8f}"

    @staticmethod
    def _format_units(value: float) -> str:
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text if text else "0"

    def _load_telegram_offset(self) -> int | None:
        path = self.settings.telegram_offset_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        value = payload.get("offset")
        return int(value) if isinstance(value, int) else None

    def _save_telegram_offset(self) -> None:
        if self.telegram_offset is None:
            return
        self.settings.telegram_offset_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.telegram_offset_path.write_text(
            json.dumps({"offset": self.telegram_offset}, indent=2),
            encoding="utf-8",
        )

    def bootstrap_candles(self) -> None:
        for symbol in self.settings.symbols:
            cached = self.candle_store.load(symbol)
            if len(cached) >= 100:
                continue
            frame = self.binance.fetch_recent_klines(symbol, self.settings.candle_interval, limit=300)
            if frame.empty:
                self.logger.warning("No candles fetched for %s during bootstrap.", symbol)
                continue
            self.candle_store.save(symbol, frame)

    def refresh_candles(self) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        for symbol in self.settings.symbols:
            latest = self.binance.fetch_recent_klines(symbol, self.settings.candle_interval, limit=300)
            if latest.empty:
                self.logger.warning("No fresh Binance candles for %s.", symbol)
                continue
            frames[symbol] = self.candle_store.upsert(symbol, latest)
        return frames

    @staticmethod
    def _latest_common_timestamp(candle_map: dict[str, pd.DataFrame]) -> pd.Timestamp | None:
        if not candle_map:
            return None
        common = None
        for frame in candle_map.values():
            index_set = set(frame.index)
            common = index_set if common is None else common.intersection(index_set)
        if not common:
            return None
        return max(common)

    def _build_account_snapshot(
        self,
        candle_map: dict[str, pd.DataFrame],
        signal_ts: pd.Timestamp,
    ) -> AccountSnapshot:
        if self.settings.live_trading and self.roostoo.is_configured():
            return self.roostoo.fetch_account_snapshot(self.settings.initial_equity)
        cash = self.state.last_cash if self.state.last_cash > 0 else self.settings.initial_equity
        equity = cash
        for symbol, position in self.state.positions.items():
            frame = candle_map.get(symbol)
            if frame is None or signal_ts not in frame.index:
                continue
            equity += position.units * float(frame.at[signal_ts, "close"])
        return AccountSnapshot(
            timestamp=utc_now_iso(),
            cash=cash,
            equity=equity,
            open_orders=list(self.state.outstanding_orders.values()),
            raw={"mode": "paper"},
        )

    def _paper_fill(self, instruction: OrderInstruction) -> dict[str, object]:
        price = instruction.limit_price or instruction.reference_close or 0.0
        return {
            "status": "filled",
            "filled_price": price,
            "filled_qty": instruction.quantity,
            "order_id": f"paper-{instruction.symbol}-{int(time.time() * 1000)}",
        }

    @staticmethod
    def _derive_order_status(response: dict[str, object]) -> str:
        status_value = response.get("status")
        if isinstance(status_value, str) and status_value:
            return status_value.lower()
        success = response.get("Success")
        if success is False:
            return "failed"
        order_detail = response.get("OrderDetail")
        if isinstance(order_detail, dict):
            for key in ("Status", "status"):
                value = order_detail.get(key)
                if isinstance(value, str) and value:
                    return value.lower()
        if response.get("filled_price") is not None or response.get("filled_qty") is not None:
            return "filled"
        if response.get("order_id") or response.get("OrderID") or isinstance(order_detail, dict):
            return "submitted"
        return "unknown"

    def _execute_instruction(self, instruction: OrderInstruction) -> tuple[dict[str, object], float, str]:
        fill_price = instruction.limit_price or instruction.reference_close or 0.0
        if self.settings.live_trading and self.roostoo.is_configured():
            response = self.roostoo.place_order(
                symbol=instruction.symbol,
                side=instruction.side,
                quantity=instruction.quantity,
                order_type=instruction.order_type,
                price=instruction.limit_price,
            )
            if isinstance(response.get("filled_price"), (int, float)):
                fill_price = float(response["filled_price"])
            return response, fill_price, self._derive_order_status(response)
        response = self._paper_fill(instruction)
        return response, fill_price, "filled"

    def _apply_fill(self, instruction: OrderInstruction, fill_price: float) -> None:
        notional = instruction.quantity * fill_price
        if instruction.side == "buy":
            self.state.last_cash -= notional
            existing = self.state.positions.get(instruction.symbol)
            if existing is None:
                self.state.positions[instruction.symbol] = PositionState(
                    symbol=instruction.symbol,
                    units=instruction.quantity,
                    target_notional=instruction.target_notional or notional,
                    tranches_filled=instruction.tranche_number or 1,
                    stop_price=instruction.stop_price or 0.0,
                    peak_close=instruction.reference_close or fill_price,
                    hold_bars=0,
                    avg_entry=fill_price,
                    bars_since_last_fill=0,
                    tp_base_units=instruction.quantity,
                    tp1_taken=False,
                    tp2_taken=False,
                )
            else:
                total_units = existing.units + instruction.quantity
                existing.avg_entry = (
                    (existing.avg_entry * existing.units) + (fill_price * instruction.quantity)
                ) / total_units
                existing.units = total_units
                existing.tranches_filled = max(existing.tranches_filled, instruction.tranche_number or 1)
                existing.target_notional = instruction.target_notional or existing.target_notional
                existing.stop_price = instruction.stop_price or existing.stop_price
                existing.bars_since_last_fill = 0
                existing.peak_close = max(existing.peak_close, instruction.reference_close or fill_price)
                existing.tp_base_units = max(existing.tp_base_units, total_units)
        else:
            self.state.last_cash += notional
            existing = self.state.positions.get(instruction.symbol)
            if existing is None:
                return
            remaining_units = existing.units - instruction.quantity
            if instruction.reason == "take_profit_1":
                existing.tp1_taken = True
                existing.stop_price = max(existing.stop_price, existing.avg_entry)
            elif instruction.reason == "take_profit_2":
                existing.tp2_taken = True
            if remaining_units <= 1e-9:
                self.state.positions.pop(instruction.symbol, None)
            else:
                existing.units = remaining_units
                existing.bars_since_last_fill = 0

    def _advance_positions(self, candle_map: dict[str, pd.DataFrame], signal_ts: pd.Timestamp, touched: set[str]) -> None:
        for symbol, position in self.state.positions.items():
            frame = candle_map.get(symbol)
            if frame is None or signal_ts not in frame.index:
                continue
            close_price = float(frame.at[signal_ts, "close"])
            position.peak_close = max(position.peak_close, close_price)
            if symbol not in touched:
                position.bars_since_last_fill += 1
            position.hold_bars += 1

    def _mark_to_market(self, candle_map: dict[str, pd.DataFrame], signal_ts: pd.Timestamp) -> None:
        equity = self.state.last_cash
        for symbol, position in self.state.positions.items():
            frame = candle_map.get(symbol)
            if frame is None or signal_ts not in frame.index:
                continue
            equity += position.units * float(frame.at[signal_ts, "close"])
        self.state.last_equity = equity

    @staticmethod
    def _extract_wallet_from_balances(balances: dict[str, object] | None) -> dict[str, dict[str, object]]:
        if not isinstance(balances, dict):
            return {}
        wallet = balances.get("SpotWallet") or balances.get("Wallet") or {}
        return wallet if isinstance(wallet, dict) else {}

    def _latest_close_for_symbol(self, symbol: str) -> float | None:
        latest_frame = None
        try:
            latest_frame = self.binance.fetch_recent_klines(symbol, self.settings.candle_interval, limit=10)
        except Exception:  # noqa: BLE001
            latest_frame = None
        if latest_frame is not None and not latest_frame.empty:
            frame = self.candle_store.upsert(symbol, latest_frame)
            if not frame.empty:
                return float(frame["close"].iloc[-1])

        cached = self.candle_store.load(symbol)
        if not cached.empty:
            return float(cached["close"].iloc[-1])
        return None

    def _build_wallet_marks(self, wallet: dict[str, dict[str, object]]) -> dict[str, float]:
        marks: dict[str, float] = {}
        for asset, payload in wallet.items():
            if asset == "USD" or not isinstance(payload, dict):
                continue
            total_units = float(payload.get("Free", 0.0) or 0.0) + float(payload.get("Lock", 0.0) or 0.0)
            if total_units <= 0:
                continue
            symbol = f"{asset}USDT"
            price = self._latest_close_for_symbol(symbol)
            if price is not None:
                marks[asset] = price
        return marks

    def _mark_wallet_equity(
        self,
        wallet: dict[str, dict[str, object]],
        marks: dict[str, float],
        fallback_cash: float,
    ) -> tuple[float, float]:
        usd_wallet = wallet.get("USD", {}) if isinstance(wallet.get("USD"), dict) else {}
        cash = float(usd_wallet.get("Free", fallback_cash) or fallback_cash)
        equity = cash + float(usd_wallet.get("Lock", 0.0) or 0.0)
        for asset, payload in wallet.items():
            if asset == "USD" or not isinstance(payload, dict):
                continue
            total_units = float(payload.get("Free", 0.0) or 0.0) + float(payload.get("Lock", 0.0) or 0.0)
            if total_units <= 0:
                continue
            mark = marks.get(asset)
            if mark is not None:
                equity += total_units * mark
        return cash, equity

    def _load_event_rows(self) -> list[dict[str, object]]:
        path = self.settings.event_log_path
        if not path.exists():
            return []
        rows: list[dict[str, object]] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
        rows.sort(key=lambda row: str(row.get("timestamp_utc", "")))
        return rows

    def _rebuild_position_ledger(self) -> dict[str, dict[str, float]]:
        ledger: dict[str, dict[str, float]] = {}
        for row in self._load_event_rows():
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            action = str(row.get("action", ""))
            qty = float(row.get("units", 0.0) or 0.0)
            price = float(row.get("entry_price", row.get("price", 0.0)) or 0.0)
            stop_price = float(row.get("stop_price", 0.0) or 0.0)
            if action in {"entry", "add"}:
                existing = ledger.get(symbol)
                if existing is None:
                    ledger[symbol] = {
                        "units": qty,
                        "avg_entry": price,
                        "stop_price": stop_price,
                        "target_notional": float(row.get("notional", qty * price) or qty * price),
                        "tranches_filled": 1.0 if action == "entry" else 0.0,
                        "tp_base_units": qty,
                        "tp1_taken": False,
                        "tp2_taken": False,
                    }
                else:
                    total_units = existing["units"] + qty
                    if total_units > 0:
                        existing["avg_entry"] = (
                            existing["avg_entry"] * existing["units"] + price * qty
                        ) / total_units
                    existing["units"] = total_units
                    existing["stop_price"] = stop_price or existing["stop_price"]
                    existing["target_notional"] = max(existing["target_notional"], float(row.get("notional", qty * price) or qty * price))
                    existing["tranches_filled"] += 1.0
                    existing["tp_base_units"] = max(existing["tp_base_units"], total_units)
            elif action == "partial_exit":
                existing = ledger.get(symbol)
                if existing is None:
                    continue
                existing["units"] = max(0.0, existing["units"] - qty)
                if row.get("reason") == "take_profit_1":
                    existing["tp1_taken"] = True
                    existing["stop_price"] = max(existing["stop_price"], existing["avg_entry"])
                elif row.get("reason") == "take_profit_2":
                    existing["tp2_taken"] = True
                if existing["units"] <= 1e-9:
                    ledger.pop(symbol, None)
            elif action == "full_exit":
                ledger.pop(symbol, None)
        return ledger

    def _reconcile_live_positions(
        self,
        account: AccountSnapshot,
        candle_map: dict[str, pd.DataFrame],
        signal_ts: pd.Timestamp,
    ) -> None:
        wallet = {}
        if isinstance(account.raw, dict):
            balances = account.raw.get("balances", {})
            if isinstance(balances, dict):
                wallet = balances.get("SpotWallet") or balances.get("Wallet") or {}
        if not isinstance(wallet, dict):
            return

        ledger = self._rebuild_position_ledger()
        reconciled: dict[str, PositionState] = {}
        for asset, payload in wallet.items():
            if asset == "USD" or not isinstance(payload, dict):
                continue
            units = float(payload.get("Free", 0.0) or 0.0) + float(payload.get("Lock", 0.0) or 0.0)
            if units <= 0:
                continue
            symbol = f"{asset}USDT"
            frame = candle_map.get(symbol)
            close_price = None
            if frame is not None and signal_ts in frame.index:
                close_price = float(frame.at[signal_ts, "close"])
            elif frame is not None and not frame.empty:
                close_price = float(frame["close"].iloc[-1])
            ledger_row = ledger.get(symbol, {})
            existing = self.state.positions.get(symbol)
            avg_entry = float(
                ledger_row.get(
                    "avg_entry",
                    existing.avg_entry if existing is not None else (close_price or 0.0),
                )
            )
            stop_price = float(ledger_row.get("stop_price", existing.stop_price if existing is not None else 0.0))
            target_notional = float(
                ledger_row.get(
                    "target_notional",
                    existing.target_notional if existing is not None else units * (avg_entry or close_price or 0.0),
                )
            )
            tranches_filled = int(round(float(ledger_row.get("tranches_filled", existing.tranches_filled if existing is not None else 1.0))))
            peak_close = max(close_price or avg_entry, existing.peak_close if existing is not None else avg_entry)
            hold_bars = existing.hold_bars if existing is not None else 0
            bars_since_last_fill = existing.bars_since_last_fill if existing is not None else 0
            tp_base_units = float(ledger_row.get("tp_base_units", existing.tp_base_units if existing is not None else units))
            tp1_taken = bool(ledger_row.get("tp1_taken", existing.tp1_taken if existing is not None else False))
            tp2_taken = bool(ledger_row.get("tp2_taken", existing.tp2_taken if existing is not None else False))
            reconciled[symbol] = PositionState(
                symbol=symbol,
                units=units,
                target_notional=target_notional,
                tranches_filled=max(tranches_filled, 1),
                stop_price=stop_price,
                peak_close=peak_close,
                hold_bars=hold_bars,
                avg_entry=avg_entry or (close_price or 0.0),
                bars_since_last_fill=bars_since_last_fill,
                tp_base_units=max(tp_base_units, units),
                tp1_taken=tp1_taken,
                tp2_taken=tp2_taken,
            )

        self.state.positions = reconciled

    def _log_event(
        self,
        instruction: OrderInstruction,
        response: dict[str, object],
        order_status: str,
        signal_ts: pd.Timestamp,
        cash_before: float,
        equity_before: float,
    ) -> None:
        append_jsonl(
            self.settings.event_log_path,
            {
                "timestamp_utc": signal_ts.isoformat(),
                "symbol": instruction.symbol,
                "action": (
                    "entry" if instruction.reason == "trend_entry" else (
                        "add" if instruction.reason == "trend_add" else (
                            "partial_exit" if instruction.reason in {"take_profit_1", "take_profit_2"} else "full_exit"
                        )
                    )
                ),
                "reason": instruction.reason,
                "trend_score": instruction.trend_score,
                "breakout_high": instruction.breakout_high,
                "exit_low": instruction.exit_low,
                "trend_ema": instruction.trend_ema,
                "trend_ema_slope": instruction.trend_ema_slope,
                "entry_price": instruction.limit_price or instruction.reference_close,
                "stop_price": instruction.stop_price,
                "peak_close": self.state.positions.get(instruction.symbol).peak_close
                if instruction.symbol in self.state.positions
                else instruction.reference_close,
                "tranche_number": instruction.tranche_number,
                "units": instruction.quantity,
                "notional": instruction.quantity * (instruction.limit_price or instruction.reference_close or 0.0),
                "cash_before": cash_before,
                "cash_after": self.state.last_cash,
                "equity_before": equity_before,
                "equity_after": self.state.last_equity,
                "order_type": instruction.order_type,
                "order_status": order_status,
                "error_message": response.get("ErrMsg") or response.get("error") or response.get("Message"),
                "roostoo_order_id": response.get("order_id")
                or response.get("OrderID")
                or (response.get("OrderDetail", {}) if isinstance(response.get("OrderDetail"), dict) else {}).get("OrderID"),
            },
        )

    def _notify_order_event(
        self,
        instruction: OrderInstruction,
        response: dict[str, object],
        order_status: str,
        fill_price: float,
        position_before: PositionState | None = None,
    ) -> None:
        order_id = response.get("order_id") or response.get("OrderID")
        order_detail = response.get("OrderDetail")
        if not order_id and isinstance(order_detail, dict):
            order_id = order_detail.get("OrderID")
        if order_status == "filled" and instruction.side == "sell" and position_before is not None:
            entry_price = position_before.avg_entry
            qty = instruction.quantity
            pnl = (fill_price - entry_price) * qty
            pnl_pct = (fill_price / entry_price - 1.0) * 100 if entry_price > 0 else 0.0
            risk_per_unit = max(entry_price - position_before.stop_price, 0.0)
            total_risk = risk_per_unit * qty
            r_text = f"{(pnl / total_risk):+.2f}R" if total_risk > 0 else "n/a"
            reason = instruction.reason.replace("_", " ")
            if instruction.reason in {"take_profit_1", "take_profit_2"}:
                remaining_units = self.state.positions.get(instruction.symbol).units if instruction.symbol in self.state.positions else 0.0
                stop_price = self.state.positions.get(instruction.symbol).stop_price if instruction.symbol in self.state.positions else position_before.stop_price
                self.notifier.send(
                    f"[{self.settings.bot_name}] TAKE PROFIT {instruction.symbol}\n"
                    f"Qty: {qty:.6f} | Entry: {self._format_price(entry_price)} | Exit: {self._format_price(fill_price)}\n"
                    f"P/L: {pnl:+.2f} ({pnl_pct:+.2f}%) | R: {r_text}\n"
                    f"Reason: {reason} | Remaining qty: {self._format_units(remaining_units)} | Stop: {self._format_price(stop_price)}\n"
                    f"Open positions: {len(self.state.positions)} | Equity: {self.state.last_equity:,.2f}"
                )
            else:
                self.notifier.send(
                    f"[{self.settings.bot_name}] EXIT {instruction.symbol}\n"
                    f"Qty: {qty:.6f} | Entry: {self._format_price(entry_price)} | Exit: {self._format_price(fill_price)}\n"
                    f"P/L: {pnl:+.2f} ({pnl_pct:+.2f}%) | R: {r_text}\n"
                    f"Reason: {reason}\n"
                    f"Open positions: {len(self.state.positions)} | Equity: {self.state.last_equity:,.2f}"
                )
            return
        if order_status == "filled":
            title = "Order filled"
        elif order_status in {"submitted", "open", "pending", "new"}:
            title = "Order submitted"
        elif order_status in {"failed", "rejected", "cancelled", "canceled"}:
            title = "Order failed"
        else:
            title = "Order update"
        error_message = response.get("ErrMsg") or response.get("error") or response.get("Message")
        error_line = f"\n- error: {error_message}" if error_message else ""
        stop_line = (
            f"\n- stop_loss: {instruction.stop_price:.6f}"
            if instruction.stop_price is not None
            else "\n- stop_loss: n/a"
        )
        self.notifier.send(
            f"[{self.settings.bot_name}] {title}\n"
            f"- symbol: {instruction.symbol}\n"
            f"- side: {instruction.side}\n"
            f"- reason: {instruction.reason}\n"
            f"- qty: {instruction.quantity:.6f}\n"
            f"- price: {self._format_price(fill_price)}\n"
            f"- status: {order_status}\n"
            f"- order_id: {order_id or 'n/a'}"
            f"{stop_line}"
            f"{error_line}"
        )

    @staticmethod
    def _open_order_ids(open_orders: list[dict[str, object]]) -> set[str]:
        ids: set[str] = set()
        for order in open_orders:
            if not isinstance(order, dict):
                continue
            order_id = order.get("OrderID") or order.get("order_id")
            if order_id is not None:
                ids.add(str(order_id))
        return ids

    def _track_outstanding_order(
        self,
        instruction: OrderInstruction,
        response: dict[str, object],
        order_status: str,
        fill_price: float,
        position_before: PositionState | None,
    ) -> None:
        if order_status not in {"submitted", "pending", "open", "new"}:
            return
        order_id = response.get("order_id") or response.get("OrderID")
        order_detail = response.get("OrderDetail")
        if not order_id and isinstance(order_detail, dict):
            order_id = order_detail.get("OrderID")
        if not order_id:
            return
        payload: dict[str, object] = {
            "symbol": instruction.symbol,
            "side": instruction.side,
            "quantity": instruction.quantity,
            "order_type": instruction.order_type,
            "price": fill_price,
            "reason": instruction.reason,
            "stop_price": instruction.stop_price,
        }
        if position_before is not None:
            payload["position_before"] = asdict(position_before)
        self.state.outstanding_orders[str(order_id)] = payload

    def _reconcile_outstanding_orders(self, account: AccountSnapshot) -> None:
        if not self.state.outstanding_orders:
            return
        open_ids = self._open_order_ids(account.open_orders)
        settled_ids: list[str] = []
        held_symbols = set(self.state.positions)

        for order_id, payload in self.state.outstanding_orders.items():
            if order_id in open_ids:
                continue

            symbol = str(payload.get("symbol", ""))
            side = str(payload.get("side", "")).lower()
            is_filled = False
            current_position = self.state.positions.get(symbol)
            if side == "buy" and symbol in held_symbols:
                is_filled = True
            elif side == "sell" and symbol not in held_symbols:
                is_filled = True
            elif side == "sell" and current_position is not None:
                stored_position = payload.get("position_before")
                if isinstance(stored_position, dict):
                    before_units = float(stored_position.get("units", 0.0) or 0.0)
                    expected_units = max(0.0, before_units - float(payload.get("quantity", 0.0) or 0.0))
                    if current_position.units <= expected_units + 1e-6:
                        is_filled = True

            if is_filled:
                instruction = OrderInstruction(
                    symbol=symbol,
                    side=side,
                    quantity=float(payload.get("quantity", 0.0) or 0.0),
                    order_type=str(payload.get("order_type", "limit")),
                    limit_price=float(payload.get("price", 0.0) or 0.0),
                    reason=str(payload.get("reason", "")),
                    stop_price=float(payload.get("stop_price", 0.0) or 0.0),
                    reference_close=float(payload.get("price", 0.0) or 0.0),
                )
                position_before = None
                stored_position = payload.get("position_before")
                if isinstance(stored_position, dict):
                    position_before = PositionState(**stored_position)
                self._notify_order_event(
                    instruction,
                    {"order_id": order_id},
                    "filled",
                    float(payload.get("price", 0.0) or 0.0),
                    position_before=position_before,
                )

            settled_ids.append(order_id)

        for order_id in settled_ids:
            self.state.outstanding_orders.pop(order_id, None)

    def _send_scan_summary(self, eligible: list[str], actions: list[OrderInstruction], signal_ts: pd.Timestamp) -> None:
        top = ", ".join(eligible[:5]) if eligible else "none"
        action_text = ", ".join(
            f"{action.reason}:{action.symbol}@{self._format_price(action.limit_price or action.reference_close or 0.0)}" for action in actions[:6]
        ) if actions else "no orders"
        self.notifier.send(
            f"[{self.settings.bot_name}] {self.settings.candle_interval} cycle {signal_ts.isoformat()}\n"
            f"Eligible: {top}\n"
            f"Actions: {action_text}\n"
            f"Open positions: {len(self.state.positions)} | Cash: {self.state.last_cash:,.0f} | Equity: {self.state.last_equity:,.0f}"
        )

    def _send_scan_log_summary(self, diagnostics: list[ScanDiagnostic], actions: list[OrderInstruction], signal_ts: pd.Timestamp) -> None:
        if not self.settings.telegram_log_id:
            return
        ranked = sorted(
            diagnostics,
            key=lambda item: item.trend_score if item.trend_score is not None else float("-inf"),
            reverse=True,
        )
        lines = [
            f"[{self.settings.bot_name}] {self.settings.candle_interval} scan {signal_ts.isoformat()}",
            f"Eligible: {sum(1 for item in diagnostics if item.eligible)} | Actions: {len(actions)}",
            "Top ranked:",
        ]
        for diagnostic in ranked[:5]:
            score = f"{diagnostic.trend_score:.2f}" if diagnostic.trend_score is not None else "n/a"
            if diagnostic.selected_for_entry:
                status = "ENTRY"
            elif diagnostic.selected_for_add:
                status = "ADD"
            elif diagnostic.selected_for_exit:
                status = "EXIT"
            elif diagnostic.eligible:
                status = "ELIGIBLE"
            else:
                status = diagnostic.failed_reasons[0] if diagnostic.failed_reasons else "no_signal"
            lines.append(f"- {diagnostic.symbol}: score={score}, status={status}")
        self.notifier.send_to(self.settings.telegram_log_id, "\n".join(lines))

    def _log_scan_diagnostics(self, diagnostics: list[ScanDiagnostic], signal_ts: pd.Timestamp) -> None:
        rank_map = {diag.symbol: rank + 1 for rank, diag in enumerate(sorted(
            diagnostics,
            key=lambda item: item.trend_score if item.trend_score is not None else float("-inf"),
            reverse=True,
        ))}
        for diagnostic in diagnostics:
            payload = asdict(diagnostic)
            payload["timestamp_utc"] = signal_ts.isoformat()
            payload["rank"] = rank_map.get(diagnostic.symbol)
            append_jsonl(self.settings.scan_diagnostics_path, payload)

    def _format_bot_positions(self) -> str:
        if self.settings.live_trading and self.roostoo.is_configured():
            try:
                snapshot = self.roostoo.fetch_account_snapshot(self.settings.initial_equity)
                latest_candles = self.refresh_candles()
                signal_ts = self._latest_common_timestamp(latest_candles)
                if signal_ts is not None:
                    self._reconcile_live_positions(snapshot, latest_candles, signal_ts)
                    self.state.last_cash = snapshot.cash
                    self._mark_to_market(latest_candles, signal_ts)
            except Exception as exc:  # noqa: BLE001
                return f"Positions query failed: {exc}"

        if not self.state.positions:
            return "No tracked positions."
        lines = ["Tracked positions:"]
        latest_candles = self.refresh_candles() if self.settings.live_trading else {}
        for symbol, pos in sorted(self.state.positions.items()):
            mark = None
            if symbol in latest_candles and not latest_candles[symbol].empty:
                mark = float(latest_candles[symbol]["close"].iloc[-1])
            pnl = (mark - pos.avg_entry) * pos.units if mark is not None else 0.0
            pnl_pct = ((mark / pos.avg_entry) - 1.0) * 100 if mark is not None and pos.avg_entry > 0 else 0.0
            lines.append(
                f"- {symbol} | qty {pos.units:.6f} | entry {self._format_price(pos.avg_entry)} "
                f"| stop {self._format_price(pos.stop_price)} | mark {self._format_price(mark)} "
                f"| uPnL {pnl:+.2f} ({pnl_pct:+.2f}%)"
            )
        return "\n".join(lines)

    def _refresh_live_positions_for_reporting(self) -> None:
        if not (self.settings.live_trading and self.roostoo.is_configured()):
            return
        snapshot = self.roostoo.fetch_account_snapshot(self.settings.initial_equity)
        latest_candles = self.refresh_candles()
        signal_ts = self._latest_common_timestamp(latest_candles)
        if signal_ts is None:
            return
        self._reconcile_live_positions(snapshot, latest_candles, signal_ts)
        self.state.last_cash = snapshot.cash
        self._mark_to_market(latest_candles, signal_ts)

    def _format_account_snapshot(self) -> str:
        try:
            if self.settings.live_trading and self.roostoo.is_configured():
                snapshot = self.roostoo.fetch_account_snapshot(self.settings.initial_equity)
                balances = snapshot.raw.get("balances", {}) if isinstance(snapshot.raw, dict) else {}
                wallet = self._extract_wallet_from_balances(balances)
                marks = self._build_wallet_marks(wallet)
                cash, equity = self._mark_wallet_equity(wallet, marks, snapshot.cash)
                snapshot = AccountSnapshot(
                    timestamp=snapshot.timestamp,
                    cash=cash,
                    equity=equity,
                    open_orders=snapshot.open_orders,
                    raw=snapshot.raw,
                )
            else:
                snapshot = AccountSnapshot(
                    timestamp=utc_now_iso(),
                    cash=self.state.last_cash,
                    equity=self.state.last_equity,
                    open_orders=list(self.state.outstanding_orders.values()),
                    raw={"mode": "paper"},
                )
        except Exception as exc:  # noqa: BLE001
            return f"Account query failed: {exc}"
        return (
            f"Account snapshot\n"
            f"- timestamp: {snapshot.timestamp}\n"
            f"- cash: {snapshot.cash:,.2f}\n"
            f"- equity: {snapshot.equity:,.2f}\n"
            f"- open_orders: {len(snapshot.open_orders)}\n"
            f"- mode: {'live' if self.settings.live_trading else 'paper'}"
        )

    def _format_wallet(self) -> str:
        if not self.roostoo.is_configured():
            return "Roostoo is not configured."
        try:
            balances = self.roostoo.get_balances()
        except Exception as exc:  # noqa: BLE001
            return f"Wallet query failed: {exc}"
        wallet = self._extract_wallet_from_balances(balances)
        if not wallet:
            return f"Wallet response: {balances}"
        marks = self._build_wallet_marks(wallet)
        lines = ["Roostoo wallet:"]
        for asset, payload in sorted(wallet.items()):
            if not isinstance(payload, dict):
                continue
            free = float(payload.get("Free", 0.0) or 0.0)
            locked = float(payload.get("Lock", 0.0) or 0.0)
            total_units = free + locked
            if asset == "USD":
                lines.append(f"- {asset}: free={free:,.2f}, lock={locked:,.2f} | usd_value={total_units:,.2f}")
                continue
            mark = marks.get(asset)
            if mark is None:
                lines.append(
                    f"- {asset}: free={self._format_units(free)}, lock={self._format_units(locked)} | usd_value=n/a"
                )
                continue
            usd_value = total_units * mark
            lines.append(
                f"- {asset}: free={self._format_units(free)}, lock={self._format_units(locked)} "
                f"| mark={self._format_price(mark)} | usd_value={usd_value:,.2f}"
            )
        return "\n".join(lines)

    def _format_orders(self) -> str:
        try:
            self._refresh_live_positions_for_reporting()
        except Exception:
            pass
        rows = self._load_event_rows()
        if not rows:
            return "No recent bot orders found."
        held_symbols = set(self.state.positions)
        lines = ["Recent bot orders:"]
        for row in reversed(rows[-10:]):
            symbol = str(row.get("symbol", "n/a"))
            action = str(row.get("action", "")).lower()
            side = "buy" if action in {"entry", "add"} else "sell" if action == "full_exit" else action or "n/a"
            qty = float(row.get("units", 0.0) or 0.0)
            price = row.get("entry_price")
            status = str(row.get("order_status", "unknown"))
            if status in {"submitted", "pending", "open", "new"}:
                if side == "buy" and symbol in held_symbols:
                    status = "filled"
                elif side == "sell" and symbol not in held_symbols:
                    status = "filled"
            timestamp = str(row.get("timestamp_utc", "n/a"))
            price_text = ""
            if isinstance(price, (int, float)):
                price_text = f" @ {self._format_price(float(price))}"
            lines.append(f"{timestamp} | {symbol} | {side} qty {self._format_units(qty)}{price_text} | {status}")
        return "\n".join(lines)

    def _format_config(self) -> str:
        return (
            "Bot config\n"
            f"- interval: {self.settings.candle_interval}\n"
            f"- breakout_lookback: {self.settings.breakout_lookback}\n"
            f"- exit_lookback: {self.settings.exit_lookback}\n"
            f"- max_hold_bars: {self.settings.max_hold_bars}\n"
            f"- trailing_stop_pct: {self.settings.trailing_stop_pct:.2f}\n"
            f"- take_profit_1_pct: {self.settings.take_profit_1_pct:.2f}\n"
            f"- take_profit_2_pct: {self.settings.take_profit_2_pct:.2f}\n"
            f"- trend_ema_exit_buffer_pct: {self.settings.trend_ema_exit_buffer_pct:.3f}\n"
            f"- tranche_scheme: {self.settings.tranche_scheme}\n"
            f"- use_btc_filter: {self.settings.use_btc_filter}\n"
            f"- live_trading: {self.settings.live_trading}"
        )

    def _format_state(self) -> str:
        return (
            "Bot state\n"
            f"- last_processed_candle_ts: {self.state.last_processed_candle_ts}\n"
            f"- cash: {self.state.last_cash:,.2f}\n"
            f"- equity: {self.state.last_equity:,.2f}\n"
            f"- positions: {len(self.state.positions)}"
        )

    def _load_scan_diagnostics(self) -> list[dict[str, object]]:
        path = self.settings.scan_diagnostics_path
        if not path.exists():
            return []
        rows: list[dict[str, object]] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
        return rows

    def _format_scan_command(self, command: str) -> str:
        rows = self._load_scan_diagnostics()
        if not rows:
            return "No scan diagnostics found yet."

        parts = command.split()
        symbol = parts[1].upper().strip() if len(parts) > 1 else None
        if symbol:
            filtered = [row for row in rows if str(row.get("symbol", "")).upper() == symbol]
            if not filtered:
                return f"No scan diagnostics found for {symbol}."
            selected = filtered[-5:]
            lines = [f"Latest scan diagnostics for {symbol}:"]
            for row in selected:
                failed_reasons = row.get("failed_reasons") or []
                reason_text = ", ".join(str(item) for item in failed_reasons) if failed_reasons else "passed"
                score = row.get("trend_score")
                score_text = f"{float(score):.2f}" if isinstance(score, (int, float)) else "n/a"
                lines.append(
                    f"- {row.get('timestamp_utc')}: close={row.get('close')} breakout_level={row.get('breakout_high')} "
                    f"score={score_text} eligible={row.get('eligible')} reason={reason_text}"
                )
            return "\n".join(lines)

        latest_ts = str(rows[-1].get("timestamp_utc"))
        latest_rows = [row for row in rows if str(row.get("timestamp_utc")) == latest_ts]
        latest_rows.sort(
            key=lambda row: float(row.get("trend_score")) if isinstance(row.get("trend_score"), (int, float)) else float("-inf"),
            reverse=True,
        )
        lines = [f"Latest scan snapshot {latest_ts}:"]
        for row in latest_rows[:10]:
            failed_reasons = row.get("failed_reasons") or []
            reason_text = ", ".join(str(item) for item in failed_reasons[:2]) if failed_reasons else "passed"
            score = row.get("trend_score")
            score_text = f"{float(score):.2f}" if isinstance(score, (int, float)) else "n/a"
            lines.append(
                f"- {row.get('symbol')}: score={score_text} eligible={row.get('eligible')} "
                f"close={row.get('close')} breakout_level={row.get('breakout_high')} reason={reason_text}"
            )
        return "\n".join(lines)

    def _help_text(self) -> str:
        return (
            "Available commands:\n"
            "/help - show commands\n"
            "/ping - basic liveness/status\n"
            "/account - current account snapshot\n"
            "/wallet - Roostoo wallet balances\n"
            "/orders - recent bot orders\n"
            "/positions - local bot positions\n"
            "/state - local bot state summary\n"
            "/config - active strategy/runtime config\n"
            "/scan [SYMBOL] - latest scan diagnostics"
        )

    def _handle_telegram_command(self, command: str) -> str:
        base = command.split()[0].lower()
        if base == "/help":
            return self._help_text()
        if base == "/ping":
            return (
                f"{self.settings.bot_name} is alive\n"
                f"- live_trading: {self.settings.live_trading}\n"
                f"- last_processed_candle_ts: {self.state.last_processed_candle_ts}\n"
                f"- cash: {self.state.last_cash:,.2f}\n"
                f"- equity: {self.state.last_equity:,.2f}"
            )
        if base == "/account":
            return self._format_account_snapshot()
        if base == "/wallet":
            return self._format_wallet()
        if base == "/orders":
            return self._format_orders()
        if base == "/positions":
            return self._format_bot_positions()
        if base == "/state":
            return self._format_state()
        if base == "/config":
            return self._format_config()
        if base == "/scan":
            return self._format_scan_command(command)
        return "Unknown command. Use /help"

    def poll_telegram_commands(self) -> None:
        if not self.notifier.enabled():
            return
        updates = self.notifier.get_updates(offset=self.telegram_offset, timeout=0)
        if not updates:
            return
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.telegram_offset = update_id + 1
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat", {})
            chat_id = str(chat.get("id", ""))
            if chat_id != str(self.settings.telegram_chat_id):
                continue
            text = str(message.get("text", "")).strip()
            if not text.startswith("/"):
                continue
            try:
                reply = self._handle_telegram_command(text)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Telegram command failed: %s", exc)
                reply = f"Command failed: {exc}"
            self.notifier.send_to(chat_id, reply)
        self._save_telegram_offset()

    def run_cycle(self) -> bool:
        candle_map = self.refresh_candles()
        signal_ts = self._latest_common_timestamp(candle_map)
        if signal_ts is None:
            self.logger.warning("No common candle timestamp across universe.")
            return False
        if self.state.last_processed_candle_ts == signal_ts.isoformat():
            append_jsonl(
                self.settings.heartbeat_log_path,
                {
                    "timestamp_utc": utc_now_iso(),
                    "status": "idle",
                    "last_processed_candle_ts": self.state.last_processed_candle_ts,
                },
            )
            return False

        account = self._build_account_snapshot(candle_map, signal_ts)
        cash_before = account.cash
        equity_before = account.equity
        self.state.last_cash = account.cash
        self.state.last_equity = account.equity
        if self.settings.live_trading and self.roostoo.is_configured():
            self._reconcile_live_positions(account, candle_map, signal_ts)
            self._mark_to_market(candle_map, signal_ts)
            self._reconcile_outstanding_orders(account)

        snapshot = self.strategy.evaluate(candle_map, self.state, account, signal_ts)
        touched: set[str] = set()
        for instruction in snapshot.actions:
            position_before = None
            if instruction.side == "sell" and instruction.symbol in self.state.positions:
                position_before = PositionState(**asdict(self.state.positions[instruction.symbol]))
            response, fill_price, order_status = self._execute_instruction(instruction)
            if order_status == "filled":
                self._apply_fill(instruction, fill_price)
                touched.add(instruction.symbol)
                self._mark_to_market(candle_map, signal_ts)
            self._log_event(instruction, response, order_status, signal_ts, cash_before, equity_before)
            self._notify_order_event(instruction, response, order_status, fill_price, position_before=position_before)
            self._track_outstanding_order(instruction, response, order_status, fill_price, position_before)
            self.logger.info(
                "Order %s %s %.6f @ %.4f | reason=%s | status=%s",
                instruction.side,
                instruction.symbol,
                instruction.quantity,
                fill_price,
                instruction.reason,
                order_status,
            )

        self._advance_positions(candle_map, signal_ts, touched)
        self._mark_to_market(candle_map, signal_ts)
        self.state.last_processed_candle_ts = signal_ts.isoformat()
        self.state_store.save(self.state)

        append_jsonl(
            self.settings.heartbeat_log_path,
            {
                "timestamp_utc": utc_now_iso(),
                "status": "processed",
                "last_processed_candle_ts": self.state.last_processed_candle_ts,
                "eligible_symbols": snapshot.eligible_symbols,
                "actions_count": len(snapshot.actions),
                "cash": self.state.last_cash,
                "equity": self.state.last_equity,
            },
        )
        self._log_scan_diagnostics(snapshot.diagnostics, signal_ts)
        self._send_scan_summary(snapshot.eligible_symbols, snapshot.actions, signal_ts)
        self._send_scan_log_summary(snapshot.diagnostics, snapshot.actions, signal_ts)
        return True

    def run_forever(self) -> None:
        self.bootstrap_candles()
        self.logger.info("Starting %s", self.settings.bot_name)
        self.notifier.send(
            f"[{self.settings.bot_name}] bot started\n"
            f"- live_trading: {self.settings.live_trading}\n"
            f"- interval: {self.settings.candle_interval}\n"
            f"- symbols: {len(self.settings.symbols)}"
        )
        next_strategy_poll = 0.0
        next_telegram_poll = 0.0
        while True:
            try:
                now = time.time()
                if now >= next_telegram_poll:
                    self.poll_telegram_commands()
                    next_telegram_poll = now + max(self.settings.telegram_poll_seconds, 1)
                if now >= next_strategy_poll:
                    processed = self.run_cycle()
                    if processed:
                        self.logger.info(
                            "Cycle complete | cash=%.2f equity=%.2f positions=%s",
                            self.state.last_cash,
                            self.state.last_equity,
                            len(self.state.positions),
                        )
                    next_strategy_poll = now + max(self.settings.polling_seconds, 1)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Bot cycle failed: %s", exc)
                self.notifier.send(f"[{self.settings.bot_name}] cycle failed: {exc}")
            time.sleep(1)
