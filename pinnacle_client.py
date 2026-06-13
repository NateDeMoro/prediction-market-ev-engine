"""Low-level Pinnacle read client, shared by the poller and the decision-time
re-fetch in `engine` (#6b).

Open when: changing how Pinnacle is fetched, or how a raw Pinnacle market becomes a
snapshot row. `market_to_row` is the single source of truth for the snapshot-row schema;
`pinnacle_poller.record_market` delegates to it.

The API key is read **lazily** — only when a request is actually built (`_headers`) —
so a key-less process (a read-only dashboard, the CLI) can import this module without
exporting PINNACLE_API_KEY. Contrast `pinnacle_poller`, which requires the key at import.
"""
import os

import requests

from data_utils import RateGate
import config

BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
REQUEST_TIMEOUT        = config.POLLER_REQUEST_TIMEOUT
INTER_REQUEST_SLEEP    = config.PINNACLE_INTER_REQUEST_SLEEP
RATE_LIMIT_RETRIES     = config.PINNACLE_RATE_LIMIT_RETRIES
RATE_LIMIT_BACKOFF_SEC = config.PINNACLE_RATE_LIMIT_BACKOFF_SEC

# Own gate instance: in the dashboard/engine process (which does not run the poller)
# this spaces the decision-time re-fetches; it never shares the poller's gate.
_gate = RateGate(INTER_REQUEST_SLEEP)


def _headers():
    """Build request headers, reading PINNACLE_API_KEY at call time (lazy)."""
    key = os.environ.get("PINNACLE_API_KEY")
    if not key:
        raise RuntimeError(
            "PINNACLE_API_KEY env var is required for a live Pinnacle fetch. "
            "Export it in the process that runs the engine/placement path."
        )
    return {
        "X-API-Key": key,
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Referer": "https://www.pinnacle.com/",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }


def get(path, **params):
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        _gate.claim_slot()
        r = requests.get(f"{BASE}{path}", headers=_headers(), params=params,
                         timeout=REQUEST_TIMEOUT)
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
    """Sports with at least one matchup (objects carry `id` and `name`)."""
    return [s for s in get("/sports") if s.get("matchupCount", 0) > 0]


def fetch_bulk_markets(sport_id):
    """All straight markets for a sport in one call (pregame; designation inline)."""
    return get(f"/sports/{sport_id}/markets/straight", primaryOnly="false")


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


def market_to_row(market, matchup, sport_name, is_live):
    """Single source of truth for the snapshot-row schema. Pure: no fingerprint /
    log / shared-state side effects (those stay in `pinnacle_poller.record_market`)."""
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
    # team_total rows come in a pair per (matchupId, period, points) distinguished
    # only by the top-level `side`. Preserve it so the matcher pairs the right side.
    if market.get("type") == "team_total":
        row["side"] = market.get("side")
    return row
