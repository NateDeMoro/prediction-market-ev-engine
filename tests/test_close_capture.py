"""Close-capture tests — #20 (pin freshness gate) and #21 (team last-write-wins
until live, then lock). See notes/clv-close-capture/test-plan.md."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import paper_tracker as pt
from devig_utils import devig_multiplicative

from conftest import (
    pin_total_row, pin_prop_row, placement, seed_open, seed_close, read_closes,
)


@pytest.fixture(autouse=True)
def reset_module_state():
    """Snapshot and restore paper_tracker's in-memory close-capture state so a
    test never leaks positions/closes into the next one. Scoped to this module
    so the rest of the suite is untouched. Also resets the stale-log throttle."""
    saved_open = dict(pt._open_positions)
    saved_closes = dict(pt._closes_by_key)
    pt._open_positions.clear()
    pt._closes_by_key.clear()
    if hasattr(pt, "_PAPER_CTX"):
        pt._PAPER_CTX.pin_stale_logged = False
        pt._PAPER_CTX.interp_dropped_keys.clear()
    yield
    pt._open_positions.clear()
    pt._open_positions.update(saved_open)
    pt._closes_by_key.clear()
    pt._closes_by_key.update(saved_closes)


# ===========================================================================
# Integration — the one end-to-end proof
# ===========================================================================

def test_capture_closes_once_fresh_writes_stale_skips(write_pin_snapshot, closes_path):
    rec = placement(market_type="total", line=45.5, start_in_sec=30)
    seed_open(rec)

    # Fresh pregame snapshot -> one close recorded.
    write_pin_snapshot([pin_total_row(line=45.5, over=-110, under=-110)], age_sec=0)
    pt.capture_closes_once()
    closes = read_closes(closes_path)
    assert len(closes) == 1
    assert closes[0]["market_id"] == "KX-1"
    assert closes[0]["fair_prob_close"] == round(devig_multiplicative([-110, -110])[0], 6)

    # A stale snapshot with a *moved* line would append a new (different) close
    # if the gate were absent. With the gate it is skipped: still one close.
    write_pin_snapshot([pin_total_row(line=45.5, over=120, under=-140)], age_sec=200)
    pt.capture_closes_once()
    assert len(read_closes(closes_path)) == 1


# ===========================================================================
# Group A — #20 freshness gate
# ===========================================================================

def test_load_latest_pin_snapshot_with_age_returns_age(write_pin_snapshot):
    write_pin_snapshot([pin_total_row()], age_sec=70)
    rows, age = pt._load_latest_pin_snapshot_with_age()
    assert rows and rows[0]["type"] == "total"
    assert 65 <= age <= 80
    # Legacy wrapper preserved (real_tracker calls this) — rows only.
    assert pt._load_latest_pin_snapshot() == rows


def test_capture_skips_when_snapshot_stale(write_pin_snapshot, closes_path):
    seed_open(placement(start_in_sec=30))
    write_pin_snapshot([pin_total_row(line=45.5)],
                       age_sec=config.CLOSE_CAPTURE_MAX_PIN_AGE_SEC + 5)
    pt.capture_closes_once()
    assert read_closes(closes_path) == []
    assert pt._closes_by_key == {}


def test_capture_records_when_snapshot_just_fresh(write_pin_snapshot, closes_path):
    seed_open(placement(start_in_sec=30))
    write_pin_snapshot([pin_total_row(line=45.5)],
                       age_sec=config.CLOSE_CAPTURE_MAX_PIN_AGE_SEC - 5)
    pt.capture_closes_once()
    assert len(read_closes(closes_path)) == 1


# ===========================================================================
# Group B — #21 isLive lock + last-write-wins
# ===========================================================================

def test_find_pin_prices_skips_live_rows_for_non_prop():
    rec = placement(market_type="total", line=45.5)
    live = pin_total_row(line=45.5, over=120, under=-140, is_live=True)
    pregame = pin_total_row(line=45.5, over=-110, under=-110, is_live=False)

    assert pt._find_pin_prices([live, pregame], rec) == (-110, -110)   # pregame wins
    assert pt._find_pin_prices([live], rec) is None                    # only live -> lock

    legacy = pin_total_row(line=45.5, over=-105, under=-115)
    legacy.pop("isLive")  # legacy snapshot missing the flag -> treated as pregame
    assert pt._find_pin_prices([legacy], rec) == (-105, -115)


def test_find_pin_prices_ignores_islive_for_props():
    rec = placement(market_type="player_prop", line=20.5, prop_matchup_id=99,
                    yes_designation="over", opposite_designation="under")
    live_prop = pin_prop_row(prop_matchup_id=99, line=20.5,
                             over=-110, under=-110, is_live=True)
    assert pt._find_pin_prices([live_prop], rec) == (-110, -110)


def test_team_close_last_write_wins_before_live(closes_path):
    rec = placement(market_type="total", line=45.5, start_in_sec=30)
    key = seed_open(rec)
    seed_close(rec, fair_prob_close=0.5)
    now = datetime.now(timezone.utc)

    pt._capture_close_for(rec, [pin_total_row(line=45.5, over=120, under=-140)], now)

    expected = round(devig_multiplicative([120, -140])[0], 6)
    assert expected != 0.5
    assert pt._closes_by_key[key]["fair_prob_close"] == expected
    assert len(read_closes(closes_path)) == 1


def test_team_close_locks_when_only_live_rows_remain(closes_path):
    rec = placement(market_type="total", line=45.5, start_in_sec=-60)  # game started
    key = seed_open(rec)
    seed_close(rec, fair_prob_close=0.5)
    now = datetime.now(timezone.utc)

    pt._capture_close_for(rec, [pin_total_row(line=45.5, over=120, under=-140, is_live=True)], now)

    assert pt._closes_by_key[key]["fair_prob_close"] == 0.5   # unchanged: locked
    assert read_closes(closes_path) == []


def test_team_close_throttles_unchanged_value(closes_path):
    rec = placement(market_type="total", line=45.5, start_in_sec=30)
    key = seed_open(rec)
    seed_close(rec, fair_prob_close=round(devig_multiplicative([-110, -110])[0], 6))
    now = datetime.now(timezone.utc)

    pt._capture_close_for(rec, [pin_total_row(line=45.5, over=-110, under=-110)], now)

    assert read_closes(closes_path) == []   # same value -> no duplicate append
    assert pt._closes_by_key[key]["fair_prob_close"] == 0.5


def test_prop_capture_unchanged(closes_path):
    rec = placement(market_type="player_prop", line=20.5, prop_matchup_id=99,
                    yes_designation="over", opposite_designation="under",
                    start_in_sec=30)
    key = seed_open(rec)
    now = datetime.now(timezone.utc)

    pt._capture_close_for(rec, [pin_prop_row(prop_matchup_id=99, line=20.5,
                                             over=-110, under=-110)], now)
    assert pt._closes_by_key[key]["fair_prob_close"] == 0.5
    assert len(read_closes(closes_path)) == 1

    # moved -> overwrite (continuous last-seen)
    pt._capture_close_for(rec, [pin_prop_row(prop_matchup_id=99, line=20.5,
                                             over=120, under=-140)], now)
    expected = round(devig_multiplicative([120, -140])[0], 6)
    assert pt._closes_by_key[key]["fair_prob_close"] == expected
    assert len(read_closes(closes_path)) == 2

    # unchanged -> throttle
    pt._capture_close_for(rec, [pin_prop_row(prop_matchup_id=99, line=20.5,
                                             over=120, under=-140)], now)
    assert len(read_closes(closes_path)) == 2


def test_capture_closes_once_rechecks_team_with_existing_close(write_pin_snapshot, closes_path):
    rec = placement(market_type="total", line=45.5, start_in_sec=30)
    key = seed_open(rec)
    seed_close(rec, fair_prob_close=0.5)

    write_pin_snapshot([pin_total_row(line=45.5, over=120, under=-140, is_live=False)],
                       age_sec=0)
    pt.capture_closes_once()

    expected = round(devig_multiplicative([120, -140])[0], 6)
    assert pt._closes_by_key[key]["fair_prob_close"] == expected   # re-checked & updated
    assert len(read_closes(closes_path)) == 1


# ===========================================================================
# #22 — moved-line interpolation (totals)
# ===========================================================================

def _interp(f_lo, f_hi, lo, hi, line):
    return f_lo + (line - lo) / (hi - lo) * (f_hi - f_lo)


def test_interpolate_total_fair_brackets():
    rec = placement(market_type="total", line=45.5, yes_designation="over",
                    opposite_designation="under")
    rows = [pin_total_row(line=45.0, over=-110, under=-110),
            pin_total_row(line=46.0, over=120, under=-130)]
    f_lo = devig_multiplicative([-110, -110])[0]
    f_hi = devig_multiplicative([120, -130])[0]
    expected = _interp(f_lo, f_hi, 45.0, 46.0, 45.5)
    assert pt._interpolate_total_fair(rows, rec) == pytest.approx(expected)


def test_interpolate_devigs_before_interpolating():
    # With vig present, interpolating devigged fairs differs from interpolating
    # raw implied probs (1/decimal, un-normalized). Result must match the former.
    rec = placement(market_type="total", line=45.5, yes_designation="over",
                    opposite_designation="under")
    rows = [pin_total_row(line=45.0, over=-120, under=-110),
            pin_total_row(line=46.0, over=110, under=-150)]
    devig_interp = _interp(devig_multiplicative([-120, -110])[0],
                           devig_multiplicative([110, -150])[0], 45.0, 46.0, 45.5)
    from devig_utils import american_to_decimal
    raw_interp = _interp(1 / american_to_decimal(-120), 1 / american_to_decimal(110),
                         45.0, 46.0, 45.5)
    got = pt._interpolate_total_fair(rows, rec)
    assert got == pytest.approx(devig_interp)
    assert got != pytest.approx(raw_interp)


def test_interpolate_respects_distance_cap():
    rec = placement(market_type="total", line=45.5)
    rows = [pin_total_row(line=44.0, over=-110, under=-110),
            pin_total_row(line=47.0, over=-110, under=-110)]  # gap 3.0 > cap
    assert pt._interpolate_total_fair(rows, rec) is None


def test_interpolate_no_extrapolation():
    rec = placement(market_type="total", line=46.5)  # above all offered
    rows = [pin_total_row(line=44.0, over=-110, under=-110),
            pin_total_row(line=45.0, over=-110, under=-110)]
    assert pt._interpolate_total_fair(rows, rec) is None


def test_interpolate_skips_live_neighbors():
    rec = placement(market_type="total", line=45.5)
    rows = [pin_total_row(line=45.0, over=-110, under=-110, is_live=False),
            pin_total_row(line=46.0, over=-110, under=-110, is_live=True)]  # live hi
    assert pt._interpolate_total_fair(rows, rec) is None  # no pregame upper bracket


def test_capture_prefers_exact_over_interpolation(closes_path):
    rec = placement(market_type="total", line=45.5, start_in_sec=30)
    seed_open(rec)
    now = datetime.now(timezone.utc)
    rows = [pin_total_row(line=45.5, over=-110, under=-110),   # exact present
            pin_total_row(line=45.0, over=120, under=-130)]
    pt._capture_close_for(rec, rows, now)
    close = pt._closes_by_key[pt._record_key(rec)]
    assert not close.get("was_interpolated")
    assert close["yes_side_price_close"] is not None


def test_capture_marks_interpolated_and_nulls_prices(closes_path):
    rec = placement(market_type="total", line=45.5, start_in_sec=30)
    seed_open(rec)
    now = datetime.now(timezone.utc)
    rows = [pin_total_row(line=45.0, over=-110, under=-110),
            pin_total_row(line=46.0, over=120, under=-130)]   # no exact 45.5
    pt._capture_close_for(rec, rows, now)
    close = pt._closes_by_key[pt._record_key(rec)]
    assert close["was_interpolated"] is True
    assert close["yes_side_price_close"] is None
    assert close["opposite_side_price_close"] is None


def test_capture_drop_counter_increments(closes_path):
    rec = placement(market_type="total", line=45.5, start_in_sec=30)
    seed_open(rec)
    now = datetime.now(timezone.utc)
    rows = [pin_total_row(line=48.0, over=-110, under=-110),
            pin_total_row(line=49.0, over=-110, under=-110)]  # line out of range
    assert pt._capture_close_for(rec, rows, now) is None
    assert read_closes(closes_path) == []
    assert pt._record_key(rec) in pt._PAPER_CTX.interp_dropped_keys


def test_interpolate_side_perspective():
    # Under bet: yes_designation="under". Interpolated fair must be in the under
    # perspective (P(under) rises with the line), matching the exact path.
    rec = placement(market_type="total", line=45.5, yes_designation="under",
                    opposite_designation="over")
    rows = [pin_total_row(line=45.0, over=-110, under=-110),
            pin_total_row(line=46.0, over=120, under=-130)]
    f_lo = devig_multiplicative([-110, -110])[0]            # [under, over] order
    f_hi = devig_multiplicative([-130, 120])[0]
    expected = _interp(f_lo, f_hi, 45.0, 46.0, 45.5)
    assert pt._interpolate_total_fair(rows, rec) == pytest.approx(expected)


# ===========================================================================
# #23 — shared close-capture routine across paper + real
# ===========================================================================

def _seed_real(rt, record):
    rt._open_positions.clear()
    rt._closes_by_key.clear()
    rt._open_positions[pt._record_key(record)] = record


def test_real_uses_shared_capture_with_interpolation(tmp_path, monkeypatch):
    import real_tracker as rt
    monkeypatch.setattr(config, "REAL_CLOSES_PATH", str(tmp_path / "real_closes.jsonl"))
    rec = placement(market_type="total", line=45.5, start_in_sec=30)
    _seed_real(rt, rec)
    now = datetime.now(timezone.utc)
    rows = [pin_total_row(line=45.0, over=-110, under=-110),
            pin_total_row(line=46.0, over=120, under=-130)]   # interpolation case
    pt.capture_close_for(rt._REAL_CTX, rec, rows, now)
    close = rt._closes_by_key[pt._record_key(rec)]
    assert close["was_interpolated"] is True   # real now has #22 via the shared routine
    expected = _interp(devig_multiplicative([-110, -110])[0],
                       devig_multiplicative([120, -130])[0], 45.0, 46.0, 45.5)
    assert close["fair_prob_close"] == pytest.approx(round(expected, 6))


def test_paper_and_real_capture_identical(tmp_path, monkeypatch):
    import real_tracker as rt
    monkeypatch.setattr(config, "PAPER_CLOSES_PATH", str(tmp_path / "p.jsonl"))
    monkeypatch.setattr(config, "REAL_CLOSES_PATH", str(tmp_path / "r.jsonl"))
    rec = placement(market_type="total", line=45.5, start_in_sec=30)
    seed_open(rec)
    _seed_real(rt, rec)
    now = datetime.now(timezone.utc)
    rows = [pin_total_row(line=45.0, over=-110, under=-110, is_live=True),    # live, skipped
            pin_total_row(line=45.5, over=-110, under=-110, is_live=False)]   # exact pregame
    pt.capture_close_for(pt._PAPER_CTX, rec, rows, now)
    pt.capture_close_for(rt._REAL_CTX, rec, rows, now)
    p = pt._PAPER_CTX.closes_by_key[pt._record_key(rec)]
    r = rt._REAL_CTX.closes_by_key[pt._record_key(rec)]
    assert p["fair_prob_close"] == r["fair_prob_close"]
    assert p["was_interpolated"] == r["was_interpolated"]
    assert rt._REAL_CTX.closes_by_key is not pt._PAPER_CTX.closes_by_key   # isolated state
