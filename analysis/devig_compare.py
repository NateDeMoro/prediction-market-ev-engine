"""
Compare devig methods (multiplicative vs power vs Shin) on the real closing
two-sided Pinnacle lines we captured, to quantify how much a favorite-longshot
correction would actually change our fair probabilities.

use when:
  Deciding whether to swap find_ev_bet's multiplicative devig for power/Shin.
  Read-only; mutates nothing.

Two reports:
  1. Calibration  — Brier + log-loss of each method's fair-YES vs the realized
     YES outcome, over settled bets with a captured close. Lower = the method
     whose probabilities better predict truth. This is the "does it help" test.
  2. Shift        — per-bet favorite-side fair shift (pp) of power/Shin vs
     multiplicative, bucketed by favorite probability. Shows magnitude + that
     the direction matches FLB theory (favorites up, longshots down).

Data: closing prices live in paper_closes.jsonl (yes_side_price_close /
opposite_side_price_close); outcomes in paper_settlements.jsonl. We join on
(book, market_id, side). Closing line, not placement line — placement-time
two-sided prices were not historically persisted (see find_ev_bet instrument).

Run: python3 -m analysis.devig_compare            (uses 1000BetsTracked/)
     DATA=data python3 -m analysis.devig_compare  (uses live data/)
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from statistics import mean

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIR)

from pmev.core.devig import devig_multiplicative, devig_power, devig_shin

DATA_DIR = os.path.join(DIR, os.getenv("DATA", "1000BetsTracked"))
CLOSES = os.path.join(DATA_DIR, "paper_closes.jsonl")
SETTLES = os.path.join(DATA_DIR, "paper_settlements.jsonl")

METHODS = {
    "multiplicative": devig_multiplicative,
    "power": devig_power,
    "shin": devig_shin,
}


def _read(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return rows


def _settlement_index(settlements):
    out = {}
    for s in settlements:
        key = (s.get("book"), s.get("market_id"), s.get("side") or "yes")
        out[key] = s
    return out


def _clip(p, eps=1e-6):
    return min(1.0 - eps, max(eps, p))


def main():
    closes = _read(CLOSES)
    s_idx = _settlement_index(_read(SETTLES))
    print(f"Loaded {len(closes)} closes, {len(s_idx)} settlements from {DATA_DIR}\n")

    # Build the joined sample: each row carries the three methods' fair-YES, the
    # realized YES (0/1), favorite probability, overround, and sport.
    rows = []
    skip_price = skip_devig = skip_settle = 0
    for c in closes:
        yp, op = c.get("yes_side_price_close"), c.get("opposite_side_price_close")
        if yp is None or op is None:
            skip_price += 1
            continue
        fairs = {}
        ok = True
        for name, fn in METHODS.items():
            d = fn([yp, op])
            if d is None:
                ok = False
                break
            fairs[name] = d[0]  # index 0 == yes side
        if not ok:
            skip_devig += 1
            continue

        key = (c.get("book"), c.get("market_id"), c.get("side") or "yes")
        settle = s_idx.get(key)
        result = settle.get("result") if settle else None
        realized = None
        if result in ("yes", "no"):
            realized = 1.0 if result == "yes" else 0.0
        else:
            skip_settle += 1  # pending / void / scalar -> shift report only

        rows.append({
            "fairs": fairs,
            "realized": realized,
            "fav": max(fairs["multiplicative"], 1.0 - fairs["multiplicative"]),
            "sport": c.get("pin_sport") or (settle or {}).get("pin_sport") or "?",
        })

    print(f"Joined {len(rows)} usable closes "
          f"(skipped: {skip_price} no-price, {skip_devig} devig-fail); "
          f"{skip_settle} have no yes/no settlement (shift-only)\n")

    settled = [r for r in rows if r["realized"] is not None]

    # ---------- 1. calibration ----------
    print("=== CALIBRATION (settled bets, fair-YES vs realized YES) ===")
    print(f"  n={len(settled)}   lower Brier / log-loss = better predictor\n")
    base = sum(r["realized"] for r in settled) / len(settled) if settled else 0
    print(f"  base rate (YES resolves): {base:.3%}\n")
    for name in METHODS:
        briers = [(r["fairs"][name] - r["realized"]) ** 2 for r in settled]
        lls = [-(r["realized"] * math.log(_clip(r["fairs"][name]))
                 + (1 - r["realized"]) * math.log(1 - _clip(r["fairs"][name])))
               for r in settled]
        print(f"  {name:14s}  Brier={mean(briers):.5f}  log-loss={mean(lls):.5f}")
    print()

    # ---------- 2. shift magnitude vs multiplicative ----------
    print("=== SHIFT vs multiplicative (favorite-side fair, pp) ===")
    print("  positive = method pushes the favorite UP (longshot down) = FLB fix\n")
    buckets = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.0)]
    header = f"  {'fav prob':12s} {'n':>5s}  {'power Δpp':>11s}  {'shin Δpp':>11s}"
    print(header)
    for lo, hi in buckets:
        grp = [r for r in rows if lo <= r["fav"] < hi]
        if not grp:
            continue
        def favshift(name):
            ds = []
            for r in grp:
                m = r["fairs"]["multiplicative"]
                v = r["fairs"][name]
                # express on the favorite side
                fav_m = m if m >= 0.5 else 1 - m
                fav_v = v if m >= 0.5 else 1 - v
                ds.append((fav_v - fav_m) * 100)
            return mean(ds)
        print(f"  {lo:.2f}-{hi:.2f}    {len(grp):>5d}  "
              f"{favshift('power'):>+11.3f}  {favshift('shin'):>+11.3f}")
    print()

    # by sport (mean absolute favorite shift, magnitude only)
    print("=== mean |power shift| by sport (pp) ===")
    by_sport = defaultdict(list)
    for r in rows:
        m = r["fairs"]["multiplicative"]
        fav_m = m if m >= 0.5 else 1 - m
        fav_p = r["fairs"]["power"] if m >= 0.5 else 1 - r["fairs"]["power"]
        by_sport[r["sport"]].append(abs(fav_p - fav_m) * 100)
    for sport, ds in sorted(by_sport.items(), key=lambda kv: -mean(kv[1])):
        print(f"  {sport:24s} n={len(ds):>4d}  mean|Δ|={mean(ds):.3f}pp")


if __name__ == "__main__":
    main()
