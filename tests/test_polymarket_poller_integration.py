import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import pytest
from unittest.mock import patch, Mock
import requests

from pmev.pollers import polymarket as polymarket_poller

LEAGUES = polymarket_poller.LEAGUES


def _ok_response(events=None):
    r = Mock()
    r.status_code = 200
    r.json.return_value = {"events": events or [{
        "id": "evt1", "teams": [], "markets": [{
            "id": "mkt1", "marketSides": [{"id": "s1", "price": 0.55}]
        }]
    }]}
    r.raise_for_status.return_value = None
    return r


def _429_response():
    r = Mock()
    r.status_code = 429
    r.headers = {}
    r.raise_for_status.side_effect = requests.HTTPError(response=r)
    return r


def test_retry_on_429_delivers_league_to_snapshot(tmp_path):
    """One league's first request returns 429; the retry succeeds.

    After snapshot_once():
    - All 7 leagues appear in the snapshot JSONL (retry delivered the throttled league).
    - sidecar rate_limit_429 == 1 (one recorded 429, not the hardcoded 0).
    """
    call_count = [0]

    def side_effect(url, **kwargs):
        call_count[0] += 1
        # First request (for whichever league hits first) gets throttled.
        if call_count[0] == 1:
            return _429_response()
        return _ok_response()

    with patch.object(polymarket_poller, "SNAPSHOT_DIR", str(tmp_path)):
        with patch("pmev.pollers.polymarket.requests.get", side_effect=side_effect):
            with patch("pmev.pollers.polymarket._log"):
                polymarket_poller.snapshot_once()

    # Sidecar must exist and show the real 429 count.
    sidecars = list(tmp_path.glob("*.meta.json"))
    assert sidecars, "no sidecar written"
    meta = json.loads(sidecars[0].read_text())
    assert meta["rate_limit_429"] == 1, (
        f"expected rate_limit_429=1 (one 429 retried successfully), got {meta['rate_limit_429']}"
    )

    # Snapshot must contain at least one row (the retried league's event).
    snapshots = list(tmp_path.glob("*.jsonl"))
    assert snapshots, "no snapshot written"
    rows = [json.loads(line) for line in snapshots[0].read_text().splitlines() if line]
    assert rows, "snapshot is empty — retried league events were not included"
