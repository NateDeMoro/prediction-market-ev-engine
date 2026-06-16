"""One-off probe: does Pinnacle expose MLB player props as a sharp reference?

Reuses the live poller helpers so the endpoints/auth match production exactly.
Run: PINNACLE_API_KEY=... python3 -m analysis.probe_mlb_props
Read-only. Prints a summary of baseball parent matchups, which carry a
"Player Props" special-category child tree, the stats/players/lines exposed,
and whether two-sided (Over/Under) prices are actually present.
"""
import collections

from pmev.pollers.pinnacle import (
    fetch_sports,
    fetch_raw_matchups,
    fetch_related_matchups,
    fetch_live_matchup_markets,
    parse_prop_description,
    is_team_pair,
)

BASEBALL_SPORT_NAMES = {"Baseball"}


def main():
    sports = fetch_sports()
    baseball = [s for s in sports if s.get("name") in BASEBALL_SPORT_NAMES]
    print(f"Baseball sports with matchups: "
          f"{[(s['id'], s['name'], s.get('matchupCount')) for s in baseball]}")
    if not baseball:
        print("No live baseball sport -> cannot probe MLB props right now.")
        return

    parents = []
    for s in baseball:
        ms = fetch_raw_matchups(s["id"])
        # Top-level team-pair matchups only (the prop tree hangs off these).
        for m in ms:
            if m.get("parentId") is None and is_team_pair(m):
                league = ((m.get("league") or {}).get("name"))
                parents.append((m, league))
    leagues = collections.Counter(lg for _, lg in parents)
    print(f"Top-level baseball team matchups: {len(parents)}  leagues={dict(leagues)}")

    mlb_parents = [m for m, lg in parents if lg == "MLB"]
    print(f"MLB parents: {len(mlb_parents)}")
    if not mlb_parents:
        print("No MLB parents in window -> no MLB games pregame right now.")
        # still probe whatever baseball league exists, to see prop structure
        sample = [m for m, _ in parents][:3]
    else:
        sample = mlb_parents  # scan all MLB parents in window

    stat_counter = collections.Counter()
    examples = []
    parents_with_props = 0
    all_categories = collections.Counter()

    for parent in sample:
        pid = parent["id"]
        try:
            related = fetch_related_matchups(pid)
        except Exception as e:
            print(f"  parent {pid}: /related failed: {e}")
            continue
        prec = next((r for r in related if r.get("id") == pid), None)
        league_name = ((prec or {}).get("league") or {}).get("name")

        # child special "Player Props" matchups
        prop_meta = {}
        for r in related:
            if r.get("type") != "special":
                continue
            sp = r.get("special") or {}
            if sp.get("category") != "Player Props":
                continue
            player, stat = parse_prop_description(sp.get("description", ""))
            prop_meta[r["id"]] = (player, stat, sp.get("description"), r.get("units"))

        # categories present at all (diagnostic)
        cats = collections.Counter(
            (r.get("special") or {}).get("category")
            for r in related if r.get("type") == "special"
        )
        all_categories.update(cats)
        print(f"\n  parent {pid} league={league_name} "
              f"special-categories={dict(cats)} prop-children={len(prop_meta)}")

        if not prop_meta:
            continue
        parents_with_props += 1

        try:
            markets = fetch_live_matchup_markets(pid)
        except Exception as e:
            print(f"    markets fetch failed: {e}")
            continue
        priced = {mk.get("matchupId"): mk for mk in markets}

        for child_id, (player, stat, desc, units) in list(prop_meta.items())[:8]:
            mk = priced.get(child_id)
            n_prices = len(mk.get("prices") or []) if mk else 0
            line = None
            desigs = []
            if mk:
                for p in mk.get("prices") or []:
                    if p.get("points") is not None:
                        line = p.get("points")
                    desigs.append(p.get("designation"))
            stat_counter[stat] += 1
            examples.append((league_name, player, stat, line, n_prices, desigs, desc))
            print(f"    - {player!r:28} stat={stat!r:18} line={line} "
                  f"prices={n_prices} desigs={desigs}")

    print("\n==== SUMMARY ====")
    print(f"All special categories across sampled parents: {dict(all_categories)}")
    print(f"Sampled parents: {len(sample)}  with Player-Props child tree: {parents_with_props}")
    print(f"Stat distribution (sampled): {dict(stat_counter)}")
    twosided = [e for e in examples if e[4] == 2]
    print(f"Two-sided priced prop rows (Over/Under present): {len(twosided)}/{len(examples)}")
    if examples and not any(parse_ok(e) for e in examples):
        print("WARNING: stats parsed as None -> parse_prop_description needs MLB rules")


def parse_ok(e):
    return e[2] not in (None, "")


if __name__ == "__main__":
    main()
