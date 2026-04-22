"""
Shared soft-book adapter primitives.

`NormalizedMarket` is the book-agnostic row the matcher and EV pipeline
consume. Each adapter is responsible for turning its raw snapshot rows
into NormalizedMarket instances; the matcher only sees this shape.

Also hosts team-name fuzzy utilities used across adapters and the
matcher.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import re


@dataclass
class NormalizedMarket:
    book: str                         # "kalshi" | "polymarket"
    market_id: str                    # Kalshi ticker | Polymarket slug
    event_id: str                     # shared key within a single game across sibling markets
    title: str
    yes_sub_title: Optional[str]
    market_type: str                  # "moneyline" | "spread" | "total" | "team_total"
    period_label: str                 # "FULL" | "1H" | "2H"
    line: Optional[float]             # None for moneyline
    team: Optional[str]               # YES-side team for spread/team_total; None otherwise
    side: Optional[str]               # "over" | "under" for team_total; None otherwise
    start_time: str                   # ISO-8601
    raw: dict                         # pass-through for diagnostics


def tokenize(name):
    s = (name or "").lower()
    s = re.sub(r"'s\b", "", s)
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


def fuzzy_match(short_name, long_name):
    """True if every token of short_name is a prefix of some unused token in long_name."""
    s = tokenize(short_name)
    l = tokenize(long_name)
    if not s or not l:
        return False
    used = set()
    for st in s:
        hit = False
        for i, lt in enumerate(l):
            if i in used:
                continue
            if lt.startswith(st):
                used.add(i)
                hit = True
                break
        if not hit:
            return False
    return True
