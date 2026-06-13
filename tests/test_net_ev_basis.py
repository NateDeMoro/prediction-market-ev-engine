"""#25 — net_ev reports two consistent bases: placement (modeled-at-entry) and
close (vs Pinnacle's closing fair). See notes/clv-close-capture/test-plan-22-23-25.md."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import paper_tracker as pt
import real_tracker as rt


@pytest.fixture(autouse=True)
def _reset():
    saved = (list(pt._settled_records), dict(pt._open_positions), list(pt._placements))
    rt_saved = (list(rt._settled_records), dict(rt._open_positions))
    pt._settled_records.clear(); pt._open_positions.clear()
    pt._closes_by_key.clear()
    rt._settled_records.clear(); rt._open_positions.clear()
    rt._closes_by_key.clear()
    yield
    pt._settled_records[:] = saved[0]
    pt._open_positions.clear(); pt._open_positions.update(saved[1])
    pt._placements[:] = saved[2]
    rt._settled_records[:] = rt_saved[0]
    rt._open_positions.clear(); rt._open_positions.update(rt_saved[1])


def _settled(expected_profit, fair_prob_close=None, result="yes", market_id="KX-1"):
    s = {
        "book": "kalshi", "market_id": market_id, "side": "yes",
        "result": result, "shares": 100, "avg_fill_price": 0.5,
        "stake": 50.0, "fair_prob": 0.55, "net_pnl": 1.0,
        "expected_profit": expected_profit,
    }
    if fair_prob_close is not None:
        s["fair_prob_close"] = fair_prob_close
    return s


def test_net_ev_sums_placement_ev_over_closed_subset():
    pt._settled_records[:] = [_settled(3.0, fair_prob_close=0.6),
                              _settled(2.0, market_id="KX-2")]  # no close -> excluded
    summ = pt.snapshot()["summary"]
    # net_ev and net_ev_close share the closed subset, so the no-close bet is out.
    assert summ["net_ev"] == 3.0


def test_net_ev_close_sums_only_rows_with_close():
    pt._settled_records[:] = [_settled(3.0, fair_prob_close=0.6),
                              _settled(2.0, market_id="KX-2")]  # no close
    summ = pt.snapshot()["summary"]
    # Close fair is haircut so it shares the placement fair's basis.
    expected = round(pt._expected_profit_at(pt._settled_records[0],
                                            config.haircut_fair(0.6)), 4)
    assert summ["net_ev_close"] == expected
    assert summ["net_ev_close_samples"] == 1


def test_fair_delta_haircuts_close_fair():
    # Fair Δ must compare like bases: haircut close fair minus haircut placement
    # fair (the placement fair is already haircut). A raw close fair would bake in
    # a ~K*p*(1-p) offset even with zero line movement.
    pt._settled_records[:] = [_settled(3.0, fair_prob_close=0.6)]  # placement fair_prob=0.55
    summ = pt.snapshot()["summary"]
    assert summ["avg_fair_delta"] == round(config.haircut_fair(0.6) - 0.55, 6)
    assert summ["fair_delta_samples"] == 1


def test_settle_does_not_overwrite_expected_profit(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PAPER_SETTLEMENTS_PATH", str(tmp_path / "s.jsonl"))

    class _Stub:
        def fetch_settlement(self, market_id):
            return "yes"
        def fee_on_win_per_share(self, p):
            return 0.0
    monkeypatch.setattr(pt, "adapter_for", lambda book: _Stub())

    rec = {"book": "kalshi", "market_id": "KX-9", "side": "yes", "shares": 100,
           "avg_fill_price": 0.5, "stake": 50.0, "expected_profit": 3.0, "fair_prob": 0.55}
    key = pt._record_key(rec)
    pt._open_positions[key] = rec
    pt._closes_by_key[key] = {"fair_prob_close": 0.6}

    settlement = pt._settle_one(rec)
    assert settlement["expected_profit"] == 3.0       # placement value, not close-basis
    assert settlement["fair_prob_close"] == 0.6       # close still recorded
    assert "clv" in settlement


def test_replay_restores_placement_ev(monkeypatch, tmp_path):
    trades = tmp_path / "t.jsonl"
    settles = tmp_path / "se.jsonl"
    closes = tmp_path / "c.jsonl"
    monkeypatch.setattr(config, "PAPER_TRADES_PATH", str(trades))
    monkeypatch.setattr(config, "PAPER_SETTLEMENTS_PATH", str(settles))
    monkeypatch.setattr(config, "PAPER_CLOSES_PATH", str(closes))
    import json
    with open(trades, "w") as f:
        f.write(json.dumps({"book": "kalshi", "market_id": "KX-1", "side": "yes",
                            "expected_profit": 3.0, "avg_fill_price": 0.5, "shares": 100}) + "\n")
    with open(settles, "w") as f:
        # stored expected_profit is close-basis (9.9), as old code would have written
        f.write(json.dumps({"book": "kalshi", "market_id": "KX-1", "side": "yes",
                            "expected_profit": 9.9, "fair_prob_close": 0.6,
                            "avg_fill_price": 0.5, "shares": 100,
                            "result": "yes", "net_pnl": 5.0}) + "\n")
    closes.write_text("")

    pt._replay_state()
    s = next(r for r in pt._settled_records if r.get("market_id") == "KX-1")
    assert s["expected_profit"] == 3.0   # restored from the placement record


def test_void_excluded_from_both_ev():
    pt._settled_records[:] = [_settled(3.0, fair_prob_close=0.6),
                              _settled(2.0, fair_prob_close=0.6, result="void", market_id="KX-2")]
    summ = pt.snapshot()["summary"]
    assert summ["net_ev"] == 3.0              # void's placement EV excluded
    assert summ["net_ev_close_samples"] == 1  # void's close excluded


def test_real_snapshot_has_both_ev():
    rt._settled_records[:] = [_settled(3.0, fair_prob_close=0.6)]
    summ = rt.snapshot()["summary"]
    assert summ["net_ev"] == 3.0
    assert summ["net_ev_close"] == round(
        pt._expected_profit_at(rt._settled_records[0], config.haircut_fair(0.6)), 4)
    assert summ["net_ev_close_samples"] == 1
