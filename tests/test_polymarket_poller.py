import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import pytest
from unittest.mock import patch, Mock, call
import requests

import polymarket_poller
from data_utils import RateGate

SAMPLE_URL = f"{polymarket_poller.GATEWAY}/v2/leagues/nba/events"


def _ok_response(body=None):
    r = Mock()
    r.status_code = 200
    r.json.return_value = body or {"events": []}
    r.raise_for_status.return_value = None
    return r


def _429_response(retry_after=None):
    r = Mock()
    r.status_code = 429
    r.headers = {"Retry-After": str(retry_after)} if retry_after else {}
    r.raise_for_status.side_effect = requests.HTTPError(response=r)
    return r


def _http_error_response(status):
    r = Mock()
    r.status_code = status
    e = requests.HTTPError(response=r)
    r.raise_for_status.side_effect = e
    return r


# ---------------------------------------------------------------------------
# Group A — get() wrapper slot-claim behaviour
# RateGate uses __slots__, so we replace _gate on the module with a Mock
# rather than patching individual methods on the instance.
# ---------------------------------------------------------------------------

class TestGetSlotClaim:
    def test_a1_slot_claimed_before_request(self):
        """_gate.claim_slot() called once per get() call."""
        mock_gate = Mock()
        with patch.object(polymarket_poller, "_gate", mock_gate):
            with patch("polymarket_poller.requests.get", return_value=_ok_response()):
                polymarket_poller.get(SAMPLE_URL)
        mock_gate.claim_slot.assert_called_once()

    def test_a2_success_returns_parsed_json(self):
        """get() returns the parsed dict from r.json() on 200."""
        body = {"events": [{"id": "x"}]}
        mock_gate = Mock()
        with patch.object(polymarket_poller, "_gate", mock_gate):
            with patch("polymarket_poller.requests.get", return_value=_ok_response(body)):
                result = polymarket_poller.get(SAMPLE_URL)
        assert result == body


# ---------------------------------------------------------------------------
# Group B — 429 retry logic
# ---------------------------------------------------------------------------

class TestRetryLogic:
    def test_b1_429_calls_record_429_with_retry_after(self):
        """Retry-After header is parsed and passed to record_429."""
        responses = iter([_429_response(retry_after=5), _ok_response()])
        mock_gate = Mock()
        with patch.object(polymarket_poller, "_gate", mock_gate):
            with patch("polymarket_poller.requests.get", side_effect=responses):
                polymarket_poller.get(SAMPLE_URL)
        mock_gate.record_429.assert_called_once_with(5.0)

    def test_b2_429_uses_exponential_fallback_without_retry_after(self):
        """When Retry-After is absent, backoff = RATE_LIMIT_BACKOFF_SEC * 2**attempt."""
        expected_backoff = polymarket_poller.RATE_LIMIT_BACKOFF_SEC * (2 ** 0)
        responses = iter([_429_response(retry_after=None), _ok_response()])
        mock_gate = Mock()
        with patch.object(polymarket_poller, "_gate", mock_gate):
            with patch("polymarket_poller.requests.get", side_effect=responses):
                polymarket_poller.get(SAMPLE_URL)
        mock_gate.record_429.assert_called_once_with(expected_backoff)

    def test_b3_retry_succeeds_on_second_attempt(self):
        """429 then 200 → get() returns the JSON; claim_slot called twice."""
        body = {"events": [{"id": "y"}]}
        responses = iter([_429_response(), _ok_response(body)])
        mock_gate = Mock()
        with patch.object(polymarket_poller, "_gate", mock_gate):
            with patch("polymarket_poller.requests.get", side_effect=responses):
                result = polymarket_poller.get(SAMPLE_URL)
        assert result == body
        assert mock_gate.claim_slot.call_count == 2

    def test_b4_all_retries_exhausted_raises(self):
        """After RATE_LIMIT_RETRIES+1 consecutive 429s, HTTPError is raised."""
        n = polymarket_poller.RATE_LIMIT_RETRIES + 1
        responses = iter([_429_response()] * n)
        mock_gate = Mock()
        with patch.object(polymarket_poller, "_gate", mock_gate):
            with patch("polymarket_poller.requests.get", side_effect=responses):
                with pytest.raises(requests.HTTPError):
                    polymarket_poller.get(SAMPLE_URL)


# ---------------------------------------------------------------------------
# Group C — 404 handling in fetch_league_events
# ---------------------------------------------------------------------------

class TestFetchLeagueEvents404:
    def test_c1_404_returns_empty_list(self):
        """HTTP 404 from get() is caught and returns []."""
        r = _http_error_response(404)
        with patch("polymarket_poller.get", side_effect=requests.HTTPError(response=r)):
            result = polymarket_poller.fetch_league_events("nba")
        assert result == []

    def test_c2_non_404_error_propagates(self):
        """Non-404 HTTP errors are not swallowed by fetch_league_events."""
        r = _http_error_response(503)
        with patch("polymarket_poller.get", side_effect=requests.HTTPError(response=r)):
            with pytest.raises(requests.HTTPError):
                polymarket_poller.fetch_league_events("nba")


# ---------------------------------------------------------------------------
# Group D — snapshot_once() 429 counter lifecycle
# ---------------------------------------------------------------------------

def _all_ok_mock():
    """Side-effect function: returns a valid events response for any league URL."""
    return _ok_response({"events": []})


class TestSnapshotOnceCounter:
    def _run_snapshot_once(self, tmp_path, req_side_effect):
        with patch.object(polymarket_poller, "SNAPSHOT_DIR", str(tmp_path)):
            with patch("polymarket_poller.requests.get", side_effect=req_side_effect):
                with patch("polymarket_poller._log"):
                    polymarket_poller.snapshot_once()
        # Read the sidecar written to tmp_path
        sidecars = list(tmp_path.glob("*.meta.json"))
        assert sidecars, "no sidecar written"
        with open(sidecars[0]) as f:
            return json.load(f)

    def test_d1_stale_count_not_included_in_sidecar(self, tmp_path):
        """reset_and_get_429() at cycle start discards counts from between cycles.

        Setup: inject 3 stale counts, then run one cycle with exactly 1 new 429.
        Expected sidecar: rate_limit_429 == 1 (not 4).
        """
        polymarket_poller._gate._count_429 = 3  # stale from previous cycle
        # First league gets 429 then 200; rest get 200
        call_count = [0]
        def side_effect(url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _429_response()
            return _ok_response({"events": []})
        meta = self._run_snapshot_once(tmp_path, side_effect)
        assert meta["rate_limit_429"] == 1

    def test_d2_sidecar_reflects_actual_429_count(self, tmp_path):
        """One 429 in a cycle → sidecar rate_limit_429 == 1 (not hardcoded 0)."""
        call_count = [0]
        def side_effect(url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _429_response()
            return _ok_response({"events": []})
        meta = self._run_snapshot_once(tmp_path, side_effect)
        assert meta["rate_limit_429"] == 1

    def test_d3_clean_cycle_shows_zero(self, tmp_path):
        """All-200 cycle → rate_limit_429 == 0 (reset per cycle, not cumulative)."""
        meta = self._run_snapshot_once(tmp_path, lambda url, **kw: _ok_response({"events": []}))
        assert meta["rate_limit_429"] == 0


# ---------------------------------------------------------------------------
# Group E — _gate module-level instantiation
# ---------------------------------------------------------------------------

class TestGateInstantiation:
    def test_e1_gate_is_rategate_at_import(self):
        """polymarket_poller._gate is a RateGate instance at import time."""
        assert hasattr(polymarket_poller, "_gate")
        assert isinstance(polymarket_poller._gate, RateGate)
