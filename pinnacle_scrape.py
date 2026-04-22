#!/usr/bin/env python3
"""
Pinnacle odds scraper — proof of concept.

Pulls a few matchups and their straight markets from Pinnacle's public
guest API. No account, no auth beyond the static X-API-Key header that
Pinnacle's own web frontend uses.

Run: python3 pinnacle_scrape.py
"""
import sys
import requests

BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
API_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"

HEADERS = {
    "X-API-Key": API_KEY,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.pinnacle.com/",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def american_to_decimal(a):
    if a >= 100:
        return 1 + a / 100
    return 1 + 100 / abs(a)


def get(path, **params):
    r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=15)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code} on {path}: {r.text[:200]}")
        r.raise_for_status()
    return r.json()


def pick_sport():
    """Pick first sport with matchups. Prefers soccer (29)."""
    sports = get("/sports")
    preferred = [29, 33, 4, 12]  # soccer, tennis, basketball, baseball
    by_id = {s["id"]: s for s in sports if s.get("matchupCount", 0) > 0}
    for sid in preferred:
        if sid in by_id:
            return by_id[sid]
    if by_id:
        return next(iter(by_id.values()))
    raise RuntimeError("No sports with active matchups.")


def fmt_price(price):
    a = price["price"]
    d = american_to_decimal(a)
    designation = price.get("designation", "")
    points = price.get("points")
    tag = f" ({designation}" + (f" {points:+g}" if points is not None else "") + ")"
    return f"{a:+d} [{d:.3f}]{tag}"


def main():
    try:
        sport = pick_sport()
    except requests.HTTPError as e:
        print(f"Failed to list sports: {e}", file=sys.stderr)
        sys.exit(1)

    sid = sport["id"]
    print(f"Sport: {sport['name']} (id={sid}, matchups={sport.get('matchupCount')})")
    print("=" * 70)

    matchups = get(f"/sports/{sid}/matchups", brandId=0)
    # Filter out parent "parlay" or prop wrappers — keep real head-to-head matchups.
    singles = [m for m in matchups if m.get("type") == "matchup" and m.get("parent") is None]
    if not singles:
        singles = matchups
    singles = singles[:3]

    for m in singles:
        participants = m.get("participants") or []
        name = " vs ".join(p.get("name", "?") for p in participants[:2]) or m.get("id")
        start = m.get("startTime", "?")
        print(f"\n{name}")
        print(f"  starts: {start}   matchup_id={m['id']}")

        try:
            markets = get(f"/matchups/{m['id']}/markets/related/straight")
        except requests.HTTPError:
            continue

        # Group by market key (moneyline / spread / total).
        for mk in markets:
            mtype = mk.get("type", "?")
            period = mk.get("period", "?")
            if period != 0:  # 0 == full game
                continue
            prices = mk.get("prices") or []
            if not prices:
                continue
            print(f"  [{mtype}] " + " | ".join(fmt_price(p) for p in prices))


if __name__ == "__main__":
    main()
