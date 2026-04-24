"""
Monte Carlo bankroll simulation for the paper-tracker strategy.

Default run reports the percentile breakdown of final bankrolls across
`num_trials` independent runs of `num_bets` bets each. Pass
`--bankroll X --bets N` to score an in-progress position: the sim runs with
num_bets=N and reports how many standard deviations your bankroll X is
from the simulated mean (plus percentile rank).

Fair-prob and edge-pct distributions are fit from the observed paper
trades; calibration bias is fixed at the measured +0.6pp fair overshoot.
"""
import argparse
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np


# Per-bucket edge-pct distribution fit from 94 VPS paper trades.
# (fair_lo, fair_hi) -> (mean_pct, std_pct, lo_cap_pct, hi_cap_pct).
DEFAULT_EDGE_PCT_BY_BUCKET = {
    (0.00, 0.20): (11.95, 3.36, 4.0, 22.0),
    (0.20, 0.30): (7.92, 2.27, 3.0, 18.0),
    (0.30, 0.40): (6.63, 2.14, 3.0, 14.0),
    (0.40, 0.50): (7.64, 2.47, 3.0, 16.0),
    (0.50, 1.00): (4.85, 1.18, 2.5, 10.0),
}


@dataclass
class SimConfig:
    initial_bankroll: float = 5000.0
    num_bets: int = 5000
    num_trials: int = 1000
    kelly_fraction: float = 0.25
    min_edge_pct: float = 2.0
    # Optional price-aware threshold: `threshold_fn(px)` returns the per-bet
    # minimum edge_pct to clear. If set, overrides `min_edge_pct`. Used by the
    # threshold sweep to compare flat vs. break-even-relative strategies.
    threshold_fn: Optional[Callable[[float], float]] = None
    # Fair-prob distribution fit from 94 VPS paper trades (underdog-skewed).
    fair_prob_mean: float = 0.280
    fair_prob_std: float = 0.119
    fair_prob_low: float = 0.13
    fair_prob_high: float = 0.68
    edge_pct_buckets: dict = field(default_factory=lambda: dict(DEFAULT_EDGE_PCT_BY_BUCKET))
    # Measured fair-overconfidence bias: +0.6pp from n=52 CLV pairs. True YES
    # prob on a bet is (fair - calibration_bias).
    calibration_bias: float = 0.006
    # Taker fee = rate * P * (1-P) charged upfront on stake (not on profit).
    # Matches Kalshi (0.07) and Polymarket sports (0.03) schedules. Default 0.05
    # is a blended midpoint; set to 0.07 for Kalshi-only, 0.03 for Polymarket-only.
    taker_fee_rate: float = 0.05
    seed: Optional[int] = None


def _draw_truncated_normal(rng, n, mean, std, lo, hi):
    out = np.empty(n)
    filled = 0
    while filled < n:
        draw = rng.normal(mean, std, size=max(n * 2, 256))
        draw = draw[(draw >= lo) & (draw <= hi)]
        take = min(len(draw), n - filled)
        out[filled:filled + take] = draw[:take]
        filled += take
    return out


def _draw_edge_pct_per_bucket(rng, fair_arr, buckets):
    out = np.empty_like(fair_arr)
    bucket_list = sorted(buckets.items(), key=lambda kv: kv[0][0])
    assigned = np.zeros_like(fair_arr, dtype=bool)
    for (lo, hi), (mean, std, lo_cap, hi_cap) in bucket_list:
        mask = (fair_arr >= lo) & (fair_arr < hi) & (~assigned)
        n = int(mask.sum())
        if n:
            out[mask] = _draw_truncated_normal(rng, n, mean, std, lo_cap, hi_cap)
            assigned |= mask
    if not assigned.all():
        below = fair_arr < bucket_list[0][0][0]
        above = fair_arr >= bucket_list[-1][0][1]
        for mask, params in [(below & ~assigned, bucket_list[0][1]),
                             (above & ~assigned, bucket_list[-1][1])]:
            n = int(mask.sum())
            if n:
                mean, std, lo_cap, hi_cap = params
                out[mask] = _draw_truncated_normal(rng, n, mean, std, lo_cap, hi_cap)
                assigned |= mask
    return out


def run_trial(cfg: SimConfig, rng: np.random.Generator):
    fair = _draw_truncated_normal(
        rng, cfg.num_bets, cfg.fair_prob_mean, cfg.fair_prob_std,
        cfg.fair_prob_low, cfg.fair_prob_high,
    )
    edge_pct = _draw_edge_pct_per_bucket(rng, fair, cfg.edge_pct_buckets)
    price = fair / (1.0 + edge_pct / 100.0)
    true_p = np.clip(fair - cfg.calibration_bias, 1e-9, 1.0 - 1e-9)
    outcomes = rng.uniform(size=cfg.num_bets) < true_p

    bankroll = cfg.initial_bankroll
    placed = wins = 0
    total_stake = 0.0

    for i in range(cfg.num_bets):
        if bankroll <= 0:
            break
        p, px = fair[i], price[i]
        if px <= 0 or px >= 1:
            continue
        min_edge = cfg.threshold_fn(px) if cfg.threshold_fn is not None else cfg.min_edge_pct
        if edge_pct[i] < min_edge:
            continue
        # Upfront-fee Kelly: shares cost (px + fee) out-of-pocket; winner receives
        # $1/share, loser receives $0. b = net-win-per-stake-dollar.
        fee = cfg.taker_fee_rate * px * (1.0 - px)
        eff_cost = px + fee
        if eff_cost >= 1.0:
            continue
        b = (1.0 - eff_cost) / eff_cost
        f_full = p - (1.0 - p) / b
        if f_full <= 0:
            continue
        stake = bankroll * f_full * cfg.kelly_fraction
        if stake <= 0:
            continue

        placed += 1
        total_stake += stake
        if outcomes[i]:
            bankroll += stake * b
            wins += 1
        else:
            bankroll -= stake

    return {
        "final_bankroll": bankroll,
        "placed": placed,
        "wins": wins,
        "total_stake": total_stake,
    }


def run_simulation(cfg: SimConfig):
    rng = np.random.default_rng(cfg.seed)
    trials = [run_trial(cfg, rng) for _ in range(cfg.num_trials)]
    finals = np.array([t["final_bankroll"] for t in trials])
    placed = np.array([t["placed"] for t in trials])
    wins = np.array([t["wins"] for t in trials])

    pcts = [1, 5, 10, 25, 50, 75, 90, 95, 99]
    return {
        "finals": finals,
        "mean": float(finals.mean()),
        "std": float(finals.std()),
        "median": float(np.median(finals)),
        "percentiles": {p: float(np.percentile(finals, p)) for p in pcts},
        "mean_placed": float(placed.mean()),
        "mean_wins": float(wins.mean()),
        "loss_rate": float((finals < cfg.initial_bankroll).mean()),
    }


def _print_distribution(cfg: SimConfig, out: dict):
    print(f"\n=== Simulation: {cfg.num_trials} trials x {cfg.num_bets} bets ===")
    print(f"  Starting bankroll:  ${cfg.initial_bankroll:,.0f}")
    print(f"  Kelly fraction:     {cfg.kelly_fraction}")
    print(f"  Min edge:           {cfg.min_edge_pct:.2f}%")
    print(f"  Calibration bias:   {cfg.calibration_bias*100:.2f} pp (fair overconfidence)")
    print(f"  Taker fee:          {cfg.taker_fee_rate:.3f} × P × (1-P) upfront")
    print()
    print(f"  Mean final:         ${out['mean']:>10,.0f}")
    print(f"  Std final:          ${out['std']:>10,.0f}")
    print(f"  Median final:       ${out['median']:>10,.0f}")
    print(f"  Mean bets placed:   {out['mean_placed']:>10,.0f} / {cfg.num_bets}")
    print(f"  Mean wins:          {out['mean_wins']:>10,.0f}")
    print(f"  Loss rate:          {out['loss_rate']*100:>9.1f}%")
    print()
    print("  Percentile breakdown of final bankroll:")
    for p, v in out["percentiles"].items():
        pnl = v - cfg.initial_bankroll
        marker = "  <-- median" if p == 50 else ""
        print(f"    P{p:>2}:  ${v:>10,.0f}   (pnl {pnl:+11,.0f}){marker}")


def score_position(bankroll: float, bets_completed: int, base_cfg: SimConfig):
    cfg_dict = {**base_cfg.__dict__, "num_bets": bets_completed}
    cfg = SimConfig(**cfg_dict)
    out = run_simulation(cfg)
    finals = out["finals"]
    mean, std = out["mean"], out["std"]
    z = (bankroll - mean) / std if std > 0 else 0.0
    pct_rank = float((finals < bankroll).mean() * 100)
    pnl = bankroll - cfg.initial_bankroll

    print(f"\n=== Position score: ${bankroll:,.2f} after {bets_completed} bets ===")
    print(f"  Simulated mean:     ${mean:>10,.0f}")
    print(f"  Simulated std:      ${std:>10,.0f}")
    print(f"  Simulated median:   ${out['median']:>10,.0f}")
    print(f"  Your bankroll:      ${bankroll:>10,.2f}   (pnl {pnl:+,.2f})")
    print(f"  Z-score:            {z:+.2f}σ from mean")
    print(f"  Percentile rank:    {pct_rank:.1f}% of trials did worse than you")
    print()
    print("  Reference percentiles at this bet count:")
    for p, v in out["percentiles"].items():
        pnl_p = v - cfg.initial_bankroll
        here = "  <-- you" if (p == 50 and abs(z) < 0.01) else ""
        print(f"    P{p:>2}:  ${v:>10,.0f}   (pnl {pnl_p:+11,.0f}){here}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bankroll", type=float,
                        help="Current bankroll to score against the simulated distribution.")
    parser.add_argument("--bets", type=int,
                        help="Bets completed so far (required with --bankroll).")
    parser.add_argument("--trials", type=int, default=SimConfig.num_trials, help="Monte Carlo trials.")
    parser.add_argument("--num-bets", type=int, default=SimConfig.num_bets,
                        help="Bets per trial (ignored in scoring mode).")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed.")
    args = parser.parse_args()

    if (args.bankroll is None) != (args.bets is None):
        parser.error("--bankroll and --bets must be supplied together")

    if args.bankroll is not None:
        cfg = SimConfig(seed=args.seed, num_trials=args.trials)
        score_position(args.bankroll, args.bets, cfg)
    else:
        cfg = SimConfig(seed=args.seed, num_trials=args.trials, num_bets=args.num_bets)
        out = run_simulation(cfg)
        _print_distribution(cfg, out)
