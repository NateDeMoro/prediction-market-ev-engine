"""
Soft-book adapter registry.

Each adapter module exposes a common duck-typed surface (see
`adapters/kalshi.py` for the reference implementation):

    BOOK: str
    SNAPSHOT_DIR: str
    normalize_market(raw_row) -> NormalizedMarket | None
    fetch_yes_ask_ladder(market_id) -> list[(price_0_1, qty)]
    taker_fee_per_share(price, fair_prob) -> float
    market_url(normalized) -> str
    fetch_settlement(market_id) -> "yes" | "no" | None
    parse_moneyline_teams(normalized) -> (team_a, team_b) | None
    event_group_key(normalized) -> str

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
