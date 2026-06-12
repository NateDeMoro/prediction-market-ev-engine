import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "analysis"))

import pytest
from estimate_bias import estimate


def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Group A — Core key correctness (the Q5 constraint)
# ---------------------------------------------------------------------------

def test_yes_no_same_market_each_matches_own_side(tmp_path):
    """A1 — YES and NO trades on the same market each match their own side's close."""
    closes = [
        {"book": "kalshi", "market_id": "MKT1", "side": "yes",
         "fair_prob_close": 0.70, "captured_at": "2024-01-01T12:00:00"},
        # NO close is newer — old code (market_id-only key) picks this for both trades
        {"book": "kalshi", "market_id": "MKT1", "side": "no",
         "fair_prob_close": 0.72, "captured_at": "2024-01-01T12:01:00"},
    ]
    trades = [
        {"book": "kalshi", "market_id": "MKT1", "side": "yes",
         "fair_prob": 0.65, "avg_fill_price": 0.60},
        {"book": "kalshi", "market_id": "MKT1", "side": "no",
         "fair_prob": 0.75, "avg_fill_price": 0.68},
    ]
    tp = tmp_path / "trades.jsonl"
    cp = tmp_path / "closes.jsonl"
    _write_jsonl(tp, trades)
    _write_jsonl(cp, closes)

    res = estimate(str(tp), str(cp))
    assert res["n_pairs"] == 2
    # Reconstruct per-pair closes from bias_mean and clv_mean
    # YES: diff = 0.65 - 0.70 = -0.05; NO: diff = 0.75 - 0.72 = +0.03
    assert res["bias_mean"] == pytest.approx((-0.05 + 0.03) / 2, abs=1e-9)


def test_yes_no_same_market_clv_uses_side_correct_close(tmp_path):
    """A3 — CLV is computed from the side-correct close, not the most-recent capture."""
    closes = [
        {"book": "kalshi", "market_id": "MKT1", "side": "yes",
         "fair_prob_close": 0.70, "captured_at": "2024-01-01T12:00:00"},
        {"book": "kalshi", "market_id": "MKT1", "side": "no",
         "fair_prob_close": 0.72, "captured_at": "2024-01-01T12:01:00"},
    ]
    trades = [
        {"book": "kalshi", "market_id": "MKT1", "side": "yes",
         "fair_prob": 0.65, "avg_fill_price": 0.60},
        {"book": "kalshi", "market_id": "MKT1", "side": "no",
         "fair_prob": 0.75, "avg_fill_price": 0.68},
    ]
    tp = tmp_path / "trades.jsonl"
    cp = tmp_path / "closes.jsonl"
    _write_jsonl(tp, trades)
    _write_jsonl(cp, closes)

    res = estimate(str(tp), str(cp))
    # YES CLV = 0.70 - 0.60 = 0.10; NO CLV = 0.72 - 0.68 = 0.04
    assert res["clv_mean"] == pytest.approx((0.10 + 0.04) / 2, abs=1e-9)


# ---------------------------------------------------------------------------
# Group B — Legacy schema fallbacks
# ---------------------------------------------------------------------------

def test_legacy_ticker_field_matches(tmp_path):
    """B1 — Close and trade using `ticker` instead of `market_id` still match."""
    closes = [
        {"book": "kalshi", "ticker": "KXNFL-23456", "side": "yes",
         "fair_prob_close": 0.55, "captured_at": "2024-01-01T10:00:00"},
    ]
    trades = [
        {"book": "kalshi", "ticker": "KXNFL-23456", "side": "yes",
         "fair_prob": 0.58, "avg_fill_price": 0.52},
    ]
    tp = tmp_path / "trades.jsonl"
    cp = tmp_path / "closes.jsonl"
    _write_jsonl(tp, trades)
    _write_jsonl(cp, closes)

    res = estimate(str(tp), str(cp))
    assert res["n_pairs"] == 1


def test_legacy_record_no_side_defaults_to_yes(tmp_path):
    """B2 — Trade with no `side` field defaults to 'yes' and matches a yes close."""
    closes = [
        {"book": "kalshi", "market_id": "MKT2", "side": "yes",
         "fair_prob_close": 0.60, "captured_at": "2024-01-01T10:00:00"},
    ]
    trades = [
        # no "side" key — legacy record
        {"book": "kalshi", "market_id": "MKT2",
         "fair_prob": 0.63, "avg_fill_price": 0.57},
    ]
    tp = tmp_path / "trades.jsonl"
    cp = tmp_path / "closes.jsonl"
    _write_jsonl(tp, trades)
    _write_jsonl(cp, closes)

    res = estimate(str(tp), str(cp))
    assert res["n_pairs"] == 1


def test_missing_book_field_uses_kalshi_fallback(tmp_path):
    """B3 — Trade with no `book` field uses Kalshi fallback and matches a kalshi close."""
    closes = [
        {"book": "kalshi", "market_id": "MKT3", "side": "yes",
         "fair_prob_close": 0.45, "captured_at": "2024-01-01T10:00:00"},
    ]
    trades = [
        # no "book" key — legacy kalshi record
        {"market_id": "MKT3", "side": "yes",
         "fair_prob": 0.48, "avg_fill_price": 0.43},
    ]
    tp = tmp_path / "trades.jsonl"
    cp = tmp_path / "closes.jsonl"
    _write_jsonl(tp, trades)
    _write_jsonl(cp, closes)

    res = estimate(str(tp), str(cp))
    assert res["n_pairs"] == 1


# ---------------------------------------------------------------------------
# Group C — Edge cases
# ---------------------------------------------------------------------------

def test_no_matching_close_produces_zero_pairs(tmp_path):
    """C1 — Trade with no corresponding close produces no pair and does not crash."""
    closes = []
    trades = [
        {"book": "kalshi", "market_id": "MKT4", "side": "yes",
         "fair_prob": 0.50, "avg_fill_price": 0.48},
    ]
    tp = tmp_path / "trades.jsonl"
    cp = tmp_path / "closes.jsonl"
    _write_jsonl(tp, trades)
    _write_jsonl(cp, closes)

    res = estimate(str(tp), str(cp))
    assert res["n_pairs"] == 0
    assert res["n_trades"] == 1


def test_close_with_null_market_id_and_null_ticker_skipped(tmp_path):
    """C2 — Close record with neither market_id nor ticker is silently skipped."""
    closes = [
        {"book": "kalshi", "side": "yes",
         "fair_prob_close": 0.50, "captured_at": "2024-01-01T10:00:00"},
    ]
    trades = [
        {"book": "kalshi", "market_id": "MKT5", "side": "yes",
         "fair_prob": 0.50, "avg_fill_price": 0.48},
    ]
    tp = tmp_path / "trades.jsonl"
    cp = tmp_path / "closes.jsonl"
    _write_jsonl(tp, trades)
    _write_jsonl(cp, closes)

    res = estimate(str(tp), str(cp))
    assert res["n_pairs"] == 0


def test_two_closes_same_side_newer_captured_at_wins(tmp_path):
    """C3 — Two close records for the same (book, market_id, side): newer captured_at wins."""
    closes = [
        {"book": "kalshi", "market_id": "MKT6", "side": "yes",
         "fair_prob_close": 0.60, "captured_at": "2024-01-01T12:00:00"},
        {"book": "kalshi", "market_id": "MKT6", "side": "yes",
         "fair_prob_close": 0.65, "captured_at": "2024-01-01T12:01:00"},
    ]
    trades = [
        {"book": "kalshi", "market_id": "MKT6", "side": "yes",
         "fair_prob": 0.68, "avg_fill_price": 0.63},
    ]
    tp = tmp_path / "trades.jsonl"
    cp = tmp_path / "closes.jsonl"
    _write_jsonl(tp, trades)
    _write_jsonl(cp, closes)

    res = estimate(str(tp), str(cp))
    assert res["n_pairs"] == 1
    # bias = 0.68 - 0.65 = 0.03 (uses newer close)
    assert res["bias_mean"] == pytest.approx(0.03, abs=1e-9)
