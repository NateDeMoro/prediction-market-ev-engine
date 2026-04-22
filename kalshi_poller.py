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
import signal
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"

POLL_INTERVAL_SEC = 60
WINDOW_HOURS = 24           # match Pinnacle's window (pregame + live lookback)
SERIES_REFRESH_SEC = 3600   # re-list sports series hourly
REQUEST_TIMEOUT = 15
MAX_WORKERS = 3             # Kalshi unauth limit is ~2 req/sec; keep pool small
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF_SEC = 2.0
# Series with no in-window markets for this many consecutive cycles are skipped
# until the next series refresh.
DEAD_SERIES_SKIP_AFTER = 3
SNAPSHOT_RETENTION = 60  # keep the N most recent snapshots (≈ last hour)

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DIR, "data")
SNAPSHOT_DIR = os.path.join(DATA_DIR, "kalshi_snapshots")
LOG_PATH = os.path.join(DATA_DIR, "kalshi.log")


def prune_snapshots(dir_path, keep_n):
    """Delete all but the `keep_n` most recent *.jsonl files under dir_path.

    Names sort chronologically (timestamp-prefixed), so filename order is
    a safe proxy for recency.
    """
    try:
        names = sorted(n for n in os.listdir(dir_path) if n.endswith(".jsonl"))
    except OSError:
        return
    for stale in names[:-keep_n] if keep_n > 0 else names:
        try:
            os.remove(os.path.join(dir_path, stale))
        except OSError:
            pass

# Core head-to-head / spread / total / moneyline series.
# Anything ending in GAME, SPREAD, TOTAL, or ML is a primary line market
# with a clean Pinnacle counterpart. Prop/stat series are excluded.
CORE_SUFFIX = re.compile(r"(GAME|SPREAD|TOTAL|ML|H2H)$")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_running = True


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


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
    """List every Sports series whose ticker ends in GAME/SPREAD/TOTAL/ML/H2H."""
    data = get("/series", category="Sports")
    series = data.get("series", [])
    core = [s for s in series if CORE_SUFFIX.search(s.get("ticker", ""))]
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


def handle_signal(signum, frame):
    global _running
    _running = False
    log("shutdown signal received, exiting after current cycle")


def run_cycle(prev_fps, core_series, dead_counts):
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=WINDOW_HOURS)

    new_fps = {}
    changes = 0
    total_markets = 0
    in_window_markets = 0
    series_with_hits = 0
    snapshot = []

    # Skip series that have been empty for DEAD_SERIES_SKIP_AFTER consecutive cycles.
    live_series = [s for s in core_series if dead_counts.get(s["ticker"], 0) < DEAD_SERIES_SKIP_AFTER]

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
            if hit:
                series_with_hits += 1
                dead_counts[stick] = 0
            else:
                dead_counts[stick] = dead_counts.get(stick, 0) + 1

    if snapshot:
        path = os.path.join(SNAPSHOT_DIR, now.strftime("%Y%m%dT%H%M%SZ") + ".jsonl")
        try:
            with open(path, "w") as f:
                for row in snapshot:
                    f.write(json.dumps(row, default=str) + "\n")
        except OSError as e:
            log(f"  ! snapshot write failed: {e}")
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
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log(f"kalshi poller starting: window={WINDOW_HOURS}h interval={POLL_INTERVAL_SEC}s")

    try:
        core_series = fetch_core_sports_series()
    except requests.RequestException as e:
        log(f"failed to list sports series on startup: {e}")
        return
    log(f"loaded {len(core_series)} core sports series")
    series_loaded_at = time.time()

    prev_fps = {}
    dead_counts = {}
    cycle = 0
    while _running:
        cycle += 1
        t0 = time.time()

        if t0 - series_loaded_at > SERIES_REFRESH_SEC:
            try:
                core_series = fetch_core_sports_series()
                series_loaded_at = t0
                dead_counts.clear()  # give all series another chance
                log(f"refreshed sports series list: {len(core_series)} entries")
            except requests.RequestException as e:
                log(f"series refresh failed (keeping old list): {e}")

        try:
            prev_fps, stats = run_cycle(prev_fps, core_series, dead_counts)
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

        sleep_for = max(1, POLL_INTERVAL_SEC - (time.time() - t0))
        end = time.time() + sleep_for
        while _running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    log("kalshi poller stopped cleanly")


if __name__ == "__main__":
    main()
