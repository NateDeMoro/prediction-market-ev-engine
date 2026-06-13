# prediction-market-ev-engine

A multi-book +EV scanner for U.S.-legal sports prediction markets. It treats Pinnacle as the sharp reference book, devigs its 2-way lines into a fair price, then walks the live YES-ask ladder on each soft book (Kalshi, Polymarket US) under that book's actual fee model to surface positive-expected-value bets. It paper-trades every signal and, when explicitly enabled, auto-places real money under hard risk caps.

## What it does

1. Three pollers snapshot each book to JSONL files under `data/` — Pinnacle every 30s (its snapshot is the only price-accuracy risk), the soft books every 60s. Each snapshot gets a `.meta.json` sidecar recording cycle/write timing and the cycle's 429 count.
2. The matcher pairs each soft-book market with its Pinnacle counterpart across moneyline, spread, total, team-total, the 1H/2H half-point variants, and player props.
3. Pinnacle's 2-way price is devigged multiplicatively to a no-vig fair probability.
4. The scanner walks the soft book's YES-ask ladder, applies the book's fee rate (`config.PER_BOOK_FEE_RATE`: Kalshi 7%, Polymarket 3%), and computes per-share EV against a per-(book, market-type) edge floor.
5. The dashboard or CLI surfaces the best opportunities; the paper tracker simulates fills with fractional-Kelly sizing, and the real tracker optionally places live orders.

## Layout

| Path | Description |
|------|-------------|
| `config.py` | Single source of truth for every tunable: market enablement, edge thresholds, fee rates, bankroll/risk caps, timing, feature flags, file paths. |
| `pinnacle_poller.py` | Sharp-book poller. Pregame + live matchups across active sports every 30s. |
| `kalshi_poller.py` | Kalshi sports poller (moneyline, spread, total, team-total, props) inside a 24h window. |
| `polymarket_poller.py` | Polymarket US poller — full-game moneyline / spread / total only. |
| `data_utils.py` | Shared poller plumbing: logging, atomic writes, snapshot rotation + `.meta.json` sidecars, SIGTERM/shutdown, cycle pacing, the shared `RateGate` (min-interval spacing + 429 backoff), and snapshot-freshness checks. |
| `market_matcher.py` | Book-agnostic Pinnacle ↔ soft-book pairing across all market types and periods. |
| `devig_utils.py` | Multiplicative devigging of 2-way Pinnacle lines. |
| `find_ev_bet.py` | One-shot CLI that prints the single best +EV opportunity across all books. |
| `ev_dashboard.py` | Flask dashboard at `127.0.0.1:5055`. Tabs: Live EV (`/`), Paper (`/paper`), Real Trading (`/real`). A Poll-time column reads each book's sidecar so stale-line decisions trace back to the cycle that produced them. |
| `paper_tracker.py` | Append-only paper-trading sim: Kelly sizing, settlement polling, CLV capture, replay. |
| `real_tracker.py` | Real-money tracker. $1000 bankroll, per-book balances, $30/bet cap, -$100 daily halt, gated on `REAL_TRADING_ENABLED`. |
| `adapters/` | Per-book normalization, ladder fetch, fee model, and authenticated trade clients (`*_trade.py`). New books drop in here and register in `__init__.py`. |
| `analysis/` | Bias estimation, simulations, haircut backtests, threshold sweeps, latency planning. |
| `diagnostics/` | Match-rate audits and miss-debug helpers for spot-checking the matcher. |
| `1000BetsTracked/` | Static snapshot of paper/real trade history pulled from the VPS, for offline backtesting. |
| `deploy/` | Ubuntu 24.04 VPS setup script and the four systemd units (three pollers + dashboard). |
| `legacy/` | Earlier single-book scrape + server, kept for reference only. |

## Getting started

```bash
pip3 install requests flask
cp deploy/env.example .env   # set PINNACLE_API_KEY (required); others optional
python3 pinnacle_poller.py   # in one shell
python3 kalshi_poller.py     # in another
python3 polymarket_poller.py
python3 ev_dashboard.py      # then open http://127.0.0.1:5055
```

`find_ev_bet.py` is a single-shot alternative that prints the top opportunity and exits. Real-money trading additionally requires `KALSHI_*` / `POLYMARKETUS_*` keys and `REAL_TRADING_ENABLED=1`; see `deploy/env.example` for the full variable set.

## Deployment

`deploy/setup.sh` provisions a fresh Ubuntu 24.04 box (user, firewall, Tailscale, Python deps). The four systemd units in `deploy/systemd/` run the three pollers and the dashboard as services, reading config from an `EnvironmentFile`. The dashboard is reached over Tailscale; only SSH is exposed publicly. Production state lives at `~/Arbitrage_Betting` on the VPS — the local repo is for editing code only.

## Design notes

- The scanner's surfacing window is 0.5h–3h pregame (`config.MIN_HOURS_TO_START` / `MAX_HOURS_TO_START`) — where news-driven line moves on Pinnacle create tradeable lag on soft books without competing against sub-second live-arb bots.
- Edge floors are per-(book, market-type) in `config.EDGE_FLOOR_PCT`. Player props use a higher gate (`PROP_MIN_EDGE`, default 4%) than team markets (2%) because Pinnacle's prop max-stake is small, so prop quotes are noisier.
- Fees are modeled once in `config.PER_BOOK_FEE_RATE` and consumed by both the edge formula and the read adapters; change a rate in one place.
- Snapshots are age-gated before any signal is acted on, two-tier by role: Pinnacle's fair value is read straight from its snapshot, so it is gated tight (`MAX_PIN_SNAPSHOT_AGE_SEC`, 45s); soft ladders are re-fetched live at decision time, so their snapshot age is only a coverage/liveness check and is gated loose (`MAX_SOFT_SNAPSHOT_AGE_SEC`, 120s). The scanner and dashboard share this check via `data_utils.stale_snapshot_reason`.
- Market enablement is config-gated via `config.market_enabled()`. Soccer moneylines (both books) and Kalshi basketball moneylines are off after backtesting; soccer is further restricted to a 14-league whitelist (`adapters/common.py` `SHARP_SOCCER_LEAGUES`).
- Real-money trading is off by default and gated on `REAL_TRADING_ENABLED=1`, with a $30 per-bet cap and a -$100 daily-loss halt.
- All tracker state is append-only JSONL; in-memory state is reconstructed by replaying it on import — nothing is mutated in place.
