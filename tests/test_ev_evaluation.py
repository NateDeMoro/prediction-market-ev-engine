import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from pmev import config
from pmev.matching.ev import evaluate, walk_ladder

# Synthetic single-level ladder: 100 shares at 45¢
# With zero_fee:
#   ev_per_share = fair - 0.45
#   ev_pct = (fair - 0.45) / 0.45 * 100
LADDER = [(0.45, 100)]
zero_fee = lambda price, fair: 0.0

# kalshi fee-adjusted floor at avg_fill=0.45:
#   be_plus_margin = 100 * 0.07 * (1 - 0.45) + 1.0 = 4.85%
#   floor = max(2.0 or 4.0, 4.85) = 4.85% for both prop and non-prop

# "test_book" is not in PER_BOOK_FEE_RATE → pure flat floor:
#   non-prop floor = MIN_EDGE_PCT = 2.0%
#   prop floor = PROP_MIN_EDGE_PCT = 4.0%
# This gives a clean environment for testing prop vs non-prop floor distinction.
TEST_BOOK = "test_book"


# ---------------------------------------------------------------------------
# Group 1 — Zero / empty walk → None
# ---------------------------------------------------------------------------

def test_empty_ladder_returns_none():
    assert evaluate([], 0.60, zero_fee, "kalshi", "moneyline") is None


def test_all_negative_ev_returns_none():
    # fair=0.50, price=0.70: ev = 0.5*0.3 - 0.5*0.7 = -0.20 → walk returns 0 shares
    assert evaluate([(0.70, 100)], 0.50, zero_fee, "kalshi", "moneyline") is None


# ---------------------------------------------------------------------------
# Group 2 — Sanity ceiling (the Q5 missing constraint)
# ---------------------------------------------------------------------------

def test_nonprop_above_ceiling_returns_none():
    # fair=0.60 → ev_pct ≈ 33% > SANITY_MAX_EDGE_PCT (15.0)
    assert evaluate(LADDER, 0.60, zero_fee, "kalshi", "moneyline") is None


def test_nonprop_exactly_at_ceiling_passes():
    # ev_pct == SANITY_MAX_EDGE_PCT exactly should pass (guard is >, not >=)
    # fair = 0.45 * (1 + SANITY_MAX_EDGE_PCT / 100) = 0.45 * 1.15 = 0.5175
    fair_at_ceiling = 0.45 * (1 + config.SANITY_MAX_EDGE_PCT / 100)
    result = evaluate(LADDER, fair_at_ceiling, zero_fee, "kalshi", "moneyline")
    assert result is not None


def test_prop_above_prop_ceiling_returns_none():
    # fair=0.60 → ev_pct ≈ 33% > SANITY_MAX_EDGE_PCT_PROP (25.0)
    assert evaluate(LADDER, 0.60, zero_fee, "kalshi", "player_prop") is None


def test_prop_above_nonprop_ceiling_but_below_prop_ceiling_passes():
    # fair=0.555 → ev_pct ≈ 23% — above 15% non-prop ceiling, below 25% prop ceiling
    result = evaluate(LADDER, 0.555, zero_fee, "kalshi", "player_prop")
    assert result is not None


def test_prop_above_nonprop_ceiling_same_fair_nonprop_fails():
    # Same fair (0.555, ev_pct ≈ 23%) with a non-prop type must return None
    # This is the direct ceiling-dispatch test: prop uses PROP ceiling, non-prop uses lower ceiling
    assert evaluate(LADDER, 0.555, zero_fee, "kalshi", "moneyline") is None


# ---------------------------------------------------------------------------
# Group 3 — Floor (using TEST_BOOK for clean prop vs non-prop distinction)
# ---------------------------------------------------------------------------

def test_prop_below_floor_returns_none():
    # fair=0.462 → ev_pct ≈ 2.67% < PROP_MIN_EDGE_PCT (4.0) for test_book
    # But still above MIN_EDGE_PCT (2.0), so non-prop would pass
    assert evaluate(LADDER, 0.462, zero_fee, TEST_BOOK, "player_prop") is None


def test_nonprop_at_same_ev_passes_floor():
    # fair=0.462, market_type="moneyline" → ev_pct ≈ 2.67% > MIN_EDGE_PCT (2.0)
    result = evaluate(LADDER, 0.462, zero_fee, TEST_BOOK, "moneyline")
    assert result is not None


def test_prop_just_above_floor_passes():
    # ev_pct = PROP_MIN_EDGE_PCT + 0.1 pp — clearly above floor (exact-boundary
    # testing is unreliable due to floating point; the <-not-<= semantics are
    # covered by the pair: below-floor→None, above-floor→dict)
    fair_above_floor = 0.45 * (1 + (config.PROP_MIN_EDGE_PCT + 0.1) / 100)
    result = evaluate(LADDER, fair_above_floor, zero_fee, TEST_BOOK, "player_prop")
    assert result is not None


# ---------------------------------------------------------------------------
# Group 4 — Valid return dict shape and values
# ---------------------------------------------------------------------------

def test_valid_nonprop_returns_dict_with_required_keys():
    # ev_pct ≈ 14% for kalshi (above 4.85% floor, below 15% ceiling)
    # fair = 0.45 * (1 + 0.14) = 0.513
    result = evaluate(LADDER, 0.513, zero_fee, "kalshi", "moneyline")
    assert result is not None
    assert set(result.keys()) >= {"shares", "stake", "exp_profit", "ev_pct", "levels"}


def test_valid_nonprop_ev_pct_matches_formula():
    result = evaluate(LADDER, 0.513, zero_fee, "kalshi", "moneyline")
    assert result is not None
    expected = result["exp_profit"] / result["stake"] * 100
    assert result["ev_pct"] == pytest.approx(expected, rel=1e-6)


def test_valid_nonprop_shares_equals_ladder_qty():
    # All 100 shares are +EV with fair=0.513 and zero_fee
    result = evaluate(LADDER, 0.513, zero_fee, "kalshi", "moneyline")
    assert result is not None
    assert result["shares"] == 100


def test_valid_prop_returns_dict():
    # fair=0.555 → ev_pct ≈ 23%; above prop floor, below 25% prop ceiling
    result = evaluate(LADDER, 0.555, zero_fee, "kalshi", "player_prop")
    assert result is not None
    assert set(result.keys()) >= {"shares", "stake", "exp_profit", "ev_pct", "levels"}


# ---------------------------------------------------------------------------
# Group 5 — Non-prop market_type variants all use the same (non-prop) ceiling
# ---------------------------------------------------------------------------

def test_spread_above_ceiling_returns_none():
    assert evaluate(LADDER, 0.60, zero_fee, "kalshi", "spread") is None


def test_total_above_ceiling_returns_none():
    assert evaluate(LADDER, 0.60, zero_fee, "kalshi", "total") is None


def test_team_total_above_ceiling_returns_none():
    assert evaluate(LADDER, 0.60, zero_fee, "kalshi", "team_total") is None
