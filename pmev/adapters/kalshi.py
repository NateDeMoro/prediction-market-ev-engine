"""
Kalshi soft-book adapter.

Normalizes Kalshi snapshot rows into `NormalizedMarket` instances and
wraps Kalshi-specific API calls (orderbook, settlement) behind the
adapter surface declared in `adapters/__init__.py`.

Kalshi encodes most structural info in the series ticker (suffix =
market type, 1H/2H marker = period) and the YES sub-title (team /
over-under / line). We parse all of it here so the matcher and EV
pipeline can stay book-agnostic.
"""
import os
import re

import requests

from .common import (
    NormalizedMarket,
    canonical_stat,
    resolve_team_abbrev,
    series_ticker_league,
)
from pmev import config

BOOK = "kalshi"
SUPPORTS_NO_SIDE = True

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_DIR = config.KALSHI_SNAPSHOT_DIR

BASE = "https://api.elections.kalshi.com/trade-api/v2"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
REQUEST_TIMEOUT = config.ADAPTER_READ_TIMEOUT

# Kalshi taker fee per the official fee schedule (effective 2026-02-05).
# Update config.PER_BOOK_FEE_RATE["kalshi"] when the schedule changes.
FEE_RATE = config.PER_BOOK_FEE_RATE["kalshi"]

# Series ticker classifiers.
MONEYLINE_SUFFIX = re.compile(r"(GAME|ML|H2H)$")
SPREAD_SUFFIX = re.compile(r"SPREAD$")
TOTAL_SUFFIX = re.compile(r"TOTAL$")
TEAMTOTAL_MARKER = "TEAMTOTAL"
_KALSHI_HALF_RE = re.compile(r"([12])H(?=SPREAD|TOTAL)")
_KALSHI_F5_MARKER = "F5"

# Per-game player-prop series allowlist; mirrors kalshi_poller.PER_GAME_PROP_SERIES
# so the adapter recognizes the exact tickers the poller ingests. Kept here (not
# imported from the poller) so adapters don't depend on poller module state.
PROP_SERIES = {
    # NBA
    "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBABLK", "KXNBASTL", "KXNBA3PT",
    "KXNBAPRA", "KXNBAPR", "KXNBARA", "KXNBAPA",
    # NHL
    "KXNHLPTS", "KXNHLGOALS", "KXNHLSOG",
    # NFL / MLB — ingested by poller but not matched yet (no Pinnacle prop
    # coverage validated). Accepted here so we don't silently drop them.
    "KXNFLPASSYDS", "KXNFLPASSTDS", "KXNFLRSHYDS", "KXNFLRECYDS",
    "KXNFLREC", "KXNFLANYTD", "KXNFLNEXTTD",
}

# Stat portion of a prop series ticker: everything after "KX<sport>". Used to
# pull the canonical stat key via common.canonical_stat().
_PROP_SERIES_STAT_RE = re.compile(r"^KX(?:NBA|NHL|NFL|MLB)(?P<stat>.+)$")

# "Sidney Crosby: 3+ points" -> ("Sidney Crosby", 3)
_PROP_TITLE_RE = re.compile(
    r"^(?P<player>[^:]+):\s*(?P<n>\d+(?:\.\d+)?)\+\s*\w+\s*$"
)

# "KXNBAREB-26APR22ORLDET-ORLWCARTER34-8": per-player segment is the third
# hyphen-delimited chunk. Leading 3 chars are the player's team abbreviation.
_PROP_TEAM_ABBREV_RE = re.compile(r"^[-]?(?P<abbr>[A-Z]{2,3})")

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

# Title parsers for the moneyline stage.
_TITLE_PRIMARY = re.compile(
    r"^(?:Game\s*\d+:\s*)?(.+?)\s+(?:vs|at)\s+(.+?)\s+Winner\??$",
    re.I,
)
_TITLE_WILL_WIN = re.compile(
    r"^Will\s+.+?\s+win\s+the\s+(.+?)\s+vs\.?\s+(.+?)\s+.+?\s*match\??$",
    re.I,
)

# Sub-title parsers for non-mainline markets.
_KS_TOTAL_OVER_RE = re.compile(r"Over\s+(\d+(?:\.\d+)?)", re.I)
_KS_SPREAD_SUB_RE = re.compile(
    r"^(.+?)\s+wins?\s+(?:the\s+[12]H\s+)?by\s+over\s+(\d+(?:\.\d+)?)",
    re.I,
)
_KS_TEAM_TOTAL_SUB_RE = re.compile(
    r"^(.+?)\s+(over|under)\s+(\d+(?:\.\d+)?)",
    re.I,
)


# ---------------------------------------------------------------------------
# Parsers / classifiers (previously in market_matcher.py)
# ---------------------------------------------------------------------------

def parse_title_teams(title):
    """Return (team_a, team_b) from a Kalshi moneyline title, or None."""
    if not title:
        return None
    m = _TITLE_PRIMARY.match(title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = _TITLE_WILL_WIN.match(title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None


def date_from_ticker(ticker):
    """Parse '26APR20' date token out of a Kalshi ticker. Returns 'YYYY-MM-DD' or ''."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker or "")
    if not m:
        return ""
    yy, mmm, dd = m.group(1), m.group(2).lower(), m.group(3)
    if mmm not in MONTHS:
        return ""
    return f"20{yy}-{MONTHS[mmm]:02d}-{dd}"


def _is_half_point(x):
    try:
        return not float(x).is_integer()
    except (TypeError, ValueError):
        return False


def kalshi_period_label(series_ticker):
    """FULL / 1H / 2H from the series suffix. F5 returned as 'F5' (caller skips)."""
    st = series_ticker or ""
    if _KALSHI_F5_MARKER in st:
        return "F5"
    m = _KALSHI_HALF_RE.search(st)
    return f"{m.group(1)}H" if m else "FULL"


def event_suffix(event_ticker):
    """Portion of a Kalshi event_ticker after the first '-' (shared across series)."""
    if not event_ticker or "-" not in event_ticker:
        return ""
    return event_ticker.split("-", 1)[1]


def parse_total_line(yes_sub_title):
    if not yes_sub_title:
        return None
    m = _KS_TOTAL_OVER_RE.search(yes_sub_title)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def parse_spread_sub(yes_sub_title):
    """Return (team, line) or None."""
    if not yes_sub_title:
        return None
    m = _KS_SPREAD_SUB_RE.match(yes_sub_title)
    if not m:
        return None
    try:
        return m.group(1).strip(), float(m.group(2))
    except ValueError:
        return None


def parse_team_total_sub(yes_sub_title):
    """Return (team, 'over'|'under', line) or None."""
    if not yes_sub_title:
        return None
    m = _KS_TEAM_TOTAL_SUB_RE.match(yes_sub_title)
    if not m:
        return None
    try:
        return m.group(1).strip(), m.group(2).lower(), float(m.group(3))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Adapter surface
# ---------------------------------------------------------------------------

def normalize_market(raw):
    """Classify a raw Kalshi snapshot row into a NormalizedMarket.

    Returns None for rows that are out of scope (unparseable, F5, whole-
    number lines, unknown series type).
    """
    st = raw.get("series_ticker") or ""
    ticker = raw.get("ticker")
    if not ticker:
        return None

    yes_sub = raw.get("yes_sub_title") or ""
    title = raw.get("title") or ""
    # Use occurrence_datetime only for the primary anchor — expected_expiration_time
    # is game-end, not game-start, on some series. When occurrence_datetime is
    # missing, the matcher falls back via `fallback_anchor` below.
    start_time = raw.get("occurrence_datetime") or ""
    event_id = event_suffix(raw.get("event_ticker"))
    period_label = kalshi_period_label(st)
    # F5 (first-5-innings) is wired for the clean 2-way types (spread/total)
    # against Pinnacle's period-1 line; the moneyline branch drops F5 (3-way).
    # Disabled wholesale via config.F5_ENABLED.
    if period_label == "F5" and not config.F5_ENABLED:
        return None
    # League hint: lets the matcher constrain Pinnacle candidates by sport
    # (MLB->Baseball, NHL->Hockey, ...) to avoid cross-league city-name collisions.
    league = series_ticker_league(st)

    def _nm(market_type, line, team, side):
        return NormalizedMarket(
            book=BOOK,
            market_id=ticker,
            event_id=event_id,
            title=title,
            yes_sub_title=yes_sub or None,
            market_type=market_type,
            period_label=period_label,
            line=line,
            team=team,
            side=side,
            start_time=start_time,
            raw=raw,
            league=league,
        )

    if TEAMTOTAL_MARKER in st:
        parsed = parse_team_total_sub(yes_sub)
        if not parsed:
            return None
        team, side, line = parsed
        if not _is_half_point(line):
            return None
        return _nm("team_total", line, team, side)

    if SPREAD_SUFFIX.search(st):
        parsed = parse_spread_sub(yes_sub)
        if not parsed:
            return None
        team, line = parsed
        if not _is_half_point(line):
            return None
        return _nm("spread", line, team, None)

    if TOTAL_SUFFIX.search(st):
        line = parse_total_line(yes_sub)
        if line is None or not _is_half_point(line):
            return None
        return _nm("total", line, None, None)

    if MONEYLINE_SUFFIX.search(st):
        if period_label == "F5":
            return None  # F5 moneyline is 3-way (tie) — out of scope
        # YES-side team resolution against Pinnacle happens in the matcher;
        # we just carry the raw yes_sub_title text for that stage.
        return _nm("moneyline", None, yes_sub.strip() or None, None)

    if st in PROP_SERIES:
        # Kalshi N+ threshold -> Pinnacle Over N-0.5. Player and N come from
        # the title; team abbrev comes from the ticker's per-player segment.
        m_title = _PROP_TITLE_RE.match(title)
        if not m_title:
            return None
        try:
            n = float(m_title.group("n"))
        except ValueError:
            return None
        player = m_title.group("player").strip()
        pin_line = n - 0.5

        # Canonical stat from series suffix (more robust than the plural
        # word in `title`; e.g. "threes" vs "3PT" vs "ThreePointersMade").
        m_st = _PROP_SERIES_STAT_RE.match(st)
        stat_canonical = canonical_stat(m_st.group("stat")) if m_st else None
        if stat_canonical is None:
            return None

        # Team abbreviation: third hyphen-delimited chunk's leading chars. Most
        # Kalshi codes are 3 chars (NBA: LAL/BOS/...; NHL: ANA/EDM/...), but a
        # handful of NHL clubs use a 2-char code (TB Lightning, and historically
        # NJ/SJ/LA) with the player's first-name initial occupying char 3.
        # Try 3-char first, then fall back to 2-char.
        parts = ticker.split("-")
        chunk = parts[2] if len(parts) >= 3 else ""
        pin_team = None
        for width in (3, 2):
            if len(chunk) >= width:
                pin_team = resolve_team_abbrev(chunk[:width], league)
                if pin_team:
                    break

        nm = NormalizedMarket(
            book=BOOK,
            market_id=ticker,
            event_id=event_id,
            title=title,
            yes_sub_title=yes_sub or None,
            market_type="player_prop",
            period_label="FULL",
            line=pin_line,
            team=pin_team,
            side="over",  # Kalshi YES = over; matcher uses this to pick Pinnacle side
            start_time=start_time,
            raw=raw,
            player=player,
            stat=stat_canonical,
            league=league,
        )
        return nm

    return None


def parse_moneyline_teams(normalized):
    """Return (team_a, team_b) from the underlying Kalshi title, or None."""
    return parse_title_teams(normalized.raw.get("title", ""))


def event_group_key(normalized):
    """Group ML sibling with spread/total/team_total markets for the same game.

    Namespaced by league because Kalshi reuses the same date+team suffix
    across series (NHL `KXNHLGAME-26APR22DALMIN` and MLS `KXMLSGAME-26APR22DALMIN`
    share `26APR22DALMIN`). Without the league prefix the ML matches overwrite
    each other in market_matcher.event_to_pin, then non-ML markets inherit
    whichever was written last — which produced cross-sport matches on 2026-04-22.
    """
    lg = normalized.league or "?"
    return f"{lg}:{normalized.event_id or ''}"


def fallback_anchor(normalized):
    """(iso_str, tolerance_sec) when NormalizedMarket.start_time is empty.

    Kalshi: recover the game date from the ticker (e.g. `26APR20`) and peg
    it to T12:00:00Z with a 30h tolerance. Used for the ~3% of Kalshi rows
    where `occurrence_datetime` is missing but the ticker still carries a
    date token. Returns (None, None) if no fallback can be derived.
    """
    ticker_date = date_from_ticker(normalized.raw.get("ticker"))
    if not ticker_date:
        return None, None
    return ticker_date + "T12:00:00Z", 30 * 3600


def market_url(normalized):
    ev = normalized.raw.get("event_ticker") or ""
    slug = ev.lower().split("-")[0] if "-" in ev else ev.lower()
    return f"https://kalshi.com/markets/{slug}" if slug else "https://kalshi.com"


def fee_on_win_per_share(price):
    """Fee deducted from the winning payoff per share. Kalshi charges its
    trading fee upfront at fill (see `fee_on_stake_per_share`), so winners
    collect the full $1/contract — nothing is skimmed on settlement."""
    return 0.0


def fee_on_stake_per_share(price):
    """Trading fee paid upfront at fill per share: 0.07 * P * (1-P).

    Per-trade cent rounding (Kalshi rounds the total fee up to the next cent
    per trade) is ignored here — a second-order effect that averages out over
    many-share fills and is immaterial to EV / Kelly math.
    """
    return FEE_RATE * price * (1.0 - price)


def taker_fee_per_share(price, fair_prob):
    """Expected per-share fee at the given fill price. Upfront fee is
    deterministic (paid regardless of outcome), so `fair_prob` is unused —
    kept in the signature for adapter-interface symmetry."""
    return fee_on_stake_per_share(price)


# ---------------------------------------------------------------------------
# Live API calls
# ---------------------------------------------------------------------------

def _parse_ladder(entries):
    """Transform a raw Kalshi book side ([[bid_price, qty], ...]) into an
    ascending ask ladder in the complement's price space.

    Feeding the `no_dollars` side yields YES-asks; feeding `yes_dollars` yields
    NO-asks. Filters out zero-qty and out-of-range entries the same way either
    direction.
    """
    ladder = []
    for entry in entries:
        try:
            bid_price = float(entry[0])
            qty = int(float(entry[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if qty <= 0:
            continue
        ask = round(1.0 - bid_price, 4)
        if ask <= 0 or ask >= 1:
            continue
        ladder.append((ask, qty))
    ladder.sort(key=lambda x: x[0])
    return ladder


def _fetch_orderbook(market_id):
    r = requests.get(f"{BASE}/markets/{market_id}/orderbook",
                     headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    return body.get("orderbook_fp") or body.get("orderbook") or {}


def fetch_yes_ask_ladder(market_id):
    """Return ascending YES-ask ladder derived from Kalshi's NO-bid book."""
    ob = _fetch_orderbook(market_id)
    return _parse_ladder(ob.get("no_dollars") or ob.get("no") or [])


def fetch_no_ask_ladder(market_id):
    """Return ascending NO-ask ladder derived from Kalshi's YES-bid book."""
    ob = _fetch_orderbook(market_id)
    return _parse_ladder(ob.get("yes_dollars") or ob.get("yes") or [])


def fetch_both_ladders(market_id):
    """Return (yes_ask_ladder, no_ask_ladder) parsed from one orderbook call.

    use when: the caller needs both sides (EV scanner, dashboard). Halves
    traffic vs. calling `fetch_yes_ask_ladder` + `fetch_no_ask_ladder`.
    """
    ob = _fetch_orderbook(market_id)
    yes_ladder = _parse_ladder(ob.get("no_dollars") or ob.get("no") or [])
    no_ladder = _parse_ladder(ob.get("yes_dollars") or ob.get("yes") or [])
    return yes_ladder, no_ladder


def fetch_settlement(market_id):
    """Return 'yes' | 'no' | float | None from Kalshi's market status+result.

    Binary markets resolve with result 'yes'/'no'. Some sports markets resolve
    'scalar' (postponed/cancelled past the 2-day window, or ties) — finalized
    with a fractional `settlement_value_dollars` per-YES-share payout instead of
    a clean winner. For those, return that payout as a float in [0,1]; the caller
    credits the side-correct share of it. Fail closed (None) if the scalar value
    is missing/unparseable, leaving the position open to retry."""
    r = requests.get(f"{BASE}/markets/{market_id}",
                     headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    market = r.json().get("market") or {}
    status = (market.get("status") or "").lower()
    result = (market.get("result") or "").lower()
    finalized = status in ("settled", "finalized", "closed")
    if result == "scalar" and finalized:
        try:
            payout = float(market.get("settlement_value_dollars"))
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, payout))
    if result not in ("yes", "no"):
        return None
    if not finalized and not result:
        return None
    return result
