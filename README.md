# prediction-market-ev-engine

A multi-book +EV scanner for U.S.-legal sports prediction markets. It treats Pinnacle as the sharp reference book, devigs its 2-way lines into a fair price, then walks the live YES-ask ladder on each soft book under that book's actual fee model to surface positive-expected-value bets.

## What it does

1. Pollers snapshot Pinnacle (the reference) and each soft book (Kalshi, Polymarket US) once per minute to local JSONL files.
2. The matcher pairs each soft-book market with its Pinnacle counterpart across moneyline, spread, total, team-total, and the 1H/2H half-point variants.
3. Pinnacle's 2-way price is devigged multiplicatively to a no-vig fair probability.
4. The scanner walks the soft book's YES-ask ladder, applies the book's fee schedule (Kalshi: 5% on profit; Polymarket US: curved notional fee paid at placement), and computes per-share EV.
5. The dashboard or CLI surfaces the best opportunities; the paper tracker simulates fills with fractional Kelly sizing.

## Layout

| Path | Description |
|------|-------------|
| `pinnacle_poller.py` | Sharp-book poller. Pulls pregame + live matchups across active sports every 60s. |
| `kalshi_poller.py` | Kalshi sports poller (head-to-head, spread, total) for games inside a 6h window. |
| `polymarket_poller.py` | Polymarket US poller (moneyline, spread, total only). |
| `market_matcher.py` | Book-agnostic Pinnacle ↔ soft-book pairing across all market types and periods. |
| `devig_utils.py` | Multiplicative devigging of 2-way Pinnacle lines. |
| `find_ev_bet.py` | One-shot CLI that prints the single best +EV opportunity across all books. |
| `ev_dashboard.py` | Flask dashboard at `127.0.0.1:5055` showing the top 25 EV bets with book/market filters. |
| `paper_tracker.py` | Append-only paper-trading log with Kelly sizing and 30-min settlement polling. |
| `adapters/` | Per-book normalization, ladder fetch, and fee model. New books drop in here. |
| `analysis/` | Bias estimation, simulations, threshold sweeps, latency planning. |
| `diagnostics/` | Match-rate audits and miss-debug helpers for spot-checking the matcher. |
| `scripts/` | Operational utilities: live test orders, paper-bet voids, cleanup. |
| `deploy/` | Hetzner VPS setup script and systemd units for the four long-running services. |
| `legacy/` | Earlier single-book scrape + server, kept for reference only. |

## Getting started

```bash
pip3 install requests flask
cp .env.example .env        # only needed for authenticated Polymarket endpoints
python3 pinnacle_poller.py  # in one shell
python3 kalshi_poller.py    # in another
python3 polymarket_poller.py
python3 ev_dashboard.py     # then open http://127.0.0.1:5055
```

`find_ev_bet.py` is a single-shot alternative that prints the top opportunity and exits.

## Deployment

`deploy/setup.sh` provisions a fresh Ubuntu 24.04 box (user, firewall, Tailscale, Python deps). The four systemd units in `deploy/systemd/` run the three pollers and the dashboard as services. The dashboard is reached over Tailscale; only SSH is exposed publicly.

## Design notes

- The scanner's surfacing window is 0.5h–3h pregame. That is where news-driven line moves on Pinnacle create tradeable lag on soft books without competing against sub-second live arb bots.
- Player props use a higher edge gate (`PROP_MIN_EDGE`, default 4%) because Pinnacle's prop max-stake is $250 vs $7.5k+ on team markets, so prop quotes are noisier.
- Fees are modeled per-book in the adapter, not globally, so adding a book is one file plus a registration in `adapters/__init__.py`.
- Paper-trade state is reconstructed by replaying `data/paper_trades.jsonl` and `data/paper_settlements.jsonl` on import; nothing is mutated in place.
