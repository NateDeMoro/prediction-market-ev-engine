import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("PINNACLE_API_KEY", "test-key")

from unittest.mock import patch
import pytest

import pinnacle_poller
from market_matcher import _find_prop_prices
from paper_tracker import _find_pin_prices


# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------

PARENT_ID = 1000
CHILD_ID = 2000
OVER_PID = 101
UNDER_PID = 102

def _parent_matchup():
    return {
        "id": PARENT_ID,
        "type": "matchup",
        "parentId": None,
        "isLive": False,
        "startTime": "2026-06-12T19:00:00Z",
        "participants": [
            {"alignment": "home", "name": "Sacramento Kings"},
            {"alignment": "away", "name": "Oklahoma City Thunder"},
        ],
    }

def _related(league_name="NBA"):
    parent = {
        "id": PARENT_ID,
        "type": "matchup",
        "league": {"name": league_name},
        "participants": [
            {"alignment": "home", "name": "Sacramento Kings"},
            {"alignment": "away", "name": "Oklahoma City Thunder"},
        ],
    }
    child = {
        "id": CHILD_ID,
        "type": "special",
        "units": "Points",
        "special": {
            "category": "Player Props",
            "description": "De'Aaron Fox (Points)",
        },
        "participants": [
            {"id": OVER_PID, "name": "Over", "alignment": "neutral", "order": 0},
            {"id": UNDER_PID, "name": "Under", "alignment": "neutral", "order": 0},
        ],
    }
    return [parent, child]

def _markets(over_price=-115, under_price=102, over_first=True):
    over_leg = {"participantId": OVER_PID, "price": over_price, "points": 4.5}
    under_leg = {"participantId": UNDER_PID, "price": under_price, "points": 4.5}
    prices = [over_leg, under_leg] if over_first else [under_leg, over_leg]
    return [{"matchupId": CHILD_ID, "type": "total", "prices": prices}]

def _prop_row(over_price=-115, under_price=102, over_first=True, labeled=True):
    """Synthetic stored prop row as it would appear in a snapshot."""
    if labeled:
        over_leg = {"participantId": OVER_PID, "designation": "over",
                    "price": over_price, "points": 4.5}
        under_leg = {"participantId": UNDER_PID, "designation": "under",
                     "price": under_price, "points": 4.5}
    else:
        over_leg = {"participantId": OVER_PID, "price": over_price, "points": 4.5}
        under_leg = {"participantId": UNDER_PID, "price": under_price, "points": 4.5}
    prices = [over_leg, under_leg] if over_first else [under_leg, over_leg]
    return {
        "type": "player_prop",
        "matchupId": PARENT_ID,
        "prop_matchupId": CHILD_ID,
        "player": "De'Aaron Fox",
        "stat": "Points",
        "stat_units": "Points",
        "line": 4.5,
        "prices": prices,
    }

def _record(prop_matchup_id=None):
    """Minimal placement record for _find_pin_prices."""
    return {
        "pin_matchup_id": PARENT_ID,
        "market_type": "player_prop",
        "period_label": "FULL",
        "yes_designation": "over",
        "opposite_designation": "under",
        "line": 4.5,
        "prop_matchup_id": prop_matchup_id,
    }


# ---------------------------------------------------------------------------
# Group 1: collect_prop_rows — designation stamped at capture
# ---------------------------------------------------------------------------

class TestCollectPropRowsDesignation:

    def _run(self, related, markets):
        with patch.object(pinnacle_poller, "fetch_related_matchups", return_value=related), \
             patch.object(pinnacle_poller, "fetch_live_matchup_markets", return_value=markets):
            rows, _, _, err = pinnacle_poller.collect_prop_rows(
                _parent_matchup(), "Basketball", {}
            )
        assert err is None
        return rows

    def test_normal_order_over_first_gets_correct_designations(self):
        rows = self._run(_related(), _markets(over_first=True))
        assert len(rows) == 1
        prices = rows[0]["prices"]
        by_pid = {p["participantId"]: p["designation"] for p in prices}
        assert by_pid[OVER_PID] == "over"
        assert by_pid[UNDER_PID] == "under"

    def test_inverted_order_under_first_gets_correct_designations(self):
        # Primary regression test: Under is index 0, Over is index 1 in API response.
        rows = self._run(_related(), _markets(over_first=False))
        assert len(rows) == 1
        prices = rows[0]["prices"]
        by_pid = {p["participantId"]: p["designation"] for p in prices}
        assert by_pid[OVER_PID] == "over",  "Over price must be labeled 'over' regardless of position"
        assert by_pid[UNDER_PID] == "under", "Under price must be labeled 'under' regardless of position"

    def test_unresolvable_participant_id_logs_warning_and_designation_is_none(self):
        markets_bad = [{"matchupId": CHILD_ID, "type": "total",
                        "prices": [{"participantId": 999, "price": -115, "points": 4.5}]}]
        logged = []
        with patch.object(pinnacle_poller, "fetch_related_matchups", return_value=_related()), \
             patch.object(pinnacle_poller, "fetch_live_matchup_markets", return_value=markets_bad), \
             patch.object(pinnacle_poller, "log", side_effect=logged.append):
            rows, _, _, err = pinnacle_poller.collect_prop_rows(
                _parent_matchup(), "Basketball", {}
            )
        assert err is None
        assert len(rows) == 1
        assert rows[0]["prices"][0]["designation"] is None
        assert any("designation" in line.lower() or "unknown" in line.lower()
                   for line in logged), f"Expected warning log, got: {logged}"

    def test_empty_participants_no_crash_designation_is_none(self):
        related_no_parts = _related()
        related_no_parts[1]["participants"] = []
        rows = self._run(related_no_parts, _markets())
        assert len(rows) == 1
        for p in rows[0]["prices"]:
            assert p["designation"] is None

    def test_non_prop_league_returns_empty(self):
        rows = self._run(_related(league_name="WNBA"), _markets())
        assert rows == []


# ---------------------------------------------------------------------------
# Group 2: _find_prop_prices — designation-keyed extraction
# ---------------------------------------------------------------------------

class TestFindPropPrices:

    def test_labeled_normal_order_returns_correct_pair(self):
        row = _prop_row(over_price=-115, under_price=102, over_first=True, labeled=True)
        result = _find_prop_prices([row], 4.5)
        assert result is not None
        _, over_price, under_price = result
        assert over_price == -115
        assert under_price == 102

    def test_labeled_inverted_order_still_returns_correct_pair(self):
        # Primary regression test: Under is at index 0 in the stored row.
        row = _prop_row(over_price=-115, under_price=102, over_first=False, labeled=True)
        result = _find_prop_prices([row], 4.5)
        assert result is not None, "Should find prop prices when designation is labeled"
        _, over_price, under_price = result
        assert over_price == -115, "over_price must be the Over odd (-115), not the Under (+102)"
        assert under_price == 102, "under_price must be the Under odd (+102), not the Over (-115)"

    def test_missing_designation_returns_none(self):
        row = _prop_row(labeled=False)
        result = _find_prop_prices([row], 4.5)
        assert result is None

    def test_one_leg_missing_designation_returns_none(self):
        row = _prop_row(labeled=True)
        # Strip designation from Under leg
        row["prices"][1] = {k: v for k, v in row["prices"][1].items() if k != "designation"}
        result = _find_prop_prices([row], 4.5)
        assert result is None

    def test_line_mismatch_returns_none_despite_correct_designations(self):
        row = _prop_row(labeled=True)  # line=4.5
        result = _find_prop_prices([row], 5.5)
        assert result is None


# ---------------------------------------------------------------------------
# Group 3: _find_pin_prices (paper_tracker) — player_prop branch
# ---------------------------------------------------------------------------

class TestFindPinPricesPlayerProp:

    def test_labeled_normal_order_returns_yes_and_opp(self):
        row = _prop_row(over_price=-115, under_price=102, over_first=True, labeled=True)
        result = _find_pin_prices([row], _record())
        assert result == (-115, 102)

    def test_labeled_inverted_order_returns_correct_yes_and_opp(self):
        # Primary regression test: Under is index 0, Over is index 1.
        row = _prop_row(over_price=-115, under_price=102, over_first=False, labeled=True)
        result = _find_pin_prices([row], _record())
        assert result is not None, "Should resolve prices when designation is present"
        yes_price, opp_price = result
        assert yes_price == -115, "yes_price must be Over (-115), not Under (+102)"
        assert opp_price == 102,  "opp_price must be Under (+102), not Over (-115)"

    def test_missing_designation_returns_none(self):
        row = _prop_row(labeled=False)
        result = _find_pin_prices([row], _record())
        assert result is None

    def test_prop_matchup_id_filter_wrong_child_skipped(self):
        row = _prop_row(labeled=True)  # prop_matchupId = CHILD_ID = 2000
        result = _find_pin_prices([row], _record(prop_matchup_id=9999))
        assert result is None
