import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import types
import pytest

import config
from devig_utils import devig_multiplicative
import engine


def test_scan_places_with_live_refetched_fair(monkeypatch):
    """End-to-end: snapshot fair is F0; the live bulk re-fetch returns a MOVED
    price -> F1. The emitted placement must carry F1 (real _find_pin_prices ->
    devig -> haircut), fair_refreshed True, and the snapshot->live delta. No
    real network: the bulk fetch + soft adapter are mocked at the boundary."""
    F0 = 0.55
    monkeypatch.setattr(engine, "load_latest_snapshot", lambda d: ([{}], 5.0))
    monkeypatch.setattr(engine, "_load_soft_markets", lambda: (["soft"], {"kalshi": 20.0}))
    monkeypatch.setattr(engine, "read_latest_snapshot_meta", lambda d: {"cycle_elapsed_sec": 5.0})
    monkeypatch.setattr(engine, "_pin_sport_id_map", lambda: {"Basketball": 7})
    monkeypatch.setattr(
        engine.pinnacle_client, "fetch_bulk_markets",
        lambda sid: [{
            "matchupId": 123, "type": "moneyline", "period": 0,
            "prices": [{"designation": "home", "price": -150},
                       {"designation": "away", "price": 140}],
        }],
    )

    class _FakeAdapter:
        BOOK = "kalshi"
        SNAPSHOT_DIR = "x"
        SUPPORTS_NO_SIDE = False
        def fetch_both_ladders(self, mid): return ([(0.50, 100)], None)
        def taker_fee_per_share(self, price, fair): return 0.0
        def market_url(self, nm): return "u"

    fa = _FakeAdapter()
    monkeypatch.setattr(engine, "adapter_for", lambda b: fa)
    monkeypatch.setattr(engine, "all_adapters", lambda: [fa])

    nm = types.SimpleNamespace(title="A vs B", book="kalshi", market_id="K1")
    candidate = {
        "market": nm, "book": "kalshi", "market_id": "K1", "market_url": "u",
        "pin_sport": "Basketball", "pin_matchup": "A vs B", "pin_matchup_id": 123,
        "pin_start_time": "2026-06-12T20:00:00Z", "market_type": "moneyline",
        "period_label": "FULL", "line": None, "selection": "A ML",
        "yes_pin_name": "A", "yes_side_label": "A", "opposite_side_label": "B",
        "yes_designation": "home", "opposite_designation": "away",
        "yes_side_price": -150, "opposite_side_price": 140,
        "yes_fair": F0, "opposite_fair": 1 - F0,
        "yes_fair_raw": F0, "opposite_fair_raw": 1 - F0,
        "in_window": True,
    }
    monkeypatch.setattr(engine, "find_matches", lambda pr, sm: ([candidate], {}))

    out = engine.scan()

    raw = devig_multiplicative([-150, 140])
    f1 = config.haircut_fair(raw[0])

    assert len(out["placements"]) == 1
    side_row, ladder = out["placements"][0]
    assert side_row["fair_prob"] == pytest.approx(f1)
    assert side_row["fair_prob"] != F0
    assert side_row["fair_refreshed"] is True
    assert side_row["pin_refetch_delta"] == pytest.approx(round(f1 - F0, 6))
