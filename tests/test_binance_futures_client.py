from __future__ import annotations

from roostoo_bot.clients.binance_futures import BinanceFuturesClient


def test_normalize_order_params_allows_fractional_btc(settings) -> None:
    client = BinanceFuturesClient(settings)
    client._symbol_rules = {
        "BTCUSDT": {
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "price_filter": {"tickSize": "0.10"},
            "lot_size": {"stepSize": "0.001", "minQty": "0.001"},
            "market_lot_size": {"stepSize": "0.001", "minQty": "0.001"},
            "min_notional": {"notional": "5"},
        }
    }

    result = client.normalize_order_params(
        symbol="BTCUSDT",
        quantity=0.311848,
        price=67340.47,
        order_type="market",
    )

    assert result["ok"] is True
    assert result["quantity"] == 0.311
    assert result["price"] == 67340.4


def test_normalize_order_params_rejects_below_min_notional(settings) -> None:
    client = BinanceFuturesClient(settings)
    client._symbol_rules = {
        "DOGEUSDT": {
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "price_filter": {"tickSize": "0.00001"},
            "lot_size": {"stepSize": "1", "minQty": "1"},
            "market_lot_size": {"stepSize": "1", "minQty": "1"},
            "min_notional": {"notional": "5"},
        }
    }

    result = client.normalize_order_params(
        symbol="DOGEUSDT",
        quantity=10,
        price=0.10,
        order_type="limit",
    )

    assert result["ok"] is False
    assert "minNotional" in result["error"]
