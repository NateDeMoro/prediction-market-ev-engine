import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("PINNACLE_API_KEY", "test-key")

import pytest

from pmev.adapters.kalshi import normalize_market


def _raw(series, ticker, title, yes_sub):
    event = "-".join(ticker.split("-")[:2])
    return {
        "series_ticker": series,
        "ticker": ticker,
        "event_ticker": event,
        "title": title,
        "yes_sub_title": yes_sub,
        "occurrence_datetime": "2026-06-15T22:10:00Z",
    }


# ---------------------------------------------------------------------------
# MLB props normalize end-to-end: player + threshold from the title, stat from
# the series suffix, team from the per-player ticker chunk, line = N - 0.5.
# ---------------------------------------------------------------------------

def test_mlb_home_runs_normalizes():
    nm = normalize_market(_raw(
        "KXMLBHR",
        "KXMLBHR-26JUN152210TBLAD-TBYDIAZ2-2",
        "Yandy Díaz: 2+ home runs?",
        "Yandy Díaz: 2+",
    ))
    assert nm is not None
    assert nm.market_type == "player_prop"
    assert nm.stat == "home_runs"
    assert nm.player == "Yandy Díaz"
    assert nm.line == 1.5
    assert nm.side == "over"
    assert nm.team == "Tampa Bay Rays"   # 2-char "TB" via 3->2 fallback
    assert nm.league == "MLB"


def test_mlb_total_bases_normalizes():
    nm = normalize_market(_raw(
        "KXMLBTB",
        "KXMLBTB-26JUN152210TBLAD-TBYDIAZ2-3",
        "Yandy Díaz: 3+ total bases?",
        "Yandy Díaz: 3+",
    ))
    assert nm is not None
    assert nm.stat == "total_bases"
    assert nm.line == 2.5
    assert nm.team == "Tampa Bay Rays"


def test_mlb_strikeouts_normalizes_three_char_team():
    nm = normalize_market(_raw(
        "KXMLBKS",
        "KXMLBKS-26JUN161940CLEMIL-MILRGASSER54-7",
        "Robert Gasser: 7+ strikeouts?",
        "Robert Gasser: 7+",
    ))
    assert nm is not None
    assert nm.stat == "strikeouts"
    assert nm.player == "Robert Gasser"
    assert nm.line == 6.5
    assert nm.team == "Milwaukee Brewers"   # 3-char "MIL" direct


# ---------------------------------------------------------------------------
# Regression: relaxing the title regex for MLB must not break the existing
# single-word NBA prop titles.
# ---------------------------------------------------------------------------

def test_nba_points_still_normalizes():
    nm = normalize_market(_raw(
        "KXNBAPTS",
        "KXNBAPTS-26APR22LALBOS-LALLJAMES23-25",
        "LeBron James: 25+ points",
        "LeBron James: 25+",
    ))
    assert nm is not None
    assert nm.stat == "points"
    assert nm.line == 24.5
    assert nm.team == "Los Angeles Lakers"


# ---------------------------------------------------------------------------
# Out of scope: batter-hits has no clean Pinnacle counterpart, so the series
# is not allowlisted and the market is dropped.
# ---------------------------------------------------------------------------

def test_mlb_hits_is_dropped():
    nm = normalize_market(_raw(
        "KXMLBHIT",
        "KXMLBHIT-26JUN152210TBLAD-TBYDIAZ2-2",
        "Yandy Díaz: 2+ hits?",
        "Yandy Díaz: 2+",
    ))
    assert nm is None
