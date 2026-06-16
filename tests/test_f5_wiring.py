"""F5 (first-5-innings) 2-way wiring: adapters emit F5 spread/total with
period_label='F5', both period maps resolve F5 -> Pinnacle period 1, and F5 is
paper-only (excluded from real placement). Moneyline F5 (3-way) stays dropped.

Replaces the old test_polymarket_f5_block.py (which pinned the now-removed
blanket F5 drop).
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pmev import config
from pmev.adapters import polymarket as pm
from pmev.adapters import kalshi as ks
from pmev.matching import matcher
from pmev.execution import paper as paper_tracker
from pmev.execution import real as real_tracker


# --- raw-row builders -----------------------------------------------------

def _pm_row(slug, mtype, *, description="Over", line=4.5, team=None, long=True):
    """A raw Polymarket snapshot row (one marketSide), shaped like
    pollers/polymarket.flatten_event output."""
    return {
        "event": {
            "event_slug": "mlb-cin-phi-2026-05-20",
            "event_id": 123,
            "event_title": "Reds @ Phillies",
            "start_time": "2026-05-20T17:05:00Z",
            "league": "mlb",
            "teams": [{"name": "Cincinnati Reds"}, {"name": "Philadelphia Phillies"}],
        },
        "market": {
            "slug": slug,
            "question": "q",
            "sports_market_type_v2": mtype,
            "line": line,
            "active": True, "closed": False, "archived": False,
        },
        "side": {"long": long, "description": description, "team_name": team},
    }


def _ks_row(series_ticker, yes_sub, *, ticker="T-1", title="MLB"):
    return {
        "series_ticker": series_ticker,
        "ticker": ticker,
        "yes_sub_title": yes_sub,
        "title": title,
        "occurrence_datetime": "2026-05-20T17:05:00Z",
        "event_ticker": f"{series_ticker}-26MAY20CINPHI",
    }


# --- Polymarket adapter (tests 1-4) ---------------------------------------

def test_pm_f5_total_normalizes_as_f5():
    nm = pm.normalize_market(
        _pm_row("tsc-mlb-cin-phi-2026-05-20-f5-4pt5", "SPORTS_MARKET_TYPE_TOTAL",
                description="Over", line=4.5))
    assert nm is not None
    assert nm.market_type == "total"
    assert nm.period_label == "F5"
    assert nm.line == 4.5


def test_pm_f5_spread_normalizes_as_f5():
    nm = pm.normalize_market(
        _pm_row("asc-mlb-cin-phi-2026-05-20-f5-neg-1pt5", "SPORTS_MARKET_TYPE_SPREAD",
                description="-1.50", team="Cincinnati Reds"))
    assert nm is not None
    assert nm.market_type == "spread"
    assert nm.period_label == "F5"
    assert nm.line == 1.5


def test_pm_f5_moneyline_is_dropped():
    """F5 moneyline is 3-way (tie) — excluded from this 2-way wiring."""
    nm = pm.normalize_market(
        _pm_row("aec-mlb-cin-phi-2026-05-20-f5", "SPORTS_MARKET_TYPE_MONEYLINE",
                team="Cincinnati Reds"))
    assert nm is None


def test_pm_full_game_total_still_full():
    nm = pm.normalize_market(
        _pm_row("tsc-mlb-cin-phi-2026-05-20-6pt5", "SPORTS_MARKET_TYPE_TOTAL", line=6.5))
    assert nm is not None
    assert nm.period_label == "FULL"


# --- Kalshi adapter (tests 5-7) -------------------------------------------

def test_ks_f5_total_normalizes_as_f5():
    nm = ks.normalize_market(_ks_row("KXMLBF5TOTAL", "Over 4.5"))
    assert nm is not None
    assert nm.market_type == "total"
    assert nm.period_label == "F5"
    assert nm.line == 4.5


def test_ks_f5_spread_normalizes_as_f5():
    nm = ks.normalize_market(_ks_row("KXMLBF5SPREAD", "Cincinnati Reds wins by over 1.5"))
    assert nm is not None
    assert nm.market_type == "spread"
    assert nm.period_label == "F5"
    assert nm.line == 1.5


def test_ks_f5_moneyline_is_dropped():
    nm = ks.normalize_market(_ks_row("KXMLBF5GAME", "Cincinnati Reds"))
    assert nm is None


# --- F5_ENABLED kill switch (test 8) --------------------------------------

def test_f5_disabled_drops_both_books(monkeypatch):
    monkeypatch.setattr(config, "F5_ENABLED", False)
    assert pm.normalize_market(
        _pm_row("tsc-mlb-cin-phi-2026-05-20-f5-4pt5", "SPORTS_MARKET_TYPE_TOTAL")) is None
    assert ks.normalize_market(_ks_row("KXMLBF5TOTAL", "Over 4.5")) is None
    # full game unaffected
    assert pm.normalize_market(
        _pm_row("tsc-mlb-cin-phi-2026-05-20-6pt5", "SPORTS_MARKET_TYPE_TOTAL")) is not None


# --- Period maps (tests 9-11) ---------------------------------------------

def test_matcher_period_map_has_f5():
    """If a future change regenerates PERIOD_LABEL_TO_INT from PIN_PERIOD_LABEL
    alone, F5 would vanish and F5 markets would hit `unsupported_period`."""
    assert matcher.PERIOD_LABEL_TO_INT["F5"] == 1


def test_paper_period_map_has_f5():
    assert paper_tracker.PIN_PERIOD_LABEL_TO_INT["F5"] == 1


def _pin_total(period, *, line=4.5, over=-110, under=-110, mid=1):
    return {"matchupId": mid, "type": "total", "period": period, "isLive": False,
            "prices": [{"designation": "over", "points": line, "price": over},
                       {"designation": "under", "points": line, "price": under}]}


def _f5_total_record(line=4.5):
    return {"pin_matchup_id": 1, "period_label": "F5", "market_type": "total",
            "yes_designation": "over", "opposite_designation": "under", "line": line}


def test_find_pin_prices_picks_f5_period_not_full():
    """With both a full (period 0) and F5 (period 1) total, an F5 record resolves
    the period-1 price — never falling back to the full-game line (the bug that
    caused the Jun-14 disable)."""
    rows = [_pin_total(0, over=-200, under=160), _pin_total(1, over=-110, under=-105)]
    got = paper_tracker._find_pin_prices(rows, _f5_total_record())
    assert got == (-110, -105)


def test_find_pin_prices_f5_no_fullgame_fallback():
    """If only the full-game (period 0) line exists, an F5 record matches nothing
    rather than mispricing against the full game."""
    assert paper_tracker._find_pin_prices([_pin_total(0)], _f5_total_record()) is None


# --- Real paper-only gate (tests 13-14) -----------------------------------

class _Reached(Exception):
    pass


def _f5_row(period_label="F5"):
    return {"in_window": True, "period_label": period_label,
            "market": SimpleNamespace(book="kalshi", market_id="K1"),
            "market_type": "total", "side": "yes", "fair_prob": 0.55,
            "pin_matchup_id": 1}


def test_real_f5_blocked_when_paper_only(monkeypatch):
    """F5 row returns None before reaching placement logic when F5_PAPER_ONLY."""
    monkeypatch.setattr(config, "F5_PAPER_ONLY", True)
    monkeypatch.setattr(real_tracker, "_key", lambda *a: (_ for _ in ()).throw(_Reached()))
    assert real_tracker.maybe_place(_f5_row(), [(0.50, 100)]) is None


def test_real_f5_proceeds_when_flag_off(monkeypatch):
    """With the gate off, the same F5 row proceeds past the gate into the normal path."""
    monkeypatch.setattr(config, "F5_PAPER_ONLY", False)
    monkeypatch.setattr(real_tracker, "_key", lambda *a: (_ for _ in ()).throw(_Reached()))
    with pytest.raises(_Reached):
        real_tracker.maybe_place(_f5_row(), [(0.50, 100)])


def test_real_nonf5_unaffected_by_gate(monkeypatch):
    """A full-game row is never blocked by the F5 gate."""
    monkeypatch.setattr(config, "F5_PAPER_ONLY", True)
    monkeypatch.setattr(real_tracker, "_key", lambda *a: (_ for _ in ()).throw(_Reached()))
    with pytest.raises(_Reached):
        real_tracker.maybe_place(_f5_row(period_label="FULL"), [(0.50, 100)])


def test_paper_does_not_gate_f5(monkeypatch):
    """The paper path must still place F5 — it reaches its own placement logic."""
    monkeypatch.setattr(paper_tracker, "_key", lambda *a: (_ for _ in ()).throw(_Reached()))
    with pytest.raises(_Reached):
        paper_tracker.maybe_place(_f5_row(), [(0.50, 100)])
