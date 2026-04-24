
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# Per-bucket edge-pct distribution fit from 94 VPS paper trades.
# Format: (fair_lo, fair_hi) -> (mean_pct, std_pct, lo_pct, hi_pct). Used to
# draw edge_pct conditional on the fair prob bucket — a low-fair bet in
# practice carries a much bigger edge_pct than a high-fair bet.
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
    num_bets: int = 10000         # bets per trial
    num_trials: int = 1000         # independent Monte Carlo trials
    kelly_fraction: float = 0.25
    # Parametric threshold schedule: min_edge_pct(p) = threshold_a + threshold_b / p.
    # Set threshold_b = 0 for a flat threshold equal to threshold_a (legacy behavior).
    threshold_a: float = 2.0
    threshold_b: float = 0.0
    # Fair-prob distribution fit from 94 VPS paper trades. Heavily skewed
    # toward underdog YES legs, so we draw from a truncated normal.
    fair_prob_mean: float = 0.280
    fair_prob_std: float = 0.119
    fair_prob_low: float = 0.13
    fair_prob_high: float = 0.68
    # Edge_pct is drawn conditional on the fair-prob bucket (sampler c).
    edge_pct_buckets: dict = field(default_factory=lambda: dict(DEFAULT_EDGE_PCT_BY_BUCKET))
    # Calibration bias: if >0, our "fair" is overconfident by this much, so
    # the true YES probability is fair_prob - calibration_bias. Leave at 0
    # to model perfectly calibrated fair probs.
    calibration_bias: float = 0.0
    ruin_threshold: float = 0.10   # bankroll fraction below which we call it ruin
    # 5% fee on winning profit (Kalshi / Polymarket US). Applied to winning
    # pnl AND baked into Kelly `b` so full-Kelly matches paper_tracker.
    fee_on_win: float = 0.05
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


def _draw_edge_pct_per_bucket(rng, fair_arr, buckets):
    """Per-fair-bucket truncated-normal draw of edge_pct (%). Vectorized:
    groups fair values by bucket, fills each group's samples in one draw.
    Fair values outside all bucket ranges fall into the nearest bucket."""
    out = np.empty_like(fair_arr)
    bucket_list = sorted(buckets.items(), key=lambda kv: kv[0][0])
    assigned = np.zeros_like(fair_arr, dtype=bool)
    for (lo, hi), (mean, std, lo_cap, hi_cap) in bucket_list:
        mask = (fair_arr >= lo) & (fair_arr < hi) & (~assigned)
        n = int(mask.sum())
        if n:
            out[mask] = _draw_truncated_normal(rng, n, mean, std, lo_cap, hi_cap)
            assigned |= mask
    # Unassigned: fair outside any bucket. Use nearest.
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


# ---------------------------------------------------------------------------
# Single-trial and Monte Carlo
# ---------------------------------------------------------------------------

def run_trial(cfg: SimConfig, rng: np.random.Generator):
    """Simulate one trial of up to cfg.num_bets bets. Returns summary dict."""
    fair = _draw_truncated_normal(
        rng, cfg.num_bets, cfg.fair_prob_mean, cfg.fair_prob_std,
        cfg.fair_prob_low, cfg.fair_prob_high,
    )
    edge_pct = _draw_edge_pct_per_bucket(rng, fair, cfg.edge_pct_buckets)
    # edge_pct = (fair - price) / price * 100  =>  price = fair / (1 + edge_pct/100)
    price = fair / (1.0 + edge_pct / 100.0)

    true_p = np.clip(fair - cfg.calibration_bias, 1e-9, 1.0 - 1e-9)
    outcomes = rng.uniform(size=cfg.num_bets) < true_p

    # Per-bet threshold: A + B/p. Handles flat thresholds (B=0) cheaply.
    thresholds = cfg.threshold_a + cfg.threshold_b / np.clip(fair, 1e-3, None)

    bankroll = cfg.initial_bankroll
    peak = bankroll
    min_ratio = 1.0  # bankroll / running_peak, tracked for max drawdown
    placed = wins = 0
    total_stake = 0.0
    realized_pnl_by_bucket = {}  # fair-prob bucket -> [stake, pnl, count, wins]

    for i in range(cfg.num_bets):
        if bankroll <= 0:
            break
        if edge_pct[i] < thresholds[i]:
            continue
        p, px = fair[i], price[i]
        if px <= 0 or px >= 1:
            continue
        # Fee-adjusted Kelly odds: winner nets (1-px)*(1-fee), loser loses px.
        b = (1.0 - px) * (1.0 - cfg.fee_on_win) / px
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

        if bankroll > peak:
            peak = bankroll
        if peak > 0 and bankroll / peak < min_ratio:
            min_ratio = bankroll / peak

        bucket = int(p * 10) / 10.0
        slot = realized_pnl_by_bucket.setdefault(bucket, [0.0, 0.0, 0, 0])
        slot[0] += stake
        slot[1] += pnl
        slot[2] += 1
        slot[3] += 1 if outcomes[i] else 0

    roi_pct = (bankroll - cfg.initial_bankroll) / total_stake * 100.0 if total_stake > 0 else 0.0
    # Mean log growth per bet across the full fixed horizon (skipped bets
    # count as zero growth — this penalizes over-tight thresholds).
    if bankroll > 0:
        log_growth_per_bet = float(np.log(bankroll / cfg.initial_bankroll) / cfg.num_bets)
    else:
        log_growth_per_bet = float("-inf")

    return {
        "final_bankroll": bankroll,
        "placed": placed,
        "wins": wins,
        "hit_rate_pct": wins / placed * 100.0 if placed else 0.0,
        "total_stake": total_stake,
        "pnl": bankroll - cfg.initial_bankroll,
        "roi_pct": roi_pct,
        "bust": bankroll <= 0,
        "max_drawdown": 1.0 - min_ratio,
        "log_growth_per_bet": log_growth_per_bet,
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

    log_growth = np.array([t["log_growth_per_bet"] for t in trials])
    log_growth_finite = log_growth[np.isfinite(log_growth)]
    max_dd = np.array([t["max_drawdown"] for t in trials])

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
        "mean_log_growth": float(log_growth_finite.mean()) if len(log_growth_finite) else float("-inf"),
        "median_log_growth": float(np.median(log_growth_finite)) if len(log_growth_finite) else float("-inf"),
        "mean_max_drawdown": float(max_dd.mean()),
        "p95_max_drawdown": float(np.percentile(max_dd, 95)),
        "bucket_stats": bucket_stats,
    }


# ---------------------------------------------------------------------------
# Threshold schedule sweep
# ---------------------------------------------------------------------------

# Reference guideline schedule from the post the user cited. Keyed by fair
# prob, values are min_edge_pct. We fit A + B/p against this to compare
# against optimum.
POST_SCHEDULE = {
    0.9: 1.2, 0.8: 1.4, 0.7: 1.8, 0.6: 2.2, 0.5: 2.6,
    0.4: 3.2, 0.3: 3.0, 0.2: 5.5, 0.1: 9.0,
}


def fit_post_schedule(schedule: dict = POST_SCHEDULE):
    """Least-squares fit of min_edge_pct = A + B/p to a discrete schedule."""
    p = np.array(list(schedule.keys()))
    e = np.array(list(schedule.values()))
    X = np.vstack([np.ones_like(p), 1.0 / p]).T
    (A, B), *_ = np.linalg.lstsq(X, e, rcond=None)
    return float(A), float(B)


def sweep_threshold_schedule(a_values, b_values, base: Optional[SimConfig] = None):
    """2-d sweep over (threshold_a, threshold_b). Returns list of rows
    sorted by mean_log_growth descending."""
    base = base or SimConfig()
    base_kwargs = {k: v for k, v in base.__dict__.items()
                   if k not in ("threshold_a", "threshold_b")}

    rows = []
    for a in a_values:
        for b in b_values:
            cfg = SimConfig(**base_kwargs, threshold_a=a, threshold_b=b)
            out = run_simulation(cfg)
            rows.append({
                "threshold_a": a,
                "threshold_b": b,
                "mean_log_growth": out["mean_log_growth"],
                "median_log_growth": out["median_log_growth"],
                "median_final": out["median_final"],
                "p05": out["p05"],
                "mean_max_drawdown": out["mean_max_drawdown"],
                "p95_max_drawdown": out["p95_max_drawdown"],
                "loss_rate": out["loss_rate"],
                "mean_placed": out["mean_placed"],
            })
    rows.sort(key=lambda r: r["mean_log_growth"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def _threshold_label(a, b):
    if b == 0:
        return f"flat {a:.2f}%"
    return f"{a:+.2f} + {b:.2f}/p"


def _print_run(name, out, cfg):
    print(f"{name}  (threshold: {_threshold_label(cfg.threshold_a, cfg.threshold_b)})")
    print(f"  Mean log-growth / bet: {out['mean_log_growth']*1e4:>7.2f} bps")
    print(f"  Median final:  ${out['median_final']:>10,.0f}   "
          f"P05: ${out['p05']:>10,.0f}")
    print(f"  Mean max drawdown: {out['mean_max_drawdown']*100:>5.1f}%   "
          f"P95: {out['p95_max_drawdown']*100:>5.1f}%   "
          f"Loss rate: {out['loss_rate']*100:>4.1f}%")
    print(f"  Mean bets placed / trial: {out['mean_placed']:>5.0f} of {cfg.num_bets}")


def _print_threshold_curves(a, b):
    """Show min_edge_pct at representative fair probs for a given schedule."""
    probs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 0.9]
    vals = [f"{p:.1f}:{a + b/p:4.1f}%" for p in probs]
    print(f"    {_threshold_label(a, b):>18} -> " + "  ".join(vals))


def _print_sweep_top(rows, top=10):
    print(f"\nTop {top} (A, B) cells by mean log-growth / bet:")
    print(f"    {'A':>6} {'B':>6} {'logg(bps)':>10} {'medF$':>10} "
          f"{'p05$':>10} {'meanDD%':>8} {'p95DD%':>8} {'loss%':>7} {'bets':>6}")
    for r in rows[:top]:
        print(f"    {r['threshold_a']:>6.2f} {r['threshold_b']:>6.2f} "
              f"{r['mean_log_growth']*1e4:>10.2f} {r['median_final']:>10,.0f} "
              f"{r['p05']:>10,.0f} {r['mean_max_drawdown']*100:>7.1f}% "
              f"{r['p95_max_drawdown']*100:>7.1f}% {r['loss_rate']*100:>6.1f}% "
              f"{r['mean_placed']:>6.0f}")


def sweep_with_bias(bias_values, a_values, b_values, base: Optional[SimConfig] = None):
    """For each calibration_bias level, sweep (A, B). Returns
    {bias: sorted_rows}."""
    base = base or SimConfig()
    results = {}
    for bias in bias_values:
        biased_base = SimConfig(**{**base.__dict__, "calibration_bias": bias})
        results[bias] = sweep_threshold_schedule(a_values, b_values, base=biased_base)
    return results


if __name__ == "__main__":
    base = SimConfig(seed=42, num_trials=500)
    a_values = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0]
    b_values = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
    bias_values = [0.000, 0.010, 0.020, 0.030, 0.050]

    # ---------- Reference: legacy flat 2% under zero bias
    flat_cfg = SimConfig(**{**base.__dict__, "threshold_a": 2.0, "threshold_b": 0.0})
    flat_out = run_simulation(flat_cfg)
    _print_run("Flat 2%, no bias (legacy reference)", flat_out, flat_cfg)

    post_a, post_b = fit_post_schedule()
    print(f"\nPost-schedule fit to A + B/p:  A={post_a:+.2f}, B={post_b:.2f}")
    print("Implied B for additive calibration bias δ: B ≈ 100·δ.")
    print(f"Post's B=0.86 implies assumed δ ≈ 0.0086 (i.e. ~0.9% fair overconfidence).")

    # ---------- Bias sweep
    print(f"\nSweeping calibration bias x (A, B):  "
          f"{len(bias_values)} biases × {len(a_values)}×{len(b_values)} "
          f"= {len(bias_values)*len(a_values)*len(b_values)} cells...")
    all_results = sweep_with_bias(bias_values, a_values, b_values, base=base)

    # ---------- Per-bias top results
    for bias, rows in all_results.items():
        print(f"\n--- calibration_bias = {bias:.3f} "
              f"({'perfectly calibrated' if bias == 0 else f'fair overshoot by {bias*100:.1f}pp'}) ---")
        # Find baseline (A=0, B=0) row for this bias, and the optimum
        flat_row = next(r for r in rows if r["threshold_a"] == 0 and r["threshold_b"] == 0)
        best = rows[0]
        print(f"  flat A=0, B=0:  log-g {flat_row['mean_log_growth']*1e4:+6.2f} bps, "
              f"medF ${flat_row['median_final']:>6,.0f}, "
              f"DD {flat_row['mean_max_drawdown']*100:>4.1f}%, "
              f"bets {flat_row['mean_placed']:>4.0f}")
        print(f"  OPTIMUM (A={best['threshold_a']:+.2f}, B={best['threshold_b']:.2f}): "
              f"log-g {best['mean_log_growth']*1e4:+6.2f} bps, "
              f"medF ${best['median_final']:>6,.0f}, "
              f"DD {best['mean_max_drawdown']*100:>4.1f}%, "
              f"bets {best['mean_placed']:>4.0f}")
        print(f"  Top 5 cells:")
        print(f"    {'A':>5} {'B':>5} {'logg(bps)':>10} {'medF$':>8} "
              f"{'p05$':>8} {'DD%':>6} {'bets':>6}")
        for r in rows[:5]:
            print(f"    {r['threshold_a']:>5.1f} {r['threshold_b']:>5.1f} "
                  f"{r['mean_log_growth']*1e4:>10.2f} "
                  f"{r['median_final']:>8,.0f} {r['p05']:>8,.0f} "
                  f"{r['mean_max_drawdown']*100:>5.1f}% {r['mean_placed']:>6.0f}")

    # ---------- Threshold curves for optima at each bias level
    print("\nOptimal threshold curves (min_edge_pct %) at each bias level:")
    probs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 0.9]
    print(f"    {'bias':>6}  {'(A,B)':>12}    " + "  ".join(f"p={p:.1f}" for p in probs))
    for bias, rows in all_results.items():
        best = rows[0]
        a, b = best["threshold_a"], best["threshold_b"]
        vals = [f" {a + b/p:5.2f}" for p in probs]
        print(f"    {bias:>6.3f}  ({a:+.1f},{b:.1f})   " + " ".join(vals))
    # Show the post's recommendation alongside for comparison
    a, b = post_a, post_b
    vals = [f" {a + b/p:5.2f}" for p in probs]
    print(f"    {'post':>6}  ({a:+.1f},{b:.2f})   " + " ".join(vals))
