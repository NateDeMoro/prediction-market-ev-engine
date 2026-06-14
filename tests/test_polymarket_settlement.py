"""Polymarket settlement resolution, including the order-book fallback.

Cleanly-resolved ("Tier 1") markets answer `/v1/markets/{slug}/settlement` with
a 0/1 long-side payout. Fallback-tier ("Tier 3") resolutions — used for
suspended/edge-case games like partial-game period markets — 404 there and
expose the result only in the order book's `marketData.stats.settlementPx` once
`state == "MARKET_STATE_EXPIRED"`. A fractional settlementPx is credited as a
scalar float YES-payout, mirroring Kalshi's scalar path.

No network: requests.get and _fetch_book are monkeypatched.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pmev.adapters import polymarket as pm


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _book(state="MARKET_STATE_EXPIRED", settlement_px="1.0000"):
    stats = {}
    if settlement_px is not None:
        stats["settlementPx"] = {"value": settlement_px, "currency": "USD"}
    return {"state": state, "stats": stats}


# --- Tier-1 path (unchanged: /settlement answers) -------------------------

def test_settlement_endpoint_long_and_short(monkeypatch):
    monkeypatch.setattr(pm.requests, "get",
                        lambda *a, **k: _FakeResp({"settlement": 1.0}))
    assert pm.fetch_settlement("slug:long") == "yes"
    assert pm.fetch_settlement("slug:short") == "no"
    monkeypatch.setattr(pm.requests, "get",
                        lambda *a, **k: _FakeResp({"settlement": 0.0}))
    assert pm.fetch_settlement("slug:long") == "no"
    assert pm.fetch_settlement("slug:short") == "yes"


# --- Tier-3 fallback: /settlement 404s, result lives in the book ----------

def _route_404_then_book(monkeypatch, book):
    """Make /settlement 404 and _fetch_book return `book`."""
    monkeypatch.setattr(pm.requests, "get",
                        lambda *a, **k: _FakeResp({}, status_code=404))
    monkeypatch.setattr(pm, "_fetch_book", lambda slug: book)


def test_book_fallback_clean_binary(monkeypatch):
    _route_404_then_book(monkeypatch, _book(settlement_px="1.0000"))
    assert pm.fetch_settlement("slug:long") == "yes"
    assert pm.fetch_settlement("slug:short") == "no"
    _route_404_then_book(monkeypatch, _book(settlement_px="0.0000"))
    assert pm.fetch_settlement("slug:long") == "no"
    assert pm.fetch_settlement("slug:short") == "yes"


def test_book_fallback_fractional_returns_scalar(monkeypatch):
    # Mirrors the stuck nyy-tor f5 market: long settlementPx 0.77.
    _route_404_then_book(monkeypatch, _book(settlement_px="0.7700"))
    long_out = pm.fetch_settlement("slug:long")
    short_out = pm.fetch_settlement("slug:short")
    assert isinstance(long_out, float) and long_out == pytest.approx(0.77)
    assert isinstance(short_out, float) and short_out == pytest.approx(0.23)


def test_book_fallback_not_expired_returns_none(monkeypatch):
    _route_404_then_book(monkeypatch, _book(state="MARKET_STATE_ACTIVE",
                                            settlement_px="1.0000"))
    assert pm.fetch_settlement("slug:long") is None


def test_book_fallback_missing_px_returns_none(monkeypatch):
    _route_404_then_book(monkeypatch, _book(settlement_px=None))
    assert pm.fetch_settlement("slug:long") is None


def test_bad_market_id_returns_none(monkeypatch):
    # No network call should happen for an unparseable id.
    assert pm.fetch_settlement("no-side-suffix") is None
