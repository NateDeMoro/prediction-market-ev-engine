import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("PINNACLE_API_KEY", "test-key")

import pytest

from pmev.adapters.common import resolve_team_abbrev


# 3-char Kalshi codes resolve directly to the exact Pinnacle team name.
@pytest.mark.parametrize("abbr,name", [
    ("MIL", "Milwaukee Brewers"),
    ("LAD", "Los Angeles Dodgers"),
    ("HOU", "Houston Astros"),
    ("CWS", "Chicago White Sox"),
    ("ATH", "Athletics"),
    ("STL", "St. Louis Cardinals"),
])
def test_mlb_three_char_codes(abbr, name):
    assert resolve_team_abbrev(abbr, "MLB") == name


# 2-char Kalshi codes resolve directly (the adapter tries 3-char first, then
# falls back to 2; here we pin the 2-char entries themselves).
@pytest.mark.parametrize("abbr,name", [
    ("TB", "Tampa Bay Rays"),
    ("SD", "San Diego Padres"),
    ("SF", "San Francisco Giants"),
    ("KC", "Kansas City Royals"),
    ("AZ", "Arizona Diamondbacks"),
])
def test_mlb_two_char_codes(abbr, name):
    assert resolve_team_abbrev(abbr, "MLB") == name


def test_mlb_three_char_overgrab_misses_so_adapter_falls_back():
    # "TBYDIAZ2" -> the adapter slices [:3]="TBY" (no match) then [:2]="TB".
    # The 3-char over-grab MUST miss so the 2-char fallback fires.
    assert resolve_team_abbrev("TBY", "MLB") is None
    assert resolve_team_abbrev("TB", "MLB") == "Tampa Bay Rays"


def test_unknown_mlb_code_is_none():
    assert resolve_team_abbrev("ZZZ", "MLB") is None
