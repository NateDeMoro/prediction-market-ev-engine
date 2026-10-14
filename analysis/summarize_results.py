"""
Recompute every figure in the README's Results section from the raw settlement log.

use when:
  Verifying the README numbers, or refreshing them after a new snapshot.
  Read-only; standard library only.

Definitions (match 1000BetsTracked/README.md):
  - CLV = fair_prob_close - avg_fill_price (positive = bought below the close).
  - Expected profit at closing fair = shares * fair_prob_close - stake,
    over bets with a captured close. Stake already includes the venue fee.
  - SD of total P&L under closing fair = sqrt(sum shares^2 * p * (1 - p)),
    p = fair_prob_close, i.e. the spread of outcomes if every bet were priced
    exactly at its closing fair.

Run: python3 -m analysis.summarize_results [path/to/paper_settlements.jsonl]
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from statistics import mean, stdev

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTLEMENTS = os.path.join(DIR, "1000BetsTracked", "paper_settlements.jsonl")


def _load(path):
    with open(path) as f:
        rows = [json.loads(line) for line in f]
    rows.sort(key=lambda r: r["settled_at"])
    return rows


def _usd(x, sign=False):
    """Format dollars as -$353 / +$1,456 rather than $-353 / $+1,456."""
    body = f"${abs(x):,.0f}"
    if x < 0:
        return "-" + body
    return ("+" + body) if sign else body


def _ci95(xs):
    m = mean(xs)
    half = 1.96 * stdev(xs) / math.sqrt(len(xs))
    return m, m - half, m + half


def main():
    rows = _load(sys.argv[1] if len(sys.argv) > 1 else SETTLEMENTS)
    closed = [r for r in rows if r.get("fair_prob_close") is not None]

    staked = sum(r["stake"] for r in rows)
    pnl = sum(r["net_pnl"] for r in rows)
    books = defaultdict(int)
    for r in rows:
        books[r["book"]] += 1

    clv = [r["clv"] for r in closed if r.get("clv") is not None]
    clv_mean, clv_lo, clv_hi = _ci95(clv)
    beat = sum(c > 0 for c in clv) / len(clv)

    exp_close = sum(r["shares"] * r["fair_prob_close"] - r["stake"] for r in closed)
    staked_close = sum(r["stake"] for r in closed)
    sd_close = math.sqrt(sum(r["shares"] ** 2 * r["fair_prob_close"]
                             * (1 - r["fair_prob_close"]) for r in closed))

    running = peak = drawdown = 0.0
    for r in rows:
        running += r["net_pnl"]
        peak = max(peak, running)
        drawdown = min(drawdown, running - peak)

    print("Headline")
    print(f"  Settled bets             {len(rows):,}  "
          + "  ".join(f"{b} {n}" for b, n in sorted(books.items())))
    print(f"  Total staked             ${staked:,.0f}  (avg ${staked / len(rows):,.2f})")
    print(f"  Mean CLV                 {100 * clv_mean:+.2f} pp  "
          f"95% CI [{100 * clv_lo:.2f}, {100 * clv_hi:.2f}]  n = {len(clv):,}")
    print(f"  Beat the closing line    {100 * beat:.1f}%")
    print(f"  Expected at closing fair {_usd(exp_close, True)}  "
          f"({100 * exp_close / staked_close:+.1f}% of stake)")
    print(f"  Realized P&L             {_usd(pnl, True)}  ({100 * pnl / staked:+.1f}% of stake)")
    print(f"  SD of total P&L at close {_usd(sd_close)}  "
          f"95% range [{_usd(exp_close - 1.96 * sd_close, True)}, "
          f"{_usd(exp_close + 1.96 * sd_close, True)}]")
    print(f"  Max drawdown             {_usd(drawdown)}")

    print("\nBy market type")
    groups = defaultdict(list)
    for r in rows:
        groups[r["market_type"]].append(r)
    for mtype, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        roi = sum(r["net_pnl"] for r in rs) / sum(r["stake"] for r in rs)
        c = [r["clv"] for r in rs if r.get("clv") is not None]
        print(f"  {mtype:<12} {len(rs):>5}  ROI {100 * roi:+6.1f}%  "
              f"CLV {100 * mean(c):+.1f} pp")

    sports = defaultdict(int)
    for r in rows:
        sports[r.get("pin_sport")] += 1
    top, n_top = max(sports.items(), key=lambda kv: kv[1])
    print(f"\nLargest sport: {top} ({100 * n_top / len(rows):.0f}% of bets)")


if __name__ == "__main__":
    main()
