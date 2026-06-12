"""Estimate fair-prob calibration bias from paper_closes vs paper_trades.

Joins each placed trade to its closing Pinnacle fair (captured by
paper_tracker within startTime -60s..+15min) and reports:
  - Mean entry_fair - close_fair: positive = we overshoot Pinnacle close
  - Per-book and per-fair-bucket bias
  - CLV (close_fair - avg_fill_price): positive = we bought below close

Use the mean bias as `calibration_bias` in simulations.py to ground the
threshold-schedule sweep in realized calibration rather than a guess.

Run:  python3 estimate_bias.py  [--trades PATH --closes PATH]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

import numpy as np


def _load_jsonl(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _close_key(r: dict) -> tuple | None:
    # Schema drift: older rows used `ticker`, newer ones use `market_id`.
    # fair_prob_close is stored in side-perspective, so the key must include
    # side — otherwise YES and NO closes for the same market collide.
    mid = r.get("market_id") or r.get("ticker")
    if mid is None:
        return None
    book = r.get("book") or "kalshi"
    return (book, mid, r.get("side") or "yes")


def _book(t: dict) -> str:
    return t.get("book") or ("kalshi" if t.get("ticker") else "?")


def estimate(trades_path: str, closes_path: str) -> dict:
    trades = _load_jsonl(trades_path)
    closes = _load_jsonl(closes_path)

    closes_by_id: dict[tuple, dict] = {}
    for c in closes:
        k = _close_key(c)
        if k is None:
            continue
        prev = closes_by_id.get(k)
        if prev is None or c["captured_at"] > prev["captured_at"]:
            closes_by_id[k] = c

    pairs = []
    for t in trades:
        c = closes_by_id.get(_close_key(t))
        if c is None:
            continue
        pairs.append({
            "entry": t["fair_prob"],
            "close": c["fair_prob_close"],
            "price": t["avg_fill_price"],
            "book": _book(t),
        })

    if not pairs:
        return {"n_trades": len(trades), "n_closes": len(closes), "n_pairs": 0}

    entry = np.array([p["entry"] for p in pairs])
    close = np.array([p["close"] for p in pairs])
    price = np.array([p["price"] for p in pairs])
    books = np.array([p["book"] for p in pairs])
    diff = entry - close

    sem = diff.std() / np.sqrt(len(diff))
    result = {
        "n_trades": len(trades),
        "n_closes": len(closes),
        "n_pairs": len(pairs),
        "bias_mean": float(diff.mean()),
        "bias_median": float(np.median(diff)),
        "bias_std": float(diff.std()),
        "bias_sem": float(sem),
        "bias_ci95_lo": float(diff.mean() - 1.96 * sem),
        "bias_ci95_hi": float(diff.mean() + 1.96 * sem),
        "bias_t_stat": float(diff.mean() / sem) if sem > 0 else float("nan"),
        "clv_mean": float((close - price).mean()),
        "clv_median": float(np.median(close - price)),
        "by_book": {},
        "by_bucket": {},
    }

    for b in sorted(set(books)):
        mask = books == b
        d = diff[mask]
        if len(d):
            result["by_book"][b] = {
                "n": int(len(d)),
                "mean": float(d.mean()),
                "std": float(d.std()),
            }

    for lo, hi in [(0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 1.0)]:
        mask = (entry >= lo) & (entry < hi)
        d = diff[mask]
        if len(d):
            result["by_bucket"][f"{lo:.1f}-{hi:.1f}"] = {
                "n": int(len(d)),
                "mean": float(d.mean()),
                "median": float(np.median(d)),
            }
    return result


def _print(res: dict) -> None:
    print(f"Trades: {res['n_trades']}   Closes: {res['n_closes']}   "
          f"Matched pairs: {res['n_pairs']}")
    if res["n_pairs"] == 0:
        print("No matched pairs — cannot estimate bias.")
        return

    print()
    print("Calibration bias (entry_fair - close_fair):")
    print(f"  mean:    {res['bias_mean']*100:+.2f} pp")
    print(f"  median:  {res['bias_median']*100:+.2f} pp")
    print(f"  std:     {res['bias_std']*100:.2f} pp")
    print(f"  SEM:     {res['bias_sem']*100:.2f} pp")
    print(f"  95% CI:  [{res['bias_ci95_lo']*100:+.2f}, {res['bias_ci95_hi']*100:+.2f}] pp")
    print(f"  t-stat:  {res['bias_t_stat']:+.2f}")
    print()
    print(f"CLV (close_fair - fill_price):")
    print(f"  mean:    {res['clv_mean']*100:+.2f} pp   "
          f"median: {res['clv_median']*100:+.2f} pp")
    print()
    print("By book:")
    for b, s in res["by_book"].items():
        print(f"  {b:12s} n={s['n']:>3}  mean={s['mean']*100:+.2f}pp  std={s['std']*100:.2f}pp")
    print()
    print("By entry-fair bucket:")
    for k, s in res["by_bucket"].items():
        print(f"  p in [{k}):  n={s['n']:>3}  mean={s['mean']*100:+.2f}pp  "
              f"median={s['median']*100:+.2f}pp")
    print()
    print(f"Suggested calibration_bias for simulations.py: {res['bias_mean']:.4f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trades", default="data/paper_trades.jsonl")
    ap.add_argument("--closes", default="data/paper_closes.jsonl")
    args = ap.parse_args()
    _print(estimate(args.trades, args.closes))
