"""Unit + integration tests for the #7 calibration haircut.

The haircut (config.haircut_fair) shrinks Pinnacle's devigged fair before any EV/Kelly
math, wired once in find_ev_bet.find_matches so every downstream EV site inherits the
adjusted value with no double-application. No network, no snapshot files — find_matches is
driven with a faked match report (match_all_markets monkeypatched).
"""
import os
import sys
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import find_ev_bet
from devig_utils import devig_multiplicative

ZERO_FEE = lambda price, fair: 0.0


# ---------------------------------------------------------------------------
# U7a–c — the helper itself
# ---------------------------------------------------------------------------

def test_haircut_fair_shape_at_half():
    # U7a: at the default K, fair 0.5 is shaved by K*0.25.
    assert config.HAIRCUT_K == pytest.approx(0.064)
    assert config.haircut_fair(0.5) == pytest.approx(0.5 - 0.064 * 0.25)


def test_haircut_fair_zero_k_is_identity(monkeypatch):
    # U7b: K=0 is an exact no-op (the kill-switch).
    monkeypatch.setattr(config, "HAIRCUT_K", 0.0)
    for p in (0.01, 0.5, 0.99):
        assert config.haircut_fair(p) == p


def test_haircut_fair_monotonic_and_bounded():
    # U7c: monotonic increasing, stays in (0,1) across the range.
    ps = [i / 100 for i in range(1, 100)]
    adj = [config.haircut_fair(p) for p in ps]
    assert all(0.0 < a < 1.0 for a in adj)
    assert all(b > a for a, b in zip(adj, adj[1:]))


def test_haircut_fair_none_passthrough():
    assert config.haircut_fair(None) is None


# ---------------------------------------------------------------------------
# find_matches wiring (U7d + IT-7) — faked match report
# ---------------------------------------------------------------------------

def _fake_report(yes_price, opp_price):
    nm = SimpleNamespace(
        book="kalshi", market_id="KX-1", title="Home ML",
        player=None, stat=None, yes_sub_title="Home", team="Home",
    )
    start = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    pair = SimpleNamespace(
        market=nm, pin_matchup_id=1, pin_matchup="Home vs Away",
        pin_home_name="Home", pin_away_name="Away", pin_start=start,
        pin_sport="nba", pin_is_live=False, market_type="moneyline",
        period_label="FULL", line=None, yes_side_label="Home",
        opposite_side_label="Away", yes_side_price=yes_price,
        opposite_side_price=opp_price, delta_seconds=0.0, selection="Home",
        yes_designation="home", opposite_designation="away",
        pin_prop_matchup_id=None,
    )
    return SimpleNamespace(
        pairs=[pair],
        moneyline_report=SimpleNamespace(stats={}),
        stats={"matched_by_type": {}},
    )


def _candidate(monkeypatch, yes_price=-150, opp_price=130):
    monkeypatch.setattr(find_ev_bet, "match_all_markets",
                        lambda pin, soft: _fake_report(yes_price, opp_price))
    monkeypatch.setattr(find_ev_bet, "adapter_for",
                        lambda book: SimpleNamespace(market_url=lambda nm: "http://x"))
    cands, _ = find_ev_bet.find_matches([], [])
    assert len(cands) == 1
    return cands[0]


def test_find_matches_haircuts_both_sides(monkeypatch):
    # U7d: both sides shaved independently → fairs no longer sum to 1, and the
    # raw devig is preserved on *_raw.
    raw_yes, raw_opp = devig_multiplicative([-150, 130])
    c = _candidate(monkeypatch)
    assert c["yes_fair"] == pytest.approx(config.haircut_fair(raw_yes))
    assert c["opposite_fair"] == pytest.approx(config.haircut_fair(raw_opp))
    assert c["fair_prob"] == pytest.approx(c["yes_fair"])
    assert c["yes_fair_raw"] == pytest.approx(raw_yes)
    assert c["opposite_fair_raw"] == pytest.approx(raw_opp)
    assert c["yes_fair"] + c["opposite_fair"] < 1.0  # both shaded down


def test_find_matches_k_zero_is_raw(monkeypatch):
    # IT-7: with the haircut off, candidate fairs equal the raw devig.
    monkeypatch.setattr(config, "HAIRCUT_K", 0.0)
    raw_yes, raw_opp = devig_multiplicative([-150, 130])
    c = _candidate(monkeypatch)
    assert c["yes_fair"] == pytest.approx(raw_yes)
    assert c["opposite_fair"] == pytest.approx(raw_opp)
    assert c["yes_fair_raw"] == pytest.approx(raw_yes)


# ---------------------------------------------------------------------------
# U7e — no second haircut downstream
# ---------------------------------------------------------------------------

def test_evaluate_applies_no_second_haircut():
    # evaluate consumes the (already-adjusted) fair verbatim. fair=0.55, price=0.50,
    # zero fee → ev_pct = 10%. A hidden re-haircut would drop it to ~6.8%.
    result = find_ev_bet.evaluate([(0.50, 100)], 0.55, ZERO_FEE, "kalshi", "total")
    assert result is not None
    assert result["ev_pct"] == pytest.approx(10.0, abs=0.01)
