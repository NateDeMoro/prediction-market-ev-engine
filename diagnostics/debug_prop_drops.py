#!/usr/bin/env python3
"""
One-shot: enumerate every Kalshi prop that the matcher drops as
`no_player_match` and print it alongside the Pinnacle prop universe for
the same parent matchup. Use to categorize whether drops are caused by
wrong-matchup anchoring, player-key normalization, or Pinnacle simply
not publishing that player.
"""
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pmev.adapters import all_adapters, adapter_for
from pmev.adapters.common import (
    COMBO_STATS,
    canonical_stat,
    player_key,
)
from pmev.matching.matcher import (
    match_markets,
    load_pinnacle_props,
    _pinnacle_prop_index,
    _pinnacle_prop_fuzzy_lookup,
)

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_PIN = os.path.join(DIR, "data", "snapshots")


def _load_latest(dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, "*.jsonl")))
    if not files:
        return None
    path = files[-1]
    return [json.loads(l) for l in open(path)]


def main():
    pin_rows = _load_latest(SNAP_PIN)
    assert pin_rows, "missing pinnacle snapshot"

    soft = []
    for a in all_adapters():
        rows = _load_latest(a.SNAPSHOT_DIR)
        if rows is None:
            continue
        for raw in rows:
            nm = a.normalize_market(raw)
            if nm is not None:
                soft.append(nm)

    # Build the (book, event_group_key) -> Pinnacle matchup map the same way
    # match_all_markets does, via the ML stage.
    soft_mls = [nm for nm in soft if nm.market_type == "moneyline"]
    ml_report = match_markets(pin_rows, soft_mls)
    event_to_pin = {}
    for m in ml_report.matched:
        adapter = adapter_for(m.soft.book)
        sfx = adapter.event_group_key(m.soft)
        if not sfx:
            continue
        pin = m.pinnacle
        event_to_pin[(m.soft.book, sfx)] = {
            "matchup_id": pin.get("matchupId"),
            "matchup": pin.get("matchup"),
        }

    pin_prop_rows = load_pinnacle_props(pin_rows)
    prop_index = _pinnacle_prop_index(pin_prop_rows)

    # matchup_id -> list of (player, stat, line) from Pinnacle, for diagnostic
    pin_by_matchup = defaultdict(list)
    for r in pin_prop_rows:
        stat = canonical_stat(r.get("stat_units")) or canonical_stat(r.get("stat"))
        pin_by_matchup[r.get("matchupId")].append({
            "player": r.get("player"),
            "pkey": player_key(r.get("player")),
            "stat_raw": r.get("stat_units") or r.get("stat"),
            "stat": stat,
            "line": r.get("line"),
        })

    soft_props = [nm for nm in soft if nm.market_type == "player_prop"]

    buckets = Counter()
    samples = defaultdict(list)

    for nm in soft_props:
        if nm.stat in COMBO_STATS:
            buckets["combo"] += 1
            continue
        if nm.stat is None:
            buckets["missing_stat"] += 1
            continue
        if not nm.team:
            buckets["no_team_anchor"] += 1
            continue

        adapter = adapter_for(nm.book)
        sfx = adapter.event_group_key(nm)
        pin_info = event_to_pin.get((nm.book, sfx))
        if not pin_info:
            buckets["no_event_match"] += 1
            samples["no_event_match"].append({
                "book": nm.book,
                "ticker": nm.market_id,
                "league": nm.league,
                "team": nm.team,
                "player": nm.player,
                "stat": nm.stat,
                "line": nm.line,
                "sfx": sfx,
            })
            continue

        pkey = player_key(nm.player)
        if not pkey:
            buckets["empty_pkey"] += 1
            continue

        mid = pin_info["matchup_id"]
        prop_rows = _pinnacle_prop_fuzzy_lookup(prop_index, mid, nm.stat, pkey)
        if prop_rows:
            found_line = any(
                abs((r.get("line") or 0) - nm.line) < 1e-9 for r in prop_rows
            )
            buckets["matched_player" if found_line else "line_miss"] += 1
            continue

        # Player wasn't found. Bucket by whether Pinnacle has the player at all
        # (stat mismatch) vs whether Pinnacle has no props on this matchup.
        pin_for_mu = pin_by_matchup.get(mid, [])
        if not pin_for_mu:
            buckets["pin_has_no_props_for_matchup"] += 1
            samples["pin_has_no_props_for_matchup"].append({
                "book": nm.book,
                "ticker": nm.market_id,
                "league": nm.league,
                "team": nm.team,
                "player": nm.player,
                "stat": nm.stat,
                "pin_matchup": pin_info["matchup"],
                "pin_matchup_id": mid,
            })
            continue

        # Pinnacle has props for this matchup. Does it have the player under
        # ANY stat?
        from pmev.adapters.common import fuzzy_match
        same_player_rows = [
            r for r in pin_for_mu
            if fuzzy_match(pkey, r["pkey"]) or fuzzy_match(r["pkey"], pkey)
        ]
        if same_player_rows:
            stats_offered = sorted({r["stat"] for r in same_player_rows if r["stat"]})
            buckets["player_found_but_stat_mismatch"] += 1
            samples["player_found_but_stat_mismatch"].append({
                "book": nm.book,
                "ticker": nm.market_id,
                "league": nm.league,
                "player": nm.player,
                "pkey": pkey,
                "asked_stat": nm.stat,
                "line": nm.line,
                "pin_player_keys": sorted({r["pkey"] for r in same_player_rows}),
                "pin_stats_for_player": stats_offered,
            })
            continue

        # Pinnacle has props for this matchup for this stat, but no matching
        # player. Likely a player-name normalization problem.
        same_stat_rows = [r for r in pin_for_mu if r["stat"] == nm.stat]
        if same_stat_rows:
            buckets["stat_exists_no_player_match"] += 1
            samples["stat_exists_no_player_match"].append({
                "book": nm.book,
                "ticker": nm.market_id,
                "league": nm.league,
                "player": nm.player,
                "pkey": pkey,
                "stat": nm.stat,
                "pin_matchup": pin_info["matchup"],
                "pin_players_offered_this_stat": sorted({r["pkey"] for r in same_stat_rows}),
            })
            continue

        # Pinnacle has prop rows on this matchup but not this stat & not this
        # player. Usually means the soft book offers a wider player slate than
        # Pinnacle.
        buckets["pin_missing_player_and_stat"] += 1
        samples["pin_missing_player_and_stat"].append({
            "book": nm.book,
            "ticker": nm.market_id,
            "league": nm.league,
            "player": nm.player,
            "stat": nm.stat,
            "pin_stats_any_player": sorted({r["stat"] for r in pin_for_mu if r["stat"]}),
        })

    print("== Bucket counts ==")
    for b, n in buckets.most_common():
        print(f"  {b:<36} {n}")

    SHOW = 8
    for bucket_name in (
        "stat_exists_no_player_match",
        "player_found_but_stat_mismatch",
        "no_event_match",
        "pin_has_no_props_for_matchup",
        "pin_missing_player_and_stat",
    ):
        rows = samples.get(bucket_name) or []
        if not rows:
            continue
        print()
        print(f"== Sample: {bucket_name} (showing {min(len(rows), SHOW)} of {len(rows)}) ==")
        for r in rows[:SHOW]:
            print(f"  {json.dumps(r, default=str)}")


if __name__ == "__main__":
    main()
