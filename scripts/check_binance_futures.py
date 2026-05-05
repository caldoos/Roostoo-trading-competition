from __future__ import annotations

from pprint import pprint

from roostoo_bot.clients.binance import BinanceClient
from roostoo_bot.clients.binance_futures import BinanceFuturesClient
from roostoo_bot.config import load_settings


def main() -> None:
    settings = load_settings()
    client = BinanceFuturesClient(settings)
    market = BinanceClient(settings.binance_futures_base_url, market_type="usdtm")

    print(f"configured: {client.is_configured()}")
    print(f"exchange: {settings.exchange}")
    pprint({"server_time": client.get_server_time()})
    symbols = market.fetch_usdt_m_symbols(
        limit=settings.binance_symbol_limit or 10,
        excluded_symbols=settings.binance_excluded_symbols,
    )
    pprint({"sample_symbols": symbols[:10], "sample_count": len(symbols)})

    if not client.is_configured():
        print("Set BINANCE_API_KEY and BINANCE_API_SECRET before checking private account data.")
        return

    pprint({"balances": client.get_balances()})
    pprint({"pending_count": client.get_pending_count()})
    pprint({"snapshot": client.fetch_account_snapshot(settings.initial_equity)})


if __name__ == "__main__":
    main()
