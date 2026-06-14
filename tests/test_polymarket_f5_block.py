import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pmev.adapters import polymarket as pm


def _total_row(slug, line=6.5):
    """A raw Polymarket snapshot row (one marketSide) for an Over total, shaped
    like pollers/polymarket.flatten_event output. `slug` is the only thing that
    distinguishes a full-game total from its F5 sibling."""
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
            "question": "Total runs over/under",
            "sports_market_type_v2": "SPORTS_MARKET_TYPE_TOTAL",
            "line": line,
            "active": True,
            "closed": False,
            "archived": False,
        },
        "side": {"long": True, "description": "Over", "team_name": None},
    }


def test_f5_total_is_dropped():
    """An F5 total slug (`...-f5-...`) normalizes to None — it must not reach the
    matcher, where it would pair against the full-game Pinnacle line."""
    row = _total_row("tsc-mlb-cin-phi-2026-05-20-f5-6pt5")
    assert pm.normalize_market(row) is None


def test_full_game_total_still_normalizes():
    """Control: the full-game sibling (no `f5` token) still normalizes to a total,
    proving the block is F5-specific and doesn't break full-game totals."""
    row = _total_row("tsc-mlb-cin-phi-2026-05-20-6pt5")
    nm = pm.normalize_market(row)
    assert nm is not None
    assert nm.market_type == "total"
    assert nm.period_label == "FULL"
    assert nm.line == 6.5


def test_f5_marker_does_not_false_positive_on_substring():
    """The `f5` token is hyphen-delimited, so an embedded 'f5' inside another
    token (hypothetical) does not trip the block — only a standalone f5 segment."""
    # 'xf5y' is not a standalone f5 token; a full-game total with such a slug
    # should still normalize.
    row = _total_row("tsc-mlb-cin-phi-2026-05-20-xf5y-6pt5")
    assert pm.normalize_market(row) is not None
