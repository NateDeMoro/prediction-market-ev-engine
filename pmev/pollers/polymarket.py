#!/usr/bin/env python3
"""
Polymarket US sports markets poller.

Every 60 seconds, pulls embedded-market events from the public gateway
(`gateway.polymarket.us/v2/leagues/{slug}/events`) across the supported
leagues, flattens each event into one row per marketSide, and dumps a
snapshot to disk. Polymarket US covers full-game moneyline / spread /
total only (no halves, no team-total, no props), so the snapshot scope
is a strict subset of Kalshi's.

Output:
  - stdout: per-cycle summary
  - data/polymarket.log: rolling log
  - data/polymarket_snapshots/<ISO>.jsonl: one row per marketSide

Run: python3 polymarket_poller.py
Stop: Ctrl-C or SIGTERM
"""
import os
import signal
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from pmev.core.io import RateGate, atomic_write_jsonl, make_logger, prune_snapshots, write_snapshot_meta
from pmev import config

GATEWAY = "https://gateway.polymarket.us"
LEAGUES = ("nba", "nhl", "mlb", "nfl", "wnba", "ncaaf", "ncaab")

POLL_INTERVAL_SEC      = config.POLLER_INTERVAL_SEC
REQUEST_TIMEOUT        = config.POLLER_REQUEST_TIMEOUT
MAX_WORKERS            = config.POLYMARKET_MAX_WORKERS
SNAPSHOT_RETENTION     = config.POLLER_SNAPSHOT_RETENTION
INTER_REQUEST_SLEEP    = config.POLYMARKET_INTER_REQUEST_SLEEP
RATE_LIMIT_RETRIES     = config.POLYMARKET_RATE_LIMIT_RETRIES
RATE_LIMIT_BACKOFF_SEC = config.POLYMARKET_RATE_LIMIT_BACKOFF_SEC

HEADERS = {
    "User-Agent": "Mozilla/5.0 (ev-scanner)",
    "Accept": "application/json",
}

DIR          = os.path.dirname(os.path.abspath(__file__))
DATA_DIR     = os.path.join(DIR, "data")
SNAPSHOT_DIR = config.POLYMARKET_SNAPSHOT_DIR
LOG_PATH     = os.path.join(DATA_DIR, "polymarket.log")
PID_PATH     = os.path.join(DATA_DIR, "polymarket.pid")


_log = make_logger(LOG_PATH)

_gate = RateGate(INTER_REQUEST_SLEEP)


def get(url):
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        _gate.claim_slot()
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
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
    r.raise_for_status()


def fetch_league_events(league):
    url = f"{GATEWAY}/v2/leagues/{league}/events"
    try:
        return get(url).get("events") or []
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise


def flatten_event(event, league):
    """Emit one row per marketSide across all markets on this event."""
    team_lookup = []
    for t in event.get("teams") or []:
        team_lookup.append({
            "id": t.get("id"),
            "name": t.get("safeName") or t.get("name"),
            "abbrev": t.get("abbreviation"),
        })

    event_meta = {
        "event_id": event.get("id"),
        "event_slug": event.get("slug"),
        "event_ticker": event.get("ticker"),
        "event_title": event.get("title"),
        "start_time": event.get("startTime") or event.get("startDate"),
        "game_id": event.get("gameId"),
        "sportradar_game_id": event.get("sportradarGameId"),
        "league": league,
        "live": event.get("live"),
        "ended": event.get("ended"),
        "closed": event.get("closed"),
        "teams": team_lookup,
    }

    rows = []
    for m in event.get("markets") or []:
        market_meta = {
            "market_id": m.get("id"),
            "slug": m.get("slug"),
            "question": m.get("question"),
            "market_type": m.get("marketType"),
            "sports_market_type_v2": m.get("sportsMarketTypeV2"),
            "line": m.get("line"),
            "game_start_time": m.get("gameStartTime"),
            "fee_coefficient": m.get("feeCoefficient"),
            "outcomes": m.get("outcomes"),
            "active": m.get("active"),
            "closed": m.get("closed"),
            "archived": m.get("archived"),
            "ep3_status": m.get("ep3Status"),
            "best_bid": (m.get("bestBidQuote") or {}).get("value"),
            "best_ask": (m.get("bestAskQuote") or {}).get("value"),
        }
        for side in m.get("marketSides") or []:
            team = side.get("team") or {}
            rows.append({
                "event": event_meta,
                "market": market_meta,
                "side": {
                    "id": side.get("id"),
                    "identifier": side.get("identifier"),
                    "description": side.get("description"),
                    "long": side.get("long"),
                    "team_id": side.get("teamId"),
                    "team_name": team.get("safeName") or team.get("name"),
                    "team_abbrev": team.get("abbreviation"),
                    "price": side.get("price"),
                    "quote": (side.get("quote") or {}).get("value"),
                    "participant_id": side.get("participantId"),
                },
            })
    return rows


def snapshot_once():
    start = time.time()
    _gate.reset_and_get_429()  # discard stale count from between cycles
    rows = []
    errors = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_league_events, lg): lg for lg in LEAGUES}
        for fut in as_completed(futures):
            lg = futures[fut]
            try:
                events = fut.result()
            except Exception as e:
                errors += 1
                _log(f"fetch {lg} failed: {type(e).__name__}: {e}")
                continue
            for event in events:
                rows.extend(flatten_event(event, lg))

    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    write_now = datetime.now(timezone.utc)
    captured_at = write_now.isoformat()
    # Stamp capture time on freshly-fetched rows; setdefault preserves the stamp
    # on any rows carried forward from a prior cycle (tiered cadence).
    for row in rows:
        row.setdefault("captured_at", captured_at)
    fname = write_now.strftime("%Y%m%dT%H%M%SZ") + ".jsonl"
    path = os.path.join(SNAPSHOT_DIR, fname)
    fetch_sec = time.time() - start
    write_start = time.time()
    atomic_write_jsonl(
        path, rows, dumps_kwargs={"separators": (",", ":")}, logger=_log
    )
    write_sec = time.time() - write_start
    dt = time.time() - start
    rate_limit_429 = _gate.reset_and_get_429()
    write_snapshot_meta(path, {
        "captured_at": captured_at,
        "cycle_elapsed_sec": dt,
        "fetch_sec": fetch_sec,
        "write_sec": write_sec,
        "rate_limit_429": rate_limit_429,
    }, logger=_log)
    prune_snapshots(SNAPSHOT_DIR, SNAPSHOT_RETENTION)
    _log(f"cycle rows={len(rows)} leagues={len(LEAGUES)} errors={errors} "
         f"file={os.path.basename(path)} elapsed={dt:.1f}s "
         f"fetch={fetch_sec:.1f}s write={write_sec:.2f}s")


def _install_signal_handlers():
    def _handler(signum, frame):
        _log(f"received signal {signum}, exiting")
        try:
            os.unlink(PID_PATH)
        except OSError:
            pass
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))
    _install_signal_handlers()
    _log(f"starting polymarket poller pid={os.getpid()} leagues={','.join(LEAGUES)}")
    while True:
        try:
            snapshot_once()
        except Exception:
            _log("snapshot_once raised:\n" + traceback.format_exc())
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
