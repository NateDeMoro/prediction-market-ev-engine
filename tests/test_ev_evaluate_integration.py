"""Integration test: CLI and dashboard evaluation paths agree post-unification.

Verifies that a candidate with ev_pct above SANITY_MAX_EDGE_PCT is rejected
by both the find_ev_bet inner loop and the ev_dashboard inner loop (via evaluate()).
Before the refactor the CLI path had no ceiling and would have accepted it.
"""
import config
from find_ev_bet import evaluate, walk_ladder

ZERO_FEE = lambda price, fair: 0.0


def _make_high_edge_candidate():
    """Returns a (ladder, fair, fee_fn, book, market_type) tuple whose ev_pct >> 15%."""
    # fair=0.90, price=0.10, zero fee → ev_pct = (0.80/0.10)*100 = 800%
    return [(0.10, 100)], 0.90, ZERO_FEE, "kalshi", "total"


def _make_valid_candidate():
    """Returns a tuple whose ev_pct sits between floor and ceiling."""
    # fair=0.55, price=0.50, zero fee → ev_pct = (0.05/0.50)*100 = 10%
    return [(0.50, 100)], 0.55, ZERO_FEE, "kalshi", "total"


def test_high_edge_rejected_by_evaluate():
    """The canonical regression test: a sanity-ceiling rejection that the old CLI
    path would have missed (no ceiling existed there before unification)."""
    ladder, fair, fee_fn, book, mtype = _make_high_edge_candidate()
    # Confirm ev_pct is actually above the ceiling so this is a meaningful test.
    shares, stake, exp_profit, _ = walk_ladder(ladder, fair, fee_fn)
    ev_pct = exp_profit / stake * 100
    assert ev_pct > config.SANITY_MAX_EDGE_PCT, (
        f"Test setup error: ev_pct={ev_pct:.1f}% must exceed ceiling {config.SANITY_MAX_EDGE_PCT}"
    )
    result = evaluate(ladder, fair, fee_fn, book, mtype)
    assert result is None, f"Expected rejection but got accepted: ev_pct={ev_pct:.1f}%"


def test_valid_candidate_accepted_by_evaluate():
    """Companion test: a genuinely +EV candidate still passes after unification."""
    ladder, fair, fee_fn, book, mtype = _make_valid_candidate()
    result = evaluate(ladder, fair, fee_fn, book, mtype)
    assert result is not None
    assert 0 < result["ev_pct"] < config.SANITY_MAX_EDGE_PCT


