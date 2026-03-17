from __future__ import annotations

import json
from pathlib import Path

from roostoo_bot.models import BotState


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return BotState.from_dict(payload)

    def save(self, state: BotState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(self.path)
