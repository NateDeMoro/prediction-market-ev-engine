import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("PINNACLE_API_KEY", "test-key")

import pytest

from pmev.adapters.common import canonical_stat


# ---------------------------------------------------------------------------
# MLB prop stats: the three with a true Pinnacle counterpart. Each canonical
# key must be reachable from BOTH the Kalshi series suffix and the Pinnacle
# units string, so the matcher's join lands.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    # Kalshi series suffixes
    ("TB", "total_bases"),
    ("HR", "home_runs"),
    ("KS", "strikeouts"),
    # Pinnacle units strings
    ("TotalBases", "total_bases"),
    ("HomeRuns", "home_runs"),
    ("Strikeouts", "strikeouts"),
])
def test_mlb_stats_map_to_canonical(raw, expected):
    assert canonical_stat(raw) == expected


# ---------------------------------------------------------------------------
# The trap: Kalshi HIT is *batter hits*; Pinnacle HitsAllowed is a *pitcher*
# stat. They are NOT the same market and must never share a canonical key.
# Both stay unmapped so the prop is dropped rather than mismatched.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["HIT", "HitsAllowed", "EarnedRuns", "PitchingOuts"])
def test_unsupported_mlb_stats_stay_unmapped(raw):
    assert canonical_stat(raw) is None
