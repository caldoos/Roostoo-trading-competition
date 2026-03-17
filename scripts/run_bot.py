from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roostoo_bot.bot import TrendBot
from roostoo_bot.config import load_settings


def main() -> None:
    settings = load_settings()
    bot = TrendBot(settings)
    bot.run_forever()


if __name__ == "__main__":
    main()
