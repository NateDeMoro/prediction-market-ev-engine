"""One-shot migration: fix Polymarket NO-side avg_fill_price corruption.

Polymarket's avgPx is always returned in long-side (YES) perspective, but
real_tracker historically stored it as-is. For NO buys (api_side="no") this
made the recorded price the complement of what was actually paid, which
inflated stake / over-debited the local polymarket balance / overstated
losses on settlements.

This script rewrites real_fills.jsonl and real_settlements.jsonl:

  1. For each polymarket fill where the placement's api_side resolves to
     "no" (recomputed from market_id + side via the same XOR used in
     real_tracker._place_polymarket_order), replace avg_fill_price with
     1 - avg_fill_price.

  2. For each polymarket settlement matching the same condition, recompute
     stake, gross_return, net_pnl, and the *_balance_after fields using the
     corrected avg_fill_price + the existing fee_upfront.

Backups are written alongside (.bak.YYYYMMDD).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIR)

from adapters.polymarket import _parse_market_id

DATA_DIR = os.path.join(DIR, "data")
TRADES_PATH = os.path.join(DATA_DIR, "real_trades.jsonl")
FILLS_PATH = os.path.join(DATA_DIR, "real_fills.jsonl")
SETTLEMENTS_PATH = os.path.join(DATA_DIR, "real_settlements.jsonl")

# Real-tracker initial-deposit constants (kept in sync with real_tracker.py).
INITIAL_KALSHI_BALANCE = 500.0
INITIAL_POLYMARKET_BALANCE = 500.0


def _load_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _backup(path):
    if not os.path.exists(path):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = f"{path}.bak.{stamp}"
    shutil.copy2(path, dst)
    return dst


def _api_side(market_id, internal_side):
    slug, long_or_short = _parse_market_id(market_id)
    if not slug:
        return None
    is_long = (long_or_short == "long")
    return "yes" if (is_long == (internal_side == "yes")) else "no"


def _is_polymarket_no(book, market_id, side):
    return book == "polymarket" and _api_side(market_id, side) == "no"


def main():
    trades = _load_jsonl(TRADES_PATH)
    fills = _load_jsonl(FILLS_PATH)
    settlements = _load_jsonl(SETTLEMENTS_PATH)

    print(f"loaded {len(trades)} trades, {len(fills)} fills, "
          f"{len(settlements)} settlements")

    # --- fix fills -------------------------------------------------------
    fill_changes = []
    for f in fills:
        if not _is_polymarket_no(f.get("book"), f.get("market_id"), f.get("side")):
            continue
        old = f.get("avg_fill_price")
        if not isinstance(old, (int, float)):
            continue
        new = round(1.0 - old, 6)
        f["avg_fill_price"] = new
        fill_changes.append((f.get("market_id"), f.get("side"), old, new))

    print(f"\nfills to rewrite: {len(fill_changes)}")
    for mid, side, old, new in fill_changes:
        print(f"  {mid} {side}: {old} -> {new}")

    # --- fix settlements --------------------------------------------------
    settlement_changes = []
    for s in settlements:
        if not _is_polymarket_no(s.get("book"), s.get("market_id"), s.get("side")):
            continue
        old_price = s.get("avg_fill_price")
        if not isinstance(old_price, (int, float)):
            continue
        new_price = round(1.0 - old_price, 6)

        shares = s.get("shares") or 0
        fee_upfront = s.get("fee_upfront") or 0.0
        old_stake = s.get("stake")
        old_pnl = s.get("net_pnl")

        new_stake = round(new_price * shares + fee_upfront, 4)

        result = s.get("result")
        side = s.get("side")
        won = (result == side)
        if won:
            # Polymarket charges no fee on win (fee_on_win_per_share = 0).
            new_gross_return = round(float(shares), 4)
            new_net_pnl = round(new_gross_return - new_stake, 4)
        else:
            new_gross_return = 0.0
            new_net_pnl = round(-new_stake, 4)

        s["avg_fill_price"] = new_price
        s["stake"] = new_stake
        s["gross_return"] = new_gross_return
        s["net_pnl"] = new_net_pnl

        settlement_changes.append({
            "market_id": s.get("market_id"),
            "side": side,
            "result": result,
            "old_price": old_price, "new_price": new_price,
            "old_stake": old_stake, "new_stake": new_stake,
            "old_pnl": old_pnl, "new_pnl": new_net_pnl,
        })

    print(f"\nsettlements to rewrite: {len(settlement_changes)}")
    for c in settlement_changes:
        print(f"  {c['market_id']} side={c['side']} result={c['result']}: "
              f"price {c['old_price']}->{c['new_price']} "
              f"stake {c['old_stake']}->{c['new_stake']} "
              f"pnl {c['old_pnl']}->{c['new_pnl']}")

    # --- recompute *_balance_after sequentially across all settlements ---
    # The balance_after fields were captured live, so any settlement after a
    # corrupted one is also wrong. Replay in chronological order using the
    # corrected stakes.
    settlements_sorted = sorted(settlements, key=lambda r: r.get("settled_at") or "")
    k_bal = INITIAL_KALSHI_BALANCE
    p_bal = INITIAL_POLYMARKET_BALANCE
    for s in settlements_sorted:
        pnl = s.get("net_pnl") or 0
        if s.get("book") == "kalshi":
            k_bal += pnl
        elif s.get("book") == "polymarket":
            p_bal += pnl
        s["kalshi_balance_after"] = round(k_bal, 4)
        s["polymarket_balance_after"] = round(p_bal, 4)

    # --- write back -------------------------------------------------------
    if not (fill_changes or settlement_changes):
        print("\nno changes — nothing to write")
        return

    print("\nbacking up + writing...")
    if fill_changes:
        bak = _backup(FILLS_PATH)
        print(f"  backup: {bak}")
        _write_jsonl(FILLS_PATH, fills)
        print(f"  wrote: {FILLS_PATH}")
    if settlement_changes:
        bak = _backup(SETTLEMENTS_PATH)
        print(f"  backup: {bak}")
        # Preserve original line order on disk; the in-place mutation already
        # touched the same dict objects in `settlements`, so re-write it.
        _write_jsonl(SETTLEMENTS_PATH, settlements)
        print(f"  wrote: {SETTLEMENTS_PATH}")

    print("\ndone. restart the dashboard service so _replay_state() picks up "
          "the corrected balances.")


if __name__ == "__main__":
    main()
