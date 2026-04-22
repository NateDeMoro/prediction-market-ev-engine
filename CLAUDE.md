# Arbitrage_Betting — Index

Scrapes Pinnacle (sharp) and every registered soft-book adapter
(Kalshi + Polymarket US today), devigs Pinnacle, matches markets,
and surfaces +EV bets.

## Files

| File | Purpose | Open when |
| --- | --- | --- |
| `pinnacle_poller.py` | 60s poller for Pinnacle odds: pregame (next 24h) + live (last 6h). Snapshot rows carry `sport/matchupId/matchup/startTime/isLive/period/type/prices` plus `side` on team-total rows. Writes `data/snapshots/*.jsonl`. | Changing sharp feed, sport scope, polling cadence, snapshot schema, or Pinnacle API key rotation. |
| `kalshi_poller.py` | 60s poller for Kalshi sports series ending in GAME/SPREAD/TOTAL/ML/H2H (24h window) — catches full-game, 1H/2H, TEAMTOTAL variants. 3-worker pool, 429 backoff, dead-series cache. Writes `data/kalshi_snapshots/*.jsonl`. | Changing Kalshi scope, rate-limit behavior, or snapshot schema. |
| `polymarket_poller.py` | 60s poller for Polymarket US. Fetches `gateway.polymarket.us/v2/leagues/{slug}/events` across NBA/NHL/MLB/NFL/WNBA/NCAAF/NCAAB, flattens each event into one row per `marketSide`. 4-worker pool; gateway is public (no auth). Writes `data/polymarket_snapshots/*.jsonl`. | Changing Polymarket league scope, rate-limit behavior, or snapshot schema. |
| `adapters/` | Per-book adapter registry. `common.py` hosts `NormalizedMarket` + fuzzy helpers. `kalshi.py` / `polymarket.py` each expose `normalize_market`, `fetch_yes_ask_ladder`, `fetch_settlement`, `taker_fee_per_share`, `fee_on_win_per_share`, `fee_on_stake_per_share`, `market_url`, `parse_moneyline_teams`, `event_group_key`, `fallback_anchor`. `__init__.py` lazy-loads via `adapter_for(book)` / `all_adapters()`. | Adding a new book, changing a book's fee model, URL format, settlement semantics, or normalization rules. |
| `market_matcher.py` | Book-agnostic. Consumes `list[NormalizedMarket]`; pairs each with its Pinnacle 2-way line across moneyline / spread / total / team-total (FULL + 1H/2H half-point only). Non-mainlines resolve their Pinnacle matchup via the moneyline sibling keyed on `adapter.event_group_key`. `MatchedPair.market` carries the NormalizedMarket; period map is FULL/1H/2H (NHL thirds and MLB innings out of scope). | Changing match logic, the FULL/1H/2H period map, or adding additional market types. |
| `match_report.py` | Diagnostic one-shot: loads Pinnacle + every adapter's latest snapshot, runs `match_all_markets`, prints per-market-type + per-book coverage plus per-series unmatched samples, writes `data/match_report.json`. | Validating match coverage after scope changes or investigating why a book/league fails to match. |
| `find_ev_bet.py` | One-shot +EV scanner. Iterates all adapters to load soft-book snapshots, runs `match_all_markets`, devigs per-line (2-way multiplicative; 3-way soccer ML still deferred pending Shin), walks each book's YES-ask ladder under that book's own `fee_fn` (Kalshi: 5% of profit; Polymarket: same model until a real fill confirms). Strictly drops started games; soft-tags `in_window` for startTime in 0.5-3h. Candidate rows carry `book/market_id/market_url` plus generic `yes_side_price`/`opposite_side_price`. | Changing EV math, devig method, fee decomposition, start-time window, or YES-side resolution. |
| `ev_dashboard.py` | Flask dashboard on `http://127.0.0.1:5055`. Background thread scans every 60s via `find_matches`; UI polls `/api/ev` every 5s. Shows top 25 ranked by `(in_window, EV/share)` with per-type and per-book filter-chip rows and a book badge on each market cell. Market-id column links to `adapter.market_url`. Paper tab renders Bankroll / Net P&L / Net EV / Avg CLV / ROI tiles and settled-row columns for Close % + CLV (pp). | Changing dashboard layout, columns, ranking, refresh cadence, filter chips, API shape, or paper-tab UI. |
| `paper_tracker.py` | Simulated-bet tracker. Places a paper bet the first time a `(book, market_id)` key enters the 0.5-3h in-window bucket, sized by fractional Kelly (f=0.25) off a compounding $5,000 bankroll shared across books, gated by 2% minimum edge. Dedupes by `{book}:{market_id}`. Settlement dispatches to `adapter.fetch_settlement`. A parallel `_close_capture_loop` fires in the `startTime −60s…+15min` window to locate each open position's Pinnacle row (keyed on `pin_matchup_id` + period + market_type + designation + line), devig it with the same 2-way multiplicative method as the live scanner, and persist `fair_prob_close` to `data/paper_closes.jsonl`. Legacy records without a `book` field default to `"kalshi"` on replay; legacy settlements missing matchup/selection or CLV are backfilled from their placement + close at replay time. Appends `data/paper_trades.jsonl` + `data/paper_settlements.jsonl` + `data/paper_closes.jsonl`. | Changing paper-bankroll rules, Kelly fraction, edge threshold, settlement cadence, CLV capture window, or per-book settlement semantics. |
| `.env.example` | Template for `POLYMARKET_US_KEY_ID` / `POLYMARKET_US_SECRET`. Credentials are only required for authenticated trading; the read pipeline (events, order book, settlement) uses the public gateway. | Wiring up live order placement on Polymarket. |
| `instructions.txt` | nohup start/check/stop commands for the pollers. | Ops questions about running the scrapers. |
| `server.py` / `index.html` | Older unrelated betting dashboard. | Only if explicitly asked about the legacy dashboard. |
| `data/` | `snapshots/` (Pinnacle), `kalshi_snapshots/`, `polymarket_snapshots/`, `*.log`, `*.pid`, `polymarket_probe/` (one-off API spike artifacts). | Inspecting historical data or debugging a poller. |

## Conventions

- Pinnacle matchup string is `"<home> vs <away>"`; `designation` on price objects is `"home"` or `"away"`.
- Kalshi YES ask is derived as `1 - no_bid` from the NO resting-bid book; Polymarket YES ask on the long side reads `book.offers[]` directly and on the short side uses `1 - bid` (single book covers both sides of a two-sided market).
- Paper-tracker dedupe key is `{book}:{market_id}`; a single game may generate multiple keys across books.
- Fee decomposition: each adapter exposes `fee_on_win_per_share(price)` + `fee_on_stake_per_share(price)` so Kelly sizing and settlement P&L stay book-agnostic. Kalshi and Polymarket both charge 5% on winning profit, zero upfront.
- Actionable window: 0.5-3h before Pinnacle `startTime`. Late-pregame news moves the sharp line in discrete jumps while soft-book MMs lag; earlier than 30m loses to live-arb bots.
- Live games are excluded because Pinnacle's public feed lags at 60s polling (Live Center is US-geoblocked).
- Scope: moneyline, spread, total, team-total; half-point lines only; FULL + 1H/2H. Polymarket US publishes only FULL-game ML/spread/total today (no halves, no team-totals) — that's a hard upstream limit, not an adapter filter choice.
- CLV = `fair_prob_close − avg_fill_price` on the YES side (classical CLV: Pinnacle's closing fair vs. the price we actually paid, not vs. the fair at placement). Captured inside the 1-hour Pinnacle snapshot retention window so settlement delay doesn't cause data loss. CLV has ~20× the signal-per-bet of realized P&L and is the intended gate for the go-live decision (not P&L significance, which requires ~10k+ bets).

## Long-form plan

See `~/Documents/Nate_Obsidian/+ EV Betting Project.md` for phase-level
roadmap and open questions.
