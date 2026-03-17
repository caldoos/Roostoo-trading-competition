from __future__ import annotations

import sys
from pathlib import Path
from pprint import pprint

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roostoo_bot.clients.roostoo import RoostooClient
from roostoo_bot.config import load_settings


def main() -> None:
    settings = load_settings()
    client = RoostooClient(settings)
    print("configured:", client.is_configured())
    pprint({"server_time": client.get_server_time()})
    pprint({"balances": client.get_balances()})
    pprint({"pending_count": client.get_pending_count()})
    pprint({"open_orders": client.query_orders(pending_only=True)})
    pprint({"snapshot": client.fetch_account_snapshot(settings.initial_equity)})


if __name__ == "__main__":
    main()
