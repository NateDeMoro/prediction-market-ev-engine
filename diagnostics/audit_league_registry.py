#!/usr/bin/env python3
"""
League-registry audit.

Scans the latest soft-book snapshots and prints every Kalshi `series_ticker`
prefix / Polymarket league that is not yet mapped in
`adapters/common.SERIES_TICKER_LEAGUE_PREFIXES` or
`adapters/common.LEAGUE_TO_PIN_SPORT`. The matcher fails closed on unknown
leagues, so unregistered series produce no matches — this script is the
checklist for which ones to add.

Run: python3 audit_league_registry.py
"""
import glob
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pmev.adapters.common import (
    LEAGUE_TO_PIN_SPORT,
    SERIES_TICKER_LEAGUE_PREFIXES,
    series_ticker_league,
)

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def latest(pattern):
    files = sorted(glob.glob(os.path.join(DIR, pattern)))
    return files[-1] if files else None


def audit_kalshi():
    path = latest("data/kalshi_snapshots/*.jsonl")
    if not path:
        print("(no Kalshi snapshots)")
        return
    counts = Counter()
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            counts[r.get("series_ticker")] += 1

    print(f"[kalshi] snapshot: {os.path.basename(path)}")
    print(f"[kalshi] unique series: {len(counts)}")
    rows = []
    for series, n in counts.items():
        if not series:
            continue
        league = series_ticker_league(series)
        rows.append((n, series, league, LEAGUE_TO_PIN_SPORT.get(league) if league else None))
    rows.sort(reverse=True)

    registered = [r for r in rows if r[2] is not None]
    unregistered = [r for r in rows if r[2] is None]

    print(f"\n[kalshi] registered ({len(registered)}):")
    for n, series, league, sport in registered:
        print(f"  {n:5d}  {series:30s}  league={league:<16s}  sport={sport}")

    print(f"\n[kalshi] UNREGISTERED ({len(unregistered)}) — matcher fails closed:")
    for n, series, _, _ in unregistered:
        print(f"  {n:5d}  {series}")


def audit_polymarket():
    path = latest("data/polymarket_snapshots/*.jsonl")
    if not path:
        print("\n(no Polymarket snapshots)")
        return
    counts = Counter()
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            counts[(r.get("event") or {}).get("league")] += 1

    print(f"\n[polymarket] snapshot: {os.path.basename(path)}")
    rows = []
    for league, n in counts.items():
        if not league:
            continue
        up = league.upper()
        rows.append((n, up, LEAGUE_TO_PIN_SPORT.get(up)))
    rows.sort(reverse=True)

    registered = [r for r in rows if r[2] is not None]
    unregistered = [r for r in rows if r[2] is None]

    print(f"\n[polymarket] registered ({len(registered)}):")
    for n, league, sport in registered:
        print(f"  {n:5d}  {league:<16s}  sport={sport}")

    if unregistered:
        print(f"\n[polymarket] UNREGISTERED ({len(unregistered)}) — matcher fails closed:")
        for n, league, _ in unregistered:
            print(f"  {n:5d}  {league}")


def main():
    print(f"Kalshi prefixes registered: {len(SERIES_TICKER_LEAGUE_PREFIXES)}")
    print(f"Leagues mapped to Pinnacle sport: {len(LEAGUE_TO_PIN_SPORT)}\n")
    audit_kalshi()
    audit_polymarket()


if __name__ == "__main__":
    main()
