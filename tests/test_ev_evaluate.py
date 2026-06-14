"""Unit tests for find_ev_bet.evaluate().

All tests use synthetic ladders and simple fee lambdas — no snapshot files, no network.
Zero-fee lambda is used for ladder-walk arithmetic; config.min_edge_pct() still pulls
the real fee rate from config.PER_BOOK_FEE_RATE, which is intentional: it lets us test
that evaluate() calls the config formula rather than re-implementing it.
"""
import pytest
from pmev import config
from pmev.matching.ev import evaluate

ZERO_FEE = lambda price, fair: 0.0
BOOK = "kalshi"
NON_PROP = "total"
PROP = "player_prop"


def _ladder(price, qty):
    """Single-level ladder."""
    return [(price, qty)]


def _multi_ladder(*levels):
    """Multi-level ladder as list of (price, qty) pairs."""
    return list(levels)


# ---------------------------------------------------------------------------
# Group 1 — Basic accept / reject
# ---------------------------------------------------------------------------

def test_evaluate_accepts_valid_candidate():
    # fair=0.60, price=0.50, zero fee → ev=0.10, ev_pct=20%... wait that's above ceiling.
    # fair=0.55, price=0.50 → ev=0.05, ev_pct=10% — between 2% floor and 15% ceiling.
    ladder = _ladder(0.50, 100)
    result = evaluate(ladder, 0.55, ZERO_FEE, BOOK, NON_PROP)
    assert result is not None
    assert result["ev_pct"] > 0


def test_evaluate_returns_correct_metrics():
    ladder = _ladder(0.50, 100)
    result = evaluate(ladder, 0.55, ZERO_FEE, BOOK, NON_PROP)
    assert result is not None
    assert "shares" in result
    assert "stake" in result
    assert "exp_profit" in result
    assert "ev_pct" in result
    assert "avg_fill" in result
    assert "levels" in result
    assert abs(result["exp_profit"] / result["stake"] * 100 - result["ev_pct"]) < 0.001
    assert abs(result["stake"] / result["shares"] - result["avg_fill"]) < 1e-9


# ---------------------------------------------------------------------------
# Group 2 — Floor gate (minimum edge)
# ---------------------------------------------------------------------------

def test_evaluate_rejects_below_fee_adjusted_floor():
    # kalshi/total at avg_fill=0.50:
    #   min_edge = max(2.0, 100*0.07*(1-0.50) + 1.0) = max(2.0, 4.5) = 4.5%
    # fair=0.52, price=0.50 → ev=0.02, ev_pct=4.0% < 4.5%  → rejected
    ladder = _ladder(0.50, 100)
    result = evaluate(ladder, 0.52, ZERO_FEE, BOOK, NON_PROP)
    assert result is None


def test_evaluate_accepts_above_fee_adjusted_floor():
    # Same setup but fair bumped so ev_pct lands above 4.5%.
    # fair=0.55, price=0.50 → ev=0.05, ev_pct=10% > 4.5%  → accepted
    ladder = _ladder(0.50, 100)
    result = evaluate(ladder, 0.55, ZERO_FEE, BOOK, NON_PROP)
    assert result is not None


def test_evaluate_rejects_prop_below_prop_floor():
    # PROP_MIN_EDGE_PCT default = 4.0%.
    # fair=0.52, price=0.50, prop → ev_pct=4.0% (not strictly above 4.0 since
    # fee-adjusted floor for prop is max(4.0, 4.5) = 4.5%).
    # Use fair=0.52 → ev_pct=4.0% < 4.5% → rejected.
    ladder = _ladder(0.50, 100)
    result = evaluate(ladder, 0.52, ZERO_FEE, BOOK, PROP)
    assert result is None


def test_evaluate_accepts_prop_above_prop_floor():
    # fair=0.60, price=0.50 → ev=0.10, ev_pct=20% > prop ceiling of 25%? No — 20% < 25%.
    # min_edge for prop at 0.50 fill = max(4.0, 4.5) = 4.5% → 20% > 4.5% → accepted.
    ladder = _ladder(0.50, 100)
    result = evaluate(ladder, 0.60, ZERO_FEE, BOOK, PROP)
    assert result is not None


def test_evaluate_prop_floor_higher_than_non_prop():
    # At a lower price where fee-adjusted formula doesn't dominate:
    # book="polymarket", price=0.80 (ask), avg_fill=0.80
    #   non-prop floor: max(2.0, 100*0.03*0.20 + 1.0) = max(2.0, 1.6) = 2.0%
    #   prop floor:     max(4.0, 1.6) = 4.0%
    # fair=0.83, price=0.80 → ev=0.83*0.20 - 0.17*0.80 = 0.166-0.136 = 0.030
    # ev_pct = 0.030/0.80*100 = 3.75% → above non-prop floor (2%), below prop floor (4%)
    ladder = _ladder(0.80, 100)
    fair = 0.83
    fee_fn = ZERO_FEE
    assert evaluate(ladder, fair, fee_fn, "polymarket", NON_PROP) is not None
    assert evaluate(ladder, fair, fee_fn, "polymarket", PROP) is None


# ---------------------------------------------------------------------------
# Group 3 — Ceiling gate (sanity max edge)
# ---------------------------------------------------------------------------

def test_evaluate_rejects_above_sanity_ceiling_non_prop():
    # fair=0.90, price=0.10, zero fee → ev=0.80, ev_pct=800% >> 15% ceiling → rejected.
    ladder = _ladder(0.10, 100)
    result = evaluate(ladder, 0.90, ZERO_FEE, BOOK, NON_PROP)
    assert result is None


def test_evaluate_rejects_above_sanity_ceiling_prop():
    # fair=0.90, price=0.20, zero fee → ev=0.90*0.80-0.10*0.20=0.72-0.02=0.70
    # ev_pct=0.70/0.20*100=350% >> 25% prop ceiling → rejected.
    ladder = _ladder(0.20, 100)
    result = evaluate(ladder, 0.90, ZERO_FEE, BOOK, PROP)
    assert result is None


def test_evaluate_accepts_just_below_ceiling():
    # Need ev_pct just below 15% (SANITY_MAX_EDGE_PCT default).
    # price=0.50, zero fee: ev_pct = (fair-0.5)/0.5*100 → set to 14%:
    # (fair-0.5)/0.5 = 0.14 → fair=0.57, ev_pct=14% < 15% → accepted (floor ~4.5%).
    ladder = _ladder(0.50, 100)
    result = evaluate(ladder, 0.57, ZERO_FEE, BOOK, NON_PROP)
    assert result is not None
    assert result["ev_pct"] < config.SANITY_MAX_EDGE_PCT


def test_evaluate_prop_ceiling_higher_than_non_prop():
    # ev_pct=20%: above non-prop ceiling (15%), below prop ceiling (25%).
    # price=0.50, zero fee: fair=0.60 → ev=0.10, ev_pct=20%.
    ladder = _ladder(0.50, 100)
    fair = 0.60
    fee_fn = ZERO_FEE
    assert evaluate(ladder, fair, fee_fn, BOOK, NON_PROP) is None   # above 15% ceiling
    assert evaluate(ladder, fair, fee_fn, BOOK, PROP) is not None   # below 25% ceiling


# ---------------------------------------------------------------------------
# Group 4 — Empty / zero-shares paths
# ---------------------------------------------------------------------------

def test_evaluate_returns_none_for_empty_ladder():
    result = evaluate([], 0.60, ZERO_FEE, BOOK, NON_PROP)
    assert result is None


def test_evaluate_returns_none_when_no_positive_levels():
    # fair < price → every level has ev < 0 → walk_ladder returns shares=0.
    ladder = _ladder(0.70, 100)
    result = evaluate(ladder, 0.20, ZERO_FEE, BOOK, NON_PROP)
    assert result is None


# ---------------------------------------------------------------------------
# Group 5 — avg_fill derivation
# ---------------------------------------------------------------------------

def test_evaluate_avg_fill_equals_stake_over_shares():
    # Two levels consumed: (0.45, 50) and (0.50, 50), both +EV at fair=0.52.
    # ev_pct ≈ 9.5% — above fee-adjusted floor (~4.7%) and below ceiling (15%).
    ladder = _multi_ladder((0.45, 50), (0.50, 50))
    result = evaluate(ladder, 0.52, ZERO_FEE, BOOK, NON_PROP)
    assert result is not None
    expected_avg_fill = result["stake"] / result["shares"]
    assert abs(result["avg_fill"] - expected_avg_fill) < 1e-9


def test_evaluate_avg_fill_used_for_floor_not_best_ask():
    # Ladder with two levels. If floor were computed at best_ask (0.30) instead of
    # avg_fill (~0.50 after consuming both levels), the floor would differ.
    # With kalshi/total:
    #   at 0.30: max(2.0, 100*0.07*0.70+1.0) = max(2.0, 5.9) = 5.9%
    #   at 0.50: max(2.0, 100*0.07*0.50+1.0) = max(2.0, 4.5) = 4.5%
    # Choose ev_pct between 4.5% and 5.9% — accepted only if avg_fill is used:
    # Ladder: (0.30, 50), (0.70, 50). fair such that combined ev_pct ≈ 5%.
    # At 0.30, fair=0.60: ev=0.60*0.70-0.40*0.30=0.42-0.12=0.30 → +EV.
    # At 0.70, fair=0.60: ev=0.60*0.30-0.40*0.70=0.18-0.28=-0.10 → negative, stops here.
    # So only first level consumed: shares=50, stake=15, exp_profit=15, ev_pct=100%.
    # That's above ceiling. Use a case where both levels are consumed:
    # Ladder: (0.50, 50), (0.52, 50). fair=0.56.
    # Level 1: ev=0.56*0.50-0.44*0.50=0.06 > 0 → consumed.
    # Level 2: ev=0.56*0.48-0.44*0.52=0.2688-0.2288=0.04 > 0 → consumed.
    # shares=100, stake=0.50*50+0.52*50=25+26=51, exp_profit=0.06*50+0.04*50=5.0
    # ev_pct=5.0/51*100≈9.8%, avg_fill=51/100=0.51
    # floor at avg_fill 0.51: max(2.0, 100*0.07*0.49+1.0) = max(2.0, 4.43) = 4.43%
    # 9.8% > 4.43% → accepted.
    ladder = _multi_ladder((0.50, 50), (0.52, 50))
    result = evaluate(ladder, 0.56, ZERO_FEE, BOOK, NON_PROP)
    assert result is not None
    assert abs(result["avg_fill"] - result["stake"] / result["shares"]) < 1e-9


# ---------------------------------------------------------------------------
# Group 6 — Ceiling logging (#11)
# Over-ceiling rejections must stop being silent; near-ceiling passes log a
# distinct line; ordinary rejections (floor/empty/zero-share) log nothing.
# ---------------------------------------------------------------------------

def test_over_ceiling_reject_logs(capsys):
    # fair=0.90, price=0.10, zero fee → ev_pct=800% >> 15% ceiling → rejected + logged.
    result = evaluate(_ladder(0.10, 100), 0.90, ZERO_FEE, BOOK, NON_PROP)
    assert result is None
    out = capsys.readouterr().out
    assert "over-ceiling" in out
    assert BOOK in out
    assert NON_PROP in out


def test_over_ceiling_log_uses_prop_ceiling_for_props(capsys):
    # A prop between the non-prop ceiling (15%) and prop ceiling (25%) is accepted,
    # with no over-ceiling line. fair=0.59, price=0.50 → ev_pct=18%.
    accepted = evaluate(_ladder(0.50, 100), 0.59, ZERO_FEE, BOOK, PROP)
    assert accepted is not None
    assert "over-ceiling" not in capsys.readouterr().out
    # A prop above 25% is rejected and the log names the prop ceiling (25).
    rejected = evaluate(_ladder(0.50, 100), 0.70, ZERO_FEE, BOOK, PROP)
    assert rejected is None
    out = capsys.readouterr().out
    assert "over-ceiling" in out
    assert "25" in out


def test_near_ceiling_pass_logs_distinctly(capsys):
    # ev_pct=14% (fair=0.57, price=0.50): accepted, inside the 0.8x band [12%,15%).
    accepted = evaluate(_ladder(0.50, 100), 0.57, ZERO_FEE, BOOK, NON_PROP)
    assert accepted is not None
    out = capsys.readouterr().out
    assert "near-ceiling" in out
    assert "over-ceiling" not in out


def test_far_from_ceiling_pass_logs_nothing(capsys):
    # ev_pct=10% (fair=0.55, price=0.50): accepted, well below the band → no ceiling log.
    accepted = evaluate(_ladder(0.50, 100), 0.55, ZERO_FEE, BOOK, NON_PROP)
    assert accepted is not None
    assert "ceiling" not in capsys.readouterr().out


def test_ordinary_rejects_log_no_ceiling(capsys):
    # Empty, no-positive-levels, and below-floor rejects must not emit a ceiling line.
    assert evaluate([], 0.60, ZERO_FEE, BOOK, NON_PROP) is None
    assert evaluate(_ladder(0.70, 100), 0.20, ZERO_FEE, BOOK, NON_PROP) is None
    assert evaluate(_ladder(0.50, 100), 0.52, ZERO_FEE, BOOK, NON_PROP) is None  # 4% < 4.5% floor
    assert "ceiling" not in capsys.readouterr().out
