"""Kalshi scalar (fractional) settlement handling.

Some Kalshi sports markets resolve `finalized` with `result == "scalar"` and a
`settlement_value_dollars` per-YES-share payout instead of "yes"/"no" (postponed,
cancelled past the 2-day window, or ties). These tests pin:

  - kalshi.fetch_settlement returns a float YES-payout for scalar, None when the
    value is missing, and the binary "yes"/"no" path is unchanged.
  - paper/real _settle_one credit shares*payout*(1-fee)-stake on the float path,
    record result="scalar" + settlement_value, stay idempotent, and leave the
    binary path byte-identical.
  - paper/real snapshot() classify scalar settlements by P&L sign.

No network: requests.get and adapter.fetch_settlement are monkeypatched.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import paper_tracker as pt
import real_tracker as rt
from adapters import kalshi


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _market(**fields):
    base = {"status": "finalized", "result": "scalar",
            "settlement_value_dollars": "0.61"}
    base.update(fields)
    return {"market": base}


# --- adapter: kalshi.fetch_settlement -------------------------------------

def test_kalshi_fetch_settlement_scalar_returns_float(monkeypatch):
    monkeypatch.setattr(kalshi.requests, "get",
                        lambda *a, **k: _FakeResp(_market()))
    out = kalshi.fetch_settlement("KXKBOGAME-26MAY070530LOTKTW-KTW")
    assert isinstance(out, float)
    assert out == pytest.approx(0.61)


def test_kalshi_fetch_settlement_scalar_missing_value_returns_none(monkeypatch):
    monkeypatch.setattr(kalshi.requests, "get",
                        lambda *a, **k: _FakeResp(_market(settlement_value_dollars=None)))
    assert kalshi.fetch_settlement("X") is None
    monkeypatch.setattr(kalshi.requests, "get",
                        lambda *a, **k: _FakeResp(_market(settlement_value_dollars="n/a")))
    assert kalshi.fetch_settlement("X") is None


def test_kalshi_fetch_settlement_binary_unchanged(monkeypatch):
    monkeypatch.setattr(kalshi.requests, "get",
                        lambda *a, **k: _FakeResp(_market(result="yes")))
    assert kalshi.fetch_settlement("X") == "yes"
    monkeypatch.setattr(kalshi.requests, "get",
                        lambda *a, **k: _FakeResp(_market(result="no")))
    assert kalshi.fetch_settlement("X") == "no"


# --- paper_tracker._settle_one --------------------------------------------

@pytest.fixture
def paper_state(monkeypatch, tmp_path):
    """Reset paper_tracker module state and route settlements to a temp file."""
    monkeypatch.setattr(config, "PAPER_SETTLEMENTS_PATH",
                        str(tmp_path / "paper_settlements.jsonl"))
    monkeypatch.setattr(pt, "_open_positions", {})
    monkeypatch.setattr(pt, "_settled_records", [])
    monkeypatch.setattr(pt, "_closes_by_key", {})
    monkeypatch.setattr(pt, "_bankroll", 5000.0)
    return pt


def _seed_paper(pt_mod, side, shares=100, fill=0.30, stake=None):
    if stake is None:
        stake = round(shares * fill, 4)
    rec = {
        "book": "kalshi", "market_id": "MKT", "side": side,
        "shares": shares, "avg_fill_price": fill, "stake": stake,
        "selection": "X", "market_type": "moneyline",
    }
    pt_mod._open_positions[pt_mod._key("kalshi", "MKT", side)] = rec
    return rec


def test_paper_settle_scalar_yes_side(paper_state, monkeypatch):
    monkeypatch.setattr(kalshi, "fetch_settlement", lambda mid: 0.61)
    rec = _seed_paper(paper_state, "yes", shares=100, fill=0.30, stake=30.0)
    s = paper_state._settle_one(rec)
    assert s["result"] == "scalar"
    assert s["settlement_value"] == pytest.approx(0.61)
    assert s["gross_return"] == pytest.approx(61.0)
    assert s["net_pnl"] == pytest.approx(31.0)
    assert paper_state._bankroll == pytest.approx(5031.0)
    assert paper_state._open_positions == {}


def test_paper_settle_scalar_no_side(paper_state, monkeypatch):
    # NO position; yes_payout 0.32 => my (no) payout 0.68. Mirrors the stuck
    # TOH/HAN pair (0.32 / 0.68).
    monkeypatch.setattr(kalshi, "fetch_settlement", lambda mid: 0.32)
    rec = _seed_paper(paper_state, "no", shares=100, fill=0.50, stake=50.0)
    s = paper_state._settle_one(rec)
    assert s["result"] == "scalar"
    assert s["settlement_value"] == pytest.approx(0.68)
    assert s["gross_return"] == pytest.approx(68.0)
    assert s["net_pnl"] == pytest.approx(18.0)


def test_paper_settle_binary_unchanged(paper_state, monkeypatch):
    monkeypatch.setattr(kalshi, "fetch_settlement", lambda mid: "yes")
    rec = _seed_paper(paper_state, "yes", shares=100, fill=0.30, stake=30.0)
    s = paper_state._settle_one(rec)
    assert s["result"] == "yes"
    assert "settlement_value" not in s
    assert s["gross_return"] == pytest.approx(100.0)
    assert s["net_pnl"] == pytest.approx(70.0)


def test_paper_settle_idempotent_scalar(paper_state, monkeypatch):
    monkeypatch.setattr(kalshi, "fetch_settlement", lambda mid: 0.61)
    rec = _seed_paper(paper_state, "yes", shares=100, fill=0.30, stake=30.0)
    first = paper_state._settle_one(rec)
    bankroll_after_first = paper_state._bankroll
    second = paper_state._settle_one(rec)
    assert first is not None
    assert second is None
    assert paper_state._bankroll == pytest.approx(bankroll_after_first)
    assert len(paper_state._settled_records) == 1


def test_paper_snapshot_omits_win_loss_counts(paper_state):
    # Paper no longer tracks W/L / hit-rate (not shown on the paper dashboard);
    # snapshot still tallies settled rows without erroring on scalar results.
    paper_state._settled_records.extend([
        {"result": "scalar", "net_pnl": 31.0, "stake": 30.0},
        {"result": "no", "net_pnl": -30.0, "stake": 30.0},
    ])
    summ = paper_state.snapshot()["summary"]
    assert "wins" not in summ
    assert "losses" not in summ
    assert "hit_rate_pct" not in summ
    assert summ["total_settled"] == 2


# --- real_tracker._settle_one ---------------------------------------------

@pytest.fixture
def real_state(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "REAL_SETTLEMENTS_PATH",
                        str(tmp_path / "real_settlements.jsonl"))
    monkeypatch.setattr(rt, "_open_positions", {})
    monkeypatch.setattr(rt, "_settled_records", [])
    monkeypatch.setattr(rt, "_closes_by_key", {})
    monkeypatch.setattr(rt, "_kalshi_balance", 1000.0)
    monkeypatch.setattr(rt, "_polymarket_balance", 0.0)
    # Neutralize the daily-halt side effect so settlement math is isolated.
    monkeypatch.setattr(rt, "_today_realized_pnl", lambda: 0.0)
    monkeypatch.setattr(rt, "_halt_active", lambda: True)
    return rt


def _seed_real(rt_mod, side, shares=100, fill=0.30, fee=0.0):
    rec = {
        "book": "kalshi", "market_id": "MKT", "side": side,
        "status": "filled", "filled_count": shares, "shares": shares,
        "avg_fill_price": fill, "fee_upfront": fee,
        "selection": "X", "market_type": "moneyline",
    }
    rt_mod._open_positions[rt_mod._key("kalshi", "MKT", side)] = rec
    return rec


def test_real_settle_scalar(real_state, monkeypatch):
    monkeypatch.setattr(kalshi, "fetch_settlement", lambda mid: 0.32)
    rec = _seed_real(real_state, "no", shares=100, fill=0.50)
    s = real_state._settle_one(rec)
    assert s["result"] == "scalar"
    assert s["settlement_value"] == pytest.approx(0.68)
    assert s["gross_return"] == pytest.approx(68.0)
    assert s["net_pnl"] == pytest.approx(18.0)  # 68 - 50 stake


def test_real_snapshot_scalar_counts_by_pnl(real_state):
    real_state._settled_records.extend([
        {"result": "scalar", "side": "no", "net_pnl": 18.0, "stake": 50.0},
        {"result": "scalar", "side": "yes", "net_pnl": -20.0, "stake": 30.0},
        {"result": "yes", "side": "yes", "net_pnl": 70.0, "stake": 30.0},
    ])
    snap = real_state.snapshot()
    summ = snap["summary"]
    assert summ["wins"] == 2     # binary yes + scalar profit
    assert summ["losses"] == 1   # scalar loss
