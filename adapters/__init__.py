"""
Soft-book adapter registry.

Each adapter module exposes a common duck-typed surface (see
`adapters/kalshi.py` for the reference implementation):

    BOOK: str
    SNAPSHOT_DIR: str
    SUPPORTS_NO_SIDE: bool  # True => fetch_both_ladders returns a NO ladder
    normalize_market(raw_row) -> NormalizedMarket | None
    fetch_yes_ask_ladder(market_id) -> list[(price_0_1, qty)]
    fetch_both_ladders(market_id) -> (yes_ladder, no_ladder_or_None)
    taker_fee_per_share(price, fair_prob) -> float
    market_url(normalized) -> str
    fetch_settlement(market_id) -> "yes" | "no" | None
    parse_moneyline_teams(normalized) -> (team_a, team_b) | None
    event_group_key(normalized) -> str

`fetch_both_ladders` returns both sides of a single ticker's book from one
HTTP call. Adapters without a NO-side (Polymarket: separate market IDs per
outcome) return (yes_ladder, None) so callers can branch.

`adapter_for(book)` lazy-loads modules so individual adapters can be
added without touching this file.
"""
import importlib

from .common import NormalizedMarket, fuzzy_match, tokenize

_REGISTERED = ("kalshi", "polymarket")
_cache = {}


def adapter_for(book):
    if book not in _REGISTERED:
        raise ValueError(f"unknown book: {book!r}")
    if book not in _cache:
        _cache[book] = importlib.import_module(f".{book}", __name__)
    return _cache[book]


def all_adapters():
    """Return the list of adapter modules for books with an importable module.

    Silently skips adapters that haven't been implemented yet (e.g. polymarket
    during incremental rollout).
    """
    out = []
    for book in _REGISTERED:
        try:
            out.append(adapter_for(book))
        except ImportError:
            continue
    return out
