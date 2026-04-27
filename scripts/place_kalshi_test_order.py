"""Standalone proof-of-concept: place one authenticated Kalshi limit order.

use when:
  You want to verify end-to-end that your Kalshi API key + RSA private key can
  sign a request and place a real order on api.elections.kalshi.com. Throwaway
  script — not wired into the poller, dashboard, or paper tracker.

Prereqs:
  pip3 install cryptography
  export KALSHI_API_KEY_ID="<uuid>"
  export KALSHI_PRIVATE_KEY_PATH="$HOME/.kalshi/private_key.pem"   # chmod 600

Examples:
  python3 place_kalshi_test_order.py --ticker KXNBA-... --dry-run
  python3 place_kalshi_test_order.py --ticker KXNBA-... --limit-cents 40
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = "https://api.elections.kalshi.com/trade-api/v2"
ORDERS_PATH = "/trade-api/v2/portfolio/orders"
REQUEST_TIMEOUT = 15
TICKER_RE = re.compile(r"^[A-Z0-9\-]+$")
MAX_COUNT = 5


def _load_private_key(path: Path):
    if not path.exists():
        sys.exit(f"private key not found at {path}")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        sys.exit(
            f"private key {path} is group/world-readable (mode {oct(mode & 0o777)}); "
            f"run: chmod 600 {path}"
        )
    with path.open("rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign(private_key, method: str, path: str) -> tuple[str, str]:
    ts = str(int(time.time() * 1000))
    msg = (ts + method.upper() + path).encode()
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return ts, base64.b64encode(sig).decode()


def _auth_headers(private_key, key_id: str, method: str, path: str) -> dict:
    ts, sig = _sign(private_key, method, path)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Accept": "application/json",
    }


def _best_yes_ask_cents(ticker: str) -> int:
    """Unauth read of the orderbook; returns best YES ask in integer cents."""
    r = requests.get(
        f"{BASE}/markets/{ticker}/orderbook", timeout=REQUEST_TIMEOUT
    )
    r.raise_for_status()
    body = r.json()
    ob = body.get("orderbook_fp") or body.get("orderbook") or {}
    no_side = ob.get("no_dollars") or ob.get("no") or []
    best_ask_dollars = None
    for entry in no_side:
        try:
            no_price = float(entry[0])
            qty = int(float(entry[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if qty <= 0:
            continue
        yes_ask = 1.0 - no_price
        if yes_ask <= 0 or yes_ask >= 1:
            continue
        if best_ask_dollars is None or yes_ask < best_ask_dollars:
            best_ask_dollars = yes_ask
    if best_ask_dollars is None:
        sys.exit(f"no YES-ask ladder available for {ticker}")
    cents = round(best_ask_dollars * 100)
    if cents >= 99:
        sys.exit(f"best YES ask for {ticker} is {cents}c (>=99c); refusing to place")
    if cents <= 0:
        sys.exit(f"best YES ask for {ticker} is {cents}c; refusing to place")
    return cents


def main() -> int:
    ap = argparse.ArgumentParser(description="Place one small Kalshi limit order.")
    ap.add_argument("--ticker", required=True, help="Kalshi market ticker")
    ap.add_argument("--side", default="yes", choices=["yes", "no"])
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument(
        "--limit-cents",
        type=int,
        default=None,
        help="Integer 1-99. Default: read live best YES ask from orderbook.",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not TICKER_RE.match(args.ticker):
        sys.exit(f"ticker {args.ticker!r} fails [A-Z0-9-]+ check")
    if args.count < 1 or args.count > MAX_COUNT:
        sys.exit(f"--count must be 1..{MAX_COUNT} (got {args.count})")

    key_id = os.environ.get("KALSHI_API_KEY_ID")
    pem_path_str = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not pem_path_str:
        sys.exit("set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
    private_key = _load_private_key(Path(pem_path_str).expanduser())

    if args.limit_cents is None:
        if args.side != "yes":
            sys.exit("--limit-cents required when --side=no")
        limit_cents = _best_yes_ask_cents(args.ticker)
        print(f"resolved best YES ask: {limit_cents}c")
    else:
        if not (1 <= args.limit_cents <= 99):
            sys.exit("--limit-cents must be in 1..99")
        limit_cents = args.limit_cents

    body = {
        "ticker": args.ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": args.side,
        "action": "buy",
        "type": "limit",
        "count": args.count,
        ("yes_price" if args.side == "yes" else "no_price"): limit_cents,
    }

    headers = _auth_headers(private_key, key_id, "POST", ORDERS_PATH)
    headers["Content-Type"] = "application/json"
    url = f"{BASE}/portfolio/orders"

    print(f"POST {url}")
    print(f"headers: {json.dumps({k: v for k, v in headers.items()}, indent=2)}")
    print(f"body:    {json.dumps(body, indent=2)}")

    if args.dry_run:
        print("-- dry run, not sending --")
        return 0

    r = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
    print(f"status: {r.status_code}")
    try:
        resp = r.json()
    except ValueError:
        print(r.text)
        return 1
    print(json.dumps(resp, indent=2))
    if r.status_code >= 300:
        return 1

    order_id = (resp.get("order") or {}).get("order_id")
    if not order_id:
        print("no order_id in response; skipping verify")
        return 0

    verify_path = f"/trade-api/v2/portfolio/orders/{order_id}"
    verify_headers = _auth_headers(private_key, key_id, "GET", verify_path)
    v = requests.get(f"{BASE}/portfolio/orders/{order_id}",
                     headers=verify_headers, timeout=REQUEST_TIMEOUT)
    print(f"\nverify GET /portfolio/orders/{order_id} -> {v.status_code}")
    try:
        print(json.dumps(v.json(), indent=2))
    except ValueError:
        print(v.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
