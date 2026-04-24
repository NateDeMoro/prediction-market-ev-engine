# Arbitrage_Betting — Index

Scrapes Pinnacle (sharp) + soft-book adapters (Kalshi, Polymarket US), devigs Pinnacle, matches markets, surfaces +EV bets.

## Files

| File | Purpose | Open when |
| --- | --- | --- |
| `pinnacle_poller.py` | 60s sharp poller. Pregame (24h) + live (6h) ML/spread/total/team-total. `PINNACLE_INCLUDE_PROPS=1` adds NBA+NHL player_prop via `/matchups/{id}/related`. → `data/snapshots/`. | Sharp feed, sport scope, polling cadence, prop scope, API key rotation. |
| `kalshi_poller.py` | 60s poller on `(GAME\|SPREAD\|TOTAL\|ML\|H2H)$` series (24h). 3-worker pool, 429 backoff, dead-series cache. `KALSHI_INCLUDE_PROPS=1` adds `PER_GAME_PROP_SERIES` allowlist. → `data/kalshi_snapshots/`. | Kalshi scope, prop allowlist, rate-limit behavior. |
| `polymarket_poller.py` | 60s poller. Walks `gateway.polymarket.us/v2/leagues/{slug}/events` across NBA/NHL/MLB/NFL/WNBA/NCAAF/NCAAB; one row per `marketSide`. → `data/polymarket_snapshots/`. | Polymarket league scope, rate-limit behavior. |
| `adapters/` | Per-book registry. `common.py` hosts `NormalizedMarket` + team/stat/league helpers. Each adapter exposes `normalize_market`, `fetch_{yes,no,both}_ladder(s)`, `fetch_settlement`, `fee_on_{win,stake}_per_share`, `taker_fee_per_share`, `market_url`, `parse_moneyline_teams`, `event_group_key`, `fallback_anchor`, `SUPPORTS_NO_SIDE`. | Adding a book; fee/URL/settlement/normalization changes; NO-side behavior. |
| `market_matcher.py` | Pairs each `NormalizedMarket` to a Pinnacle line (2-way or 3-way). Team markets anchor via ML sibling keyed on `adapter.event_group_key`; props resolve by `(matchupId, canonical_stat, player_key)`. 3-way soccer ML: `yes_sub_title ∈ {tie,draw,tied,drawn}` → `yes_designation="draw"`; opposite is synthesized via `synthesize_combined_american` of the other two legs and tagged `opposite_designation="not_{home,away,draw}"`. Fails closed on unknown league. | Match logic, period map, prop index, 3-way ML handling, `unknown_league` drops. |
| `match_report*.py` | One-shot match-coverage diagnostics. Writes `data/match_report*.json`. | Validating coverage after scope changes. |
| `find_ev_bet.py` | One-shot +EV scanner. 2-way multiplicative devig on `[yes_price, opposite_price]` (3-way soccer ML collapses to this shape via synthesized combined-NO in the matcher). Per-book ladder walk under that book's `fee_fn`. Drops started games; tags `in_window` for 0.5–3h. | EV math, devig method, start-time window. |
| `ev_dashboard.py` | Flask UI on `$EV_DASHBOARD_HOST:5055` (default `127.0.0.1`; VPS `0.0.0.0`). Background scan every 60s, browser polls `/api/ev` every 5s. Top 25 by `(in_window, EV/share)`; type/book/side filter chips; `/paper` tab. | Dashboard layout, ranking, filter chips, API shape, bind override. |
| `paper_tracker.py` | Simulates one bet per `{book}:{market_id}:{side}` on entry to 0.5–3h window. Fractional Kelly (f=0.25) off $5000 compounding bankroll. Per-book price-aware gate: `max(MIN_EDGE_PCT, 100 × fee_rate × (1-px) + margin_pp)` — `PER_BOOK_FEE_RATE` / `PER_BOOK_EDGE_MARGIN_PP` drive it. YES/NO independent. Closes captured in side-perspective. → `data/paper_{trades,settlements,closes}.jsonl`. | Bankroll rules, Kelly fraction, edge gate, CLV capture, net_ev semantics, side-aware dedupe. |
| `void_paper_bet.py` | Manually voids open paper positions (`result:"void"`, `net_pnl:0`). `--all-open` / `--book` / `--side` / `--dry-run`. Restart `ev-dashboard` after. | Bad upstream match; bulk reset after matcher fix. |
| `simulations.py` | Monte Carlo bankroll sim for the paper-tracker strategy. Fair/edge distributions fit from paper history; Kelly uses upfront `rate × P × (1-P)`. `SimConfig.threshold_fn(px)` enables price-aware-threshold studies. | Threshold studies, bankroll percentile projections, scoring a live position (`--bankroll X --bets N`). |
| `threshold_sweep_kalshi.py` | Kalshi MC sweep over threshold strategies (flat vs. BE+margin). Drives `PER_BOOK_EDGE_MARGIN_PP["kalshi"]`. | Revalidating margin after fee or bias shifts; template for Polymarket sweep. |
| `place_kalshi_test_order.py` | Standalone POC CLI: one authenticated Kalshi limit order via RSA-PSS-SHA256 on `/portfolio/orders`. `--count ≤ 5`; `--dry-run` signs without sending. Requires `cryptography`. | Manual test order; reference for Kalshi signing; seed for `adapters/kalshi_trade.py`. |
| `audit_league_registry.py` | Lists Kalshi `series_ticker` prefixes + Polymarket `league` values in latest snapshots, flagging unregistered ones. | Adding series/league; debugging `unknown_league` drops. |
| `deploy/` | Hetzner VPS bootstrap: `setup.sh`, `env.example`, `systemd/*.service`. | Provisioning/rotating VPS, editing unit files. |
| `instructions.txt` | Local nohup + Hetzner/systemd ops. | Ops questions. |
| `data/` | `snapshots/`, `*_snapshots/`, paper JSONL, logs, pid. Probes under `{polymarket,kalshi,pinnacle}_probe/` (each with `NOTES.md`). | Inspecting history, debugging a poller. |
| `.env.example`, `server.py`, `index.html` | Polymarket key template; legacy promo dashboard (unrelated). | Polymarket live placement; don't touch promo dashboard unless asked. |

## Conventions

- Pinnacle matchup: `"<home> vs <away>"`; `designation` ∈ `{home, away, draw}` (`draw` only on 3-way soccer ML).
- Both adapters charge `rate × P × (1-P)` upfront on stake, zero on win. Kalshi rate 0.07; Polymarket sports rate 0.03 (crypto 0.072 out-of-scope). Makers pay zero; no rebate on Polymarket sports.
- Both adapters are NO-side capable (`SUPPORTS_NO_SIDE=True`); `fetch_both_ladders` serves both from one book call. Polymarket moneyline emits only `{slug}:long` — the sibling team is reached via NO-side rather than a duplicate `{slug}:short` (both sides share one book, so dual emission would produce exact-price duplicate paper bets). Polymarket spread/total were already single-sided (favored / Over only), so NO-side is the only path to dog/under exposure.
- Scanner emits up to 2 rows per ticker (YES + NO). Dashboard prefixes NO `selection` with `NO ` and exposes a `side` chip group. Dual-listed game markets (`SEA-YES` + `STL-NO` both express "SEA wins") size independently — mild over-exposure accepted.
- Paper-tracker dedupe key: `{book}:{market_id}:{side}`. Legacy two-part keys replay as `side=yes`.
- Actionable window: 0.5–3h pre-`startTime`. Earlier than 30m loses to live-arb bots. Live games excluded (Pinnacle public feed lags at 60s).
- Matcher fails closed on unknown leagues: every Kalshi `series_ticker` prefix must be in `adapters/common.SERIES_TICKER_LEAGUE_PREFIXES` with a `LEAGUE_TO_PIN_SPORT` entry, else dropped with `unknown_league`. Same for Polymarket `league`. Run `audit_league_registry.py` to find them.
- Kalshi `event_group_key` is `"{LEAGUE}:{event_suffix}"` — Kalshi reuses date+team suffixes across series (NHL + MLS can share `26APR22DALMIN`), so non-ML markets would otherwise inherit whichever ML was written last.
- Edge sanity ceiling: team rows with `edge_pct > 15` (`> 25` for props) are dropped. Overrides: `SANITY_MAX_EDGE`, `SANITY_MAX_EDGE_PROP`. Rejects → `data/sanity_rejected.jsonl`.
- Team-market scope: ML / spread / total / team-total; half-point lines; FULL + 1H/2H. Polymarket US is FULL-game ML/spread/total only.
- 3-way soccer ML (Kalshi-only as of 2026-04-24): matcher produces one `MatchedPair` per outcome (home/away/draw). `yes_designation ∈ {home,away,draw}`; `opposite_side_price` is a synthesized American via `synthesize_combined_american` of the other two Pinnacle legs (max ~0.03pp rounding drift vs full 3-way devig). `opposite_designation = "not_{home,away,draw}"` is a sentinel — paper-tracker `_find_pin_prices` detects the `not_` prefix at CLV capture and re-synthesizes against the 3 current Pinnacle prices. Polymarket soccer ML draw support pending a live snapshot.
- Player-prop scope: opt-in via `*_INCLUDE_PROPS=1`; NBA + NHL only. Kalshi `N+` ≡ Pinnacle Over `N−0.5`. Prop edge floor 4% (`PROP_MIN_EDGE`); paper placement gated behind `PAPER_INCLUDE_PROPS=1` (default off). Combo stats (PRA/PR/RA/PA) match-skip with `combo_stat_no_pinnacle`.
- Prop-match ceiling is structural, not a matcher bug: Pinnacle publishes ONE line per (player, stat); Kalshi publishes a 3–4 strike ladder (e.g. points 9.5/14.5/19.5/24.5). Only Kalshi strikes exactly equal to Pinnacle's line match — ~18% of viable strikes. Accent-strip + 2-char NHL abbrev fallback (TB/NJ/SJ/LA) close the remaining fixable gaps; `player_found_but_stat_mismatch` drops are Pinnacle simply not offering that stat for that player.
- CLV = `fair_prob_close − avg_fill_price` on YES side (NO rows flipped to side-perspective on capture). ~20× signal-per-bet vs realized P&L; intended go-live gate.
- `net_ev` = Σ `expected_profit` across non-void settlements only. Open bets don't pre-credit; voiding zeroes EV contribution.

## VPS deploy (Tailscale SSH, key-only; Claude may run without confirmation)

- Host: `arb@100.94.115.11`  Remote path: `/home/arb/Arbitrage_Betting/`
- Single file: `rsync <file> arb@100.94.115.11:/home/arb/Arbitrage_Betting/<file>`
- Full tree: `rsync -av --exclude=data --exclude=.git /Users/natedemoro/Code_Desktop/Arbitrage_Betting/ arb@100.94.115.11:/home/arb/Arbitrage_Betting/`
- Restart (NOPASSWD only in this combined form): `ssh arb@100.94.115.11 'sudo systemctl restart pinnacle-poller kalshi-poller polymarket-poller ev-dashboard'`
- Verify (no sudo): `ssh arb@100.94.115.11 'systemctl is-active pinnacle-poller kalshi-poller polymarket-poller ev-dashboard'`
- Individual-service restarts prompt for a password and fail non-interactively — always use the combined form.

## Long-form plan

See `~/Documents/Nate_Obsidian/+ EV Betting Project.md`.
