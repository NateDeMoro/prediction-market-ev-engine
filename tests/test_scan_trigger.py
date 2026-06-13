"""#1 — the event-trigger predicate + snapshot mtime watcher.

Drives the pure predicate (_should_scan) and the mtime helper directly — no Flask,
no sleeps, no live scan.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import ev_dashboard
from data_utils import latest_snapshot_mtime


# --- U1a: latest_snapshot_mtime --------------------------------------------

def test_mtime_none_when_empty(tmp_path):
    assert latest_snapshot_mtime([str(tmp_path)]) is None


def test_mtime_is_max_across_dirs(tmp_path):
    d1 = tmp_path / "pin"; d1.mkdir()
    d2 = tmp_path / "kalshi"; d2.mkdir()
    old = d1 / "a.jsonl"; old.write_text("{}\n")
    new = d2 / "b.jsonl"; new.write_text("{}\n")
    t0 = time.time()
    os.utime(str(old), (t0 - 100, t0 - 100))
    os.utime(str(new), (t0 - 10, t0 - 10))
    m = latest_snapshot_mtime([str(d1), str(d2)])
    assert m == pytest.approx(t0 - 10, abs=1.0)  # the newer file, across dirs


def test_mtime_ignores_non_jsonl(tmp_path):
    (tmp_path / "x.meta.json").write_text("{}")
    assert latest_snapshot_mtime([str(tmp_path)]) is None


# --- U1b / U1c: the predicate ----------------------------------------------

def test_scan_fires_on_mtime_advance():
    # New snapshot (mtime advanced) → scan, regardless of elapsed.
    assert ev_dashboard._should_scan(mtime=200.0, last_mtime=100.0,
                                     elapsed=1.0, refresh_sec=60.0) is True


def test_scan_skips_when_unchanged_within_refresh():
    # Same mtime, fallback window not elapsed → skip (no hot-spin re-scan).
    assert ev_dashboard._should_scan(mtime=100.0, last_mtime=100.0,
                                     elapsed=1.0, refresh_sec=60.0) is False


def test_scan_fires_once_per_advance():
    # After scanning on an advance the caller sets last_mtime=mtime; the next
    # unchanged tick must not re-scan.
    last = None
    fired = 0
    for mtime in [100.0, 100.0, 100.0]:  # one advance (None→100) then steady
        if ev_dashboard._should_scan(mtime, last, elapsed=1.0, refresh_sec=60.0):
            fired += 1
            last = mtime
    assert fired == 1


def test_fallback_forces_scan_without_change():
    # Dead poller: mtime never changes, but the refresh fallback still fires.
    assert ev_dashboard._should_scan(mtime=100.0, last_mtime=100.0,
                                     elapsed=60.0, refresh_sec=60.0) is True


def test_first_tick_scans_on_infinite_elapsed():
    # last_scan is None → elapsed inf → scan immediately on startup.
    assert ev_dashboard._should_scan(mtime=None, last_mtime=None,
                                     elapsed=float("inf"), refresh_sec=60.0) is True
