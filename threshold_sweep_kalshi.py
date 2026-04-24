"""
Kalshi-specific threshold sweep.

Compares edge-threshold strategies under the real Kalshi fee structure
(0.07 × P × (1−P) upfront, zero on win). All strategies share the same
RNG seed so trial-by-trial fair/edge/outcome draws are identical — any
difference in final bankroll is attributable to threshold choice alone.

Strategies:
  FLAT n           : accept if edge_pct ≥ n (n ∈ {1.5, 2.0, 2.5, 3.0, 4.0, 5.0})
  BE+m pp          : accept if edge_pct ≥ 100 × 0.07 × (1−px) + m  (m ∈ {0, 0.5, 1.0, 1.5})
  max(2.0, BE+m)   : floor-capped variant; keeps at least a 2% gate

Baseline for Δ comparison: current paper_tracker flat 2.0%.

Output columns:
  Mean / Median / P25 / P75 / Loss% / bets_placed-per-trial / ΔMean% vs baseline

Run: `python3 threshold_sweep_kalshi.py [--num-bets N] [--trials T] [--seed S]`.
"""
import argparse
from typing import Callable

from simulations import SimConfig, run_simulation


KALSHI_RATE = 0.07


def be_plus(margin_pp: float) -> Callable[[float], float]:
    def fn(px):
        return 100.0 * KALSHI_RATE * (1.0 - px) + margin_pp
    return fn


def max_floor(floor_pct: float, margin_pp: float) -> Callable[[float], float]:
    def fn(px):
        return max(floor_pct, 100.0 * KALSHI_RATE * (1.0 - px) + margin_pp)
    return fn


def build_strategies():
    return {
        "FLAT 1.5%":          {"min_edge_pct": 1.5},
        "FLAT 2.0% (baseline)": {"min_edge_pct": 2.0},
        "FLAT 2.5%":          {"min_edge_pct": 2.5},
        "FLAT 3.0%":          {"min_edge_pct": 3.0},
        "FLAT 4.0%":          {"min_edge_pct": 4.0},
        "FLAT 5.0%":          {"min_edge_pct": 5.0},
        "BE+0pp":             {"threshold_fn": be_plus(0.0)},
        "BE+0.5pp":           {"threshold_fn": be_plus(0.5)},
        "BE+1.0pp":           {"threshold_fn": be_plus(1.0)},
        "BE+1.5pp":           {"threshold_fn": be_plus(1.5)},
        "max(2.0, BE+0)":     {"threshold_fn": max_floor(2.0, 0.0)},
        "max(2.0, BE+0.5)":   {"threshold_fn": max_floor(2.0, 0.5)},
        "max(2.0, BE+1.0)":   {"threshold_fn": max_floor(2.0, 1.0)},
    }


def run_sweep(num_bets, num_trials, seed):
    base = dict(
        num_bets=num_bets,
        num_trials=num_trials,
        taker_fee_rate=KALSHI_RATE,
        seed=seed,
    )
    results = {}
    for name, overrides in build_strategies().items():
        cfg = SimConfig(**{**base, **overrides})
        print(f"  running {name} ...", flush=True)
        results[name] = run_simulation(cfg)
    return results


def print_sweep(results, baseline="FLAT 2.0% (baseline)"):
    base = results.get(baseline)
    base_mean = base["mean"] if base else None
    base_median = base["median"] if base else None

    print()
    header = (f"{'Strategy':<22} {'Mean':>10} {'Median':>10} {'P25':>10} "
              f"{'P75':>10} {'Loss%':>7} {'Bets':>7} {'ΔMean%':>8} {'ΔMed%':>8}")
    print(header)
    print("-" * len(header))
    for name, out in results.items():
        mean = out["mean"]
        median = out["median"]
        p25 = out["percentiles"][25]
        p75 = out["percentiles"][75]
        loss = out["loss_rate"] * 100
        bets = out["mean_placed"]
        d_mean = (mean / base_mean - 1) * 100 if base_mean else 0.0
        d_med = (median / base_median - 1) * 100 if base_median else 0.0
        print(f"{name:<22} ${mean:>9,.0f} ${median:>9,.0f} ${p25:>9,.0f} "
              f"${p75:>9,.0f} {loss:>6.1f}% {bets:>7,.0f} "
              f"{d_mean:>+7.2f}% {d_med:>+7.2f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-bets", type=int, default=10000)
    parser.add_argument("--trials", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(f"Kalshi threshold sweep: {args.trials} trials × {args.num_bets} bets, "
          f"seed={args.seed}, fee={KALSHI_RATE} × P × (1-P) upfront\n")
    results = run_sweep(args.num_bets, args.trials, args.seed)
    print_sweep(results)
