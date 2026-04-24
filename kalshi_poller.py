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

from data_utils import (
    RunFlag,
    atomic_write_jsonl,
    install_shutdown_handlers,
    make_logger,
    prune_snapshots,
    sleep_until_next_cycle,
)

BASE = "https://api.elections.kalshi.com/trade-api/v2"

POLL_INTERVAL_SEC = 60
WINDOW_HOURS = 24           # match Pinnacle's window (pregame + live lookback)
SERIES_REFRESH_SEC = 3600   # re-list sports series hourly
REQUEST_TIMEOUT = 15
MAX_WORKERS = 3             # Kalshi unauth limit is ~2 req/sec; keep pool small
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF_SEC = 2.0
# Series with no in-window markets for this many consecutive cycles become
# cold. Cold series are only re-polled every DEAD_SERIES_RETRY_AFTER cycles
# to avoid wasting request budget, but are never fully frozen until the 1h
# series refresh — so a series that starts publishing mid-hour recovers
# within ~DEAD_SERIES_RETRY_AFTER*interval seconds instead of waiting up to
# an hour for the next refresh.
DEAD_SERIES_SKIP_AFTER = 3
DEAD_SERIES_RETRY_AFTER = 5
SNAPSHOT_RETENTION = 60  # keep the N most recent snapshots (≈ last hour)

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DIR, "data")
SNAPSHOT_DIR = os.path.join(DATA_DIR, "kalshi_snapshots")
LOG_PATH = os.path.join(DATA_DIR, "kalshi.log")

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
INCLUDE_PROPS = os.getenv("KALSHI_INCLUDE_PROPS") == "1"

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


def get(path, **params):
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        r = _session.get(f"{BASE}{path}", params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429 and attempt < RATE_LIMIT_RETRIES:
            # Honor Retry-After if present, else exponential backoff.
            ra = r.headers.get("Retry-After")
            try:
                wait = float(ra) if ra else RATE_LIMIT_BACKOFF_SEC * (2 ** attempt)
            except ValueError:
                wait = RATE_LIMIT_BACKOFF_SEC * (2 ** attempt)
            time.sleep(wait)
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


def _should_poll(ticker, cycle_num, dead_counts, last_poll_cycle):
    """Decide whether to fetch a series this cycle.

    - Warm series (dead_counts < SKIP_AFTER): always poll.
    - Cold series: poll every DEAD_SERIES_RETRY_AFTER cycles so a newly-
      publishing series recovers within minutes instead of waiting for the
      hourly full-series refresh.
    """
    dc = dead_counts.get(ticker, 0)
    if dc < DEAD_SERIES_SKIP_AFTER:
        return True
    last = last_poll_cycle.get(ticker, 0)
    return (cycle_num - last) >= DEAD_SERIES_RETRY_AFTER


def run_cycle(prev_fps, core_series, dead_counts, last_poll_cycle, cycle_num):
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
        if _should_poll(s["ticker"], cycle_num, dead_counts, last_poll_cycle)
    ]

    def fetch(s):
        try:
            return s["ticker"], fetch_markets_for_series(s["ticker"]), None
        except requests.RequestException as e:
            return s["ticker"], None, e

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
            last_poll_cycle[stick] = cycle_num
            if hit:
                series_with_hits += 1
                dead_counts[stick] = 0
            else:
                dead_counts[stick] = dead_counts.get(stick, 0) + 1

    if snapshot:
        path = os.path.join(SNAPSHOT_DIR, now.strftime("%Y%m%dT%H%M%SZ") + ".jsonl")
        atomic_write_jsonl(
            path, snapshot, dumps_kwargs={"default": str}, logger=log
        )
        prune_snapshots(SNAPSHOT_DIR, SNAPSHOT_RETENTION)

    return new_fps, {
        "core_series": len(core_series),
        "queried": len(live_series),
        "series_with_hits": series_with_hits,
        "total_markets_seen": total_markets,
        "in_window_markets": in_window_markets,
        "changes": changes,
    }


def main():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    install_shutdown_handlers(_running, logger=log)

    log(
        f"kalshi poller starting: window={WINDOW_HOURS}h interval={POLL_INTERVAL_SEC}s "
        f"include_props={INCLUDE_PROPS}"
    )

    try:
        core_series = fetch_core_sports_series()
    except requests.RequestException as e:
        log(f"failed to list sports series on startup: {e}")
        return
    log(f"loaded {len(core_series)} core sports series")
    series_loaded_at = time.time()

    prev_fps = {}
    dead_counts = {}
    last_poll_cycle = {}
    cycle = 0
    while _running:
        cycle += 1
        t0 = time.time()

        if t0 - series_loaded_at > SERIES_REFRESH_SEC:
            try:
                core_series = fetch_core_sports_series()
                series_loaded_at = t0
                dead_counts.clear()  # give all series another chance
                last_poll_cycle.clear()
                log(f"refreshed sports series list: {len(core_series)} entries")
            except requests.RequestException as e:
                log(f"series refresh failed (keeping old list): {e}")

        try:
            prev_fps, stats = run_cycle(
                prev_fps, core_series, dead_counts, last_poll_cycle, cycle
            )
            elapsed = time.time() - t0
            log(
                f"cycle {cycle} ok: series={stats['core_series']} "
                f"queried={stats['queried']} "
                f"with_hits={stats['series_with_hits']} "
                f"markets_seen={stats['total_markets_seen']} "
                f"in_window={stats['in_window_markets']} "
                f"changes={stats['changes']} elapsed={elapsed:.1f}s"
            )
        except Exception as e:
            log(f"cycle {cycle} FAILED: {type(e).__name__}: {e}")

        sleep_until_next_cycle(t0, POLL_INTERVAL_SEC, _running)

    log("kalshi poller stopped cleanly")


if __name__ == "__main__":
    main()
