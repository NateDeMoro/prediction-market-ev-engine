import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from pmev.core.io import stale_snapshot_reason


# ---------------------------------------------------------------------------
# Helper behavior — stale_snapshot_reason
# Two-tier gate: pin_age gated by pin_max (tight), each soft age by soft_max
# (loose). Pinnacle checked first. A missing (None) soft book is fatal only
# when missing_soft_is_stale.
# ---------------------------------------------------------------------------

PIN_MAX, SOFT_MAX = 45, 120


def test_all_fresh_returns_none():
    assert stale_snapshot_reason(20, {"kalshi": 30, "polymarket": 70},
                                 PIN_MAX, SOFT_MAX) is None


def test_stale_pinnacle_returns_pin_reason():
    reason = stale_snapshot_reason(90, {"kalshi": 30}, PIN_MAX, SOFT_MAX)
    assert reason is not None
    assert "pinnacle" in reason.lower()
    assert "45" in reason


def test_soft_between_pin_and_soft_cap_is_fresh():
    # 80s soft snapshot: over the 45s pin cap but under the 120s soft cap.
    # Must NOT be flagged — soft books use the loose gate. This is the
    # brick-the-scanner regression guard.
    assert stale_snapshot_reason(20, {"kalshi": 80}, PIN_MAX, SOFT_MAX) is None


def test_stale_soft_returns_that_book_reason():
    reason = stale_snapshot_reason(20, {"kalshi": 130}, PIN_MAX, SOFT_MAX)
    assert reason is not None
    assert "kalshi" in reason.lower()
    assert "120" in reason


def test_missing_soft_lenient_returns_none():
    assert stale_snapshot_reason(20, {"polymarket": None}, PIN_MAX, SOFT_MAX,
                                 missing_soft_is_stale=False) is None


def test_missing_soft_strict_returns_reason():
    reason = stale_snapshot_reason(20, {"polymarket": None}, PIN_MAX, SOFT_MAX,
                                   missing_soft_is_stale=True)
    assert reason is not None
    assert "polymarket" in reason.lower()


def test_mixed_soft_one_stale_returns_stale_one():
    reason = stale_snapshot_reason(20, {"kalshi": 30, "polymarket": 200},
                                   PIN_MAX, SOFT_MAX)
    assert reason is not None
    assert "polymarket" in reason.lower()


def test_pin_and_soft_both_stale_pin_wins():
    reason = stale_snapshot_reason(90, {"kalshi": 200}, PIN_MAX, SOFT_MAX)
    assert "pinnacle" in reason.lower()


# ---------------------------------------------------------------------------
# Coordination invariants — constants must stay mutually consistent.
# ---------------------------------------------------------------------------

from pmev import config


def test_pin_cap_tighter_than_soft_cap():
    assert config.MAX_PIN_SNAPSHOT_AGE_SEC < config.MAX_SOFT_SNAPSHOT_AGE_SEC


def test_pin_gate_not_tighter_than_pin_interval():
    # Gate must be >= the poll interval or a healthy snapshot trips it.
    assert config.PINNACLE_POLL_INTERVAL_SEC <= config.MAX_PIN_SNAPSHOT_AGE_SEC


def test_pinnacle_polls_at_least_as_often_as_soft():
    assert config.PINNACLE_POLL_INTERVAL_SEC <= config.POLLER_INTERVAL_SEC


# ---------------------------------------------------------------------------
# Re-export wiring — the constants resolve through find_ev_bet / ev_dashboard.
# ---------------------------------------------------------------------------

def test_constants_resolve_through_reexport_chain():
    from pmev.matching import ev as find_ev_bet
    from pmev import dashboard as ev_dashboard
    assert find_ev_bet.MAX_PIN_SNAPSHOT_AGE_SEC == config.MAX_PIN_SNAPSHOT_AGE_SEC
    assert find_ev_bet.MAX_SOFT_SNAPSHOT_AGE_SEC == config.MAX_SOFT_SNAPSHOT_AGE_SEC
    assert ev_dashboard.MAX_PIN_SNAPSHOT_AGE_SEC == config.MAX_PIN_SNAPSHOT_AGE_SEC
    assert ev_dashboard.MAX_SOFT_SNAPSHOT_AGE_SEC == config.MAX_SOFT_SNAPSHOT_AGE_SEC


# ---------------------------------------------------------------------------
# Integration — the gate behaves correctly inside scan_once.
# Monkeypatch the snapshot loaders + find_matches so no live adapter is hit.
# ---------------------------------------------------------------------------

def _patch_scan(monkeypatch, pin_age, book_ages):
    # The scan pipeline lives in engine.scan (#2); patch its loaders + matcher
    # so the freshness gate is exercised with no live adapter.
    from pmev import engine
    monkeypatch.setattr(engine, "load_latest_snapshot",
                        lambda _dir: ([{}], pin_age))
    monkeypatch.setattr(engine, "_load_soft_markets",
                        lambda: (["soft"], book_ages))
    monkeypatch.setattr(engine, "find_matches",
                        lambda *_a, **_k: ([], {}))
    return engine


def test_scan_once_raises_on_stale_pinnacle(monkeypatch):
    engine = _patch_scan(monkeypatch, pin_age=90, book_ages={"kalshi": 30})
    with pytest.raises(RuntimeError) as exc:
        engine.scan()
    assert "pinnacle" in str(exc.value).lower()


def test_scan_once_tolerates_loose_soft(monkeypatch):
    # 100s soft snapshot: over the 45s pin gate but under the 120s soft gate.
    # scan must proceed past the freshness gate without raising.
    engine = _patch_scan(monkeypatch, pin_age=20, book_ages={"kalshi": 100})
    result = engine.scan()
    assert result["pin_age"] == 20


# ---------------------------------------------------------------------------
# Capture timestamps + cross-book skew (#5)
# ---------------------------------------------------------------------------

def test_age_from_captured_at_beats_mtime(tmp_path):
    # Sidecar captured_at is 20s old; file mtime is back-dated 300s.
    # snapshot_age_seconds must trust captured_at over mtime.
    import json, os, time
    from datetime import datetime, timezone, timedelta
    from pmev.core.io import snapshot_age_seconds

    snap = tmp_path / "20260612T120000Z.jsonl"
    snap.write_text("{}\n")
    captured = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    (tmp_path / "20260612T120000Z.meta.json").write_text(json.dumps({"captured_at": captured}))
    old = time.time() - 300
    os.utime(snap, (old, old))

    age = snapshot_age_seconds(str(snap))
    assert 18 <= age <= 25   # ~20s from captured_at, not ~300 from mtime


def test_age_falls_back_to_mtime_without_captured_at(tmp_path):
    import os, time
    from pmev.core.io import snapshot_age_seconds

    snap = tmp_path / "20260612T120000Z.jsonl"
    snap.write_text("{}\n")
    # No sidecar at all → mtime fallback.
    old = time.time() - 300
    os.utime(snap, (old, old))

    age = snapshot_age_seconds(str(snap))
    assert age >= 290


def test_scan_once_surfaces_cross_book_skew(monkeypatch):
    # pin 20s old, kalshi 45s old → capture skew = |45-20| = 25s, surfaced not gated.
    engine = _patch_scan(monkeypatch, pin_age=20, book_ages={"kalshi": 45})
    result = engine.scan()
    assert abs(result["cross_book_skew_sec"] - 25) < 1e-9


def test_cross_book_skew_none_when_no_soft_age(monkeypatch):
    # No soft ages at all → no skew computable, surfaced as None (not a crash).
    engine = _patch_scan(monkeypatch, pin_age=20, book_ages={})
    result = engine.scan()
    assert result["cross_book_skew_sec"] is None
