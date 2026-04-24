#!/usr/bin/env python3
"""Remove superseded void entries from data/paper_settlements.jsonl.

Context: a voided bet writes a settlement with result="void" + net_pnl=0.0.
If ev-dashboard isn't restarted after the void, its in-memory
_open_positions still contains the bet and the next settlement tick writes
a real settlement for the same key. The result is two rows per key: one
void, one real (yes/no).

This script rewrites paper_settlements.jsonl keeping only one row per
(book, market_id): the real settlement wins over the void. Standalone voids
(keys with no real settlement) are left alone — removing them would put the
bet back into _open_positions on the next dashboard restart.

Usage on the VPS:
    sudo systemctl stop ev-dashboard
    python3 cleanup_superseded_voids.py            # dry-run, prints plan
    python3 cleanup_superseded_voids.py --commit   # rewrites in place
    sudo systemctl start ev-dashboard

The original file is copied to paper_settlements.jsonl.bak before any write.
"""
import argparse
import fcntl
import json
import os
import shutil
import sys
from collections import defaultdict

DIR = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(DIR, "data", "paper_settlements.jsonl")
BACKUP = PATH + ".bak"


def _key(row):
    book = row.get("book") or "kalshi"
    mid = row.get("market_id") or row.get("ticker")
    return f"{book}:{mid}" if mid else None


def load():
    if not os.path.exists(PATH):
        sys.exit(f"no settlements file at {PATH}")
    rows = []
    with open(PATH) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                sys.exit(f"corrupt JSON at {PATH}:{i}: {e}")
    return rows


def plan(rows):
    """Return (keep, drop) lists. An entry is dropped iff its key has both
    a void and a non-void settlement and it is the void."""
    by_key = defaultdict(list)
    for r in rows:
        k = _key(r)
        if k:
            by_key[k].append(r)

    keep, drop = [], []
    for r in rows:
        k = _key(r)
        if not k:
            keep.append(r)
            continue
        siblings = by_key[k]
        has_void = any(s.get("result") == "void" for s in siblings)
        has_real = any(s.get("result") in ("yes", "no") for s in siblings)
        if has_void and has_real and r.get("result") == "void":
            drop.append(r)
        else:
            keep.append(r)
    return keep, drop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Actually rewrite the file. Without this flag, runs as dry-run.")
    args = ap.parse_args()

    rows = load()
    keep, drop = plan(rows)

    print(f"Loaded {len(rows)} settlement rows from {PATH}")
    print(f"  keep: {len(keep)}")
    print(f"  drop: {len(drop)} (superseded voids)")
    print()

    if not drop:
        print("Nothing to do.")
        return

    print("Would remove:")
    for r in drop:
        print(f"  [{r.get('book') or 'kalshi'}] {r.get('market_id')}  "
              f"settled_at={r.get('settled_at')}  reason={r.get('void_reason')!r}")
    print()

    if not args.commit:
        print("Dry-run. Re-run with --commit to apply.")
        return

    # Back up, then rewrite atomically under flock so a racing paper_tracker
    # append (if the dashboard is still running) can't interleave.
    shutil.copy2(PATH, BACKUP)
    print(f"Backed up original to {BACKUP}")

    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        for r in keep:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, PATH)

    print(f"Rewrote {PATH} with {len(keep)} rows ({len(drop)} superseded voids removed).")
    print("Restart ev-dashboard to reload in-memory state:")
    print("  sudo systemctl restart ev-dashboard")


if __name__ == "__main__":
    main()
