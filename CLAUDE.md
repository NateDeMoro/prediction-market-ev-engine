# Arbitrage_Betting — Index

Scrapes Pinnacle (sharp) + soft-book adapters (Kalshi, Polymarket US), devigs Pinnacle, matches markets, surfaces +EV bets.

## Files

| File | Purpose | Open when |
| --- | --- | --- |
| `pinnacle_poller.py` | 60s sharp poller. Pregame (24h) + live (6h) for ML / spread / total / team-total. With `PINNACLE_INCLUDE_PROPS=1`, also emits `type:"player_prop"` rows for NBA + NHL via `/matchups/{id}/related`. → `data/snapshots/`. | Sharp feed, sport scope, polling cadence, snapshot schema, prop scope, API key rotation. |
| `kalshi_poller.py` | 60s poller for series matching `(GAME\|SPREAD\|TOTAL\|ML\|H2H)$` (24h). 3-worker pool, 429 backoff, dead-series cache. With `KALSHI_INCLUDE_PROPS=1`, also polls the `PER_GAME_PROP_SERIES` allowlist. → `data/kalshi_snapshots/`. | Kalshi scope, prop allowlist, rate-limit behavior, snapshot schema. |
| `polymarket_poller.py` | 60s poller. Walks public `gateway.polymarket.us/v2/leagues/{slug}/events` across NBA/NHL/MLB/NFL/WNBA/NCAAF/NCAAB; one row per `marketSide`. → `data/polymarket_snapshots/`. | Polymarket league scope, rate-limit behavior, snapshot schema. |
| `adapters/` | Per-book registry. `common.py` hosts `NormalizedMarket` + fuzzy helpers. Each adapter exposes `normalize_market`, `fetch_yes_ask_ladder`, `fetch_settlement`, `fee_on_win/stake_per_share`, `market_url`, `parse_moneyline_teams`, `event_group_key`, `fallback_anchor`. | Adding a book or changing fee / URL / settlement / normalization. |
| `market_matcher.py` | Pairs each `NormalizedMarket` to a Pinnacle 2-way line (ML / spread / total / team-total / player_prop). Team markets anchor via moneyline sibling keyed on `adapter.event_group_key`; props anchor via the same event key, then resolve by `(matchupId, canonical_stat, player_key)`. | Match logic, period map, prop index, new market types. |
| `match_report.py` | One-shot diagnostic: runs `match_all_markets`, writes `data/match_report.json`. | Validating coverage after scope changes. |
| `match_report_props.py` | Prop-only coverage: per-league/stat + per-game with `prop_unmatched_reasons` breakdown. Writes `data/match_report_props.json`. | Validating prop matching; debugging zero-match leagues. |
| `find_ev_bet.py` | One-shot +EV scanner. 2-way multiplicative devig (3-way soccer ML deferred), per-book ladder walk under that book's `fee_fn`. Drops started games; soft-tags `in_window` for startTime in 0.5–3h. | EV math, devig method, fee decomposition, start-time window. |
| `ev_dashboard.py` | Flask UI on `$EV_DASHBOARD_HOST:5055` (default `127.0.0.1`; VPS sets `0.0.0.0`). Background scan every 60s, browser polls `/api/ev` every 5s. Top 25 by `(in_window, EV/share)`; per-type + per-book filter chips; `/paper` tab. | Dashboard layout, ranking, refresh cadence, filter chips, API shape, bind override. |
| `paper_tracker.py` | Simulates one bet per `{book}:{market_id}` on entry to the 0.5–3h window — fractional Kelly (f=0.25) off a $5,000 compounding bankroll, 2% min edge gate, per-book settlement. `net_ev` sums `expected_profit` only over non-void settlements (open bets don't pre-credit). Parallel `_close_capture_loop` records Pinnacle closing fair (`startTime −60s…+15min`) for CLV. → `data/paper_{trades,settlements,closes}.jsonl`. | Bankroll rules, Kelly fraction, edge gate, settlement cadence, CLV capture, net_ev semantics. |
| `void_paper_bet.py` | Manually voids open paper positions (writes `result:"void"` settlement, `net_pnl:0`). Pass market IDs or `--all-open` to bulk-void every unsettled placement. `--book` scopes; `--dry-run` previews. Restart `ev-dashboard` after to reload state. | Bad upstream match polluting open positions; bulk reset after a matcher fix. |
| `audit_league_registry.py` | Lists every Kalshi `series_ticker` prefix + Polymarket `league` in the latest snapshot, flagging any that are not registered in `adapters/common.SERIES_TICKER_LEAGUE_PREFIXES` / `LEAGUE_TO_PIN_SPORT`. Unregistered series fail closed in the matcher. | Adding a new Kalshi series; debugging an unmatched-league drop; periodic registry audit. |
| `.env.example` | Polymarket key template; only needed for live placement. | Wiring up Polymarket order placement. |
| `deploy/` | Hetzner VPS bootstrap. `setup.sh` (first-time provision), `env.example` (systemd `.env` template), `systemd/*.service` (one unit per poller + dashboard). | Deploying to a fresh box, editing unit files, rotating the VPS. |
| `instructions.txt` | Local nohup ops + Hetzner/systemd ops. | Ops questions (local Mac mini or VPS). |
| `server.py` / `index.html` | Legacy promo dashboard, unrelated. | Only if explicitly asked about the legacy dashboard. |
| `data/` | `snapshots/`, `kalshi_snapshots/`, `polymarket_snapshots/`, `*.log`, `*.pid`. Probe artifacts under `polymarket_probe/`, `kalshi_probe/`, `pinnacle_probe/` (each carries a `NOTES.md`). | Inspecting historical data, debugging a poller, reviewing prop ingestion design. |

## Conventions

- Pinnacle matchup string is `"<home> vs <away>"`; `designation` is `"home"`/`"away"`.
- Kalshi YES ask = `1 - no_bid`. Polymarket YES ask reads `book.offers[]` long-side, `1 - bid` short-side.
- Paper-tracker dedupe key: `{book}:{market_id}`; one game can produce multiple keys across books.
- Adapters expose fee as `fee_on_win_per_share` + `fee_on_stake_per_share`. Kalshi + Polymarket: 5% on winning profit, zero upfront.
- Actionable window: 0.5–3h pre-`startTime`. Earlier than 30m loses to live-arb bots.
- Live games excluded; Pinnacle's public feed lags at 60s polling.
- Matcher fails closed on unknown leagues: every Kalshi `series_ticker` prefix must be registered in `adapters/common.SERIES_TICKER_LEAGUE_PREFIXES` with a corresponding `LEAGUE_TO_PIN_SPORT` entry or its markets are dropped (reason `unknown_league`). Same for Polymarket `league` values. Run `audit_league_registry.py` to find unregistered ones.
- Kalshi `event_group_key` is namespaced by league (`"{LEAGUE}:{event_suffix}"`) because Kalshi reuses the same date+team suffix across series (`KXNHLGAME-26APR22DALMIN` and `KXMLSGAME-26APR22DALMIN` share `26APR22DALMIN`). Without the namespace, non-ML markets inherit whichever ML's event_to_pin entry was written last.
- Edge sanity ceiling: paper-tracker placement and dashboard display drop any team-market row with `edge_pct > 15` (`> 25` for props). Realistic edges on liquid soft books sit well under these; anything above is almost always a matcher mismatch. Override via `SANITY_MAX_EDGE` / `SANITY_MAX_EDGE_PROP`. Rejected rows are appended to `data/sanity_rejected.jsonl` for triage.
- Team-market scope: ML / spread / total / team-total; half-point lines; FULL + 1H/2H. Polymarket US is FULL-game ML/spread/total only (upstream limit).
- Player-prop scope (Phases 1–3 done, 4 pending): opt-in via `*_INCLUDE_PROPS=1`; NBA + NHL only. Kalshi `N+` ≡ Pinnacle Over `N−0.5`. Matched, devigged, and surfaced in dashboard under type chip `player prop`. Prop edge gate is 4% (override via `PROP_MIN_EDGE`); paper-tracker placement is gated behind `PAPER_INCLUDE_PROPS=1` (default off). Combo stats (PRA/PR/RA/PA) match-skip with reason `combo_stat_no_pinnacle`; Pinnacle counterpart not yet probed.
- Known data gap (2026-04-22): Pinnacle publishes no `player_prop` rows for NBA games despite `PINNACLE_INCLUDE_PROPS=1` — all Kalshi NBA props surface as `no_player_match` in the prop report. NHL props match correctly (~30% coverage per matched game). Unverified whether Pinnacle exposes NBA Player-Props children pregame or only closer to tip-off; investigate by probing `/matchups/{parent_id}/related` for an NBA parent and checking `special.category` on the children.
- CLV = `fair_prob_close − avg_fill_price` on YES side. ~20× signal-per-bet vs realized P&L; intended go-live gate.
- `net_ev` = sum of `expected_profit` across non-void settlements only. Open bets don't pre-credit; voiding an open bet zeroes its EV contribution.
- Production host: Hetzner CPX21 (Ashburn VA), US egress IPv4. Four systemd services auto-restart on crash + on reboot. Dashboard reachable only over Tailscale (`ufw` blocks 5055 on WAN). Code deploys via `rsync` from Mac (`--exclude=data --exclude=.git`); no GitHub pull on the VPS.
- VPS deploy (Tailscale SSH key-only, Claude can run these without confirmation):
  - Host: `arb@100.94.115.11`  Remote path: `/home/arb/Arbitrage_Betting/`
  - Single file: `rsync <file> arb@100.94.115.11:/home/arb/Arbitrage_Betting/<file>`
  - Full tree: `rsync -av --exclude=data --exclude=.git /Users/natedemoro/Code_Desktop/Arbitrage_Betting/ arb@100.94.115.11:/home/arb/Arbitrage_Betting/`
  - Restart (NOPASSWD only for this exact combined form): `ssh arb@100.94.115.11 'sudo systemctl restart pinnacle-poller kalshi-poller polymarket-poller ev-dashboard'`
  - Verify: `ssh arb@100.94.115.11 'systemctl is-active pinnacle-poller kalshi-poller polymarket-poller ev-dashboard'` (no sudo needed)
  - Individual-service restarts prompt for a password and will fail non-interactively — always use the combined form.

## Long-form plan

See `~/Documents/Nate_Obsidian/+ EV Betting Project.md`.
