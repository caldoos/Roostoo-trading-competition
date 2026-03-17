from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace
from urllib.parse import urlencode

from roostoo_bot.clients.roostoo import RoostooClient

from tests.conftest import build_settings


def test_roostoo_signed_request_uses_expected_headers(monkeypatch, tmp_path) -> None:
    settings = build_settings(tmp_path, roostoo_api_key="key-1", roostoo_api_secret="secret-1")
    client = RoostooClient(settings)
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status_code=200,
            content=b'{"Success": true}',
            json=lambda: {"Success": True},
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(client.session, "request", fake_request)
    monkeypatch.setattr(client, "_timestamp_ms", lambda: "1234567890")

    client.get_balances()

    expected_payload = urlencode({"timestamp": "1234567890"})
    expected_sig = hmac.new(b"secret-1", expected_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    assert captured["headers"]["RST-API-KEY"] == "key-1"
    assert captured["headers"]["MSG-SIGNATURE"] == expected_sig
    assert captured["params"]["timestamp"] == "1234567890"


def test_roostoo_normalize_pair_and_snapshot(tmp_path, monkeypatch) -> None:
    client = RoostooClient(build_settings(tmp_path))

    assert client._normalize_pair("BTCUSDT") == "BTC/USD"
    assert client._normalize_pair("ETH/USD") == "ETH/USD"

    monkeypatch.setattr(
        client,
        "get_balances",
        lambda: {
            "Success": True,
            "SpotWallet": {
                "USD": {"Free": 50000, "Lock": 1000},
                "BTC": {"Free": 1.5, "Lock": 0},
            },
        },
    )
    monkeypatch.setattr(client, "get_pending_count", lambda: {"TotalPending": 0})
    monkeypatch.setattr(client, "get_ticker", lambda pair=None: {"Data": {"BTC/USD": {"LastPrice": 60000}}})

    snapshot = client.fetch_account_snapshot(1_000_000.0)

    assert snapshot.cash == 50000.0
    assert snapshot.equity == 141000.0
    assert snapshot.open_orders == []
