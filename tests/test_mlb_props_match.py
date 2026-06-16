"""End-to-end: a Kalshi MLB player prop joins its Pinnacle counterpart through
the real matcher. Props resolve their game via the event's moneyline sibling,
so each case wires a matched ML pair plus the prop on both books and asserts the
cross-book join (canonical stat agreement, player fuzzy-match, line, over/under
designation) lands a player_prop pair.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("PINNACLE_API_KEY", "test-key")

from pmev.matching.matcher import match_all_markets
from pmev.adapters.common import NormalizedMarket
from pmev.adapters.kalshi import normalize_market

START = "2026-06-15T22:10:00Z"
PIN_MATCHUP_ID = 7777
PIN_PROP_ID = 8888


def _pin_ml_row():
    return {
        "type": "moneyline", "period": 0, "matchupId": PIN_MATCHUP_ID,
        "matchup": "Tampa Bay Rays vs Los Angeles Dodgers",
        "sport": "Baseball", "isLive": False, "startTime": START,
        "prices": [
            {"designation": "home", "price": -120},
            {"designation": "away", "price": 105},
        ],
    }


def _pin_prop_row(stat_units="HomeRuns", player="Yandy Díaz", line=1.5):
    return {
        "type": "player_prop", "matchupId": PIN_MATCHUP_ID,
        "prop_matchupId": PIN_PROP_ID,
        "player": player, "stat": "Home Runs", "stat_units": stat_units,
        "line": line,
        "prices": [
            {"participantId": 1, "designation": "over", "price": 120, "points": line},
            {"participantId": 2, "designation": "under", "price": -150, "points": line},
        ],
    }


def _kalshi_prop():
    # Real normalization path: KXMLBHR "Yandy Díaz: 2+" -> over 1.5 home runs.
    return normalize_market({
        "series_ticker": "KXMLBHR",
        "ticker": "KXMLBHR-26JUN152210TBLAD-TBYDIAZ2-2",
        "event_ticker": "KXMLBHR-26JUN152210TBLAD",
        "title": "Yandy Díaz: 2+ home runs?",
        "yes_sub_title": "Yandy Díaz: 2+",
        "occurrence_datetime": START,
    })


def _kalshi_ml(event_id):
    # Built directly; only needs to match the Pinnacle game so the prop's event
    # resolves. Shares the prop's event_id so event_group_key lines up.
    title = "Tampa Bay Rays vs Los Angeles Dodgers Winner?"
    return NormalizedMarket(
        book="kalshi", market_id="KXMLBGAME-26JUN152210TBLAD-TB",
        event_id=event_id, title=title, yes_sub_title="Tampa Bay Rays",
        market_type="moneyline", period_label="FULL", line=None,
        team="Tampa Bay Rays", side=None, start_time=START,
        raw={"title": title}, league="MLB",
    )


def _prop_pairs(report):
    return [p for p in report.pairs if p.market_type == "player_prop"]


def test_kalshi_mlb_prop_matches_pinnacle_end_to_end():
    prop = _kalshi_prop()
    assert prop is not None and prop.stat == "home_runs"
    ml = _kalshi_ml(prop.event_id)

    report = match_all_markets([_pin_ml_row(), _pin_prop_row()], [ml, prop])

    props = _prop_pairs(report)
    assert len(props) == 1, report.stats["prop_unmatched_reasons"]
    pair = props[0]
    # Kalshi YES = Over reaches Pinnacle's over/under prices.
    assert pair.yes_side_price == 120
    assert pair.opposite_side_price == -150
    assert pair.yes_designation == "over"
    assert pair.opposite_designation == "under"
    assert pair.line == 1.5
    assert pair.pin_prop_matchup_id == PIN_PROP_ID
    assert pair.selection == "Yandy Díaz Over 1.5 home_runs"
    assert report.stats["matched_by_type"]["player_prop"] == 1


def test_prop_unmatched_when_pinnacle_stat_has_no_counterpart():
    # Negative control / the HIT trap at the join: Pinnacle exposes only a
    # HitsAllowed (pitcher) prop, which canonicalizes to None, so a Kalshi
    # home-runs prop must NOT join it.
    prop = _kalshi_prop()
    ml = _kalshi_ml(prop.event_id)

    report = match_all_markets(
        [_pin_ml_row(), _pin_prop_row(stat_units="HitsAllowed")], [ml, prop]
    )

    assert _prop_pairs(report) == []
    assert report.stats["prop_unmatched_reasons"]["no_player_match"] == 1
