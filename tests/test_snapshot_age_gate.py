import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from data_utils import stale_snapshot_reason


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

import config


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
    import find_ev_bet
    import ev_dashboard
    assert find_ev_bet.MAX_PIN_SNAPSHOT_AGE_SEC == config.MAX_PIN_SNAPSHOT_AGE_SEC
    assert find_ev_bet.MAX_SOFT_SNAPSHOT_AGE_SEC == config.MAX_SOFT_SNAPSHOT_AGE_SEC
    assert ev_dashboard.MAX_PIN_SNAPSHOT_AGE_SEC == config.MAX_PIN_SNAPSHOT_AGE_SEC
    assert ev_dashboard.MAX_SOFT_SNAPSHOT_AGE_SEC == config.MAX_SOFT_SNAPSHOT_AGE_SEC


# ---------------------------------------------------------------------------
# Integration — the gate behaves correctly inside scan_once.
# Monkeypatch the snapshot loaders + find_matches so no live adapter is hit.
# ---------------------------------------------------------------------------

def _patch_scan(monkeypatch, pin_age, book_ages):
    import ev_dashboard
    monkeypatch.setattr(ev_dashboard, "load_latest_snapshot",
                        lambda _dir: ([{}], pin_age))
    monkeypatch.setattr(ev_dashboard, "_load_soft_markets",
                        lambda: (["soft"], book_ages))
    monkeypatch.setattr(ev_dashboard, "find_matches",
                        lambda *_a, **_k: ([], {}))
    return ev_dashboard


def test_scan_once_raises_on_stale_pinnacle(monkeypatch):
    ev_dashboard = _patch_scan(monkeypatch, pin_age=90, book_ages={"kalshi": 30})
    with pytest.raises(RuntimeError) as exc:
        ev_dashboard.scan_once()
    assert "pinnacle" in str(exc.value).lower()


def test_scan_once_tolerates_loose_soft(monkeypatch):
    # 100s soft snapshot: over the 45s pin gate but under the 120s soft gate.
    # scan_once must proceed past the freshness gate without raising.
    ev_dashboard = _patch_scan(monkeypatch, pin_age=20, book_ages={"kalshi": 100})
    result = ev_dashboard.scan_once()
    assert result["pin_age"] == 20
