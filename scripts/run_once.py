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
    bot.bootstrap_candles()
    processed = bot.run_cycle()
    print(
        {
            "processed": processed,
            "last_processed_candle_ts": bot.state.last_processed_candle_ts,
            "cash": bot.state.last_cash,
            "equity": bot.state.last_equity,
            "positions": list(bot.state.positions.keys()),
        }
    )


if __name__ == "__main__":
    main()
