#!/usr/bin/env python3
"""
Match coverage report.

Loads the latest Pinnacle snapshot + every registered soft-book
snapshot, runs `match_all_markets`, and prints a per-book + per-series
summary. Dumps the full match report to `data/match_report.json` for
inspection.

Run: python3 match_report.py
"""
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import all_adapters
from market_matcher import match_all_markets

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_PIN = os.path.join(DIR, "data", "snapshots")
OUT_PATH = os.path.join(DIR, "data", "match_report.json")

UNMATCHED_SAMPLES_PER_SERIES = 5


def _load_latest_snapshot(dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, "*.jsonl")))
    if not files:
        return None, None
    path = files[-1]
    age = time.time() - os.path.getmtime(path)
    rows = [json.loads(l) for l in open(path)]
    return rows, age


def _load_soft_markets():
    """Normalize the latest snapshot from every registered adapter."""
    out = []
    ages = {}
    for adapter in all_adapters():
        rows, age = _load_latest_snapshot(adapter.SNAPSHOT_DIR)
        if rows is None:
            ages[adapter.BOOK] = None
            continue
        ages[adapter.BOOK] = age
        for raw in rows:
            nm = adapter.normalize_market(raw)
            if nm is not None:
                out.append(nm)
    return out, ages


def main():
    pin_rows, pin_age = _load_latest_snapshot(SNAP_PIN)
    if pin_rows is None:
        print("ERROR: missing pinnacle snapshot")
        sys.exit(1)
    soft_markets, soft_ages = _load_soft_markets()

    ages_str = "  ".join(
        f"{book}={age:.0f}s" if age is not None else f"{book}=MISSING"
        for book, age in soft_ages.items()
    )
    print(f"[snapshots] pinnacle age={pin_age:.0f}s rows={len(pin_rows)}  {ages_str}")
    print(f"[scope] soft normalized: {len(soft_markets)}")

    all_report = match_all_markets(pin_rows, soft_markets)
    ml_report = all_report.moneyline_report
    s = ml_report.stats
    ns = all_report.stats

    print()
    print("== Moneyline ==")
    print(f"Soft moneyline scope:                 {s['soft_moneyline_scope']}")
    print(f"Pinnacle moneyline scope:             {s['pin_moneyline_scope']}")
    print(f"  title-matched:           {s['matched']}")
    print(f"  title_parse_fail:        {s['title_parse_fail']}")
    print(f"  no_candidate:            {s['no_candidate']}")
    print(f"  time_out_of_tolerance:   {s['time_out_of_tolerance']}")
    print(f"  ambiguous_resolved:      {s['ambiguous_resolved']}")
    print(f"  pairs_after_ml_gates:    {ns['matched_by_type'].get('moneyline', 0)}"
          f"  (3-way matched: {ns.get('moneyline_three_way_matched', 0)}, "
          f"yes-side unresolved: {ns.get('moneyline_yes_side_unresolved', 0)})")
    print()
    print("== Non-mainline ==")
    print(f"Soft non-mainline scope:              {ns['nonml_total']}")
    for mtype in ("spread", "total", "team_total"):
        print(f"  {mtype:<12} matched {ns['matched_by_type'].get(mtype, 0):>4} / "
              f"scope {ns['by_type'].get(mtype, 0):>4}")
    print(f"  no_event_match:          {ns.get('no_event_match', 0)}   "
          f"(Soft event has no matched ML sibling)")
    print(f"  no_pin_market:           {ns.get('no_pin_market', 0)}   "
          f"(Pinnacle offers no market of this type/period)")
    print(f"  no_line_match:           {ns.get('no_line_match', 0)}   "
          f"(Soft line not published by Pinnacle)")
    print(f"  team_total_side_missing: {ns.get('team_total_side_missing', 0)}   "
          f"(pre-poller-update snapshot)")
    print()
    print("== Per-book coverage ==")
    for book in sorted(s.get("by_book", {}).keys()):
        b = s["by_book"][book]
        nb = ns.get("by_book", {}).get(book, {})
        nb_m = nb.get("matched_by_type", {})
        ml_matched = b.get("matched", 0)
        nonml_total = nb.get("nonml_total", 0)
        nonml_matched = nb.get("matched", 0)
        print(f"  {book:<12} ML {ml_matched}/{b.get('total', 0)}  "
              f"SP {nb_m.get('spread', 0)}  TO {nb_m.get('total', 0)}  "
              f"TT {nb_m.get('team_total', 0)}  "
              f"(nonml {nonml_matched}/{nonml_total})")
    print()

    # Per-series breakdown (Kalshi tickers encode series; derive from raw).
    by_series = {}
    for m in ml_report.matched:
        series = m.soft.raw.get("series_ticker") or "?"
        d = by_series.setdefault(series, {"total": 0, "matched": 0})
        d["total"] += 1
        d["matched"] += 1
    for u in ml_report.unmatched_soft:
        series = u.soft.raw.get("series_ticker") or "?"
        d = by_series.setdefault(series, {"total": 0, "matched": 0})
        d["total"] += 1

    rows = sorted(by_series.items(), key=lambda x: x[1]["total"], reverse=True)
    print(f"{'series':<28} {'total':>6} {'match':>6}")
    for series, d in rows:
        print(f"{series:<28} {d['total']:>6} {d['matched']:>6}")
    print()

    # Sample unmatched per series.
    unmatched_by_series = {}
    for u in ml_report.unmatched_soft:
        series = u.soft.raw.get("series_ticker") or "?"
        unmatched_by_series.setdefault(series, []).append(u)

    print("Sample unmatched markets per series:")
    for series, items in sorted(unmatched_by_series.items(),
                                key=lambda kv: len(kv[1]), reverse=True):
        print(f"\n  {series}  ({len(items)} unmatched)")
        for u in items[:UNMATCHED_SAMPLES_PER_SERIES]:
            print(f"    [{u.reason}] {u.soft.title}")
            if u.reason == "time_out_of_tolerance" and u.candidates:
                for delta, (matchup, start) in u.candidates[:2]:
                    print(f"        cand: {matchup} @ {start}  delta={delta/3600:.1f}h")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(ml_report.to_json(), f, indent=2)
    print(f"\nFull report written to {OUT_PATH}")


if __name__ == "__main__":
    main()
