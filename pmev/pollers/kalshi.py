#!/usr/bin/env python3
"""
Kalshi sports markets poller.

Every 60 seconds, pulls open markets from Kalshi sports "game" series
(head-to-head winner, spread, and total) for games whose
`occurrence_datetime` falls within the next 6 hours. Logs any market
whose bid/ask has changed since the previous cycle; dumps full
per-cycle snapshots to disk.

Output:
  - stdout: cycle summary + one line per changed market
  - data/kalshi.log: rolling log of cycle summaries + price changes
  - data/kalshi_snapshots/<ISO-timestamp>.jsonl: per-cycle snapshot

Run: python3 kalshi_poller.py
Stop: Ctrl-C or SIGTERM
"""
import json
import os
import re
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import requests

from pmev.core.io import (
    RateGate,
    RunFlag,
    atomic_write_jsonl,
    install_shutdown_handlers,
    make_logger,
    prune_snapshots,
    sleep_until_next_cycle,
    write_snapshot_meta,
)
from pmev import config

BASE = "https://api.elections.kalshi.com/trade-api/v2"

POLL_INTERVAL_SEC      = config.POLLER_INTERVAL_SEC
WINDOW_HOURS           = config.KALSHI_WINDOW_HOURS
REQUEST_TIMEOUT        = config.POLLER_REQUEST_TIMEOUT
MAX_WORKERS            = config.KALSHI_MAX_WORKERS
RATE_LIMIT_RETRIES     = config.KALSHI_RATE_LIMIT_RETRIES
RATE_LIMIT_BACKOFF_SEC = config.KALSHI_RATE_LIMIT_BACKOFF_SEC
DEAD_SERIES_SKIP_AFTER  = config.KALSHI_DEAD_SERIES_SKIP_AFTER
DEAD_SERIES_RETRY_AFTER = config.KALSHI_DEAD_SERIES_RETRY_AFTER
SNAPSHOT_RETENTION     = config.POLLER_SNAPSHOT_RETENTION
KALSHI_INTER_REQUEST_SLEEP = config.KALSHI_INTER_REQUEST_SLEEP

DATA_DIR     = config.DATA_DIR
SNAPSHOT_DIR = config.KALSHI_SNAPSHOT_DIR
LOG_PATH     = os.path.join(DATA_DIR, "kalshi.log")

# Core head-to-head / spread / total / moneyline series.
# Anything ending in GAME, SPREAD, TOTAL, or ML is a primary line market
# with a clean Pinnacle counterpart. Prop/stat series are excluded.
CORE_SUFFIX = re.compile(r"(GAME|SPREAD|TOTAL|ML|H2H)$")

# Per-game player-prop series. Allowlist (not regex) because the noncore Sports
# bucket has ~210 series, ~190 of which are season-long awards/draft/standings
# markets with no per-game Pinnacle counterpart. See data/kalshi_probe/NOTES.md.
# NBA + NHL active today; NFL/MLB included for when their seasons start.
PER_GAME_PROP_SERIES = {
    # NBA
    "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBABLK", "KXNBASTL", "KXNBA3PT",
    "KXNBAPRA", "KXNBAPR", "KXNBARA", "KXNBAPA",
    # NHL
    "KXNHLPTS", "KXNHLGOALS", "KXNHLSOG",
    # NFL
    "KXNFLPASSYDS", "KXNFLPASSTDS", "KXNFLRSHYDS", "KXNFLRECYDS",
    "KXNFLREC", "KXNFLANYTD", "KXNFLNEXTTD",
}

# Opt-in via env-var so the existing team-market pipeline runs untouched until rollout.
INCLUDE_PROPS = config.KALSHI_INCLUDE_PROPS

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_running = RunFlag()
log = make_logger(LOG_PATH)

_session = requests.Session()
_session.headers.update(HEADERS)

_gate = RateGate(KALSHI_INTER_REQUEST_SLEEP)


def get(path, **params):
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        _gate.claim_slot()
        r = _session.get(f"{BASE}{path}", params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429 and attempt < RATE_LIMIT_RETRIES:
            ra = r.headers.get("Retry-After")
            try:
                backoff = float(ra) if ra else RATE_LIMIT_BACKOFF_SEC * (2 ** attempt)
            except ValueError:
                backoff = RATE_LIMIT_BACKOFF_SEC * (2 ** attempt)
            _gate.record_429(backoff)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()  # final failure, surface the 429


def fetch_core_sports_series():
    """List Sports series for ingestion: core team-market series, plus the
    per-game player-prop allowlist when INCLUDE_PROPS is set."""
    data = get("/series", category="Sports")
    series = data.get("series", [])
    core = [s for s in series if CORE_SUFFIX.search(s.get("ticker", ""))]
    if INCLUDE_PROPS:
        core += [s for s in series if s.get("ticker") in PER_GAME_PROP_SERIES]
    return core


def fetch_markets_for_series(series_ticker):
    """Paginate all open markets for a series."""
    out = []
    cursor = None
    for _ in range(10):  # cap at 10 pages per series (2000 markets)
        params = {"series_ticker": series_ticker, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get("/markets", **params)
        out.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def within_window(market, now, horizon):
    # Prefer occurrence_datetime (game start). Fall back to expected_expiration_time.
    start = parse_iso(market.get("occurrence_datetime")) or parse_iso(market.get("expected_expiration_time"))
    if start is None:
        return False
    return now <= start <= horizon


def market_fingerprint(market):
    keys = (
        "yes_bid_dollars", "yes_ask_dollars",
        "no_bid_dollars", "no_ask_dollars",
        "last_price_dollars", "liquidity_dollars",
        "status",
    )
    blob = json.dumps([market.get(k) for k in keys], sort_keys=True, default=str).encode()
    return hashlib.md5(blob).hexdigest()


def format_change(market, old_fp):
    tag = "NEW" if old_fp is None else "CHG"
    t = market.get("ticker", "?")
    title = market.get("title", "")
    yes_side = market.get("yes_sub_title") or "YES"
    yb = market.get("yes_bid_dollars")
    ya = market.get("yes_ask_dollars")
    nb = market.get("no_bid_dollars")
    na = market.get("no_ask_dollars")
    return f"{tag} {t} [{title}] {yes_side}: yes {yb}/{ya} no {nb}/{na}"


def _should_poll(ticker, cycle_num, dead_counts):
    """Decide whether to fetch a series this cycle.

    - Warm series (dead_counts < SKIP_AFTER): always poll.
    - Cold series: use a deterministic hash bucket so load spreads evenly across
      DEAD_SERIES_RETRY_AFTER cycles instead of all cold series polling together.
      Each cold series still polls exactly once per DEAD_SERIES_RETRY_AFTER cycles
      (<=5-cycle recovery latency, same as before).
    """
    dc = dead_counts.get(ticker, 0)
    if dc < DEAD_SERIES_SKIP_AFTER:
        return True
    bucket = int(hashlib.md5(ticker.encode()).hexdigest(), 16) % DEAD_SERIES_RETRY_AFTER
    return cycle_num % DEAD_SERIES_RETRY_AFTER == bucket


def run_cycle(prev_fps, core_series, dead_counts, cycle_num):
    _gate.reset_and_get_429()  # discard stale count
    cycle_start = time.monotonic()

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=WINDOW_HOURS)

    new_fps = {}
    changes = 0
    total_markets = 0
    in_window_markets = 0
    series_with_hits = 0
    snapshot = []

    live_series = [
        s for s in core_series
        if _should_poll(s["ticker"], cycle_num, dead_counts)
    ]

    def fetch(s):
        try:
            return s["ticker"], fetch_markets_for_series(s["ticker"]), None
        except requests.RequestException as e:
            return s["ticker"], None, e

    fetch_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(fetch, s) for s in live_series]
        for fut in as_completed(futures):
            stick, markets, err = fut.result()
            if err is not None:
                log(f"  ! fetch failed for {stick}: {err}")
                continue

            total_markets += len(markets)
            hit = False
            for m in markets:
                if not within_window(m, now, horizon):
                    continue
                hit = True
                in_window_markets += 1
                fp = market_fingerprint(m)
                key = m.get("ticker")
                new_fps[key] = fp
                if prev_fps.get(key) != fp:
                    changes += 1
                    log(format_change(m, prev_fps.get(key)))
                snapshot.append({
                    "series_ticker": stick,
                    "event_ticker": m.get("event_ticker"),
                    "ticker": key,
                    "title": m.get("title"),
                    "yes_sub_title": m.get("yes_sub_title"),
                    "no_sub_title": m.get("no_sub_title"),
                    "occurrence_datetime": m.get("occurrence_datetime"),
                    "expected_expiration_time": m.get("expected_expiration_time"),
                    "yes_bid_dollars": m.get("yes_bid_dollars"),
                    "yes_ask_dollars": m.get("yes_ask_dollars"),
                    "no_bid_dollars": m.get("no_bid_dollars"),
                    "no_ask_dollars": m.get("no_ask_dollars"),
                    "last_price_dollars": m.get("last_price_dollars"),
                    "liquidity_dollars": m.get("liquidity_dollars"),
                    "status": m.get("status"),
                })
            if hit:
                series_with_hits += 1
                dead_counts[stick] = 0
            else:
                dead_counts[stick] = dead_counts.get(stick, 0) + 1

    fetch_sec = time.monotonic() - fetch_start
    rate_limit_429 = _gate.reset_and_get_429()
    write_sec = 0.0
    if snapshot:
        write_now = datetime.now(timezone.utc)
        captured_at = write_now.isoformat()
        # Stamp capture time on freshly-fetched rows; setdefault preserves the
        # stamp on any rows carried forward from a prior cycle (tiered cadence).
        for row in snapshot:
            row.setdefault("captured_at", captured_at)
        path = os.path.join(SNAPSHOT_DIR, write_now.strftime("%Y%m%dT%H%M%SZ") + ".jsonl")
        write_start = time.monotonic()
        atomic_write_jsonl(
            path, snapshot, dumps_kwargs={"default": str}, logger=log
        )
        write_sec = time.monotonic() - write_start
        cycle_elapsed_sec = time.monotonic() - cycle_start
        write_snapshot_meta(path, {
            "captured_at": captured_at,
            "cycle_elapsed_sec": cycle_elapsed_sec,
            "fetch_sec": fetch_sec,
            "write_sec": write_sec,
            "rate_limit_429": rate_limit_429,
        }, logger=log)
        prune_snapshots(SNAPSHOT_DIR, SNAPSHOT_RETENTION)
    else:
        cycle_elapsed_sec = time.monotonic() - cycle_start

    return new_fps, {
        "core_series": len(core_series),
        "queried": len(live_series),
        "series_with_hits": series_with_hits,
        "total_markets_seen": total_markets,
        "in_window_markets": in_window_markets,
        "changes": changes,
        "fetch_sec": fetch_sec,
        "write_sec": write_sec,
        "cycle_elapsed_sec": cycle_elapsed_sec,
        "rate_limit_429": rate_limit_429,
    }


def main():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    install_shutdown_handlers(_running, logger=log)

    log(
        f"kalshi poller starting: window={WINDOW_HOURS}h interval={POLL_INTERVAL_SEC}s "
        f"include_props={INCLUDE_PROPS}"
    )

    prev_fps = {}
    dead_counts = {}
    cycle = 0
    while _running:
        cycle += 1
        t0 = time.time()

        try:
            core_series = fetch_core_sports_series()
            prev_fps, stats = run_cycle(
                prev_fps, core_series, dead_counts, cycle
            )
            elapsed = time.time() - t0
            log(
                f"cycle {cycle} ok: series={stats['core_series']} "
                f"queried={stats['queried']} "
                f"with_hits={stats['series_with_hits']} "
                f"markets_seen={stats['total_markets_seen']} "
                f"in_window={stats['in_window_markets']} "
                f"changes={stats['changes']} elapsed={elapsed:.1f}s "
                f"fetch={stats['fetch_sec']:.1f}s "
                f"write={stats['write_sec']:.2f}s "
                f"429={stats['rate_limit_429']}"
            )
        except Exception as e:
            log(f"cycle {cycle} FAILED: {type(e).__name__}: {e}")

        sleep_until_next_cycle(t0, POLL_INTERVAL_SEC, _running)

    log("kalshi poller stopped cleanly")


if __name__ == "__main__":
    main()
