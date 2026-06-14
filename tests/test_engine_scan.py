"""#2 — the headless engine.scan() pipeline.

Pins: engine returns the result dict + a placements list, NEVER places bets itself,
swallows per-candidate fetch errors, and honours the ladder-fetch timeout. The scan
loaders + matcher are monkeypatched so no live adapter is hit.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pmev import engine
from pmev.execution import paper as paper_tracker
from pmev.execution import real as real_tracker


def _nm(book="kalshi", market_id="KX-1"):
    return SimpleNamespace(book=book, market_id=market_id, title="Home ML",
                           player=None, stat=None, yes_sub_title="Home", team="Home")


def _candidate(market_id="KX-1", mtype="moneyline"):
    return {
        "market": _nm(market_id=market_id), "book": "kalshi", "market_id": market_id,
        "market_url": "http://x", "pin_matchup": "Home vs Away", "pin_matchup_id": 1,
        "pin_home_name": "Home", "pin_away_name": "Away", "pin_start_time": "2099-01-01T00:00:00+00:00",
        "pin_sport": "nba", "market_type": mtype, "period_label": "FULL", "line": None,
        "selection": "Home", "yes_side_label": "Home", "opposite_side_label": "Away",
        "yes_designation": "home", "opposite_designation": "away",
        "yes_side_price": -150, "opposite_side_price": 130,
        # fair 0.55 @ price 0.50, zero fee → ev_pct 10% (between 2% floor / 15% ceiling).
        "yes_fair": 0.55, "opposite_fair": 0.45, "fair_prob": 0.55,
        "yes_fair_raw": 0.57, "opposite_fair_raw": 0.47, "yes_pin_name": "Home",
        "yes_soft_name": "Home", "in_window": True, "player": None, "stat": None,
        "pin_prop_matchup_id": None,
    }


def _fake_adapter(ladder=None, raises=False, hang=False):
    def fetch_both_ladders(_mid):
        if raises:
            raise RuntimeError("book down")
        if hang:
            import time as _t
            _t.sleep(0.6)
        return (ladder or [(0.50, 100)]), None
    return SimpleNamespace(
        SUPPORTS_NO_SIDE=False,
        fetch_both_ladders=fetch_both_ladders,
        taker_fee_per_share=lambda price, fair: 0.0,
    )


def _patch_engine(monkeypatch, candidates, adapter):
    monkeypatch.setattr(engine, "load_latest_snapshot", lambda _dir: ([{}], 10))
    monkeypatch.setattr(engine, "_load_soft_markets", lambda: (["soft"], {"kalshi": 10}))
    monkeypatch.setattr(engine, "find_matches", lambda *_a, **_k: (candidates, {}))
    monkeypatch.setattr(engine, "read_latest_snapshot_meta", lambda _d: None)
    monkeypatch.setattr(engine, "adapter_for", lambda book: adapter)
    monkeypatch.setattr(engine, "all_adapters", lambda: [])
    # #6b: in-window candidates require a successful live re-fetch to be placed
    # (fail-closed). These structural tests aren't about the re-fetch (covered by
    # test_engine_refresh / test_live_refetch_integration), so mock it as a no-op
    # passthrough that echoes the snapshot fair.
    monkeypatch.setattr(engine, "_refresh_fair", lambda c, cache, smap: {
        "yes_fair": c["yes_fair"], "opposite_fair": c["opposite_fair"],
        "yes_fair_raw": c["yes_fair_raw"], "opposite_fair_raw": c["opposite_fair_raw"],
    })


def test_scan_returns_result_keys(monkeypatch):
    # U2a: result carries the keys the dashboard binds to _state, plus placements.
    _patch_engine(monkeypatch, [_candidate()], _fake_adapter())
    result = engine.scan()
    for k in ("pin_age", "book_ages", "cross_book_skew_sec", "stats", "rows",
              "prop_rows", "placements"):
        assert k in result


def test_scan_does_not_place(monkeypatch):
    # IT-2: the engine never calls either tracker's maybe_place.
    _patch_engine(monkeypatch, [_candidate()], _fake_adapter())
    called = {"paper": 0, "real": 0}
    monkeypatch.setattr(paper_tracker, "maybe_place",
                        lambda *a, **k: called.__setitem__("paper", called["paper"] + 1))
    monkeypatch.setattr(real_tracker, "maybe_place",
                        lambda *a, **k: called.__setitem__("real", called["real"] + 1))
    engine.scan()
    assert called == {"paper": 0, "real": 0}


def test_scan_emits_placements(monkeypatch):
    # U2c: one (side_row, ladder) per evaluated side, carrying side fair + raw.
    _patch_engine(monkeypatch, [_candidate()], _fake_adapter())
    result = engine.scan()
    assert len(result["placements"]) == 1
    side_row, ladder = result["placements"][0]
    assert side_row["side"] == "yes"
    assert side_row["fair_prob"] == 0.55
    assert side_row["fair_prob_raw"] == 0.57
    assert ladder == [(0.50, 100)]


def test_scan_swallows_candidate_errors(monkeypatch):
    # U2b: a candidate whose ladder fetch raises is skipped; others still scan.
    good = _candidate(market_id="KX-good")
    bad = _candidate(market_id="KX-bad")

    def fetch(mid):
        if mid == "KX-bad":
            raise RuntimeError("book down")
        return [(0.50, 100)], None

    adapter = SimpleNamespace(SUPPORTS_NO_SIDE=False, fetch_both_ladders=fetch,
                              taker_fee_per_share=lambda p, f: 0.0)
    _patch_engine(monkeypatch, [bad, good], adapter)
    result = engine.scan()
    ids = {r["market_id"] for r in result["rows"]}
    assert "KX-good" in ids
    assert "KX-bad" not in ids


def test_scan_honours_ladder_timeout(monkeypatch):
    # U2d: a hanging fetch is dropped via the executor timeout, scan still returns.
    monkeypatch.setattr(engine, "LADDER_FETCH_TIMEOUT_SEC", 0.2)
    _patch_engine(monkeypatch, [_candidate()], _fake_adapter(hang=True))
    result = engine.scan()
    assert result["rows"] == []
    # the timed-out candidate produced no placement either
    assert result["placements"] == []
