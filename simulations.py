
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class SimConfig:
    initial_bankroll: float = 5000.0
    num_bets: int = 1000           # bets per trial
    num_trials: int = 1000         # independent Monte Carlo trials
    kelly_fraction: float = 0.25
    min_edge_pct: float = 2.0      # expected_profit / stake, percent
    fair_prob_low: float = 0.10
    fair_prob_high: float = 0.90
    edge_mean: float = 0.035
    edge_std: float = 0.0075
    edge_min: float = 0.02
    edge_max: float = 0.05
    # Calibration bias: if >0, our "fair" is overconfident by this much, so
    # the true YES probability is fair_prob - calibration_bias. Leave at 0
    # to model perfectly calibrated fair probs.
    calibration_bias: float = 0.0
    ruin_threshold: float = 0.10   # bankroll fraction below which we call it ruin
    seed: Optional[int] = None


# ---------------------------------------------------------------------------
# Draw helpers
# ---------------------------------------------------------------------------

def _draw_truncated_normal(rng, n, mean, std, lo, hi):
    """Rejection-sample a truncated normal. Fast for moderately tight bounds."""
    out = np.empty(n)
    filled = 0
    while filled < n:
        draw = rng.normal(mean, std, size=max(n * 2, 256))
        draw = draw[(draw >= lo) & (draw <= hi)]
        take = min(len(draw), n - filled)
        out[filled:filled + take] = draw[:take]
        filled += take
    return out


# ---------------------------------------------------------------------------
# Single-trial and Monte Carlo
# ---------------------------------------------------------------------------

def run_trial(cfg: SimConfig, rng: np.random.Generator):
    """Simulate one trial of up to cfg.num_bets bets. Returns summary dict."""
    fair = rng.uniform(cfg.fair_prob_low, cfg.fair_prob_high, size=cfg.num_bets)
    edge = _draw_truncated_normal(
        rng, cfg.num_bets, cfg.edge_mean, cfg.edge_std, cfg.edge_min, cfg.edge_max
    )
    price = fair - edge
    # edge_pct = expected_profit/stake = (fair - price) / price = edge / price
    edge_pct = edge / price * 100.0

    true_p = np.clip(fair - cfg.calibration_bias, 1e-9, 1.0 - 1e-9)
    outcomes = rng.uniform(size=cfg.num_bets) < true_p

    bankroll = cfg.initial_bankroll
    placed = wins = 0
    total_stake = 0.0
    realized_pnl_by_bucket = {}  # fair-prob bucket -> [stake, pnl, count]

    for i in range(cfg.num_bets):
        if bankroll <= 0:
            break
        if edge_pct[i] < cfg.min_edge_pct:
            continue
        p, px = fair[i], price[i]
        if px <= 0 or px >= 1:
            continue
        b = (1.0 - px) / px
        f_full = p - (1.0 - p) / b
        if f_full <= 0:
            continue

        stake = bankroll * f_full * cfg.kelly_fraction
        if stake <= 0:
            continue

        placed += 1
        total_stake += stake
        if outcomes[i]:
            pnl = stake * b
            wins += 1
        else:
            pnl = -stake
        bankroll += pnl

        bucket = int(p * 10) / 10.0   # 0.1, 0.2, ..., 0.8
        slot = realized_pnl_by_bucket.setdefault(bucket, [0.0, 0.0, 0, 0])
        slot[0] += stake
        slot[1] += pnl
        slot[2] += 1
        slot[3] += 1 if outcomes[i] else 0

    roi_pct = (bankroll - cfg.initial_bankroll) / total_stake * 100.0 if total_stake > 0 else 0.0
    return {
        "final_bankroll": bankroll,
        "placed": placed,
        "wins": wins,
        "hit_rate_pct": wins / placed * 100.0 if placed else 0.0,
        "total_stake": total_stake,
        "pnl": bankroll - cfg.initial_bankroll,
        "roi_pct": roi_pct,
        "bust": bankroll <= 0,
        "by_bucket": realized_pnl_by_bucket,
    }


def run_simulation(cfg: SimConfig):
    rng = np.random.default_rng(cfg.seed)
    trials = [run_trial(cfg, rng) for _ in range(cfg.num_trials)]
    finals = np.array([t["final_bankroll"] for t in trials])
    pnls = np.array([t["pnl"] for t in trials])
    placed = np.array([t["placed"] for t in trials])
    ruin_cut = cfg.initial_bankroll * cfg.ruin_threshold

    # Aggregate bucket stats across trials (stake-weighted ROI).
    agg = {}
    for t in trials:
        for k, (stk, pnl, n, w) in t["by_bucket"].items():
            slot = agg.setdefault(k, [0.0, 0.0, 0, 0])
            slot[0] += stk
            slot[1] += pnl
            slot[2] += n
            slot[3] += w
    bucket_stats = {
        k: {
            "bets": n,
            "wins": w,
            "hit_rate_pct": w / n * 100.0 if n else 0.0,
            "stake": stk,
            "pnl": pnl,
            "roi_pct": pnl / stk * 100.0 if stk > 0 else 0.0,
        }
        for k, (stk, pnl, n, w) in sorted(agg.items())
    }

    return {
        "config": cfg,
        "mean_final": finals.mean(),
        "median_final": float(np.median(finals)),
        "std_final": finals.std(),
        "p05": float(np.percentile(finals, 5)),
        "p25": float(np.percentile(finals, 25)),
        "p75": float(np.percentile(finals, 75)),
        "p95": float(np.percentile(finals, 95)),
        "ruin_rate": float((finals < ruin_cut).mean()),
        "loss_rate": float((finals < cfg.initial_bankroll).mean()),
        "bust_rate": float(np.array([t["bust"] for t in trials]).mean()),
        "mean_pnl": float(pnls.mean()),
        "mean_placed": float(placed.mean()),
        "bucket_stats": bucket_stats,
    }


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

def grid_search(kelly_fractions=None, min_edges=None, base: Optional[SimConfig] = None):
    """Sweep Kelly fraction x min-edge threshold. Returns list of summary rows."""
    kelly_fractions = kelly_fractions or [0.10, 0.25, 0.50, 1.00]
    min_edges = min_edges or [0.0, 1.0, 2.0, 3.0, 4.0]
    base = base or SimConfig()

    rows = []
    for kf in kelly_fractions:
        for me in min_edges:
            cfg = SimConfig(
                **{**base.__dict__, "kelly_fraction": kf, "min_edge_pct": me}
            )
            out = run_simulation(cfg)
            rows.append({
                "kelly_fraction": kf,
                "min_edge_pct": me,
                "mean_final": out["mean_final"],
                "median_final": out["median_final"],
                "p05": out["p05"],
                "p95": out["p95"],
                "ruin_rate": out["ruin_rate"],
                "loss_rate": out["loss_rate"],
                "mean_placed": out["mean_placed"],
            })
    return rows


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def _print_baseline(out, cfg):
    print(f"Baseline  Kelly={cfg.kelly_fraction}  min_edge={cfg.min_edge_pct}%  "
          f"bets/trial={cfg.num_bets}  trials={cfg.num_trials}")
    print(f"  Mean   final: ${out['mean_final']:>10,.0f}")
    print(f"  Median final: ${out['median_final']:>10,.0f}")
    print(f"  P05/P95:      ${out['p05']:>10,.0f}  /  ${out['p95']:>10,.0f}")
    print(f"  Loss rate (<initial):      {out['loss_rate']*100:>5.1f}%")
    print(f"  Ruin rate (<10% initial):  {out['ruin_rate']*100:>5.1f}%")
    print(f"  Mean bets placed/trial:    {out['mean_placed']:>5.0f}")
    print()
    print("  By fair-prob bucket (stake-weighted across all trials):")
    print(f"    {'bucket':>8} {'bets':>10} {'hit%':>8} {'roi%':>8}")
    for k, s in out["bucket_stats"].items():
        print(f"    {k:>8.1f} {s['bets']:>10,} {s['hit_rate_pct']:>7.1f}% {s['roi_pct']:>7.2f}%")


def _print_grid(rows):
    print(f"\nGrid search  (columns: kelly, min_edge%, mean, median, p05, p95, ruin%, loss%)")
    print(f"{'kelly':>6} {'edge%':>6} {'mean$':>10} {'med$':>10} {'p05$':>10} "
          f"{'p95$':>10} {'ruin%':>7} {'loss%':>7}")
    for r in rows:
        print(f"{r['kelly_fraction']:>6.2f} {r['min_edge_pct']:>6.1f} "
              f"{r['mean_final']:>10,.0f} {r['median_final']:>10,.0f} "
              f"{r['p05']:>10,.0f} {r['p95']:>10,.0f} "
              f"{r['ruin_rate']*100:>6.1f}% {r['loss_rate']*100:>6.1f}%")


if __name__ == "__main__":
    cfg = SimConfig(seed=42)
    out = run_simulation(cfg)
    _print_baseline(out, cfg)

    rows = grid_search(base=SimConfig(seed=42, num_trials=500))
    _print_grid(rows)
