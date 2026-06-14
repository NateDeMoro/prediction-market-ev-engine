import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from pmev import config
from pmev.core.devig import devig_multiplicative
from pmev import engine


# ---------------------------------------------------------------------------
# Group B — engine._refresh_fair: live bulk re-fetch -> fresh side fairs / None.
# ---------------------------------------------------------------------------

SPORT_MAP = {"Basketball": 7}


def _ml_candidate(**extra):
    c = {
        "book": "kalshi", "market_id": "K1", "pin_sport": "Basketball",
        "pin_matchup_id": 123, "pin_start_time": "2026-06-12T20:00:00Z",
        "market_type": "moneyline", "period_label": "FULL", "line": None,
        "yes_designation": "home", "opposite_designation": "away",
        "yes_fair": 0.50, "opposite_fair": 0.50, "in_window": True,
    }
    c.update(extra)
    return c


def _ml_bulk(home=-150, away=140, matchup_id=123):
    return [{
        "matchupId": matchup_id, "type": "moneyline", "period": 0,
        "prices": [
            {"designation": "home", "price": home},
            {"designation": "away", "price": away},
        ],
    }]


def _patch_bulk(monkeypatch, bulk_or_exc):
    def _fetch(sid):
        if isinstance(bulk_or_exc, Exception):
            raise bulk_or_exc
        return bulk_or_exc
    monkeypatch.setattr(engine.pinnacle_client, "fetch_bulk_markets", _fetch)


def test_same_line_moved_price_recomputes(monkeypatch):
    _patch_bulk(monkeypatch, _ml_bulk(home=-150, away=140))
    c = _ml_candidate(yes_fair=0.50)
    fresh = engine._refresh_fair(c, {}, SPORT_MAP)
    assert fresh is not None
    assert fresh["yes_fair"] != 0.50  # snapshot fair was 0.50; refetch differs


def test_haircut_parity_with_find_matches(monkeypatch):
    _patch_bulk(monkeypatch, _ml_bulk(home=-150, away=140))
    raw = devig_multiplicative([-150, 140])  # [yes_raw, opp_raw]
    fresh = engine._refresh_fair(_ml_candidate(), {}, SPORT_MAP)
    assert fresh["yes_fair_raw"] == pytest.approx(raw[0])
    assert fresh["opposite_fair_raw"] == pytest.approx(raw[1])
    assert fresh["yes_fair"] == pytest.approx(config.haircut_fair(raw[0]))
    assert fresh["opposite_fair"] == pytest.approx(config.haircut_fair(raw[1]))


def test_opposite_side_perspective(monkeypatch):
    _patch_bulk(monkeypatch, _ml_bulk(home=-200, away=170))
    fresh = engine._refresh_fair(_ml_candidate(), {}, SPORT_MAP)
    # home favored -> yes (home) fair > opposite (away) fair
    assert fresh["yes_fair"] > fresh["opposite_fair"]


def test_fetch_error_fails_closed(monkeypatch):
    _patch_bulk(monkeypatch, RuntimeError("boom"))
    assert engine._refresh_fair(_ml_candidate(), {}, SPORT_MAP) is None


def test_matchup_absent_fails_closed(monkeypatch):
    _patch_bulk(monkeypatch, _ml_bulk(matchup_id=999))  # different matchup
    assert engine._refresh_fair(_ml_candidate(), {}, SPORT_MAP) is None


def test_unknown_sport_fails_closed(monkeypatch):
    _patch_bulk(monkeypatch, _ml_bulk())
    assert engine._refresh_fair(_ml_candidate(pin_sport="Curling"), {}, {}) is None


def test_bulk_fetched_once_per_sport(monkeypatch):
    calls = []
    monkeypatch.setattr(engine.pinnacle_client, "fetch_bulk_markets",
                        lambda sid: calls.append(sid) or _ml_bulk())
    cache = {}
    engine._refresh_fair(_ml_candidate(market_id="K1"), cache, SPORT_MAP)
    engine._refresh_fair(_ml_candidate(market_id="K2"), cache, SPORT_MAP)
    assert calls == [7]


def test_moved_away_total_uses_interpolation(monkeypatch):
    # Exact line 45.5 is gone; bracketing alternates 45.0 and 46.0 are within the
    # 1.0pt cap -> interpolation (#22) recovers a fair instead of fail-closed.
    bulk = [
        {"matchupId": 123, "type": "total", "period": 0,
         "prices": [{"designation": "over", "points": 45.0, "price": -120},
                    {"designation": "under", "points": 45.0, "price": 100}]},
        {"matchupId": 123, "type": "total", "period": 0,
         "prices": [{"designation": "over", "points": 46.0, "price": 100},
                    {"designation": "under", "points": 46.0, "price": -120}]},
    ]
    _patch_bulk(monkeypatch, bulk)
    c = _ml_candidate(market_type="total", line=45.5,
                      yes_designation="over", opposite_designation="under")
    fresh = engine._refresh_fair(c, {}, SPORT_MAP)
    assert fresh is not None
    assert 0.0 < fresh["yes_fair"] < 1.0


def test_moved_away_total_no_bracket_fails_closed(monkeypatch):
    # Only one alternate -> no bracket -> fail-closed.
    bulk = [
        {"matchupId": 123, "type": "total", "period": 0,
         "prices": [{"designation": "over", "points": 45.0, "price": -120},
                    {"designation": "under", "points": 45.0, "price": 100}]},
    ]
    _patch_bulk(monkeypatch, bulk)
    c = _ml_candidate(market_type="total", line=48.5,
                      yes_designation="over", opposite_designation="under")
    assert engine._refresh_fair(c, {}, SPORT_MAP) is None


def test_fetch_error_logs_skip(monkeypatch, capsys):
    # The fail-closed reason is logged (observability seam).
    _patch_bulk(monkeypatch, RuntimeError("boom"))
    engine._refresh_fair(_ml_candidate(), {}, SPORT_MAP)
    assert "fair-refresh" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Group C — engine.scan() integration of the refresh.
# ---------------------------------------------------------------------------

import types


def _scan_candidate(in_window=True, **extra):
    nm = types.SimpleNamespace(title="A vs B", book="kalshi", market_id="K1")
    c = {
        "market": nm, "book": "kalshi", "market_id": "K1", "market_url": "http://x",
        "pin_sport": "Basketball", "pin_matchup": "A vs B", "pin_matchup_id": 123,
        "pin_start_time": "2026-06-12T20:00:00Z", "market_type": "moneyline",
        "period_label": "FULL", "line": None, "selection": "A ML",
        "yes_pin_name": "A", "yes_side_label": "A", "opposite_side_label": "B",
        "yes_side_price": -150, "opposite_side_price": 140,
        "yes_fair": 0.55, "opposite_fair": 0.45,
        "yes_fair_raw": 0.56, "opposite_fair_raw": 0.44,
        "in_window": in_window,
    }
    c.update(extra)
    return c


def _patch_scan(monkeypatch, candidates):
    monkeypatch.setattr(engine, "load_latest_snapshot", lambda d: ([{}], 5.0))
    monkeypatch.setattr(engine, "_load_soft_markets", lambda: (["soft"], {"kalshi": 20.0}))
    monkeypatch.setattr(engine, "read_latest_snapshot_meta", lambda d: {"cycle_elapsed_sec": 5.0})
    monkeypatch.setattr(engine, "find_matches", lambda pr, sm: (list(candidates), {}))
    monkeypatch.setattr(engine, "_pin_sport_id_map", lambda: {"Basketball": 7})

    class _FakeAdapter:
        BOOK = "kalshi"
        SNAPSHOT_DIR = "x"
        SUPPORTS_NO_SIDE = False
        def fetch_both_ladders(self, mid): return ([(0.50, 100)], None)
        def taker_fee_per_share(self, price, fair): return 0.0
        def market_url(self, nm): return "http://x"

    fake = _FakeAdapter()
    monkeypatch.setattr(engine, "adapter_for", lambda b: fake)
    monkeypatch.setattr(engine, "all_adapters", lambda: [fake])


def test_scan_refresh_updates_placement(monkeypatch):
    _patch_scan(monkeypatch, [_scan_candidate()])
    fresh = {"yes_fair": 0.60, "opposite_fair": 0.40,
             "yes_fair_raw": 0.61, "opposite_fair_raw": 0.39}
    monkeypatch.setattr(engine, "_refresh_fair", lambda c, cache, smap: fresh)
    out = engine.scan()
    assert len(out["placements"]) == 1
    side_row, _ladder = out["placements"][0]
    assert side_row["fair_prob"] == 0.60
    assert side_row["fair_refreshed"] is True
    assert side_row["pin_refetch_delta"] == round(0.60 - 0.55, 6)


def test_scan_failed_refresh_excludes_from_placements(monkeypatch):
    _patch_scan(monkeypatch, [_scan_candidate()])
    monkeypatch.setattr(engine, "_refresh_fair", lambda c, cache, smap: None)
    out = engine.scan()
    assert out["placements"] == []  # fail-closed
    # kept in display, flagged not-refreshed
    assert out["rows"], "candidate should remain visible in display"
    assert all(r.get("fair_refreshed") is False for r in out["rows"])


def test_scan_refresh_scoped_to_in_window(monkeypatch):
    _patch_scan(monkeypatch, [_scan_candidate(in_window=False)])
    calls = []
    monkeypatch.setattr(engine, "_refresh_fair",
                        lambda c, cache, smap: calls.append(c["market_id"]) or None)
    out = engine.scan()
    assert calls == []  # out-of-window candidate never re-fetched
    # out-of-window flow unchanged: still emitted to placements (maybe_place gates it)
    assert len(out["placements"]) == 1
    assert out["placements"][0][0]["fair_refreshed"] is False


# ---------------------------------------------------------------------------
# Group D — the recorded placement carries the fair_refreshed marker.
# ---------------------------------------------------------------------------

def test_paper_record_carries_fair_refreshed(monkeypatch):
    from pmev.execution import paper as pt

    captured = {}
    monkeypatch.setattr(pt, "_bankroll", 1000.0)
    monkeypatch.setattr(pt, "_placed_keys", set())
    monkeypatch.setattr(pt, "_open_positions", {})
    monkeypatch.setattr(pt, "adapter_for", lambda b: object())
    monkeypatch.setattr(pt, "size_bet", lambda *a, **k: {
        "avg_fill_price": 0.5, "shares": 20, "stake": 10.0, "price_stake": 10.0,
        "fee_upfront": 0.0, "expected_profit": 0.5, "kelly_fraction_full": 0.1,
        "kelly_fraction_applied": 0.05, "levels": [],
    })

    def _capture(path, rec):
        if path == pt.config.PAPER_TRADES_PATH:
            captured["rec"] = rec
    monkeypatch.setattr(pt, "_append_jsonl", _capture)

    nm = types.SimpleNamespace(book="kalshi", market_id="K1")
    row = {
        "market": nm, "in_window": True, "side": "yes", "market_type": "moneyline",
        "fair_prob": 0.55, "fair_prob_raw": 0.56, "pin_matchup_id": 123,
        "fair_refreshed": True, "pin_refetch_delta": 0.05,
    }
    pt.maybe_place(row, [(0.5, 100)])
    assert captured["rec"]["fair_refreshed"] is True
    assert captured["rec"]["pin_refetch_delta"] == 0.05
