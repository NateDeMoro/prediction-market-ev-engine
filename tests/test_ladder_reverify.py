"""Post-fill ladder re-verification (paper tracker).

A paper fill is simulated against the ladder read at decision time; nothing proves
the offer was still standing a moment later. These tests drive the synchronous
re-verify unit — never the thread — matching the close-capture convention in
test_close_capture.py.
"""
import json
import types

import pytest

from pmev import config
from pmev.execution import paper as pt


@pytest.fixture(autouse=True)
def clean_reverify_state(monkeypatch):
    """Isolate the module-level queue and aggregate from other tests."""
    monkeypatch.setattr(pt, "_pending_reverify", pt._new_pending_queue())
    monkeypatch.setattr(pt, "_reverify_agg", pt._new_reverify_agg())


@pytest.fixture
def paper_state(tmp_path, monkeypatch):
    """Empty paper-tracker state with an isolated trades sidecar."""
    monkeypatch.setattr(config, "PAPER_TRADES_PATH", str(tmp_path / "paper_trades.jsonl"))
    monkeypatch.setattr(pt, "_bankroll", 1000.0)
    monkeypatch.setattr(pt, "_placed_keys", set())
    monkeypatch.setattr(pt, "_open_positions", {})
    monkeypatch.setattr(pt, "_placements", [])


def read_reverify(path):
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def entry(levels=None, side="yes", book="kalshi", market_id="K1",
          enqueued_at=1000.0, due_at=1002.0):
    return {
        "placed_at": "2026-08-10T12:00:00+00:00",
        "book": book,
        "market_id": market_id,
        "side": side,
        "levels": levels if levels is not None else [[0.45, 100]],
        "enqueued_at": enqueued_at,
        "due_at": due_at,
    }


def stub_adapter(yes_ladder, no_ladder=None, raises=None):
    def fetch_both_ladders(market_id):
        if raises is not None:
            raise raises
        return yes_ladder, no_ladder
    return types.SimpleNamespace(fetch_both_ladders=fetch_both_ladders)


# ---------------------------------------------------------------------------
# Survival helper (pure) — checks 1-7
# ---------------------------------------------------------------------------

def test_identical_ladder_returns_full_survival():
    out = pt._survival([[0.45, 100]], [(0.45, 100)])
    assert out["claimed_shares"] == 100
    assert out["survived_shares"] == 100


def test_absent_level_returns_zero():
    out = pt._survival([[0.45, 100]], [(0.60, 100)])
    assert out["claimed_shares"] == 100
    assert out["survived_shares"] == 0


def test_reduced_quantity_returns_partial_count():
    out = pt._survival([[0.45, 100]], [(0.45, 30)])
    assert out["survived_shares"] == 30


def test_price_moved_against_us_does_not_survive():
    # We claimed to buy at 0.45; the best ask is now 0.46 — worse for a buyer.
    out = pt._survival([[0.45, 100]], [(0.46, 500)])
    assert out["survived_shares"] == 0


def test_price_improvement_counts_as_full_survival():
    # Cheaper than we claimed is strictly better; improvement is not penalised.
    out = pt._survival([[0.45, 100]], [(0.40, 100)])
    assert out["survived_shares"] == 100


def test_empty_or_missing_observed_ladder_returns_zero():
    assert pt._survival([[0.45, 100]], [])["survived_shares"] == 0
    assert pt._survival([[0.45, 100]], None)["survived_shares"] == 0


def test_multi_level_fill_sums_across_levels():
    # Second level's liquidity vanished; the first level's is intact.
    out = pt._survival([[0.45, 100], [0.46, 50]], [(0.45, 100)])
    assert out["claimed_shares"] == 150
    assert out["survived_shares"] == 100
    assert out["per_level"] == [[0.45, 100, 100], [0.46, 50, 0]]


def test_observed_liquidity_is_not_double_counted_across_levels():
    # One 100-share resting offer cannot satisfy two 100-share claims.
    out = pt._survival([[0.45, 100], [0.46, 100]], [(0.45, 100)])
    assert out["survived_shares"] == 100


# ---------------------------------------------------------------------------
# Worker and queue — checks 8-13
# ---------------------------------------------------------------------------

def test_entry_not_yet_due_stays_queued(reverify_path, monkeypatch):
    monkeypatch.setattr(pt, "adapter_for", lambda b: stub_adapter([(0.45, 100)]))
    pt._pending_reverify.append(entry(due_at=1002.0))
    written = pt.run_reverify(now=1001.0)
    assert written == []
    assert len(pt._pending_reverify) == 1
    assert not reverify_path.exists()


def test_entry_past_tolerance_is_skipped_not_measured(reverify_path, monkeypatch):
    fetched = []
    monkeypatch.setattr(pt, "adapter_for",
                        lambda b: stub_adapter(fetched.append(b) or [(0.45, 100)]))
    pt._pending_reverify.append(entry(due_at=1002.0))
    pt.run_reverify(now=1002.0 + config.PAPER_REVERIFY_TOLERANCE_SEC + 1.0)
    recs = read_reverify(reverify_path)
    assert len(recs) == 1
    assert recs[0]["status"] == "skipped_late"
    assert recs[0]["survival_rate"] is None
    assert fetched == []  # a late check must not even spend the request


def test_fetch_error_is_recorded_and_worker_continues(reverify_path, monkeypatch):
    def adapter_for(book):
        if book == "kalshi":
            return stub_adapter(None, raises=RuntimeError("boom"))
        return stub_adapter([(0.45, 100)])
    monkeypatch.setattr(pt, "adapter_for", adapter_for)
    pt._pending_reverify.append(entry(book="kalshi", market_id="K1"))
    pt._pending_reverify.append(entry(book="polymarket", market_id="P1"))
    pt.run_reverify(now=1002.0)
    recs = read_reverify(reverify_path)
    assert [r["status"] for r in recs] == ["fetch_error", "measured"]
    assert "boom" in recs[0]["error"]
    assert recs[1]["survived_shares"] == 100


def test_side_selects_the_matching_ladder(reverify_path, monkeypatch):
    # fetch_both_ladders returns (yes, no); a "no" placement must be measured
    # against the second element, never the first.
    monkeypatch.setattr(pt, "adapter_for",
                        lambda b: stub_adapter([(0.45, 100)], [(0.55, 80)]))
    pt._pending_reverify.append(entry(levels=[[0.55, 80]], side="no"))
    pt.run_reverify(now=1002.0)
    rec = read_reverify(reverify_path)[0]
    assert rec["side"] == "no"
    assert rec["status"] == "measured"
    assert rec["survived_shares"] == 80


def test_missing_side_ladder_is_recorded_not_raised(reverify_path, monkeypatch):
    # Adapters without a NO side return (yes_ladder, None).
    monkeypatch.setattr(pt, "adapter_for", lambda b: stub_adapter([(0.45, 100)], None))
    pt._pending_reverify.append(entry(levels=[[0.55, 80]], side="no"))
    pt.run_reverify(now=1002.0)
    rec = read_reverify(reverify_path)[0]
    assert rec["status"] == "no_ladder"
    assert rec["survival_rate"] is None


def test_queue_depth_is_bounded_dropping_oldest(monkeypatch):
    monkeypatch.setattr(config, "PAPER_REVERIFY_MAX_PENDING", 3)
    q = pt._new_pending_queue()
    monkeypatch.setattr(pt, "_pending_reverify", q)
    for i in range(5):
        pt._pending_reverify.append(entry(market_id=f"K{i}"))
    assert len(pt._pending_reverify) == 3
    assert [e["market_id"] for e in pt._pending_reverify] == ["K2", "K3", "K4"]


def test_disabled_flag_enqueues_nothing(paper_state, reverify_path, monkeypatch):
    monkeypatch.setattr(config, "PAPER_REVERIFY_ENABLED", False)
    _place(monkeypatch)
    assert len(pt._pending_reverify) == 0
    pt.run_reverify(now=9999.0)
    assert not reverify_path.exists()


# ---------------------------------------------------------------------------
# Invariant and wiring — checks 14-18
# ---------------------------------------------------------------------------

def _place(monkeypatch, side="yes"):
    """Drive maybe_place to a real placement, following test_engine_refresh's
    monkeypatched-size_bet convention."""
    monkeypatch.setattr(pt, "adapter_for", lambda b: types.SimpleNamespace())
    monkeypatch.setattr(pt, "size_bet", lambda *a, **k: {
        "avg_fill_price": 0.45, "shares": 100, "stake": 45.0, "price_stake": 45.0,
        "fee_upfront": 0.0, "expected_profit": 5.0, "kelly_fraction_full": 0.1,
        "kelly_fraction_applied": 0.05, "levels": [[0.45, 100]],
    })
    row = {
        "market": types.SimpleNamespace(book="kalshi", market_id="K1"),
        "in_window": True, "side": side, "market_type": "moneyline",
        "fair_prob": 0.55, "pin_matchup_id": 123,
    }
    return pt.maybe_place(row, [(0.45, 100)])


def test_reverify_leaves_placement_bankroll_and_positions_unchanged(
        paper_state, reverify_path, monkeypatch):
    record = _place(monkeypatch)
    before = json.dumps(record, sort_keys=True)
    bankroll_before = pt._bankroll
    positions_before = json.dumps(
        {k: v for k, v in pt._open_positions.items()}, sort_keys=True)

    monkeypatch.setattr(pt, "adapter_for", lambda b: stub_adapter([(0.45, 40)]))
    pt.run_reverify(now=pt._pending_reverify[0]["due_at"])

    assert json.dumps(record, sort_keys=True) == before
    assert pt._bankroll == bankroll_before
    assert json.dumps({k: v for k, v in pt._open_positions.items()},
                      sort_keys=True) == positions_before
    assert read_reverify(reverify_path)[0]["survived_shares"] == 40


def test_maybe_place_returning_none_enqueues_nothing(paper_state, monkeypatch):
    monkeypatch.setattr(pt, "adapter_for", lambda b: types.SimpleNamespace())
    monkeypatch.setattr(pt, "size_bet", lambda *a, **k: None)  # Kelly <= 0
    row = {
        "market": types.SimpleNamespace(book="kalshi", market_id="K1"),
        "in_window": True, "side": "yes", "market_type": "moneyline",
        "fair_prob": 0.55, "pin_matchup_id": 123,
    }
    assert pt.maybe_place(row, [(0.45, 100)]) is None
    assert len(pt._pending_reverify) == 0


def test_sidecar_record_carries_join_identity(paper_state, reverify_path, monkeypatch):
    record = _place(monkeypatch)
    monkeypatch.setattr(pt, "adapter_for", lambda b: stub_adapter([(0.45, 100)]))
    pt.run_reverify(now=pt._pending_reverify[0]["due_at"])
    rec = read_reverify(reverify_path)[0]
    for field in ("placed_at", "book", "market_id", "side"):
        assert rec[field] == record[field]
    assert isinstance(rec["delay_sec"], float)


def test_placement_then_reverify_writes_one_record(paper_state, reverify_path, monkeypatch):
    """Integration: a placement flows through to exactly one sidecar record."""
    record = _place(monkeypatch)
    assert record is not None
    assert len(pt._pending_reverify) == 1

    monkeypatch.setattr(pt, "adapter_for", lambda b: stub_adapter([(0.45, 60)]))
    pt.run_reverify(now=pt._pending_reverify[0]["due_at"])

    recs = read_reverify(reverify_path)
    assert len(recs) == 1
    assert recs[0]["status"] == "measured"
    assert recs[0]["claimed_shares"] == 100
    assert recs[0]["survived_shares"] == 60
    assert recs[0]["survival_rate"] == 0.6
    assert len(pt._pending_reverify) == 0


def test_snapshot_aggregate_counts_measured_and_reports_skips_separately(
        reverify_path, monkeypatch):
    monkeypatch.setattr(pt, "adapter_for", lambda b: stub_adapter([(0.45, 60)]))
    pt._pending_reverify.append(entry())
    pt.run_reverify(now=1002.0)
    # A late entry contributes to skipped, never to the survival rate.
    pt._pending_reverify.append(entry(market_id="K2"))
    pt.run_reverify(now=1002.0 + config.PAPER_REVERIFY_TOLERANCE_SEC + 1.0)

    agg = pt.snapshot()["reverify"]
    assert agg["measured"] == 1
    assert agg["skipped"] == 1
    assert agg["claimed_shares"] == 100
    assert agg["survived_shares"] == 60
    assert agg["survival_pct"] == 60.0


def test_aggregate_is_rebuilt_from_the_sidecar_on_replay(reverify_path, monkeypatch):
    """The /paper figure must survive a dashboard restart."""
    monkeypatch.setattr(pt, "adapter_for", lambda b: stub_adapter([(0.45, 60)]))
    pt._pending_reverify.append(entry())
    pt.run_reverify(now=1002.0)
    pt._pending_reverify.append(entry(market_id="K2"))
    pt.run_reverify(now=1002.0 + config.PAPER_REVERIFY_TOLERANCE_SEC + 1.0)
    live = pt.snapshot()["reverify"]

    # Drop in-memory state the way a process restart would, then replay.
    monkeypatch.setattr(pt, "_reverify_agg", pt._new_reverify_agg())
    monkeypatch.setattr(config, "PAPER_TRADES_PATH", str(reverify_path.parent / "none.jsonl"))
    monkeypatch.setattr(config, "PAPER_SETTLEMENTS_PATH", str(reverify_path.parent / "none.jsonl"))
    monkeypatch.setattr(config, "PAPER_CLOSES_PATH", str(reverify_path.parent / "none.jsonl"))
    pt._replay_state()

    assert pt.snapshot()["reverify"] == live
