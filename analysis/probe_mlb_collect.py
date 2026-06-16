"""Definitive test: run the production collect_prop_rows() against a live MLB
parent with PROP_LEAGUES patched to include MLB. Confirms whether the existing
over/under designation derivation + description parsing produce clean rows for
baseball, i.e. whether wiring MLB props is just a config/stat-map change.

Read-only. Run on the VPS where PINNACLE_API_KEY is set.
"""
import collections

import pmev.pollers.pinnacle as P


def main():
    # Patch the league/sport gates so collect_prop_rows processes MLB.
    P.PROP_LEAGUES = {"NBA", "NHL", "MLB"}

    sports = P.fetch_sports()
    baseball = next((s for s in sports if s.get("name") == "Baseball"), None)
    if not baseball:
        print("No baseball in window.")
        return

    matchups = P.fetch_raw_matchups(baseball["id"])
    mlb_parents = [
        m for m in matchups
        if m.get("parentId") is None and P.is_team_pair(m)
        and ((m.get("league") or {}).get("name")) == "MLB"
    ]
    print(f"MLB parents: {len(mlb_parents)}")

    total_rows = 0
    bad_desig = 0
    none_stat = 0
    samples = []
    stat_units = collections.Counter()
    for parent in mlb_parents:
        rows, fp_updates, log_lines, err = P.collect_prop_rows(parent, "Baseball", {})
        if err:
            print(f"  parent {parent['id']} error: {err}")
            continue
        for row in rows:
            total_rows += 1
            stat_units[(row.get("stat"), row.get("stat_units"))] += 1
            prices = row.get("prices") or []
            desigs = sorted(str(p.get("designation")) for p in prices)
            if desigs != ["over", "under"]:
                bad_desig += 1
            if not row.get("stat"):
                none_stat += 1
            if len(samples) < 12:
                samples.append({
                    "player": row.get("player"),
                    "stat": row.get("stat"),
                    "units": row.get("stat_units"),
                    "line": row.get("line"),
                    "desigs": desigs,
                    "prices": [(p.get("designation"), p.get("price")) for p in prices],
                })

    print("\nDistinct Pinnacle (stat, stat_units) across all MLB prop rows:")
    for (stat, units), n in stat_units.most_common():
        print(f"   {stat!r:22} units={units!r:18} count={n}")
    print(f"\nTotal MLB prop rows produced: {total_rows}")
    print(f"Rows WITHOUT clean over/under designation: {bad_desig}")
    print(f"Rows with stat parsed as None/empty: {none_stat}")
    print("\nSample rows:")
    for s in samples:
        print(f"  {s['player']!r:26} stat={s['stat']!r:18} units={s['units']!r} "
              f"line={s['line']} prices={s['prices']}")

    print("\n==== VERDICT ====")
    if total_rows and bad_desig == 0 and none_stat == 0:
        print("Production collect_prop_rows handles MLB cleanly -> wiring is config + stat-map only.")
    else:
        print("Gaps found above -> poller/parser needs MLB-specific work.")


if __name__ == "__main__":
    main()
