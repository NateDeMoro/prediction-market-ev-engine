#!/usr/bin/env python3
"""
Player-prop match coverage report.

Loads the latest Pinnacle + soft-book snapshots, runs `match_all_markets`,
and prints a prop-only breakdown: per-league scope, matched vs unmatched
with reason codes, per-stat coverage, and sampled unmatched rows.

Use when: validating NBA / NHL prop matching after adapter or matcher
changes, or debugging low coverage after an environment flip.

Run: python3 match_report_props.py
"""
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict

from adapters import all_adapters
from market_matcher import match_all_markets, load_pinnacle_props

DIR = os.path.dirname(os.path.abspath(__file__))
SNAP_PIN = os.path.join(DIR, "data", "snapshots")
OUT_PATH = os.path.join(DIR, "data", "match_report_props.json")

SAMPLES = 8


def _load_latest(dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, "*.jsonl")))
    if not files:
        return None, None
    path = files[-1]
    age = time.time() - os.path.getmtime(path)
    return [json.loads(l) for l in open(path)], age


def main():
    pin_rows, pin_age = _load_latest(SNAP_PIN)
    if pin_rows is None:
        print("ERROR: missing pinnacle snapshot")
        sys.exit(1)

    soft = []
    ages = {}
    for a in all_adapters():
        rows, age = _load_latest(a.SNAPSHOT_DIR)
        ages[a.BOOK] = age
        if rows is None:
            continue
        for raw in rows:
            nm = a.normalize_market(raw)
            if nm is not None:
                soft.append(nm)

    soft_props = [nm for nm in soft if nm.market_type == "player_prop"]
    pin_props = load_pinnacle_props(pin_rows)

    print(f"[snapshots] pinnacle age={pin_age:.0f}s rows={len(pin_rows)} props={len(pin_props)}")
    for book, age in ages.items():
        age_s = f"{age:.0f}s" if age is not None else "MISSING"
        book_props = sum(1 for nm in soft_props if nm.book == book)
        print(f"  {book} age={age_s} props={book_props}")

    report = match_all_markets(pin_rows, soft)
    s = report.stats
    matched = [p for p in report.pairs if p.market_type == "player_prop"]
    print()
    print("== Player-prop scope ==")
    print(f"Soft props:                 {s['by_type'].get('player_prop', 0)}")
    print(f"Pinnacle props:             {s.get('prop_pin_scope', 0)}")
    print(f"Matched pairs:              {s['matched_by_type'].get('player_prop', 0)}")
    print()
    print("== Unmatched reasons ==")
    for reason, count in (s.get("prop_unmatched_reasons") or {}).items():
        print(f"  {reason:<28} {count}")
    print()

    # Per-(league, stat) breakdown.
    soft_by_stat = Counter((nm.raw.get("series_ticker", "")[:5], nm.stat) for nm in soft_props)
    matched_by_stat = Counter((p.market.raw.get("series_ticker", "")[:5], p.market.stat)
                              for p in matched)
    print("== Per league/stat ==")
    print(f"{'league':<8} {'stat':<16} {'soft':>6} {'matched':>8}")
    for (lg, stat), n in sorted(soft_by_stat.items(), key=lambda kv: -kv[1]):
        m = matched_by_stat.get((lg, stat), 0)
        print(f"{lg:<8} {str(stat):<16} {n:>6} {m:>8}")
    print()

    # Per-game coverage.
    games = defaultdict(lambda: {"soft": 0, "matched": 0})
    for nm in soft_props:
        ev = nm.event_id or "?"
        games[ev]["soft"] += 1
    for p in matched:
        ev = p.market.event_id or "?"
        games[ev]["matched"] += 1
    if games:
        print("== Per-game (event_id) coverage ==")
        print(f"{'event_id':<20} {'soft':>6} {'matched':>8}  coverage")
        for ev, d in sorted(games.items(), key=lambda kv: -kv[1]["soft"]):
            pct = (d["matched"] / d["soft"] * 100) if d["soft"] else 0
            print(f"{ev:<20} {d['soft']:>6} {d['matched']:>8}  {pct:>5.1f}%")
        print()

    # Sample matched.
    print("== Sample matched ==")
    for p in matched[:SAMPLES]:
        nm = p.market
        print(f"  [{nm.book}] {p.pin_matchup} | {nm.player} {nm.stat} line={nm.line:g} "
              f"| pin_prop_id={p.pin_prop_matchup_id} "
              f"| Pin {p.yes_side_price:+d}/{p.opposite_side_price:+d}")
    print()

    # Dump full pair list.
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    dump = {
        "stats": {
            "soft_props": s["by_type"].get("player_prop", 0),
            "pin_props": s.get("prop_pin_scope", 0),
            "matched": s["matched_by_type"].get("player_prop", 0),
            "unmatched_reasons": s.get("prop_unmatched_reasons"),
        },
        "matched_pairs": [
            {
                "book": p.market.book,
                "market_id": p.market.market_id,
                "event_id": p.market.event_id,
                "player": p.market.player,
                "stat": p.market.stat,
                "line": p.market.line,
                "pin_matchup": p.pin_matchup,
                "pin_matchup_id": p.pin_matchup_id,
                "pin_prop_matchup_id": p.pin_prop_matchup_id,
                "yes_price": p.yes_side_price,
                "opp_price": p.opposite_side_price,
            }
            for p in matched
        ],
    }
    with open(OUT_PATH, "w") as f:
        json.dump(dump, f, indent=2)
    print(f"Full report written to {OUT_PATH}")


if __name__ == "__main__":
    main()
