"""
Fair delta (closing line drift) analysis.
Join trades + closes on (book, market_id, side), compute
  fair_delta = fair_prob_close - fair_prob_at_entry
then slice by market_type, sport, book, timing, and edge bucket.

Usage:
    python analysis/fair_delta_analysis.py
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

DATA = Path(__file__).parent.parent / "1000BetsTracked"


def load_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def parse_dt(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def minutes_until_start(placed_at_str, start_str):
    t0 = parse_dt(placed_at_str)
    t1 = parse_dt(start_str)
    if t0 and t1:
        return (t1 - t0).total_seconds() / 60.0
    return None


def timing_bucket(mins):
    if mins is None:
        return "unknown"
    if mins < 5:
        return "<5min"
    if mins < 30:
        return "5-30min"
    if mins < 60:
        return "30-60min"
    if mins < 120:
        return "1-2h"
    if mins < 180:
        return "2-3h"
    return ">3h"


def edge_bucket(edge):
    if edge is None:
        return "unknown"
    if edge < 5:
        return "<5%"
    if edge < 8:
        return "5-8%"
    if edge < 12:
        return "8-12%"
    return ">=12%"


def prob_bucket(prob):
    if prob is None:
        return "unknown"
    if prob < 0.10:
        return "<10%"
    if prob < 0.25:
        return "10-25%"
    if prob < 0.50:
        return "25-50%"
    return ">=50%"


def build_close_index(closes):
    idx = {}
    for row in closes:
        key = (row.get("book"), row.get("market_id"), row.get("side"))
        idx[key] = row
    return idx


def stats(values):
    if not values:
        return {}
    n = len(values)
    mean = sum(values) / n
    sorted_v = sorted(values)
    p10, med, p90 = np.percentile(sorted_v, [10, 50, 90])
    neg = sum(1 for v in values if v < -0.005)
    pos = sum(1 for v in values if v > 0.005)
    return {
        "n": n,
        "mean": mean,
        "median": med,
        "p10": p10,
        "p90": p90,
        "pct_adverse": 100 * neg / n,   # drift against us (prob moved away)
        "pct_favorable": 100 * pos / n,  # drift in our favor
    }


def fmt(label, s, indent=2):
    pad = " " * indent
    print(f"{pad}{label:<40}  n={s['n']:>4}  mean={s['mean']:+.4f}  "
          f"med={s['median']:+.4f}  "
          f"p10={s['p10']:+.4f}  p90={s['p90']:+.4f}  "
          f"adverse={s['pct_adverse']:5.1f}%")


def section(title):
    print()
    print("=" * 80)
    print(f"  {title}")
    print("=" * 80)


def analyze(label, trades, closes_idx):
    records = []
    matched = 0
    unmatched = 0

    for t in trades:
        key = (t.get("book"), t.get("market_id"), t.get("side"))
        close = closes_idx.get(key)
        if close is None:
            unmatched += 1
            continue
        matched += 1

        fair_prob_entry = t.get("fair_prob")
        fair_prob_close = close.get("fair_prob_close")
        if fair_prob_entry is None or fair_prob_close is None:
            continue

        # Both fair_prob (entry) and fair_prob_close are stored in side perspective:
        # the winning probability of whichever side was bet (yes or no).
        # So fair_delta > 0 always means the market moved in our favor.
        fair_delta = fair_prob_close - fair_prob_entry

        mins_placed = minutes_until_start(t.get("placed_at"), t.get("pin_start_time"))

        records.append({
            "fair_delta": fair_delta,
            "fair_prob_entry": fair_prob_entry,
            "fair_prob_close": fair_prob_close,
            "edge_pct": t.get("edge_pct"),
            "market_type": t.get("market_type", "unknown"),
            "sport": t.get("pin_sport", "unknown"),
            "book": t.get("book", "unknown"),
            "mins_placed": mins_placed,
            "timing": timing_bucket(mins_placed),
            "edge_bucket": edge_bucket(t.get("edge_pct")),
            "prob_bucket": prob_bucket(fair_prob_entry),
            "period_label": t.get("period_label", "FULL"),
            "pin_matchup": t.get("pin_matchup", ""),
        })

    section(f"{label}  ({matched} matched, {unmatched} unmatched)")

    if not records:
        print("  No data.")
        return records

    all_deltas = [r["fair_delta"] for r in records]
    print(f"\n  Overall:")
    fmt("ALL", stats(all_deltas))

    # --- by market_type ---
    print(f"\n  By market_type:")
    by_type = defaultdict(list)
    for r in records:
        by_type[r["market_type"]].append(r["fair_delta"])
    for k in sorted(by_type, key=lambda k: abs(stats(by_type[k])["mean"]), reverse=True):
        fmt(k, stats(by_type[k]))

    # --- by sport ---
    print(f"\n  By sport:")
    by_sport = defaultdict(list)
    for r in records:
        by_sport[r["sport"]].append(r["fair_delta"])
    for k in sorted(by_sport, key=lambda k: abs(stats(by_sport[k])["mean"]), reverse=True):
        fmt(k, stats(by_sport[k]))

    # --- by sport x market_type ---
    print(f"\n  By sport x market_type (n>=5):")
    by_sm = defaultdict(list)
    for r in records:
        by_sm[(r["sport"], r["market_type"])].append(r["fair_delta"])
    pairs = sorted(by_sm, key=lambda k: abs(stats(by_sm[k])["mean"]), reverse=True)
    for k in pairs:
        s = stats(by_sm[k])
        if s["n"] >= 5:
            fmt(f"{k[0]} / {k[1]}", s)

    # --- by book ---
    print(f"\n  By book:")
    by_book = defaultdict(list)
    for r in records:
        by_book[r["book"]].append(r["fair_delta"])
    for k in sorted(by_book, key=lambda k: abs(stats(by_book[k])["mean"]), reverse=True):
        fmt(k, stats(by_book[k]))

    # --- by timing bucket ---
    print(f"\n  By timing (minutes before start at placement):")
    order = ["<5min", "5-30min", "30-60min", "1-2h", "2-3h", ">3h", "unknown"]
    by_time = defaultdict(list)
    for r in records:
        by_time[r["timing"]].append(r["fair_delta"])
    for k in order:
        if k in by_time:
            fmt(k, stats(by_time[k]))

    # --- by edge bucket ---
    print(f"\n  By edge at entry:")
    edge_order = ["<5%", "5-8%", "8-12%", ">=12%", "unknown"]
    by_edge = defaultdict(list)
    for r in records:
        by_edge[r["edge_bucket"]].append(r["fair_delta"])
    for k in edge_order:
        if k in by_edge:
            fmt(k, stats(by_edge[k]))

    # --- by fair prob bucket ---
    print(f"\n  By fair prob at entry (longshot vs chalk):")
    prob_order = ["<10%", "10-25%", "25-50%", ">=50%", "unknown"]
    by_prob = defaultdict(list)
    for r in records:
        by_prob[r["prob_bucket"]].append(r["fair_delta"])
    for k in prob_order:
        if k in by_prob:
            fmt(k, stats(by_prob[k]))

    # --- timing x market_type cross (n>=5) ---
    print(f"\n  By timing x market_type (n>=5):")
    by_tm = defaultdict(list)
    for r in records:
        by_tm[(r["timing"], r["market_type"])].append(r["fair_delta"])
    pairs_tm = sorted(by_tm, key=lambda k: abs(stats(by_tm[k])["mean"]), reverse=True)
    for k in pairs_tm:
        s = stats(by_tm[k])
        if s["n"] >= 5:
            fmt(f"{k[0]} / {k[1]}", s)

    # --- worst individual matchups by avg fair_delta (adverse) ---
    print(f"\n  Worst matchup segments by adverse drift (mean fair_delta < -0.01, n>=3):")
    by_seg = defaultdict(list)
    for r in records:
        seg = f"{r['sport']} | {r['market_type']} | {r['timing']}"
        by_seg[seg].append(r["fair_delta"])
    worst = [(k, stats(v)) for k, v in by_seg.items()
             if stats(v)["n"] >= 3 and stats(v)["mean"] < -0.01]
    worst.sort(key=lambda x: x[1]["mean"])
    for seg, s in worst[:15]:
        fmt(seg, s)

    return records


def correlation_timing_vs_delta(records, label):
    """Simple Pearson r between mins_placed and fair_delta."""
    pairs = [(r["mins_placed"], r["fair_delta"])
             for r in records if r["mins_placed"] is not None]
    if len(pairs) < 10:
        return
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    dy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    r = num / (dx * dy) if dx * dy else 0
    print(f"\n  [{label}] Pearson r(minutes_before_start, fair_delta) = {r:+.4f}  "
          f"(negative = earlier bets drift more adversely, positive = earlier is better)")


def recommendations(all_records):
    section("RECOMMENDATIONS — Threshold Calibration by Segment")

    # Compute mean adverse drift per (sport, market_type) segment
    by_sm = defaultdict(list)
    for r in all_records:
        by_sm[(r["sport"], r["market_type"])].append(r["fair_delta"])

    print(f"\n  Interpretation: fair_delta is how much the fair prob moved in our favor")
    print(f"  after placement. Negative = market corrected against us (phantom edge).")
    print(f"  Current PROP_MIN_EDGE=4.0 applies to props; moneylines/totals use margin_pp.\n")

    rows = []
    for (sport, mtype), deltas in by_sm.items():
        s = stats(deltas)
        if s["n"] < 5:
            continue
        # Suggested additional edge buffer = 2x abs(mean adverse drift) in prob space
        # Convert prob drift to rough implied odds adjustment for guidance only
        mean_d = s["mean"]
        adverse_rate = s["pct_adverse"]
        suggested_buffer = max(0, -mean_d * 2 * 100)  # as edge % additive
        rows.append((sport, mtype, s["n"], mean_d, adverse_rate, suggested_buffer))

    rows.sort(key=lambda r: r[3])  # worst drift first

    print(f"  {'Sport':<20} {'Type':<12} {'N':>4}  {'mean_delta':>10}  "
          f"{'adv%':>6}  {'+edge_buffer':>12}")
    print(f"  {'-'*20} {'-'*12} {'-'*4}  {'-'*10}  {'-'*6}  {'-'*12}")
    for sport, mtype, n, mean_d, adv_rate, buf in rows:
        flag = " <-- HIGH DRIFT" if mean_d < -0.02 else (" <-- moderate" if mean_d < -0.01 else "")
        print(f"  {sport:<20} {mtype:<12} {n:>4}  {mean_d:>+10.4f}  "
              f"{adv_rate:>5.1f}%  {buf:>+11.2f}%{flag}")


def main():
    print("Loading data...")
    paper_trades = load_jsonl(DATA / "paper_trades.jsonl")
    paper_closes = load_jsonl(DATA / "paper_closes.jsonl")
    real_trades = load_jsonl(DATA / "real_trades.jsonl")
    real_closes = load_jsonl(DATA / "real_closes.jsonl")

    paper_closes_idx = build_close_index(paper_closes)
    real_closes_idx = build_close_index(real_closes)

    print(f"Paper trades: {len(paper_trades)}, closes: {len(paper_closes)}")
    print(f"Real trades:  {len(real_trades)},  closes: {len(real_closes)}")

    paper_records = analyze("PAPER TRADES", paper_trades, paper_closes_idx)
    real_records = analyze("REAL TRADES", real_trades, real_closes_idx)

    all_records = paper_records + real_records

    section("TIMING CORRELATION")
    correlation_timing_vs_delta(paper_records, "paper")
    correlation_timing_vs_delta(real_records, "real")

    section("COMBINED (paper + real)")
    by_sm_all = defaultdict(list)
    for r in all_records:
        by_sm_all[(r["sport"], r["market_type"])].append(r["fair_delta"])
    print(f"\n  By sport x market_type (n>=5), combined:")
    pairs = sorted(by_sm_all, key=lambda k: abs(stats(by_sm_all[k])["mean"]), reverse=True)
    for k in pairs:
        s = stats(by_sm_all[k])
        if s["n"] >= 5:
            fmt(f"{k[0]} / {k[1]}", s)

    recommendations(all_records)


if __name__ == "__main__":
    main()
