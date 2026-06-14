"""
Polymarket US authenticated trade adapter.

Built against the public spec at https://docs.polymarket.us. Auth uses
Ed25519 signing of (timestamp + method + path), with three headers:

    X-PM-Access-Key   = key id
    X-PM-Timestamp    = ms epoch (must be within 30s of server time)
    X-PM-Signature    = base64(ed25519.sign(timestamp + method + path))

Endpoints used:
    POST /v1/orders                  Create Order
    GET  /v1/order/{orderId}         Get Order
    POST /v1/order/{orderId}/cancel  Cancel Order
    GET  /v1/account/balances        Account Balances

Env:
  POLYMARKETUS_API_KEY        Key ID (alias: POLYMARKET_US_KEY_ID, POLYMARKET_KEY_ID)
  POLYMARKETUS_PRIVATE_KEY    Base64 secret returned by the developer portal
                              (alias: POLYMARKET_US_SECRET, POLYMARKET_SECRET_KEY).
                              Per the docs, the first 32 bytes of the base64
                              decode are the Ed25519 private key seed.
"""
from __future__ import annotations

import base64
import os
import threading
import time
import uuid

import requests
from cryptography.hazmat.primitives.asymmetric import ed25519
from pmev import config

BOOK = "polymarket"
BASE = "https://api.polymarket.us"
REQUEST_TIMEOUT = config.ADAPTER_TRADE_TIMEOUT

_key_lock = threading.Lock()
_private_key = None
_key_id_cached = None


def _env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _decode_secret(secret: str) -> bytes:
    """Decode a base64 secret tolerantly. Polymarket's docs example uses
    standard base64, but their developer portal may emit url-safe (- _ instead
    of + /). Also auto-pads if the input is missing trailing `=`s.
    """
    s = secret.strip()
    # Add missing padding (multiple-of-4 rule)
    pad = (-len(s)) % 4
    s_padded = s + ("=" * pad)
    errs = []
    for label, fn in (
        ("standard", base64.b64decode),
        ("urlsafe", base64.urlsafe_b64decode),
    ):
        try:
            return fn(s_padded.encode())
        except (ValueError, base64.binascii.Error) as e:
            errs.append(f"{label}: {e}")
    raise RuntimeError("; ".join(errs))


def _load_private_key():
    global _private_key, _key_id_cached
    with _key_lock:
        if _private_key is not None:
            return _private_key, _key_id_cached
        key_id = _env("POLYMARKETUS_API_KEY", "POLYMARKET_US_KEY_ID", "POLYMARKET_KEY_ID")
        secret = _env("POLYMARKETUS_PRIVATE_KEY", "POLYMARKET_US_SECRET", "POLYMARKET_SECRET_KEY")
        if not key_id:
            raise RuntimeError("POLYMARKETUS_API_KEY must be set")
        if not secret:
            raise RuntimeError("POLYMARKETUS_PRIVATE_KEY must be set")
        try:
            raw = _decode_secret(secret)
        except Exception as e:
            raise RuntimeError(
                f"POLYMARKETUS_PRIVATE_KEY did not decode (tried both standard "
                f"and urlsafe base64): {e}. Length was {len(secret.strip())} chars."
            )
        if len(raw) < 32:
            raise RuntimeError(
                f"POLYMARKETUS_PRIVATE_KEY decodes to {len(raw)} bytes; "
                "expected at least 32 (Ed25519 seed)."
            )
        try:
            _private_key = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"POLYMARKETUS_PRIVATE_KEY did not decode to an Ed25519 key: {e}")
        _key_id_cached = key_id
        return _private_key, _key_id_cached


def _sign(method: str, path: str):
    pk, key_id = _load_private_key()
    ts = str(int(time.time() * 1000))
    msg = (ts + method.upper() + path).encode()
    sig = pk.sign(msg)
    return ts, base64.b64encode(sig).decode(), key_id


def _auth_headers(method: str, path: str) -> dict:
    ts, sig, key_id = _sign(method, path)
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": ts,
        "X-PM-Signature": sig,
        "Accept": "application/json",
    }


def _request(method: str, path: str, *, json_body=None):
    """Returns (status, body). On network failure status is None and body
    carries `error` plus `ambiguous=True` if the request may have reached
    the server (read-timeout, dropped POST). Connect-timeout / DNS / refused
    set ambiguous=False — the request was definitely not delivered.
    """
    headers = _auth_headers(method, path)
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    url = f"{BASE}{path}"
    fn = {"GET": requests.get, "POST": requests.post}[method.upper()]
    kwargs = {"headers": headers, "timeout": REQUEST_TIMEOUT}
    if json_body is not None:
        kwargs["json"] = json_body
    is_post = method.upper() == "POST"
    try:
        r = fn(url, **kwargs)
    except requests.ConnectTimeout as e:
        return None, {"error": f"connect_timeout: {e}", "ambiguous": False}
    except requests.ReadTimeout as e:
        return None, {"error": f"read_timeout: {e}", "ambiguous": is_post}
    except requests.ConnectionError as e:
        return None, {"error": f"connection_error: {e}", "ambiguous": is_post}
    except requests.RequestException as e:
        return None, {"error": f"request_exception: {e}", "ambiguous": is_post}
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text}
    return r.status_code, body


def place_limit_order(market_slug: str, side: str, count: int, limit_price_dollars: float,
                      tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL",
                      client_order_id: str | None = None) -> dict:
    """Buy `count` shares of `side` (yes|no) on `market_slug` at limit_price_dollars.

    Polymarket prices are dollars in [0.01, 0.99] with 0.01 ticks. `count` is
    in shares. side maps to OUTCOME_SIDE_YES or OUTCOME_SIDE_NO with action
    ORDER_ACTION_BUY (entering a position).
    """
    if side not in ("yes", "no"):
        return {"error": f"invalid side: {side}"}
    if count < 1:
        return {"error": f"count must be >= 1, got {count}"}
    if not (0.01 <= limit_price_dollars <= 0.99):
        return {"error": f"limit_price out of range: {limit_price_dollars}"}
    coid = client_order_id or str(uuid.uuid4())
    body = {
        "marketSlug": market_slug,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": f"{limit_price_dollars:.2f}", "currency": "USD"},
        "quantity": float(count),
        "tif": tif,
        "outcomeSide": "OUTCOME_SIDE_YES" if side == "yes" else "OUTCOME_SIDE_NO",
        "action": "ORDER_ACTION_BUY",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        # Best-effort idempotency hint: the documented schema does not list a
        # client id field, but sending one is harmless (unknown fields are
        # typically ignored) and protects us if/when the field is added.
        "clientOrderId": coid,
    }
    status, resp = _request("POST", "/v1/orders", json_body=body)
    if status is None:
        return {"error": resp.get("error"), "ambiguous": resp.get("ambiguous", False),
                "client_order_id": coid, "request": body}
    if status >= 500:
        return {"error": f"http {status}", "response": resp, "request": body,
                "ambiguous": True, "client_order_id": coid}
    if status >= 300:
        return {"error": f"http {status}", "response": resp, "request": body,
                "client_order_id": coid}
    order_id = resp.get("id")
    return {
        "order_id": order_id,
        "client_order_id": coid,
        "status": "pending",
        "filled_count": 0,
        "remaining_count": int(count),
        "raw": resp,
    }


def get_order(order_id: str) -> dict:
    path = f"/v1/order/{order_id}"
    status, resp = _request("GET", path)
    if status is None:
        return {"error": resp.get("error")}
    if status >= 300:
        return {"error": f"http {status}", "response": resp}
    # Polymarket wraps the order body in {"order": {...}}; older paths returned
    # it flat. Accept either.
    order = resp.get("order") if isinstance(resp.get("order"), dict) else resp
    state = order.get("state") or ""
    avg_px_amount = order.get("avgPx") or {}
    avg_px = None
    try:
        avg_px = float(avg_px_amount.get("value")) if avg_px_amount.get("value") else None
    except (TypeError, ValueError):
        avg_px = None
    cum = order.get("cumQuantity")
    leaves = order.get("leavesQuantity")
    return {
        "order_id": order.get("id"),
        "status": _map_order_state(state),
        "filled_count": int(cum) if isinstance(cum, (int, float)) else 0,
        "remaining_count": int(leaves) if isinstance(leaves, (int, float)) else 0,
        "avg_fill_price": avg_px,
        "raw": resp,
    }


def cancel_order(order_id: str) -> dict:
    path = f"/v1/order/{order_id}/cancel"
    status, resp = _request("POST", path, json_body={})
    return {"http_status": status, "response": resp}


def get_balance() -> dict:
    """GET /v1/account/balances. Returns {balance_dollars, raw}."""
    status, resp = _request("GET", "/v1/account/balances")
    if status is None:
        return {"error": resp.get("error")}
    if status >= 300:
        return {"error": f"http {status}", "response": resp}
    # Response shape varies; surface buying-power-like fields when present.
    out = {"raw": resp}
    # Observed shape (2026-04):
    #   { balances: [{ currency: "USD", currentBalance: 500, buyingPower: 500,
    #                  assetNotional: 0, balanceReservation: 490, ... }] }
    for k in ("balances", "items", "data"):
        v = resp.get(k)
        if isinstance(v, list):
            for entry in v:
                if (entry.get("currency") or entry.get("ccy")) == "USD":
                    for cand in ("currentBalance", "buyingPower", "value", "amount"):
                        val = entry.get(cand)
                        if isinstance(val, (int, float)):
                            out["balance_dollars"] = float(val)
                            break
                        if isinstance(val, str):
                            try:
                                out["balance_dollars"] = float(val)
                                break
                            except ValueError:
                                pass
                    bp = entry.get("buyingPower")
                    if isinstance(bp, (int, float)):
                        out["buying_power_dollars"] = float(bp)
    return out


def last_look_ok(market_slug: str, side: str, limit_price_dollars: float, required_count: int) -> bool:
    """Re-fetch the public order book and confirm depth at <= our limit on the
    side we want. Public endpoint, no auth.

    Response shape: {"marketData": {"bids":[...], "offers":[{"px":{"value":"0.15",
    "currency":"USD"}, "qty":"260"}]}}.
    """
    try:
        r = requests.get(
            f"https://gateway.polymarket.us/v1/markets/{market_slug}/book",
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        book = r.json()
    except (requests.RequestException, ValueError):
        return False
    md = book.get("marketData") or book
    levels = md.get("offers" if side == "yes" else "bids") or []
    avail = 0.0
    for entry in levels:
        try:
            px = float(entry["px"]["value"])
            qty = float(entry["qty"])
        except (KeyError, TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        if (side == "yes" and px <= limit_price_dollars) or (
            side == "no" and (1.0 - px) <= limit_price_dollars
        ):
            avail += qty
    return avail >= required_count


def _map_order_state(state: str) -> str:
    """Translate Polymarket's ORDER_STATE_* enum into the Kalshi-compatible
    status vocabulary real_tracker tracks: pending / partial / filled /
    canceled / rejected / expired."""
    s = (state or "").upper()
    if "FILLED" in s and "PARTIAL" in s:
        return "partial"
    if s.endswith("_FILLED") or s == "ORDER_STATE_FILLED":
        return "filled"
    if "CANCEL" in s:
        return "canceled"
    if "REJECT" in s:
        return "rejected"
    if "EXPIRE" in s:
        return "expired"
    return "pending"
