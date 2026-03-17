from __future__ import annotations

from pathlib import Path

import pandas as pd


class CandleStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str) -> Path:
        return self.root / f"{symbol}.csv"

    def load(self, symbol: str) -> pd.DataFrame:
        path = self.path_for(symbol)
        if not path.exists():
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = pd.read_csv(path, parse_dates=["timestamp"])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        return frame.set_index("timestamp").sort_index()

    def save(self, symbol: str, frame: pd.DataFrame) -> None:
        out = frame.reset_index().rename(columns={"index": "timestamp"})
        out.to_csv(self.path_for(symbol), index=False)

    def upsert(self, symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
        existing = self.load(symbol)
        merged = pd.concat([existing, frame]).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        self.save(symbol, merged)
        return merged
