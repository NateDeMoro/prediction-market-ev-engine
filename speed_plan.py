"""
speed_plan.py
=============

Pregame-only latency reduction plan: take the read-and-decide loop from
~90s end-to-end to ~5-15s without exceeding any book's rate budget.

Scope: pregame actionable window only (30m-3h pre-start). Live trading is
explicitly out of scope; the latency floor for live (sub-second) requires
infrastructure (Curaçao colo, streaming feed contracts, multiple keys)
that this plan does not address.


CURRENT STATE
-------------
- pinnacle_poller.py: fixed 60s REST loop on guest.api.arcadia.pinnacle.com/0.1.
  Pulls every matchup in a 24h pregame window plus 6h live lookback. Full
  snapshot each cycle, no delta cursor.
- kalshi_poller.py: fixed 60s REST loop. 3-worker pool. Full series sweep.
- polymarket_poller.py: fixed 60s REST loop on gateway.polymarket.us.
- find_ev_bet.py / ev_dashboard.py: rescan triggered every 60s off the
  most-recent snapshot file. EV decision is file-driven, not event-driven.

End-to-end latency budget today:
  poll cadence 60s + snapshot write/read 5-10s + scan 5-15s + render = ~90s.


GOAL
----
Decision-time freshness target: <=15s for the actionable window. Order
placement (when wired) should fire within 500ms of decision.

Non-goals: live in-play, exotic colocation, paid streaming feeds, scraping
of pinnacle.com retail web JSON.


SOURCE-BY-SOURCE PLAN
---------------------

1. PINNACLE (read-side, REST)
   Constraint: no public WS. Arcadia API delta semantics unverified.

   a. Two-tier polling cadence
      - Outer loop (existing 60s): broad 24h-pregame snapshot for matcher
        coverage and dashboards.
      - Inner loop (new, 5-10s): only matchups whose start_time is within
        [now+30m, now+3h]. This is typically <30 matchups across all sports
        vs ~200 in the 24h window. Cheap on rate budget.
      - Inner loop hits /matchups/{id}/markets/related/straight per matchup
        in parallel (bounded worker pool, e.g. 4-6 concurrent).
   b. Research item: arcadia `since=<ms-epoch>` parameter
      - Affiliate API supports it; arcadia is undocumented. Probe by sending
        ?since=<ts> on /sports/{sid}/markets/straight and diff response size
        vs an unfiltered call. If supported, fold into inner loop to reduce
        payload further.
   c. Connection reuse
      - Single requests.Session with HTTP keep-alive across the inner loop.
        Saves ~30-80ms per call by skipping TLS handshake.
   d. Rate-limit guardrails
      - Inner loop budget: 5-6 req/s ceiling shared with outer loop. Token
        bucket in the shared session; outer loop yields when inner is hot.

2. KALSHI (read-side, WS)
   Endpoint: wss://api.elections.kalshi.com/trade-api/ws/v2
   Auth: same RSA-PSS-SHA256 signing as REST (already implemented in
   place_kalshi_test_order.py).

   a. Channels to subscribe
      - orderbook_delta: incremental L2 updates per ticker.
      - market_lifecycle: open/close/settle transitions.
      - (Skip ticker/trade unless we find a use; orderbook_delta is enough
        for ladder walks.)
   b. Subscription scope
      - Only tickers whose underlying event start_time is in [now+30m, now+3h].
      - Re-subscribe diff every 60s as the window slides. Batch
        subscribe/unsubscribe to avoid per-message rate-limit churn.
   c. In-memory book state
      - Dict keyed by ticker, holding both yes-bid and no-bid books as
        sorted level dicts. Apply deltas in-place.
      - On reconnect/resync, fall back to a one-shot REST orderbook fetch
        per affected ticker, then resume WS.
   d. Fallback
      - If WS connection drops and reconnect fails twice, revert that
        ticker subset to REST short-poll (5s) until WS recovers.
   e. Existing kalshi_poller.py
      - Keep running on its 60s cadence as the source-of-truth snapshot
        writer (matcher and dashboard still consume snapshots). The WS
        path is additive: it feeds an in-memory cache used by the fast
        decision loop only.

3. POLYMARKET (read-side, REST or WS - to verify)
   Constraint: gateway.polymarket.us WS support is undocumented in this
   codebase. CLOB has WS, the gateway may or may not.

   a. Research item (do first)
      - Inspect the gateway response headers and the polymarket.us web app
        network trace for a wss endpoint. If present, mirror the Kalshi
        plan (subscribe to in-window markets only, in-memory book).
      - If absent, short-poll REST at 5-10s scoped to the actionable window.
   b. Existing polymarket_poller.py
      - Same role as Kalshi's: keep on 60s cadence for snapshots, add WS
        or short-poll on top for fast path.

4. DECISION LOOP (replaces 60s file-driven rescan)
   a. Event-driven recompute
      - Trigger EV recompute when any of these arrive:
        * Pinnacle inner-loop poll for a matchup updates a price
        * Kalshi WS orderbook_delta for a subscribed ticker
        * Polymarket WS or short-poll updates a market
      - Recompute is scoped to the affected (matchup, market) only, not a
        full scan. Reuses existing devig + walk_ladder logic from
        find_ev_bet.py.
   b. In-memory state structure
      - matchup_id -> Pinnacle prices (latest deltas applied)
      - (book, market_id) -> ladder (latest deltas applied)
      - matched_pairs index built once per outer cycle, mutated on the fly
        by inner events.
   c. EV result publishing
      - Push hot rows over a thread-safe deque to ev_dashboard.py instead
        of re-reading snapshots. Dashboard /api/ev reads from memory.

5. ORDER PLACEMENT (when wiring real money)
   Goal: <500ms from decision-fired to order-acked.

   a. Pre-warmed connections
      - Persistent HTTPS connection to api.elections.kalshi.com with
        HTTP/2 keep-alive. Reuse across orders.
   b. Pre-built signed templates
      - Cache the request shape and headers; only price/size/side/ticker
        slot in at fire time. RSA-PSS sign happens on the hot path
        (~5-10ms with cryptography lib), so pre-compute the canonical
        string components that don't depend on order-specific fields.
   c. Last-look orderbook check
      - Single REST GET /markets/{ticker}/orderbook immediately before
        send (~80-150ms). Confirms the level still has the depth the
        EV decision assumed. Skips send if depth has evaporated.
      - Mitigates the Nagoya ghost-liquidity case where a 60s-old book
        showed phantom shares at a too-good price.
   d. Per-book client modules
      - Seed adapters/kalshi_trade.py from place_kalshi_test_order.py.
      - Polymarket trade adapter is a separate workstream; gateway order
        endpoint and signing scheme need their own POC.

PHASED ROLLOUT
--------------

Phase 1 (low-risk, biggest single win):
  - Pinnacle inner-loop tightening (1a, 1c, 1d).
  - Switch ev_dashboard.py rescan trigger from 60s timer to file-watch
    on the most-recent snapshot. Cuts 30s of wall-clock without any WS.
  Expected effect: ~90s -> ~30-40s.

Phase 2 (Kalshi WS):
  - Kalshi WS read path (2a-2d). In-memory book state.
  - Event-driven EV recompute for Kalshi-side moves (4a, 4b).
  - Keep file-driven path as fallback.
  Expected effect: ~30s -> ~10-15s on Kalshi-anchored decisions.

Phase 3 (Polymarket parity):
  - Resolve research item 3a, then mirror Phase 2 for Polymarket.

Phase 4 (order placement, only after paper validates):
  - Pre-warm + pre-template + last-look (5a-5c).
  - Wire kalshi_trade.py from existing POC.
  - Polymarket trade adapter as a separate effort.


RATE-LIMIT NOTES
----------------
- Pinnacle arcadia: no published limit; existing INTER_REQUEST_SLEEP=0.2
  (5 req/s) is the working ceiling. Inner+outer combined must stay under
  this. Token bucket in shared session enforces it.
- Kalshi WS: per-connection subscription cap (verify current docs).
  Single connection scoped to in-window tickers stays well under.
- Kalshi REST: 100 req/min unauth, 600/min auth (verify). Last-look
  before send adds 1 req/order, negligible.
- Polymarket gateway: undocumented; treat as 1 req/s ceiling until probed.


OPEN RESEARCH ITEMS
-------------------
1. Arcadia API: does ?since=<ts> work on /sports/{sid}/markets/straight?
2. Polymarket gateway: is there a WS endpoint, and what's the auth?
3. Kalshi WS: current subscription / connection / message rate caps.
4. Pinnacle inner-loop scope: confirm <30 matchups in [now+30m, now+3h]
   across all active sports during peak hours.

Each item should be probed with a one-off script in data/<book>_probe/
before being depended on by production paths.


WHAT THIS PLAN DOES NOT DO
--------------------------
- Live (in-play) trading. Latency floor with this architecture is ~5-15s,
  which is dead money on live except in the thinnest niches.
- Multi-key Pinnacle parallelism. Single key is sufficient for the
  in-window scope; multi-key adds account-management overhead with
  marginal latency benefit at our scope.
- pinnacle.com retail web JSON scraping. Faster but ToS-grey and fragile.
- Streaming feed contracts (Sportradar / Pinnacle Pro). Commercial
  agreement, not retail-feasible.
"""
