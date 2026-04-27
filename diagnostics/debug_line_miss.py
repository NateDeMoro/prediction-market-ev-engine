#!/usr/bin/env python3
"""Within `line_miss`, compare Kalshi strike sets to the Pinnacle lines
available for the same (player, stat). Tells us whether Pinnacle usually
has ONE line (and Kalshi ladder surrounds it) or genuinely offers no
line close to any Kalshi strike."""
import glob
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters import all_adapters, adapter_for
from adapters.common import COMBO_STATS, canonical_stat, player_key, fuzzy_match
from market_matcher import (
    match_markets,
    load_pinnacle_props,
    _pinnacle_prop_index,
    _pinnacle_prop_fuzzy_lookup,
)

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_latest(dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, "*.jsonl")))
    if not files:
        return None
    return [json.loads(l) for l in open(files[-1])]


def main():
    pin_rows = _load_latest(os.path.join(DIR, "data", "snapshots"))
    soft = []
    for a in all_adapters():
        rows = _load_latest(a.SNAPSHOT_DIR) or []
        for raw in rows:
            nm = a.normalize_market(raw)
            if nm is not None:
                soft.append(nm)

    soft_mls = [nm for nm in soft if nm.market_type == "moneyline"]
    ml = match_markets(pin_rows, soft_mls)
    event_to_pin = {}
    for m in ml.matched:
        ad = adapter_for(m.soft.book)
        sfx = ad.event_group_key(m.soft)
        if sfx:
            event_to_pin[(m.soft.book, sfx)] = m.pinnacle.get("matchupId")

    pin_prop_rows = load_pinnacle_props(pin_rows)
    prop_index = _pinnacle_prop_index(pin_prop_rows)

    # (matchup_id, stat, pkey) -> list of pin lines
    pin_lines_by_triplet = defaultdict(set)
    for r in pin_prop_rows:
        stat = canonical_stat(r.get("stat_units")) or canonical_stat(r.get("stat"))
        if not stat:
            continue
        pin_lines_by_triplet[(r.get("matchupId"), stat, player_key(r.get("player")))].add(r.get("line"))

    # Aggregate by (book, matchup, player, stat)
    kalshi_by_key = defaultdict(list)  # -> list of nm.line
    pin_by_key = {}                    # -> set of pin lines
    for nm in soft:
        if nm.market_type != "player_prop":
            continue
        if nm.stat in COMBO_STATS or nm.stat is None or not nm.team:
            continue
        ad = adapter_for(nm.book)
        sfx = ad.event_group_key(nm)
        mid = event_to_pin.get((nm.book, sfx))
        if mid is None:
            continue
        pkey = player_key(nm.player)
        prop_rows = _pinnacle_prop_fuzzy_lookup(prop_index, mid, nm.stat, pkey)
        if not prop_rows:
            continue  # covered by other buckets
        key = (nm.book, mid, pkey, nm.stat)
        kalshi_by_key[key].append(nm.line)
        if key not in pin_by_key:
            pin_by_key[key] = {r.get("line") for r in prop_rows}

    # For every key where we have a Pinnacle line: count Kalshi strikes that matched vs missed.
    total_kalshi = 0
    matched = 0
    missed = 0
    miss_samples = []
    for key, klines in kalshi_by_key.items():
        pins = pin_by_key.get(key, set())
        total_kalshi += len(klines)
        for kl in klines:
            hit = any(abs(kl - pl) < 1e-9 for pl in pins)
            if hit:
                matched += 1
            else:
                missed += 1
        if any(not any(abs(kl - pl) < 1e-9 for pl in pins) for kl in klines):
            if len(miss_samples) < 15:
                miss_samples.append({
                    "book": key[0],
                    "pkey": key[2],
                    "stat": key[3],
                    "kalshi_strikes": sorted(klines),
                    "pinnacle_lines": sorted(pins),
                })

    print(f"Kalshi strikes analyzed (player found on Pinnacle): {total_kalshi}")
    print(f"  matched exactly:  {matched}")
    print(f"  line mismatch:    {missed}")
    print()

    # How many distinct Pinnacle lines per player-stat? If always 1, confirms single-line hypothesis.
    n_pin_lines = defaultdict(int)
    for pins in pin_by_key.values():
        n_pin_lines[len(pins)] += 1
    print("Pinnacle lines offered per (player,stat):")
    for n, count in sorted(n_pin_lines.items()):
        print(f"  {n} line(s): {count} (player,stat) pairs")
    print()

    print("== Sample (kalshi strikes vs pinnacle lines) ==")
    for s in miss_samples:
        print(f"  {s['pkey']:<28} {s['stat']:<14} kalshi={s['kalshi_strikes']} pin={s['pinnacle_lines']}")


if __name__ == "__main__":
    main()
