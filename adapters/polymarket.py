"""
Polymarket US soft-book adapter.

Normalizes Polymarket snapshot rows (one per marketSide) into
`NormalizedMarket` instances and wraps Polymarket-specific API calls
(order book, settlement) behind the adapter surface declared in
`adapters/__init__.py`.

Scope mirrors Kalshi but is strictly narrower — Polymarket US currently
publishes only FULL-game moneyline / spread / total, with no half-game
markets, no team-totals, and no props. The adapter filters in
`normalize_market` accordingly.

Market-id convention: `"{market_slug}:{long|short}"`. Both sides of a
Polymarket market share a single book endpoint keyed on the slug; YES-ask
on `{slug}:long` reads offers[] directly and YES-ask on `{slug}:short`
reads `1 - bids[]`. The NO ladder for each market_id is derived from the
opposite side of that same book (one API call surfaces both).

Moneyline emission drops the short side so each Polymarket event yields
one NormalizedMarket per market type — the sibling team is reachable via
NO-side scanning rather than a duplicate market_id. Spread and total were
already single-sided (favored / Over only), so NO-side is the only way to
reach dog-spread / under-total exposure.
"""
import os
import re

import requests

from .common import NormalizedMarket

BOOK = "polymarket"
SUPPORTS_NO_SIDE = True

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_DIR = os.path.join(DIR, "data", "polymarket_snapshots")

GATEWAY = "https://gateway.polymarket.us"
HEADERS = {"User-Agent": "Mozilla/5.0 (ev-scanner)", "Accept": "application/json"}
REQUEST_TIMEOUT = 10

# Polymarket taker fee per the published schedule:
#   fees = C * feeRate * P * (1-P)
# Charged upfront at fill, independent of outcome. Sports rate = 0.03 (peaks at
# $0.0075/share at P=0.5); crypto rate = 0.072 and is out-of-scope. Makers pay
# zero and receive no rebate on sports.
FEE_RATE = 0.03

_TYPE_MAP = {
    "SPORTS_MARKET_TYPE_MONEYLINE": "moneyline",
    "SPORTS_MARKET_TYPE_SPREAD":    "spread",
    "SPORTS_MARKET_TYPE_TOTAL":     "total",
}

_SIGNED_LINE_RE = re.compile(r"^([+\-]?\d+(?:\.\d+)?)")


def _is_half_point(x):
    try:
        return not float(x).is_integer()
    except (TypeError, ValueError):
        return False


def _market_id(slug, is_long):
    side = "long" if is_long else "short"
    return f"{slug}:{side}"


def _parse_market_id(market_id):
    try:
        slug, side = market_id.rsplit(":", 1)
        if side not in ("long", "short"):
            return None, None
        return slug, side
    except ValueError:
        return None, None


def _parse_signed(description):
    if not description:
        return None
    m = _SIGNED_LINE_RE.match(description.strip())
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Adapter surface
# ---------------------------------------------------------------------------

def normalize_market(raw):
    """Classify a raw Polymarket snapshot row into a NormalizedMarket.

    Rules:
      - moneyline: emit one NormalizedMarket per side (both teams as YES).
      - spread:    emit only the favored side (description starts with '-'),
                   normalizing to Kalshi's "team wins by over X.5" shape so
                   the matcher's existing `target_yes_points = -line` formula
                   applies unchanged.
      - total:     emit only the 'Over' side (long), matching Kalshi which
                   publishes only the Over half of each total market.
      - Any non-active / closed / archived market is dropped.
    """
    event = raw.get("event") or {}
    market = raw.get("market") or {}
    side = raw.get("side") or {}

    market_type = _TYPE_MAP.get(market.get("sports_market_type_v2"))
    if market_type is None:
        return None
    if market.get("closed") or market.get("archived") or not market.get("active"):
        return None

    slug = market.get("slug")
    if not slug:
        return None
    is_long = bool(side.get("long"))
    market_id = _market_id(slug, is_long)

    start_time = event.get("start_time") or market.get("game_start_time") or ""
    event_id = event.get("event_slug") or (
        str(event.get("event_id")) if event.get("event_id") else ""
    )
    title = event.get("event_title") or market.get("question") or ""
    description = side.get("description") or ""
    team = side.get("team_name")
    # League hint (poller stores `league` on each row's event meta as the
    # lowercase Polymarket slug). Matcher uses it to constrain candidate
    # Pinnacle rows by sport — see `pin_sport_for_league`.
    league_raw = event.get("league")
    league = league_raw.upper() if isinstance(league_raw, str) and league_raw else None

    def _nm(line_val, team_val, side_val):
        return NormalizedMarket(
            book=BOOK,
            market_id=market_id,
            event_id=event_id,
            title=title,
            yes_sub_title=description or None,
            market_type=market_type,
            period_label="FULL",
            line=line_val,
            team=team_val,
            side=side_val,
            start_time=start_time,
            raw=raw,
            league=league,
        )

    if market_type == "moneyline":
        if not team:
            return None
        # Emit only the long side: the short side would surface the sibling team
        # at an identical price (same book, complemented), and SUPPORTS_NO_SIDE=True
        # already reaches that team via the NO ladder. Dual emission would double-
        # count every moneyline as four paper bets for two economic positions.
        if not is_long:
            return None
        return _nm(None, team, None)

    if market_type == "spread":
        # Kalshi convention: one NormalizedMarket per market, YES = favored team
        # laying points. The favored side on Polymarket is whichever marketSide
        # shows a negative handicap in its description (e.g. "-23.50").
        signed = _parse_signed(description)
        if signed is None or signed >= 0:
            return None
        line = abs(signed)
        if not _is_half_point(line) or not team:
            return None
        return _nm(line, team, None)

    if market_type == "total":
        # Kalshi publishes only the Over side of totals; mirror that here.
        if description.strip().lower() != "over":
            return None
        line = market.get("line")
        if line is None or not _is_half_point(line):
            return None
        return _nm(float(line), None, "over")

    return None


def parse_moneyline_teams(normalized):
    """Return (team_a, team_b) from the underlying event.teams, or None."""
    teams = ((normalized.raw.get("event") or {}).get("teams")) or []
    names = [t.get("name") for t in teams if t.get("name")]
    if len(names) >= 2:
        return names[0], names[1]
    return None


def event_group_key(normalized):
    """Group ML / spread / total markets from the same Polymarket event."""
    event = normalized.raw.get("event") or {}
    return event.get("event_slug") or (
        str(event.get("event_id")) if event.get("event_id") else ""
    )


def fallback_anchor(normalized):
    """Polymarket always publishes event.startTime; no fallback needed."""
    return None, None


def market_url(normalized):
    event = normalized.raw.get("event") or {}
    slug = event.get("event_slug") or ""
    return f"https://polymarket.us/event/{slug}" if slug else "https://polymarket.us"


def fee_on_win_per_share(price):
    """Fee deducted from the winning payoff per share. Polymarket charges its
    taker fee upfront at fill (see `fee_on_stake_per_share`), so winners collect
    the full $1/contract — nothing is skimmed on settlement."""
    return 0.0


def fee_on_stake_per_share(price):
    """Taker fee paid upfront at fill per share: 0.03 * P * (1-P). Symmetric in
    P, so the same formula applies to YES and NO buys (price = whatever the
    trader pays per share on their side)."""
    return FEE_RATE * price * (1.0 - price)


def taker_fee_per_share(price, fair_prob):
    """Expected per-share fee at the given fill price. Upfront fee is
    deterministic (paid regardless of outcome), so `fair_prob` is unused —
    kept in the signature for adapter-interface symmetry."""
    return fee_on_stake_per_share(price)


# ---------------------------------------------------------------------------
# Live API calls
# ---------------------------------------------------------------------------

def _px_to_float(px):
    """Book entries surface `px` either as a raw string or an Amount object
    `{value, currency}`; coerce to float in dollars."""
    if isinstance(px, dict):
        px = px.get("value")
    try:
        return float(px)
    except (TypeError, ValueError):
        return None


def _qty_to_int(qty):
    try:
        return int(float(qty))
    except (TypeError, ValueError):
        return None


def _fetch_book(slug):
    """Return the `marketData` subtree of `/v1/markets/{slug}/book`, one side
    (long perspective). Bids are long-side buys; offers are long-side sells."""
    r = requests.get(f"{GATEWAY}/v1/markets/{slug}/book",
                     headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    return body.get("marketData") or body


def _long_ask_ladder(offers):
    """Ascending long-side ask ladder parsed directly from offers[]."""
    ladder = []
    for entry in offers:
        px = _px_to_float(entry.get("px"))
        qty = _qty_to_int(entry.get("qty"))
        if px is None or qty is None or qty <= 0:
            continue
        if px <= 0 or px >= 1:
            continue
        ladder.append((round(px, 4), qty))
    ladder.sort(key=lambda x: x[0])
    return ladder


def _short_ask_ladder(bids):
    """Ascending short-side ask ladder derived from long-side bids (1 - px).

    Selling long at bid P is economically identical to buying short at 1 - P,
    so the long-bid book IS the short-ask book in complement space.
    """
    ladder = []
    for entry in bids:
        px = _px_to_float(entry.get("px"))
        qty = _qty_to_int(entry.get("qty"))
        if px is None or qty is None or qty <= 0:
            continue
        short_ask = round(1.0 - px, 4)
        if short_ask <= 0 or short_ask >= 1:
            continue
        ladder.append((short_ask, qty))
    ladder.sort(key=lambda x: x[0])
    return ladder


def fetch_yes_ask_ladder(market_id):
    """Return ascending YES-ask ladder for one side of a Polymarket market.

    YES-ask on `{slug}:long` is offers[] directly; YES-ask on `{slug}:short`
    is `1 - bids[].px`.
    """
    slug, side = _parse_market_id(market_id)
    if not slug:
        return []
    data = _fetch_book(slug)
    if side == "long":
        return _long_ask_ladder(data.get("offers") or [])
    return _short_ask_ladder(data.get("bids") or [])


def fetch_no_ask_ladder(market_id):
    """Return ascending NO-ask ladder for one side of a Polymarket market.

    The NO side of any market_id is the YES side of its sibling, derived from
    the opposite half of the same orderbook: for `{slug}:long`, NO ≡ short-ask
    ≡ `1 - bids[]`; for `{slug}:short`, NO ≡ long-ask ≡ offers[].
    """
    slug, side = _parse_market_id(market_id)
    if not slug:
        return []
    data = _fetch_book(slug)
    if side == "long":
        return _short_ask_ladder(data.get("bids") or [])
    return _long_ask_ladder(data.get("offers") or [])


def fetch_both_ladders(market_id):
    """Return (yes_ask_ladder, no_ask_ladder) parsed from one book call.

    use when: the caller needs both sides (EV scanner, dashboard). Halves
    traffic vs. calling `fetch_yes_ask_ladder` + `fetch_no_ask_ladder`.
    """
    slug, side = _parse_market_id(market_id)
    if not slug:
        return [], []
    data = _fetch_book(slug)
    offers = data.get("offers") or []
    bids = data.get("bids") or []
    if side == "long":
        return _long_ask_ladder(offers), _short_ask_ladder(bids)
    return _short_ask_ladder(bids), _long_ask_ladder(offers)


def fetch_settlement(market_id):
    """Return 'yes' | 'no' | None from Polymarket's per-slug settlement endpoint.

    Polymarket's `/v1/markets/{slug}/settlement` returns `{slug, settlement}`
    where `settlement` is the long-side payout (0.0 or 1.0 under normal
    resolution). A 404 means the market is not yet settled. For short-side
    positions the payout is inverted.
    """
    slug, side = _parse_market_id(market_id)
    if not slug:
        return None
    r = requests.get(f"{GATEWAY}/v1/markets/{slug}/settlement",
                     headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    val = r.json().get("settlement")
    try:
        long_payout = float(val)
    except (TypeError, ValueError):
        return None
    long_yes = long_payout >= 0.5
    if side == "long":
        return "yes" if long_yes else "no"
    return "no" if long_yes else "yes"
