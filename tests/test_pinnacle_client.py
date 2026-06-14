import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import importlib
import pytest


# ---------------------------------------------------------------------------
# Group A — pinnacle_client: shared row transform + lazy API key + rate gate.
# ---------------------------------------------------------------------------

MARKET_ML = {
    "period": 0,
    "type": "moneyline",
    "prices": [
        {"designation": "home", "price": -150},
        {"designation": "away", "price": 130},
    ],
}
MARKET_TT = {
    "period": 0,
    "type": "team_total",
    "side": "home",
    "prices": [
        {"designation": "over", "price": -110, "points": 5.5},
        {"designation": "under", "price": -110, "points": 5.5},
    ],
}
MATCHUP = {
    "id": 123,
    "startTime": "2026-06-12T20:00:00Z",
    "participants": [
        {"name": "Aces", "alignment": "home"},
        {"name": "Storm", "alignment": "away"},
    ],
}


def test_market_to_row_parity_with_record_market(monkeypatch):
    # record_market must produce exactly what market_to_row produces (same schema,
    # one source of truth). Pins the drift #4/#8 fought.
    monkeypatch.setenv("PINNACLE_API_KEY", "x")  # poller requires it at import
    from pmev.core import pinnacle_client
    from pmev.pollers import pinnacle as pinnacle_poller

    for market in (MARKET_ML, MARKET_TT):
        snapshot = []
        pinnacle_poller.record_market(
            market, MATCHUP, "Basketball", False,
            snapshot, {}, {}, lambda *a, **k: None,
        )
        row = pinnacle_client.market_to_row(market, MATCHUP, "Basketball", False)
        assert snapshot == [row]


def test_team_total_row_carries_side(monkeypatch):
    monkeypatch.setenv("PINNACLE_API_KEY", "x")
    from pmev.core import pinnacle_client
    row = pinnacle_client.market_to_row(MARKET_TT, MATCHUP, "Basketball", False)
    assert row["side"] == "home"
    assert row["type"] == "team_total"
    assert row["matchupId"] == 123
    assert row["isLive"] is False


def test_import_without_key_succeeds(monkeypatch):
    # Lazy key: importing the client with no PINNACLE_API_KEY must not raise.
    monkeypatch.delenv("PINNACLE_API_KEY", raising=False)
    from pmev.core import pinnacle_client
    importlib.reload(pinnacle_client)  # re-executes module top-level; must not raise


def test_headers_without_key_raises(monkeypatch):
    # The key is required only when a request is actually built.
    monkeypatch.delenv("PINNACLE_API_KEY", raising=False)
    from pmev.core import pinnacle_client
    with pytest.raises(RuntimeError, match="PINNACLE_API_KEY"):
        pinnacle_client._headers()


def test_fetch_goes_through_rate_gate(monkeypatch):
    monkeypatch.setenv("PINNACLE_API_KEY", "x")
    from pmev.core import pinnacle_client

    class _FakeGate:
        def __init__(self): self.slots = 0
        def claim_slot(self): self.slots += 1
        def record_429(self, backoff): pass

    fake = _FakeGate()
    monkeypatch.setattr(pinnacle_client, "_gate", fake)

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return []

    monkeypatch.setattr(pinnacle_client.requests, "get", lambda *a, **k: _Resp())
    pinnacle_client.fetch_bulk_markets(99)
    assert fake.slots == 1
