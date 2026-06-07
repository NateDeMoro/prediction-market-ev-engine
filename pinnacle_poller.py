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

POLL_INTERVAL_SEC    = config.POLLER_INTERVAL_SEC
WINDOW_HOURS         = config.PINNACLE_WINDOW_HOURS
LIVE_LOOKBACK_HOURS  = config.PINNACLE_LIVE_LOOKBACK_HOURS
REQUEST_TIMEOUT      = config.POLLER_REQUEST_TIMEOUT
INTER_REQUEST_SLEEP  = config.PINNACLE_INTER_REQUEST_SLEEP
SNAPSHOT_RETENTION   = config.POLLER_SNAPSHOT_RETENTION

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


def get(path, **params):
    time.sleep(INTER_REQUEST_SLEEP)
    r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
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


def collect_prop_rows(parent_matchup, sport_name, snapshot, prev_fps, new_fps):
    """For a pregame parent matchup in a prop-target league, fetch and emit
    per-player prop rows. Two HTTP calls: /related (enumerate child matchups +
    player/stat metadata) and /markets/related/straight (price rows for the
    whole tree). Returns (rows_emitted, changes_emitted) or (0, 0) on skip.
    """
    parent_id = parent_matchup["id"]
    try:
        related = fetch_related_matchups(parent_id)
    except requests.HTTPError as e:
        log(f"  ! prop /related failed for {parent_id}: {e}")
        return 0, 0

    # Verify league via the parent record returned in /related (carries league.name).
    parent_rec = next((m for m in related if m.get("id") == parent_id), None)
    league_name = ((parent_rec or {}).get("league") or {}).get("name")
    if league_name not in PROP_LEAGUES:
        return 0, 0

    # Build child_matchupId -> (player, stat, units) for every player-prop child.
    prop_meta = {}
    for m in related:
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
        return 0, 0

    try:
        markets = fetch_live_matchup_markets(parent_id)
    except requests.HTTPError as e:
        log(f"  ! prop markets failed for {parent_id}: {e}")
        return 0, 0

    home, away = matchup_participants(parent_matchup)
    matchup_str = f"{home} vs {away}"
    rows_emitted = 0
    changes_emitted = 0
    for mk in markets:
        prop_id = mk.get("matchupId")
        if prop_id not in prop_meta:
            continue
        player, stat, units = prop_meta[prop_id]
        prices = mk.get("prices") or []
        # Prop price rows have no 'designation'; line lives in prices[*].points.
        line = next((p.get("points") for p in prices if p.get("points") is not None), None)

        # Custom fingerprint key: prop child id is unique per (player, stat).
        k = f"prop|{prop_id}"
        fp = market_fingerprint(mk)
        new_fps[k] = fp
        if prev_fps.get(k) != fp:
            tag = "NEW" if prev_fps.get(k) is None else "CHG"
            ps = " | ".join(
                f"{p.get('participantId','?')}={p.get('price'):+d}"
                if isinstance(p.get('price'), int) else f"{p.get('participantId','?')}={p.get('price')}"
                for p in prices
            )
            log(f"{tag} {matchup_str} PROP {player} ({stat}) line={line}: {ps}")
            changes_emitted += 1

        snapshot.append({
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
            "prices": prices,
        })
        rows_emitted += 1

    return rows_emitted, changes_emitted


def run_cycle(prev_fps):
    """Execute one poll cycle. Returns (new_fps dict, stats)."""
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

        # Live markets: one fetch per live sub-matchup.
        for sub in live:
            try:
                lm = fetch_live_matchup_markets(sub["id"])
            except requests.HTTPError as e:
                log(f"  ! live market fetch failed for {sub['id']}: {e}")
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

        # Player props: 2 extra calls per pregame parent in NBA/NHL. Pre-filter
        # by league.name (carried on every raw matchup) so we don't burn calls
        # on Brazilian NBB / AHL / KHL / Euroleague etc. — Kalshi has no prop
        # series for those, so any matches would be impossible.
        if INCLUDE_PROPS and sname in PROP_SPORTS:
            for parent in pregame:
                if ((parent.get("league") or {}).get("name")) not in PROP_LEAGUES:
                    continue
                rows, ch = collect_prop_rows(parent, sname, snapshot, prev_fps, new_fps)
                prop_row_count += rows
                changes += ch

    if snapshot:
        snap_path = os.path.join(
            SNAPSHOT_DIR, now.strftime("%Y%m%dT%H%M%SZ") + ".jsonl"
        )
        atomic_write_jsonl(
            snap_path, snapshot, dumps_kwargs={"default": str}, logger=log
        )
        prune_snapshots(SNAPSHOT_DIR, SNAPSHOT_RETENTION)

    return new_fps, {
        "sports": len(sports),
        "pregame": pregame_count,
        "live": live_count,
        "markets": market_count,
        "prop_rows": prop_row_count,
        "changes": changes,
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
                f"changes={stats['changes']} elapsed={elapsed:.1f}s"
            )
        except Exception as e:
            log(f"cycle {cycle} FAILED: {type(e).__name__}: {e}")

        sleep_until_next_cycle(t0, POLL_INTERVAL_SEC, _running)

    log("poller stopped cleanly")


if __name__ == "__main__":
    main()
