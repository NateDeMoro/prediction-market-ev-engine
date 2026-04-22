#!/usr/bin/env python3
"""
Void one or more open paper-tracker positions.

Use case: a placement was based on a bad upstream match (e.g. cross-league
city-name collision) and would otherwise sit in `open_positions` until the
adapter's settlement endpoint resolves it -- which for a non-existent market
means forever, contaminating the live dashboard.

Voiding writes a settlement record with `result: "void"` and `net_pnl: 0.0`,
which:
  - removes the key from `_open_positions` on the next dashboard restart
    (replay) and the next snapshot() call;
  - does not affect the running bankroll;
  - is excluded from the wins/losses counts in snapshot() (those gate on
    result == 'yes' / 'no').

Usage:
    python3 void_paper_bet.py <market_id> [<market_id> ...]
    python3 void_paper_bet.py --book kalshi <market_id> ...   # explicit book

Run on the host that owns paper_trades.jsonl. Restart ev-dashboard afterwards
so the in-memory state matches the on-disk record:
    systemctl restart ev-dashboard
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_PATH = os.path.join(DIR, "data", "paper_trades.jsonl")
SETTLEMENTS_PATH = os.path.join(DIR, "data", "paper_settlements.jsonl")


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _key(book, market_id):
    return f"{book}:{market_id}"


def _record_key(record):
    book = record.get("book") or "kalshi"
    mid = record.get("market_id") or record.get("ticker")
    return _key(book, mid) if mid else None


def main():
    ap = argparse.ArgumentParser(description="Void open paper-tracker positions.")
    ap.add_argument("market_ids", nargs="+", help="Market IDs to void.")
    ap.add_argument("--book", default=None,
                    help="Restrict matching to this book (default: any book).")
    ap.add_argument("--reason", default="cross_league_mismatch",
                    help="Free-text reason recorded on the settlement row.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be voided without writing.")
    args = ap.parse_args()

    trades = _read_jsonl(TRADES_PATH)
    settlements = _read_jsonl(SETTLEMENTS_PATH)
    settled_keys = {_record_key(s) for s in settlements if _record_key(s)}

    # Latest placement per key wins (matches paper_tracker._replay_state).
    by_key = {}
    for t in trades:
        k = _record_key(t)
        if k:
            by_key[k] = t

    targets = []
    missing = []
    already_settled = []
    for mid in args.market_ids:
        if args.book:
            k = _key(args.book, mid)
            placement = by_key.get(k)
            if placement is None:
                missing.append(mid)
            elif k in settled_keys:
                already_settled.append(mid)
            else:
                targets.append(placement)
        else:
            matches = [p for k, p in by_key.items()
                       if (p.get("market_id") == mid) and k not in settled_keys]
            if not matches and any(p.get("market_id") == mid for p in by_key.values()):
                already_settled.append(mid)
            elif not matches:
                missing.append(mid)
            else:
                targets.extend(matches)

    if missing:
        print(f"WARN: no placement found for: {', '.join(missing)}", file=sys.stderr)
    if already_settled:
        print(f"WARN: already settled (skipping): {', '.join(already_settled)}",
              file=sys.stderr)

    if not targets:
        print("Nothing to void.")
        sys.exit(1 if (missing or already_settled) else 0)

    when = datetime.now(timezone.utc).isoformat()
    rows = []
    for placement in targets:
        book = placement.get("book") or "kalshi"
        mid = placement.get("market_id")
        rows.append({
            "settled_at": when,
            "book": book,
            "market_id": mid,
            "pin_matchup": placement.get("pin_matchup"),
            "pin_sport": placement.get("pin_sport"),
            "pin_start_time": placement.get("pin_start_time"),
            "market_type": placement.get("market_type"),
            "period_label": placement.get("period_label"),
            "line": placement.get("line"),
            "selection": placement.get("selection"),
            "yes_side_label": placement.get("yes_side_label"),
            "result": "void",
            "voided": True,
            "void_reason": args.reason,
            "shares": placement.get("shares"),
            "stake": placement.get("stake"),
            "avg_fill_price": placement.get("avg_fill_price"),
            "fair_prob": placement.get("fair_prob"),
            "expected_profit": placement.get("expected_profit"),
            "edge_pct": placement.get("edge_pct"),
            "gross_return": placement.get("stake", 0.0),  # refund-equivalent
            "net_pnl": 0.0,
        })

    print(f"Voiding {len(rows)} position(s):")
    for r in rows:
        print(f"  [{r['book']}] {r['market_id']}  stake=${r['stake']}  "
              f"selection={r['selection']!r}  pin={r['pin_matchup']!r}")

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        return

    os.makedirs(os.path.dirname(SETTLEMENTS_PATH), exist_ok=True)
    with open(SETTLEMENTS_PATH, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    print(f"\nAppended {len(rows)} void settlement(s) to {SETTLEMENTS_PATH}")
    print("Restart ev-dashboard so in-memory state picks up the voids:")
    print("  systemctl restart ev-dashboard")


if __name__ == "__main__":
    main()
