from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class OrderInstruction:
    symbol: str
    side: str
    quantity: float
    order_type: str
    limit_price: float | None
    reason: str
    tranche_number: int | None = None
    target_notional: float | None = None
    stop_price: float | None = None
    trend_score: float | None = None
    breakout_high: float | None = None
    exit_low: float | None = None
    trend_ema: float | None = None
    trend_ema_slope: float | None = None
    reference_close: float | None = None


@dataclass(slots=True)
class PositionState:
    symbol: str
    units: float
    target_notional: float
    tranches_filled: int
    stop_price: float
    peak_close: float
    hold_bars: int
    avg_entry: float
    bars_since_last_fill: int
    tp_base_units: float = 0.0
    tp1_taken: bool = False
    tp2_taken: bool = False


@dataclass(slots=True)
class AccountSnapshot:
    timestamp: str
    cash: float
    equity: float
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScanDiagnostic:
    symbol: str
    close: float | None
    breakout_high: float | None
    exit_low: float | None
    trend_ema: float | None
    trend_ema_slope: float | None
    trend_score: float | None
    base_entry: bool
    eligible: bool
    in_position: bool
    selected_for_entry: bool = False
    selected_for_add: bool = False
    selected_for_exit: bool = False
    failed_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StrategySnapshot:
    timestamp: str
    ranked_symbols: list[str]
    eligible_symbols: list[str]
    trend_scores: dict[str, float]
    actions: list[OrderInstruction]
    diagnostics: list[ScanDiagnostic] = field(default_factory=list)


@dataclass(slots=True)
class BotState:
    last_processed_candle_ts: str | None = None
    positions: dict[str, PositionState] = field(default_factory=dict)
    outstanding_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_equity: float = 0.0
    last_cash: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_processed_candle_ts": self.last_processed_candle_ts,
            "positions": {symbol: asdict(pos) for symbol, pos in self.positions.items()},
            "outstanding_orders": self.outstanding_orders,
            "last_equity": self.last_equity,
            "last_cash": self.last_cash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BotState":
        positions = {
            symbol: PositionState(**pos_payload)
            for symbol, pos_payload in payload.get("positions", {}).items()
        }
        return cls(
            last_processed_candle_ts=payload.get("last_processed_candle_ts"),
            positions=positions,
            outstanding_orders=dict(payload.get("outstanding_orders", {})),
            last_equity=float(payload.get("last_equity", 0.0)),
            last_cash=float(payload.get("last_cash", 0.0)),
        )
