#!/usr/bin/env python3
"""Fully purge voided paper bets from all tracking files.

Context: voiding writes a settlement with result="void" + net_pnl=0. That
keeps the bet out of EV totals but leaves rows in paper_trades.jsonl and
paper_closes.jsonl that still surface in the dashboard's paper views.
If the original trade was a matcher mistake (not a real bet), we want it
gone entirely.

This script:
  1. Scans paper_settlements.jsonl for result="void" rows, collects their
     {book}:{market_id} keys.
  2. Removes void rows from paper_settlements.jsonl.
  3. Removes matching rows from paper_trades.jsonl.
  4. Removes matching rows from paper_closes.jsonl.

Dry-run by default; pass --commit to rewrite. Each rewritten file is
copied to <file>.bak first.

Use when: voided bets need to disappear from the dashboard entirely
because the underlying match was bad and the trade was never real.

Usage on the VPS:
    cd ~/Arbitrage_Betting
    python3 purge_voided_bets.py           # preview
    python3 purge_voided_bets.py --commit  # rewrite
    sudo systemctl restart ev-dashboard
"""
import argparse
import json
import os
import shutil
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(DIR, "data")
SETTLEMENTS = os.path.join(DATA, "paper_settlements.jsonl")
TRADES = os.path.join(DATA, "paper_trades.jsonl")
CLOSES = os.path.join(DATA, "paper_closes.jsonl")


def _key(row):
    book = row.get("book") or "kalshi"
    mid = row.get("market_id") or row.get("ticker")
    return f"{book}:{mid}" if mid else None


def _load(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                sys.exit(f"{path}:{i} bad JSON: {e}")
    return rows


def _rewrite(path, rows):
    shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Rewrite files in place (default: dry-run).")
    args = ap.parse_args()

    settlements = _load(SETTLEMENTS)
    trades = _load(TRADES)
    closes = _load(CLOSES)

    void_keys = {
        _key(r) for r in settlements
        if r.get("result") == "void" and _key(r)
    }

    if not void_keys:
        print("No void settlements found. Nothing to do.")
        return

    kept_settlements = [r for r in settlements
                        if not (r.get("result") == "void" and _key(r) in void_keys)]
    kept_trades = [r for r in trades if _key(r) not in void_keys]
    kept_closes = [r for r in closes if _key(r) not in void_keys]

    print(f"Void keys to purge: {len(void_keys)}")
    for k in sorted(void_keys):
        print(f"  {k}")
    print()
    print(f"paper_settlements.jsonl: {len(settlements)} -> {len(kept_settlements)} "
          f"(drop {len(settlements) - len(kept_settlements)})")
    print(f"paper_trades.jsonl:      {len(trades)} -> {len(kept_trades)} "
          f"(drop {len(trades) - len(kept_trades)})")
    print(f"paper_closes.jsonl:      {len(closes)} -> {len(kept_closes)} "
          f"(drop {len(closes) - len(kept_closes)})")

    if not args.commit:
        print("\nDry-run. Re-run with --commit to apply.")
        return

    _rewrite(SETTLEMENTS, kept_settlements)
    _rewrite(TRADES, kept_trades)
    _rewrite(CLOSES, kept_closes)
    print("\nRewrote files. Backups at <path>.bak. Restart ev-dashboard to reload.")


if __name__ == "__main__":
    main()
