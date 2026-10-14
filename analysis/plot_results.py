"""
Render the README result figures from the paper-trade settlement history.

use when:
  Refreshing docs/figures/ after pulling a new 1000BetsTracked snapshot.
  Read-only on the data; writes PNGs only.

Figures (each in a light and a dark variant for GitHub's theme switch):
  - cumulative_pnl:  realized P&L vs expected P&L at Pinnacle's closing fair,
                     over bets with a captured close, in settlement order.
  - clv_histogram:   per-bet CLV (closing fair - fill price) in percentage points.

Run: python3 -m analysis.plot_results   (requires matplotlib)
"""
from __future__ import annotations

import json
import os
from statistics import mean

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTLEMENTS = os.path.join(DIR, "1000BetsTracked", "paper_settlements.jsonl")
OUT_DIR = os.path.join(DIR, "docs", "figures")

THEMES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "muted": "#52514e",
              "grid": "#e4e3df", "s1": "#2a78d6", "s2": "#eb6834"},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "muted": "#c3c2b7",
             "grid": "#383835", "s1": "#3987e5", "s2": "#d95926"},
}


def _load():
    with open(SETTLEMENTS) as f:
        rows = [json.loads(line) for line in f]
    rows = [r for r in rows if r.get("fair_prob_close") is not None]
    rows.sort(key=lambda r: r["settled_at"])
    return rows


def _style(ax, t):
    ax.set_facecolor(t["surface"])
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["grid"])
    ax.tick_params(colors=t["muted"], labelsize=9)
    ax.grid(axis="y", color=t["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def plot_cumulative(rows, t, path):
    realized, expected = [], []
    r_sum = e_sum = 0.0
    for r in rows:
        r_sum += r["net_pnl"]
        e_sum += r["shares"] * r["fair_prob_close"] - r["stake"]
        realized.append(r_sum)
        expected.append(e_sum)
    x = range(1, len(rows) + 1)

    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=160)
    fig.patch.set_facecolor(t["surface"])
    _style(ax, t)
    ax.axhline(0, color=t["muted"], linewidth=0.8)
    ax.plot(x, expected, color=t["s1"], linewidth=2, label="Expected at closing fair")
    ax.plot(x, realized, color=t["s2"], linewidth=2, label="Realized")
    for series, color in ((expected, t["s1"]), (realized, t["s2"])):
        v = series[-1]
        label = f"−${-v:,.0f}" if v < 0 else f"${v:,.0f}"
        ax.annotate(label, (len(rows), v),
                    xytext=(6, 0), textcoords="offset points",
                    va="center", fontsize=9, color=t["ink"])
        ax.plot(len(rows), series[-1], "o", markersize=4, color=color)
    ax.set_xlim(0, len(rows) * 1.08)
    ax.set_xlabel(f"Settled bets with a captured close (n = {len(rows):,})",
                  color=t["muted"], fontsize=9)
    ax.set_ylabel("Cumulative P&L ($)", color=t["muted"], fontsize=9)
    ax.set_title("Realized P&L vs. closing-line expectation",
                 loc="left", color=t["ink"], fontsize=12, pad=12)
    ax.legend(frameon=False, fontsize=9, labelcolor=t["ink"], loc="upper left")
    fig.tight_layout()
    fig.savefig(path, facecolor=t["surface"])
    plt.close(fig)


def plot_clv(rows, t, path):
    clv = [100 * r["clv"] for r in rows if r.get("clv") is not None]
    avg = mean(clv)
    bins = [b / 2 for b in range(-20, 31)]  # -10 to +15 pp in 0.5 pp bins

    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=160)
    fig.patch.set_facecolor(t["surface"])
    _style(ax, t)
    ax.hist(clv, bins=bins, color=t["s1"], edgecolor=t["surface"], linewidth=1)
    ax.axvline(0, color=t["muted"], linewidth=1, linestyle="--")
    ax.axvline(avg, color=t["ink"], linewidth=1)
    ax.annotate(f"mean +{avg:.1f} pp", (avg, ax.get_ylim()[1] * 0.92),
                xytext=(6, 0), textcoords="offset points",
                fontsize=9, color=t["ink"])
    share = sum(c > 0 for c in clv) / len(clv)
    ax.set_xlabel("CLV per bet (percentage points; closing fair − fill price)",
                  color=t["muted"], fontsize=9)
    ax.set_ylabel("Bets", color=t["muted"], fontsize=9)
    ax.set_title(f"Closing-line value: {share:.0%} of {len(clv):,} bets beat the close",
                 loc="left", color=t["ink"], fontsize=12, pad=12)
    fig.tight_layout()
    fig.savefig(path, facecolor=t["surface"])
    plt.close(fig)


def main():
    rows = _load()
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, t in THEMES.items():
        plot_cumulative(rows, t, os.path.join(OUT_DIR, f"cumulative_pnl_{name}.png"))
        plot_clv(rows, t, os.path.join(OUT_DIR, f"clv_histogram_{name}.png"))
    print(f"wrote 4 figures to {OUT_DIR}")


if __name__ == "__main__":
    main()
