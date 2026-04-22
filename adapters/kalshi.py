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

from .common import NormalizedMarket

BOOK = "kalshi"

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_DIR = os.path.join(DIR, "data", "kalshi_snapshots")

BASE = "https://api.elections.kalshi.com/trade-api/v2"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
REQUEST_TIMEOUT = 10

FEE_RATE = 0.05

# Series ticker classifiers.
MONEYLINE_SUFFIX = re.compile(r"(GAME|ML|H2H)$")
SPREAD_SUFFIX = re.compile(r"SPREAD$")
TOTAL_SUFFIX = re.compile(r"TOTAL$")
TEAMTOTAL_MARKER = "TEAMTOTAL"
_KALSHI_HALF_RE = re.compile(r"([12])H(?=SPREAD|TOTAL)")
_KALSHI_F5_MARKER = "F5"

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
    if _KALSHI_F5_MARKER in st:
        return None

    yes_sub = raw.get("yes_sub_title") or ""
    title = raw.get("title") or ""
    # Use occurrence_datetime only for the primary anchor — expected_expiration_time
    # is game-end, not game-start, on some series. When occurrence_datetime is
    # missing, the matcher falls back via `fallback_anchor` below.
    start_time = raw.get("occurrence_datetime") or ""
    event_id = event_suffix(raw.get("event_ticker"))
    period_label = kalshi_period_label(st)
    if period_label == "F5":
        return None

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
        # YES-side team resolution against Pinnacle happens in the matcher;
        # we just carry the raw yes_sub_title text for that stage.
        return _nm("moneyline", None, yes_sub.strip() or None, None)

    return None


def parse_moneyline_teams(normalized):
    """Return (team_a, team_b) from the underlying Kalshi title, or None."""
    return parse_title_teams(normalized.raw.get("title", ""))


def event_group_key(normalized):
    """Group ML sibling with spread/total/team_total markets for the same game."""
    return normalized.event_id or ""


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
    """Fee deducted from the winning payoff per share. Kalshi: 5% of profit."""
    return FEE_RATE * (1.0 - price)


def fee_on_stake_per_share(price):
    """Fee paid upfront on stake per share. Kalshi: zero (fees only on win)."""
    return 0.0


def taker_fee_per_share(price, fair_prob):
    """Expected per-share fee at the given fill price, given fair probability.
    Derivable as fair_prob * fee_on_win(p) + fee_on_stake(p)."""
    return fair_prob * fee_on_win_per_share(price) + fee_on_stake_per_share(price)


# ---------------------------------------------------------------------------
# Live API calls
# ---------------------------------------------------------------------------

def fetch_yes_ask_ladder(market_id):
    """Return ascending YES-ask ladder derived from Kalshi's NO-bid book."""
    r = requests.get(f"{BASE}/markets/{market_id}/orderbook",
                     headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    ob = r.json().get("orderbook_fp") or r.json().get("orderbook") or {}
    no_side = ob.get("no_dollars") or ob.get("no") or []
    ladder = []
    for entry in no_side:
        try:
            no_price = float(entry[0])
            qty = int(float(entry[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if qty <= 0:
            continue
        yes_ask = round(1.0 - no_price, 4)
        if yes_ask <= 0 or yes_ask >= 1:
            continue
        ladder.append((yes_ask, qty))
    ladder.sort(key=lambda x: x[0])
    return ladder


def fetch_settlement(market_id):
    """Return 'yes' | 'no' | None based on Kalshi's market status+result."""
    r = requests.get(f"{BASE}/markets/{market_id}",
                     headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    market = r.json().get("market") or {}
    status = (market.get("status") or "").lower()
    result = (market.get("result") or "").lower()
    if result not in ("yes", "no"):
        return None
    if status not in ("settled", "finalized", "closed") and not result:
        return None
    return result
