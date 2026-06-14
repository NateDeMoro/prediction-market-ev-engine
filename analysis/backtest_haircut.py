"""
Replay every historical paper bet under the quadratic fair_prob haircut
and report what selection / Kelly sizing / realized PnL would look like.

use when:
  Validating a calibration adjustment before flipping it on for the real-money
  tracker. Read-only; mutates nothing.

Haircut: fair_adj = fair - K * fair * (1 - fair)   (K = 0.064 by default)
Threshold: same per-book formula real_tracker / paper_tracker uses.

Run: python3 -m analysis.backtest_haircut
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from statistics import mean

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIR)

from pmev.adapters import adapter_for
from pmev.execution import paper as pt
from pmev import config

DATA_DIR = os.path.join(DIR, "data")
TRADES = os.path.join(DATA_DIR, "paper_trades.jsonl")
SETTLES = os.path.join(DATA_DIR, "paper_settlements.jsonl")

# Single source of truth: config.haircut_fair / config.HAIRCUT_K (both read the
# HAIRCUT_K env var). Sweep K by exporting HAIRCUT_K before running this script.
HAIRCUT_K = config.HAIRCUT_K
KELLY_FRACTION = config.KELLY_FRACTION


def _read(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return rows


def _resize_under_haircut(trade, bankroll):
    """Re-run size_bet against the original fill levels with haircut fair_prob.

    The persisted `levels` is the fill detail (price, shares-taken-at-level),
    which equals the prefix of the positive-EV ladder at placement time. We
    treat it as the available ladder for the haircut replay — that's a slight
    overestimate of capacity (since the real ladder might have had more
    inventory at those prices), but it's the closest thing to ground truth we
    have without re-walking the original snapshot.
    """
    # Post-#7 trades persist the haircut fair in `fair_prob`; replay from the raw
    # devig (`fair_prob_raw`) so we never haircut an already-haircut value. Older
    # trades have no `fair_prob_raw`, where `fair_prob` is the raw devig.
    fair_raw = trade.get("fair_prob_raw")
    if not isinstance(fair_raw, (int, float)):
        fair_raw = trade.get("fair_prob")
    book = trade.get("book") or "kalshi"
    levels = trade.get("levels") or []
    if not isinstance(fair_raw, (int, float)) or not levels:
        return None
    adapter = adapter_for(book)

    fair_adj = config.haircut_fair(fair_raw)
    sized = pt.size_bet(levels, fair_adj, bankroll, adapter)
    return fair_adj, sized


def _passes_threshold(book, sized, is_prop):
    edge_pct = (sized["expected_profit"] / sized["stake"] * 100.0
                if sized["stake"] > 0 else 0.0)
    floor = config.min_edge_pct(book, "player_prop" if is_prop else "moneyline", sized["avg_fill_price"])
    return edge_pct >= floor, edge_pct, floor


def _settlement_index(settlements):
    out = {}
    for s in settlements:
        key = (s.get("book"), s.get("market_id"), s.get("side") or "yes")
        out[key] = s
    return out


def _scaled_pnl(settle, new_shares):
    """Scale realized PnL to the new (haircut-resized) share count."""
    orig_shares = settle.get("shares") or 0
    if orig_shares <= 0 or new_shares <= 0:
        return 0.0, 0.0
    orig_stake = settle.get("stake") or 0.0
    orig_pnl = settle.get("net_pnl") or 0.0
    factor = new_shares / orig_shares
    return orig_stake * factor, orig_pnl * factor


def _summarize(label, rows):
    if not rows:
        print(f"  {label}: (no rows)")
        return
    n = len(rows)
    stake = sum(r["new_stake"] for r in rows)
    pnl = sum(r["new_pnl"] for r in rows)
    wins = sum(1 for r in rows if r["won"])
    win_rate = wins / n
    pred = mean(r["fair_adj"] for r in rows)
    fair_deltas = [r["fair_delta_adj"] for r in rows if r["fair_delta_adj"] is not None]
    avg_fd = mean(fair_deltas) if fair_deltas else None
    print(f"  {label}: n={n}  stake=${stake:7.2f}  pnl=${pnl:+7.2f}  ROI={pnl/stake*100 if stake else 0:+.2f}%  "
          f"wr={win_rate:.2%} (pred {pred:.2%})  "
          + (f"fair_delta_adj={avg_fd*100:+.2f}pp" if avg_fd is not None else ""))


def main():
    trades = _read(TRADES)
    settlements = _read(SETTLES)
    s_idx = _settlement_index(settlements)
    print(f"Loaded {len(trades)} placements, {len(settlements)} settlements")
    print(f"Haircut K = {HAIRCUT_K}\n")

    kept, dropped = [], []
    skipped_unmatchable = 0

    # Replay against a *static* bankroll (the placement-time bankroll on the
    # original record). This isn't a perfect compounding sim — that would
    # require time-ordering and chained settlements — but it's close enough
    # for relative selection comparison.
    for t in trades:
        bankroll = t.get("bankroll_at_placement") or config.PAPER_INITIAL_BANKROLL
        is_prop = t.get("market_type") == "player_prop"

        out = _resize_under_haircut(t, bankroll)
        if out is None:
            skipped_unmatchable += 1
            continue
        fair_adj, sized = out

        if sized is None:
            dropped.append({"trade": t, "reason": "kelly<=0 after haircut"})
            continue

        passes, new_edge_pct, floor = _passes_threshold(t.get("book"), sized, is_prop)
        if not passes:
            dropped.append({"trade": t, "reason": f"edge_pct {new_edge_pct:.2f} < floor {floor:.2f}"})
            continue

        # Sanity ceiling — same as paper tracker placement
        max_edge = (config.SANITY_MAX_EDGE_PCT_PROP if is_prop else config.SANITY_MAX_EDGE_PCT)
        if new_edge_pct > max_edge:
            dropped.append({"trade": t, "reason": f"edge_pct {new_edge_pct:.2f} > sanity {max_edge}"})
            continue

        # Match settlement
        key = (t.get("book"), t.get("market_id"), t.get("side") or "yes")
        settle = s_idx.get(key)
        if settle is None or settle.get("result") not in ("yes", "no"):
            # Pending or void — we know it would have been placed, but no PnL
            kept.append({
                "trade": t, "fair_adj": fair_adj, "sized": sized,
                "new_edge_pct": new_edge_pct,
                "won": None, "new_stake": sized["stake"], "new_pnl": 0.0,
                "fair_delta_adj": None, "settle": None,
            })
            continue

        won = settle.get("result") == (t.get("side") or "yes")
        new_stake, new_pnl = _scaled_pnl(settle, sized["shares"])
        fpc = settle.get("fair_prob_close")
        fair_delta_adj = (fpc - fair_adj) if isinstance(fpc, (int, float)) else None

        kept.append({
            "trade": t, "fair_adj": fair_adj, "sized": sized,
            "new_edge_pct": new_edge_pct,
            "won": won, "new_stake": new_stake, "new_pnl": new_pnl,
            "fair_delta_adj": fair_delta_adj, "settle": settle,
        })

    # ---------- summary ----------
    print(f"Placed under haircut: {len(kept)} / {len(trades)} "
          f"({len(kept)/len(trades)*100:.1f}%)")
    print(f"Dropped: {len(dropped)}")
    print(f"Unmatchable (no settle / void): "
          f"{sum(1 for k in kept if k['won'] is None)} kept, "
          f"{skipped_unmatchable} skipped before threshold\n")

    settled_kept = [k for k in kept if k["won"] is not None]
    print("=== ALL haircut survivors with settlements ===")
    _summarize("ALL", settled_kept)
    print()

    print("=== by book ===")
    by_book = defaultdict(list)
    for k in settled_kept:
        by_book[k["trade"].get("book")].append(k)
    for b, rs in sorted(by_book.items()):
        _summarize(b, rs)
    print()

    print("=== by market_type ===")
    by_mt = defaultdict(list)
    for k in settled_kept:
        by_mt[k["trade"].get("market_type")].append(k)
    for mt, rs in sorted(by_mt.items()):
        _summarize(mt, rs)
    print()

    print("=== by edge bucket (new_edge_pct under haircut) ===")
    buckets = [(2, 3), (3, 4), (4, 5), (5, 7), (7, 10), (10, 15), (15, 100)]
    for lo, hi in buckets:
        rs = [k for k in settled_kept if lo <= k["new_edge_pct"] < hi]
        _summarize(f"edge {lo}-{hi}%", rs)
    print()

    print("=== fair_delta_adj on survivors (sanity: should be closer to 0) ===")
    fds = [k["fair_delta_adj"] for k in settled_kept if k["fair_delta_adj"] is not None]
    if fds:
        m = mean(fds)
        pos = sum(1 for f in fds if f > 0)
        print(f"  n={len(fds)}  mean={m*100:+.2f}pp  pos={pos}/{len(fds)}\n")

    print("=== drop reasons (top 6) ===")
    rcounts = defaultdict(int)
    for d in dropped:
        # Bucket reasons
        r = d["reason"]
        if r.startswith("edge_pct"):
            try:
                edge_val = float(r.split()[1])
                key = "edge<floor"
            except (ValueError, IndexError):
                key = "edge<floor"
        else:
            key = r
        rcounts[key] += 1
    for k, v in sorted(rcounts.items(), key=lambda kv: -kv[1])[:6]:
        print(f"  {k}: {v}")
    print()

    # Compare to original paper performance restricted to these same matches
    print("=== Comparison: original vs haircut on the same trade set ===")
    orig_settled = [(t, s_idx.get((t.get("book"), t.get("market_id"), t.get("side") or "yes")))
                    for t in trades]
    orig_settled = [(t, s) for t, s in orig_settled if s and s.get("result") in ("yes", "no")]
    orig_stake = sum(s["stake"] for _, s in orig_settled)
    orig_pnl = sum(s["net_pnl"] for _, s in orig_settled)
    print(f"  Original: n={len(orig_settled)}  stake=${orig_stake:.2f}  "
          f"pnl=${orig_pnl:+.2f}  ROI={orig_pnl/orig_stake*100 if orig_stake else 0:+.2f}%")
    new_stake = sum(k["new_stake"] for k in settled_kept)
    new_pnl = sum(k["new_pnl"] for k in settled_kept)
    print(f"  Haircut:  n={len(settled_kept)}  stake=${new_stake:.2f}  "
          f"pnl=${new_pnl:+.2f}  ROI={new_pnl/new_stake*100 if new_stake else 0:+.2f}%")


if __name__ == "__main__":
    main()
