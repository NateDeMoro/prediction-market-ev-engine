#!/usr/bin/env python3
"""
Pinnacle poller (pregame + live).

Every 60 seconds, pulls every matchup across every active sport that
either starts within the next 24 hours or is currently live (started
within the last 6 hours). Pregame markets come from the bulk sport-level
endpoint; live markets are discovered via team-vs-team sub-matchups and
fetched per-matchup because Pinnacle drops live parents from the bulk
list.

Output:
  - stdout: one line per cycle summary, plus one line per changed market
  - data/snapshots/<ISO-timestamp>.jsonl: full per-cycle snapshot
  - data/pinnacle.log: rolling log of changes

Run: python3 pinnacle_poller.py
Stop: Ctrl-C (writes a final snapshot and exits cleanly)
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
    RateGate,
    RunFlag,
    atomic_write_jsonl,
    install_shutdown_handlers,
    make_logger,
    prune_snapshots,
    read_latest_snapshot_meta,
    sleep_until_next_cycle,
    write_snapshot_meta,
)
from devig_utils import american_to_decimal
import config

BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
API_KEY = os.environ.get("PINNACLE_API_KEY")
if not API_KEY:
    raise SystemExit(
        "PINNACLE_API_KEY env var is required. Export it in your shell "
        "(macOS: `launchctl setenv` or add to ~/.zshrc) or set it in the "
        "systemd EnvironmentFile (.env) before starting the poller."
    )

POLL_INTERVAL_SEC      = config.PINNACLE_POLL_INTERVAL_SEC
WINDOW_HOURS           = config.PINNACLE_WINDOW_HOURS
LIVE_LOOKBACK_HOURS    = config.PINNACLE_LIVE_LOOKBACK_HOURS
REQUEST_TIMEOUT        = config.POLLER_REQUEST_TIMEOUT
INTER_REQUEST_SLEEP    = config.PINNACLE_INTER_REQUEST_SLEEP
MAX_WORKERS            = config.PINNACLE_MAX_WORKERS
RATE_LIMIT_RETRIES     = config.PINNACLE_RATE_LIMIT_RETRIES
RATE_LIMIT_BACKOFF_SEC = config.PINNACLE_RATE_LIMIT_BACKOFF_SEC
SNAPSHOT_RETENTION     = config.POLLER_SNAPSHOT_RETENTION

# Player-prop ingestion: 2 extra calls per parent matchup. PROP_SPORTS is a
# cheap first-pass on Pinnacle's broad sport name; PROP_LEAGUES is the precise
# filter applied after we've fetched /related (which carries league.name on the
# parent). See data/pinnacle_probe/NOTES.md.
INCLUDE_PROPS = config.PINNACLE_INCLUDE_PROPS
PROP_SPORTS = {"Basketball", "Hockey"}
PROP_LEAGUES = {"NBA", "NHL"}

DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DIR, "data")
SNAPSHOT_DIR = os.path.join(DATA_DIR, "snapshots")
LOG_PATH = os.path.join(DATA_DIR, "pinnacle.log")

HEADERS = {
    "X-API-Key": API_KEY,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.pinnacle.com/",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

_running = RunFlag()
log = make_logger(LOG_PATH)

_gate = RateGate(INTER_REQUEST_SLEEP)


def get(path, **params):
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        _gate.claim_slot()
        r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
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


def fetch_sports():
    """Return list of sports that currently have at least one matchup."""
    sports = get("/sports")
    return [s for s in sports if s.get("matchupCount", 0) > 0]


def fetch_raw_matchups(sport_id):
    """Entire matchups list for a sport, unfiltered."""
    return get(f"/sports/{sport_id}/matchups", brandId=0)


def is_team_pair(matchup):
    """True when the matchup's two participants are team-v-team (home/away alignment)."""
    parts = matchup.get("participants") or []
    if len(parts) != 2:
        return False
    alignments = {p.get("alignment") for p in parts}
    return alignments == {"home", "away"}


def classify_matchups(raw, now, earliest, latest):
    """
    Split a sport's raw matchup list into pregame primaries and live team-v-team subs
    that fall within [earliest, latest]. Returns (pregame_list, live_subs_list).

    Pregame primary: parentId is None, isLive is False, starts in [now, latest].
    Live team sub:   isLive is True, participants aligned home/away,
                     starts in [earliest, latest] (startTime usually in the past).
    """
    pregame, live = [], []
    for m in raw:
        if m.get("type") != "matchup":
            continue
        try:
            start = datetime.fromisoformat(m["startTime"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError, TypeError):
            continue
        if not m.get("parentId") and not m.get("isLive"):
            if now <= start <= latest:
                pregame.append(m)
        elif m.get("isLive") and is_team_pair(m):
            if earliest <= start <= latest:
                live.append(m)
    return pregame, live


def fetch_bulk_markets(sport_id):
    """All straight markets for a sport in one call. Covers pregame matchups only."""
    return get(f"/sports/{sport_id}/markets/straight", primaryOnly="false")


def fetch_live_matchup_markets(matchup_id):
    """Markets for a single (live) matchup. Used because live parents are absent from bulk."""
    return get(f"/matchups/{matchup_id}/markets/related/straight")


def fetch_related_matchups(parent_id):
    """Enumerate child matchups under a parent. Player props arrive here as
    type=='special' rows with special.category == 'Player Props'."""
    return get(f"/matchups/{parent_id}/related")


_PROP_DESC_PAREN = re.compile(
    r"^(?P<player>.+?)\s*\((?P<stat>[^)]+)\)(?:\s*\([^)]+\))?\s*$"
)
# NBA uses a different shape than NHL: 'Anthony Edwards Total Threes Made'
# rather than 'Anthony Edwards (Threes Made)'.
_PROP_DESC_TOTAL = re.compile(r"^(?P<player>.+?)\s+Total\s+(?P<stat>.+?)\s*$")


def parse_prop_description(desc):
    """Pinnacle special.description -> (player, stat).
    NHL: 'Sidney Crosby (Points)' -> ('Sidney Crosby', 'Points').
    NBA: 'Anthony Edwards Total Threes Made' -> ('Anthony Edwards', 'Threes Made').
    Strips trailing parenthetical metadata like '(must start)'."""
    if not desc:
        return None, None
    s = desc.strip()
    m = _PROP_DESC_PAREN.match(s)
    if m:
        return m.group("player").strip(), m.group("stat").strip()
    m = _PROP_DESC_TOTAL.match(s)
    if m:
        return m.group("player").strip(), m.group("stat").strip()
    return None, None


def market_fingerprint(market):
    """Stable hash of a market's prices. Ignores key fields so we detect price moves."""
    prices = market.get("prices") or []
    sig = [
        (p.get("designation"), p.get("participantId"), p.get("points"), p.get("price"))
        for p in prices
    ]
    blob = json.dumps(sig, sort_keys=True, default=str).encode()
    return hashlib.md5(blob).hexdigest()


def market_key(market):
    """Stable identifier for a single market (matchup + period + type + side taxonomy)."""
    return "|".join(str(market.get(k, "")) for k in ("matchupId", "period", "type", "side"))


def format_price_change(market, matchup_name, old_fp):
    period = market.get("period")
    mtype = market.get("type")
    prices = market.get("prices") or []
    parts = []
    for p in prices:
        a = p.get("price")
        d = american_to_decimal(a)
        dsg = p.get("designation") or p.get("participantId") or ""
        pts = p.get("points")
        suffix = f" {pts:+g}" if pts is not None else ""
        parts.append(f"{dsg}{suffix}={a:+d}[{d:.3f}]" if d else f"{dsg}{suffix}={a}")
    tag = "NEW" if old_fp is None else "CHG"
    return f"{tag} {matchup_name} p{period} {mtype}: " + " | ".join(parts)


def matchup_participants(matchup):
    """(home_name, away_name) from a matchup, or ('?', '?')."""
    parts = matchup.get("participants") or []
    home = next((p.get("name", "?") for p in parts if p.get("alignment") == "home"), None)
    away = next((p.get("name", "?") for p in parts if p.get("alignment") == "away"), None)
    if home is None and len(parts) >= 1:
        home = parts[0].get("name", "?")
    if away is None and len(parts) >= 2:
        away = parts[1].get("name", "?")
    return home or "?", away or "?"


def inject_designation(market, matchup):
    """
    Live matchup markets come back with prices that lack a `designation` field
    (designation is None, participantId may also be None). For 2-way markets we
    zip prices to the matchup's home/away participants by index so downstream
    tooling can key on designation like it does for pregame.
    """
    prices = market.get("prices") or []
    parts = matchup.get("participants") or []
    if len(prices) != 2 or len(parts) != 2:
        return market
    if any(p.get("designation") for p in prices):
        return market  # already designated
    # Assume positional pairing: prices[i] corresponds to participants[i].
    new_prices = []
    for price, part in zip(prices, parts):
        new_prices.append({**price, "designation": part.get("alignment")})
    return {**market, "prices": new_prices}


def record_market(market, matchup, sport_name, is_live, snapshot,
                  prev_fps, new_fps, emit_log):
    """Fingerprint, log-if-changed, and append a market row to the snapshot."""
    k = market_key(market)
    fp = market_fingerprint(market)
    new_fps[k] = fp
    changed = prev_fps.get(k) != fp
    if changed:
        home, away = matchup_participants(matchup)
        name = f"{home} vs {away}"
        live_tag = " LIVE" if is_live else ""
        emit_log(format_price_change(market, name + live_tag, prev_fps.get(k)))
    home, away = matchup_participants(matchup)
    row = {
        "sport": sport_name,
        "matchupId": matchup.get("id"),
        "matchup": f"{home} vs {away}",
        "startTime": matchup.get("startTime"),
        "isLive": bool(is_live),
        "period": market.get("period"),
        "type": market.get("type"),
        "prices": market.get("prices"),
    }
    # team_total rows come in a pair per (matchupId, period, points) — one for
    # home, one for away — distinguished only by the top-level `side` field
    # (already part of market_key). Preserve it so the matcher can pair a
    # Kalshi team-total ticker to the correct side's Pinnacle over/under.
    if market.get("type") == "team_total":
        row["side"] = market.get("side")
    snapshot.append(row)
    return changed


def collect_prop_rows(parent_matchup, sport_name, prev_fps):
    """For a pregame parent matchup in a prop-target league, fetch per-player prop rows.
    Two HTTP calls: /related (enumerate child matchups + player/stat metadata) and
    /markets/related/straight (price rows for the whole tree).
    Returns (rows, fp_updates, log_lines, error_msg). Side-effect-free: no shared-state
    mutation; the main thread applies the returned data. error_msg is None on success.
    """
    parent_id = parent_matchup["id"]
    try:
        related = fetch_related_matchups(parent_id)
    except requests.RequestException as e:
        return [], {}, [], f"  ! prop /related failed for {parent_id}: {e}"

    # Verify league via the parent record returned in /related (carries league.name).
    parent_rec = next((m for m in related if m.get("id") == parent_id), None)
    league_name = ((parent_rec or {}).get("league") or {}).get("name")
    if league_name not in PROP_LEAGUES:
        return [], {}, [], None

    # Build child_matchupId -> (player, stat, units) for every player-prop child.
    # Also build participantId -> "over"/"under" from each special matchup's
    # participants list (alignment is "neutral" for both legs; name is "Over"/"Under").
    prop_meta = {}
    participant_name_by_id = {}
    for m in related:
        for p in m.get("participants") or []:
            pid = p.get("id")
            name = (p.get("name") or "").lower()
            if pid is not None and name in ("over", "under"):
                participant_name_by_id[pid] = name
        if m.get("type") != "special":
            continue
        sp = m.get("special") or {}
        if sp.get("category") != "Player Props":
            continue
        player, stat = parse_prop_description(sp.get("description", ""))
        if not player:
            continue
        prop_meta[m["id"]] = (player, stat, m.get("units"))

    if not prop_meta:
        return [], {}, [], None

    try:
        markets = fetch_live_matchup_markets(parent_id)
    except requests.RequestException as e:
        return [], {}, [], f"  ! prop markets failed for {parent_id}: {e}"

    home, away = matchup_participants(parent_matchup)
    matchup_str = f"{home} vs {away}"
    rows = []
    fp_updates = {}
    log_lines = []
    for mk in markets:
        prop_id = mk.get("matchupId")
        if prop_id not in prop_meta:
            continue
        player, stat, units = prop_meta[prop_id]
        raw_prices = mk.get("prices") or []
        line = next((p.get("points") for p in raw_prices if p.get("points") is not None), None)

        # Stamp each price with its Over/Under designation via participantId lookup.
        # Pinnacle prop participants carry name="Over"/"Under"; alignment is "neutral"
        # for both legs and cannot be used for discrimination.
        labeled_prices = []
        for p in raw_prices:
            desig = participant_name_by_id.get(p.get("participantId"))
            if desig is None:
                log(f"  ! PROP designation unknown participantId={p.get('participantId')}"
                    f" {player} ({stat}) matchupId={prop_id}")
            labeled_prices.append({**p, "designation": desig})

        # Custom fingerprint key: prop child id is unique per (player, stat).
        k = f"prop|{prop_id}"
        fp = market_fingerprint(mk)
        fp_updates[k] = fp
        if prev_fps.get(k) != fp:
            tag = "NEW" if prev_fps.get(k) is None else "CHG"
            ps = " | ".join(
                f"{p.get('designation','?')}={p.get('price'):+d}"
                if isinstance(p.get('price'), int) else f"{p.get('designation','?')}={p.get('price')}"
                for p in labeled_prices
            )
            log_lines.append(f"{tag} {matchup_str} PROP {player} ({stat}) line={line}: {ps}")

        rows.append({
            "sport": sport_name,
            "league": league_name,
            "matchupId": parent_id,
            "prop_matchupId": prop_id,
            "matchup": matchup_str,
            "startTime": parent_matchup.get("startTime"),
            "isLive": False,
            "period": 0,
            "type": "player_prop",
            "player": player,
            "stat": stat,
            "stat_units": units,
            "line": line,
            "prices": labeled_prices,
        })

    return rows, fp_updates, log_lines, None


def run_cycle(prev_fps):
    """Execute one poll cycle. Returns (new_fps dict, stats)."""
    _gate.reset_and_get_429()  # discard any stale count from a previous cycle
    cycle_start = time.monotonic()

    now = datetime.now(timezone.utc)
    earliest = now - timedelta(hours=LIVE_LOOKBACK_HOURS)
    latest = now + timedelta(hours=WINDOW_HOURS)

    sports = fetch_sports()
    new_fps = {}
    changes = 0
    pregame_count = 0
    live_count = 0
    market_count = 0
    prop_row_count = 0
    snapshot = []

    # Phase A: serial per-sport fetches. Record bulk (pregame) markets inline;
    # accumulate work lists for the parallel phases below.
    live_work = []   # (sub, sport_name)
    prop_work = []   # (parent, sport_name)
    phase_a_start = time.monotonic()

    for sport in sports:
        sid = sport["id"]
        sname = sport.get("name", f"sport-{sid}")
        try:
            raw = fetch_raw_matchups(sid)
        except requests.HTTPError as e:
            log(f"  ! matchups fetch failed for {sname}: {e}")
            continue

        pregame, live = classify_matchups(raw, now, earliest, latest)
        if not pregame and not live:
            continue

        pregame_by_id = {m["id"]: m for m in pregame}
        pregame_count += len(pregame)
        live_count += len(live)

        # Bulk markets: only useful for pregame matchups.
        try:
            bulk = fetch_bulk_markets(sid)
        except requests.HTTPError as e:
            log(f"  ! bulk markets fetch failed for {sname}: {e}")
            bulk = []

        for market in bulk:
            mid = market.get("matchupId")
            if mid not in pregame_by_id:
                continue
            market_count += 1
            if record_market(market, pregame_by_id[mid], sname, False,
                             snapshot, prev_fps, new_fps, log):
                changes += 1

        for sub in live:
            live_work.append((sub, sname))

        # Player props: 2 extra calls per pregame parent in NBA/NHL. Pre-filter
        # by league.name so we don't burn calls on non-prop leagues.
        if INCLUDE_PROPS and sname in PROP_SPORTS:
            for parent in pregame:
                if ((parent.get("league") or {}).get("name")) not in PROP_LEAGUES:
                    continue
                prop_work.append((parent, sname))

    phase_a_sec = time.monotonic() - phase_a_start

    # Phase B: parallel live market fetch and merge.
    phase_b_start = time.monotonic()
    if live_work and _running:
        def fetch_live(sub, sport_name):
            try:
                return sub, sport_name, fetch_live_matchup_markets(sub["id"]), None
            except requests.RequestException as e:
                return sub, sport_name, [], e

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [ex.submit(fetch_live, sub, sname) for sub, sname in live_work]
            for fut in as_completed(futures):
                sub, sname, lm, err = fut.result()
                if err is not None:
                    log(f"  ! live market fetch failed for {sub['id']}: {err}")
                    continue
                for market in lm:
                    # Only take markets that belong to this specific sub-matchup
                    # (the related/straight response includes sibling-sub props).
                    if market.get("matchupId") != sub["id"]:
                        continue
                    market = inject_designation(market, sub)
                    market_count += 1
                    if record_market(market, sub, sname, True,
                                     snapshot, prev_fps, new_fps, log):
                        changes += 1

    phase_b_sec = time.monotonic() - phase_b_start

    # Phase C: parallel prop fetch and merge.
    phase_c_start = time.monotonic()
    if prop_work and _running:
        def fetch_props(parent, sport_name):
            return parent, sport_name, collect_prop_rows(parent, sport_name, prev_fps)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [ex.submit(fetch_props, parent, sname) for parent, sname in prop_work]
            for fut in as_completed(futures):
                parent, sname, (rows, fp_updates, log_lines, err_msg) = fut.result()
                if err_msg:
                    log(err_msg)
                for line in log_lines:
                    log(line)
                snapshot.extend(rows)
                new_fps.update(fp_updates)
                prop_row_count += len(rows)
                changes += len(log_lines)

    phase_c_sec = time.monotonic() - phase_c_start

    rate_limit_429 = _gate.reset_and_get_429()
    write_sec = 0.0
    if snapshot:
        write_now = datetime.now(timezone.utc)
        captured_at = write_now.isoformat()
        # Stamp capture time on freshly-fetched rows; setdefault preserves the
        # stamp on any rows carried forward from a prior cycle (tiered cadence).
        for row in snapshot:
            row.setdefault("captured_at", captured_at)
        snap_path = os.path.join(
            SNAPSHOT_DIR, write_now.strftime("%Y%m%dT%H%M%SZ") + ".jsonl"
        )
        write_start = time.monotonic()
        atomic_write_jsonl(
            snap_path, snapshot, dumps_kwargs={"default": str}, logger=log
        )
        write_sec = time.monotonic() - write_start
        cycle_elapsed_sec = time.monotonic() - cycle_start
        write_snapshot_meta(snap_path, {
            "captured_at": captured_at,
            "cycle_elapsed_sec": cycle_elapsed_sec,
            "phase_a_sec": phase_a_sec,
            "phase_b_sec": phase_b_sec,
            "phase_c_sec": phase_c_sec,
            "write_sec": write_sec,
            "rate_limit_429": rate_limit_429,
        }, logger=log)
        prune_snapshots(SNAPSHOT_DIR, SNAPSHOT_RETENTION)
    else:
        cycle_elapsed_sec = time.monotonic() - cycle_start

    return new_fps, {
        "sports": len(sports),
        "pregame": pregame_count,
        "live": live_count,
        "markets": market_count,
        "prop_rows": prop_row_count,
        "changes": changes,
        "phase_a_sec": phase_a_sec,
        "phase_b_sec": phase_b_sec,
        "phase_c_sec": phase_c_sec,
        "write_sec": write_sec,
        "cycle_elapsed_sec": cycle_elapsed_sec,
        "rate_limit_429": rate_limit_429,
    }


def main():
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    install_shutdown_handlers(_running, logger=log)

    log(
        f"poller starting: window=+{WINDOW_HOURS}h  live_lookback=-{LIVE_LOOKBACK_HOURS}h  "
        f"interval={POLL_INTERVAL_SEC}s  include_props={INCLUDE_PROPS}"
    )
    prev_fps = {}
    cycle = 0
    while _running:
        cycle += 1
        t0 = time.time()
        try:
            prev_fps, stats = run_cycle(prev_fps)
            elapsed = time.time() - t0
            log(
                f"cycle {cycle} ok: sports={stats['sports']} "
                f"pregame={stats['pregame']} live={stats['live']} "
                f"markets={stats['markets']} prop_rows={stats['prop_rows']} "
                f"changes={stats['changes']} elapsed={elapsed:.1f}s "
                f"phaseA={stats['phase_a_sec']:.1f}s "
                f"phaseB={stats['phase_b_sec']:.1f}s "
                f"phaseC={stats['phase_c_sec']:.1f}s "
                f"write={stats['write_sec']:.2f}s "
                f"429={stats['rate_limit_429']}"
            )
        except Exception as e:
            log(f"cycle {cycle} FAILED: {type(e).__name__}: {e}")

        sleep_until_next_cycle(t0, POLL_INTERVAL_SEC, _running)

    log("poller stopped cleanly")


if __name__ == "__main__":
    main()
