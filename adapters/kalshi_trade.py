"""
Kalshi authenticated trade adapter.

Wraps the order-placement endpoints behind the same shape used for
polymarket_trade.py: place_limit_order / get_order / cancel_order /
get_balance. Auth uses RSA-PSS-SHA256 signing of (timestamp + method + path),
matching the working POC at scripts/place_kalshi_test_order.py.

Env:
  KALSHI_API_KEY        UUID issued by Kalshi (alias: KALSHI_API_KEY_ID)
  KALSHI_PRIVATE_KEY    Base64-encoded PEM, single line — embeddable in .env.
                        (aliases: KALSHI_PRIVATE_KEY_B64; or set
                        KALSHI_PRIVATE_KEY_PATH for a file-on-disk PEM instead)

systemd's EnvironmentFile does not parse multi-line values, so the b64 form
is the most convenient way to put the key in .env. Generate with:
    base64 -i ~/Natekey.txt | tr -d '\\n'

The private key is loaded once, lazily, and cached. RSA signing with the
`cryptography` library runs in ~5-10ms per request.
"""
from __future__ import annotations

import base64
import os
import stat
import threading
import time
import uuid
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import config

BOOK = "kalshi"
BASE = "https://api.elections.kalshi.com/trade-api/v2"
ORDERS_PATH = "/trade-api/v2/portfolio/orders"
BALANCE_PATH = "/trade-api/v2/portfolio/balance"
REQUEST_TIMEOUT = config.ADAPTER_TRADE_TIMEOUT


def _int_fp(value) -> int:
    """Coerce a Kalshi `*_fp` field (e.g. "23.00") to int. Kalshi serializes
    fixed-point counts as strings; int() rejects them, so we round-trip
    through float()."""
    if value is None:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _map_order_state(state) -> str:
    """Translate Kalshi's order status into the cross-book vocabulary
    real_tracker tracks: pending / partial / filled / canceled / rejected /
    expired. Kalshi uses 'executed' for an immediately-filled limit order;
    'resting' for an order on book; 'canceled' for canceled."""
    s = (state or "").lower()
    if s == "executed":
        return "filled"
    if s == "resting":
        return "pending"
    if s == "canceled" or s == "cancelled":
        return "canceled"
    if s in ("pending", "partial", "filled", "rejected", "expired"):
        return s
    return "pending"

_key_lock = threading.Lock()
_private_key = None
_key_id_cached = None


def _load_private_key():
    global _private_key, _key_id_cached
    with _key_lock:
        if _private_key is not None:
            return _private_key, _key_id_cached
        key_id = (os.environ.get("KALSHI_API_KEY")
                  or os.environ.get("KALSHI_API_KEY_ID"))
        pem_path_str = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        pem_b64 = (os.environ.get("KALSHI_PRIVATE_KEY")
                   or os.environ.get("KALSHI_PRIVATE_KEY_B64"))
        if not key_id:
            raise RuntimeError("KALSHI_API_KEY must be set")
        if not pem_path_str and not pem_b64:
            raise RuntimeError(
                "Set KALSHI_PRIVATE_KEY (base64 PEM) or KALSHI_PRIVATE_KEY_PATH (file)"
            )
        if pem_path_str and pem_b64:
            raise RuntimeError(
                "Set exactly one of KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH, not both"
            )
        if pem_b64:
            try:
                pem_bytes = base64.b64decode(pem_b64.strip(), validate=True)
            except (ValueError, base64.binascii.Error) as e:
                raise RuntimeError(
                    f"KALSHI_PRIVATE_KEY is not valid base64: {e}. "
                    "Generate with: base64 -i your_pem_file | tr -d '\\n'"
                )
            try:
                _private_key = serialization.load_pem_private_key(pem_bytes, password=None)
            except (ValueError, TypeError) as e:
                raise RuntimeError(f"KALSHI_PRIVATE_KEY did not decode to a valid PEM: {e}")
        else:
            path = Path(pem_path_str).expanduser()
            if not path.exists():
                raise RuntimeError(f"Kalshi private key not found at {path}")
            mode = path.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise RuntimeError(
                    f"Kalshi private key {path} is group/world-readable "
                    f"(mode {oct(mode & 0o777)}); run chmod 600"
                )
            with path.open("rb") as f:
                _private_key = serialization.load_pem_private_key(f.read(), password=None)
        _key_id_cached = key_id
        return _private_key, _key_id_cached


def _sign(method: str, path: str):
    pk, key_id = _load_private_key()
    ts = str(int(time.time() * 1000))
    msg = (ts + method.upper() + path).encode()
    sig = pk.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return ts, base64.b64encode(sig).decode(), key_id


def _auth_headers(method: str, path: str) -> dict:
    ts, sig, key_id = _sign(method, path)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Accept": "application/json",
    }


def _request(method: str, signed_path: str, url_suffix: str, *, json_body=None):
    """Returns (status, body). On network failure status is None and body
    carries `error` plus `ambiguous=True` if the request may have reached
    the server (read-timeout, dropped POST/DELETE). Connect-timeout / DNS /
    refused set ambiguous=False — the request was not delivered."""
    headers = _auth_headers(method, signed_path)
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    url = f"{BASE}{url_suffix}"
    fn = {"GET": requests.get, "POST": requests.post, "DELETE": requests.delete}[method.upper()]
    kwargs = {"headers": headers, "timeout": REQUEST_TIMEOUT}
    if json_body is not None:
        kwargs["json"] = json_body
    is_mutating = method.upper() in ("POST", "DELETE")
    try:
        r = fn(url, **kwargs)
    except requests.ConnectTimeout as e:
        return None, {"error": f"connect_timeout: {e}", "ambiguous": False}
    except requests.ReadTimeout as e:
        return None, {"error": f"read_timeout: {e}", "ambiguous": is_mutating}
    except requests.ConnectionError as e:
        return None, {"error": f"connection_error: {e}", "ambiguous": is_mutating}
    except requests.RequestException as e:
        return None, {"error": f"request_exception: {e}", "ambiguous": is_mutating}
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text}
    return r.status_code, body


def place_limit_order(ticker: str, side: str, count: int, limit_cents: int,
                      client_order_id: str | None = None) -> dict:
    """POST a limit-buy order. Returns Kalshi order envelope or {error: ...}."""
    if side not in ("yes", "no"):
        return {"error": f"invalid side: {side}"}
    if not (1 <= limit_cents <= 99):
        return {"error": f"limit_cents out of range: {limit_cents}"}
    if count < 1:
        return {"error": f"count must be >= 1, got {count}"}
    coid = client_order_id or str(uuid.uuid4())
    body = {
        "ticker": ticker,
        "client_order_id": coid,
        "side": side,
        "action": "buy",
        "type": "limit",
        "count": int(count),
        ("yes_price" if side == "yes" else "no_price"): int(limit_cents),
    }
    status, resp = _request("POST", ORDERS_PATH, "/portfolio/orders", json_body=body)
    if status is None:
        return {"error": resp.get("error"), "ambiguous": resp.get("ambiguous", False),
                "client_order_id": coid, "request": body}
    if status >= 500:
        # 5xx may have been processed by the broker. Caller must reconcile
        # before refunding the local stake.
        return {"error": f"http {status}", "response": resp, "request": body,
                "ambiguous": True, "client_order_id": coid}
    if status >= 300:
        return {"error": f"http {status}", "response": resp, "request": body,
                "client_order_id": coid}
    order = resp.get("order") or {}
    return {
        "order_id": order.get("order_id"),
        "client_order_id": body["client_order_id"],
        "status": _map_order_state(order.get("status")),
        "filled_count": _int_fp(order.get("fill_count_fp")),
        "remaining_count": _int_fp(order.get("remaining_count_fp")),
        "avg_fill_price": _avg_fill_dollars(order),
        "taker_fees": _taker_fees_dollars(order),
        "raw": resp,
    }


def _avg_fill_dollars(order: dict):
    """Compute average fill price in dollars from Kalshi's fill cost / count,
    since Kalshi does not return a top-level avg_fill_price field."""
    try:
        cost = float(order.get("taker_fill_cost_dollars") or 0)
        count = float(order.get("fill_count_fp") or 0)
        if count > 0:
            return round(cost / count, 6)
    except (TypeError, ValueError):
        pass
    return None


def _taker_fees_dollars(order: dict):
    """Total taker fees charged for this order, in dollars. Returns None for
    resting / zero-fill orders where no fee has been charged yet.

    TODO: confirm the exact field name against a live order response before
    trusting this value. Candidates (Kalshi exposes *_dollars and *_cents
    variants for other cost fields):
      - taker_fees_dollars  (preferred — mirrors taker_fill_cost_dollars)
      - taker_fees_cents ÷ 100
    Verify via:  kalshi_trade.get_order(<recent_order_id>)["raw"]
    """
    try:
        # Prefer the dollars variant; fall back to cents ÷ 100.
        val = order.get("taker_fees_dollars")
        if val is None:
            cents = order.get("taker_fees_cents")
            if cents is not None:
                val = float(cents) / 100.0
        if val is not None:
            fee = float(val)
            if fee > 0:
                return round(fee, 6)
    except (TypeError, ValueError):
        pass
    return None


def get_order(order_id: str) -> dict:
    """GET single order details. Used by the polling thread to track fills."""
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    status, resp = _request("GET", path, f"/portfolio/orders/{order_id}")
    if status is None:
        return {"error": resp.get("error")}
    if status >= 300:
        return {"error": f"http {status}", "response": resp}
    order = resp.get("order") or {}
    return {
        "order_id": order.get("order_id"),
        "status": _map_order_state(order.get("status")),
        "filled_count": _int_fp(order.get("fill_count_fp")),
        "remaining_count": _int_fp(order.get("remaining_count_fp")),
        "avg_fill_price": _avg_fill_dollars(order),
        "taker_fees": _taker_fees_dollars(order),
        "raw": resp,
    }


def cancel_order(order_id: str) -> dict:
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    status, resp = _request("DELETE", path, f"/portfolio/orders/{order_id}")
    return {"http_status": status, "response": resp}


def get_balance() -> dict:
    """GET account balance. Returns {balance_cents, withdrawable_cents}."""
    status, resp = _request("GET", BALANCE_PATH, "/portfolio/balance")
    if status is None:
        return {"error": resp.get("error")}
    if status >= 300:
        return {"error": f"http {status}", "response": resp}
    return {
        "balance_cents": int(resp.get("balance") or 0),
        "withdrawable_cents": int(resp.get("withdrawable_balance") or 0),
        "raw": resp,
    }


def last_look_ok(ticker: str, side: str, limit_cents: int, required_count: int) -> bool:
    """Re-fetch the orderbook and confirm enough depth still exists at our
    limit price (or better) on the side we want to buy.

    For a YES buy: there must be at least `required_count` shares available
    at YES <= limit_cents. Kalshi's orderbook publishes the resting NO bids,
    so YES ask = 100 - best_no_bid_cents. We walk no_dollars looking for
    any level whose implied YES price is <= our limit, summing qty.
    """
    try:
        r = requests.get(
            f"{BASE}/markets/{ticker}/orderbook", timeout=REQUEST_TIMEOUT
        )
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError):
        return False
    ob = body.get("orderbook_fp") or body.get("orderbook") or {}
    if side == "yes":
        opp = ob.get("no_dollars") or ob.get("no") or []
        avail = 0
        for entry in opp:
            try:
                no_price = float(entry[0])
                qty = int(float(entry[1]))
            except (TypeError, ValueError, IndexError):
                continue
            if qty <= 0:
                continue
            yes_ask_cents = round((1.0 - no_price) * 100)
            if yes_ask_cents <= limit_cents:
                avail += qty
        return avail >= required_count
    elif side == "no":
        opp = ob.get("yes_dollars") or ob.get("yes") or []
        avail = 0
        for entry in opp:
            try:
                yes_price = float(entry[0])
                qty = int(float(entry[1]))
            except (TypeError, ValueError, IndexError):
                continue
            if qty <= 0:
                continue
            no_ask_cents = round((1.0 - yes_price) * 100)
            if no_ask_cents <= limit_cents:
                avail += qty
        return avail >= required_count
    return False
