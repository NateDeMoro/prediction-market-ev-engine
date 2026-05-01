"""Standalone proof-of-concept: hit the Polymarket US authenticated API.

use when:
  Verifying that POLYMARKETUS_API_KEY + POLYMARKETUS_PRIVATE_KEY work end-to-end
  before flipping REAL_TRADING_ENABLED on. By default this only fetches your
  account balance — pass --place to actually transmit a tiny ($1-ish) limit
  order on a market you specify.

Prereqs:
  pip3 install cryptography requests
  export POLYMARKETUS_API_KEY="<key id>"
  export POLYMARKETUS_PRIVATE_KEY="<base64 secret>"

Examples:
  # Just check auth + balance — no order is placed
  python3 scripts/place_polymarket_test_order.py

  # Probe a market's current best ask but don't place
  python3 scripts/place_polymarket_test_order.py --slug some-market-slug

  # Actually place a $1-ish test order at the best ask
  python3 scripts/place_polymarket_test_order.py --slug some-market-slug --place
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the repo root importable regardless of where this is run from.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests

from adapters import polymarket_trade


GATEWAY = "https://gateway.polymarket.us"


def _best_yes_ask(slug: str):
    """Public unauth read of the order book. Returns (best_ask_dollars, qty)
    on the YES side, or None if no offers.

    Real shape: {"marketData": {"bids": [...], "offers": [{"px": {"value": "0.15",
    "currency": "USD"}, "qty": "260.000"}]}}
    """
    r = requests.get(f"{GATEWAY}/v1/markets/{slug}/book", timeout=10)
    r.raise_for_status()
    body = r.json()
    md = body.get("marketData") or body
    offers = md.get("offers") or []
    best = None
    for entry in offers:
        try:
            px = float(entry["px"]["value"])
            qty = float(entry["qty"])
        except (KeyError, TypeError, ValueError):
            continue
        if qty <= 0 or px <= 0 or px >= 1:
            continue
        if best is None or px < best[0]:
            best = (px, qty)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", help="Polymarket market slug (long side).")
    ap.add_argument("--side", default="yes", choices=["yes", "no"])
    ap.add_argument("--count", type=int, default=1, help="Shares (default 1).")
    ap.add_argument("--limit", type=float, default=None,
                    help="Limit price in dollars (0.01-0.99). Defaults to live best ask.")
    ap.add_argument("--place", action="store_true",
                    help="Actually transmit the order. Without this flag the script "
                         "only fetches balance + best ask.")
    args = ap.parse_args()

    if not os.environ.get("POLYMARKETUS_API_KEY"):
        sys.exit("POLYMARKETUS_API_KEY is not set")
    if not os.environ.get("POLYMARKETUS_PRIVATE_KEY"):
        sys.exit("POLYMARKETUS_PRIVATE_KEY is not set")

    print("=== /v1/account/balances ===")
    bal = polymarket_trade.get_balance()
    print(json.dumps(bal, indent=2)[:1000])

    if not args.slug:
        print("\n(no --slug given, exiting)")
        return 0

    print(f"\n=== /v1/markets/{args.slug}/book ===")
    book = _best_yes_ask(args.slug)
    if book is None:
        sys.exit("no YES-side offers — pick a different slug")
    best_px, best_qty = book
    print(f"  best YES ask: ${best_px:.2f}  qty: {best_qty}")

    if args.limit is None:
        limit = round(best_px, 2)
    else:
        limit = round(args.limit, 2)
    if not (0.01 <= limit <= 0.99):
        sys.exit(f"limit ${limit} out of range")
    print(f"  using limit: ${limit:.2f}  count: {args.count}")
    notional = limit * args.count
    print(f"  notional: ${notional:.2f}")
    if notional > 5.0:
        sys.exit(f"refusing — notional ${notional:.2f} > $5.00 sanity cap")

    if not args.place:
        print("\n(--place not passed; not transmitting)")
        return 0

    print(f"\n=== POST /v1/orders ===")
    resp = polymarket_trade.place_limit_order(args.slug, args.side, args.count, limit)
    print(json.dumps(resp, indent=2)[:2000])
    if resp.get("error"):
        return 1

    order_id = resp.get("order_id")
    if not order_id:
        print("no order_id returned; cannot verify")
        return 0

    print(f"\n=== GET /v1/order/{order_id} ===")
    info = polymarket_trade.get_order(order_id)
    print(json.dumps(info, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
