"""One-shot: copy clv + fair_prob_close from paper_settlements to
real_settlements wherever (book, market_id, side) matches.

The NO-side price bug fix (fix_polymarket_no_prices.py) corrected
avg_fill_price in real_settlements but left the old clv field in place,
so three Polymarket NO settlements still show -55pp / -51pp / -3.7pp clv
that were computed against the buggy complement price.

Paper tracker placed the same bets at the same time off the same ladders
and was never affected by the bug, so its clv / fair_prob_close are the
canonical values to mirror into real.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(DIR, "data")
REAL_PATH = os.path.join(DATA_DIR, "real_settlements.jsonl")
PAPER_PATH = os.path.join(DATA_DIR, "paper_settlements.jsonl")


def load(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def main():
    real = load(REAL_PATH)
    paper = load(PAPER_PATH)
    paper_by_key = {(r.get("book"), r.get("market_id"), r.get("side")): r
                    for r in paper}

    changes = []
    for r in real:
        key = (r.get("book"), r.get("market_id"), r.get("side"))
        p = paper_by_key.get(key)
        if not p:
            continue
        old_clv = r.get("clv")
        old_fpc = r.get("fair_prob_close")
        new_clv = p.get("clv")
        new_fpc = p.get("fair_prob_close")
        if new_clv is None and new_fpc is None:
            continue
        # Skip if nothing actually changes (avoid noisy backups for no-op runs).
        if old_clv == new_clv and old_fpc == new_fpc:
            continue
        r["clv"] = new_clv
        r["fair_prob_close"] = new_fpc
        changes.append({
            "market_id": key[1], "side": key[2],
            "old_clv": old_clv, "new_clv": new_clv,
            "old_fpc": old_fpc, "new_fpc": new_fpc,
        })

    if not changes:
        print("no CLV mismatches with paper — nothing to do")
        return

    print(f"copying clv + fair_prob_close from paper for {len(changes)} settlements:")
    for c in changes:
        print(f"  {c['market_id']:42s} {c['side']:3s} "
              f"clv {c['old_clv']} -> {c['new_clv']}  "
              f"fpc {c['old_fpc']} -> {c['new_fpc']}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bak = f"{REAL_PATH}.bak.{stamp}"
    shutil.copy2(REAL_PATH, bak)
    print(f"\nbackup: {bak}")
    write(REAL_PATH, real)
    print(f"wrote: {REAL_PATH}")
    print("\nrestart ev-dashboard so _replay_state() picks up the corrected fields.")


if __name__ == "__main__":
    main()
