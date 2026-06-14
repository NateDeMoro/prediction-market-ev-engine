import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("PINNACLE_API_KEY", "test-key")

import datetime as _dt_module
from datetime import timezone, timedelta
import pytest
from unittest.mock import patch

from pmev.pollers import pinnacle as pinnacle_poller
from pmev.pollers import kalshi as kalshi_poller
from pmev.pollers import polymarket as polymarket_poller
from pmev.core import io as data_utils

# Two fixed timestamps: cycle-start and write-time (15s later).
T_START = _dt_module.datetime(2026, 6, 12, 12, 34, 0, tzinfo=timezone.utc)
T_WRITE = _dt_module.datetime(2026, 6, 12, 12, 34, 15, tzinfo=timezone.utc)


def _fake_datetime(*return_values):
    """Return a datetime subclass whose .now() yields return_values in order."""
    vals = list(return_values)
    idx = [0]

    class FakeDatetime(_dt_module.datetime):
        @classmethod
        def now(cls, tz=None):
            v = vals[idx[0]]
            idx[0] = min(idx[0] + 1, len(vals) - 1)
            return v

    return FakeDatetime


# ---------------------------------------------------------------------------
# Minimal fixture builders
# ---------------------------------------------------------------------------

def _pin_matchup(start_dt):
    return {
        "id": 42,
        "type": "matchup",
        "parentId": None,
        "isLive": False,
        "startTime": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "participants": [
            {"alignment": "home", "name": "Home"},
            {"alignment": "away", "name": "Away"},
        ],
    }


def _pin_market(matchup_id=42):
    return {
        "matchupId": matchup_id,
        "period": 0,
        "type": "moneyline",
        "prices": [
            {"designation": "home", "price": -110, "participantId": 1},
            {"designation": "away", "price": 100,  "participantId": 2},
        ],
    }


def _kalshi_market():
    start = T_START + timedelta(hours=1)
    return {
        "ticker": "KXNBA-GAME",
        "occurrence_datetime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expected_expiration_time": None,
        "title": "Team A v Team B",
        "yes_bid_dollars": 0.50,
        "yes_ask_dollars": 0.52,
        "no_bid_dollars": 0.48,
        "no_ask_dollars": 0.50,
        "last_price_dollars": 0.51,
        "liquidity_dollars": 1000.0,
        "status": "open",
    }


def _poly_event():
    return {
        "id": "evt1", "slug": "game1", "ticker": "POLY-GAME",
        "title": "Team A v Team B",
        "startTime": None, "startDate": None,
        "gameId": None, "sportradarGameId": None,
        "live": False, "ended": False, "closed": False,
        "teams": [],
        "markets": [{
            "id": "mkt1", "slug": "mkt1", "question": "Will A win?",
            "marketType": "binary", "sportsMarketTypeV2": None,
            "line": None, "gameStartTime": None, "feeCoefficient": 0.02,
            "outcomes": None, "active": True, "closed": False,
            "archived": False, "ep3Status": None,
            "bestBidQuote": {"value": 0.50}, "bestAskQuote": {"value": 0.52},
            "marketSides": [{
                "id": "side1", "identifier": "yes", "description": "Yes",
                "long": True, "teamId": None, "team": {},
                "price": 0.51, "quote": {"value": 0.51}, "participantId": None,
            }],
        }],
    }


# ---------------------------------------------------------------------------
# Group 1 — Filename reflects write-time, not cycle-start
# ---------------------------------------------------------------------------

def test_pinnacle_filename_uses_write_time(tmp_path):
    """snap_path filename stem must equal T_WRITE (second datetime.now call), not T_START."""
    start = T_START + timedelta(hours=1)
    FakeDate = _fake_datetime(T_START, T_WRITE)

    with patch("pmev.pollers.pinnacle.fetch_sports", return_value=[{"id": 1, "name": "Soccer", "matchupCount": 1}]), \
         patch("pmev.pollers.pinnacle.fetch_raw_matchups", return_value=[_pin_matchup(start)]), \
         patch("pmev.pollers.pinnacle.fetch_bulk_markets", return_value=[_pin_market()]), \
         patch("pmev.pollers.pinnacle.SNAPSHOT_DIR", str(tmp_path)), \
         patch("pmev.pollers.pinnacle.log", lambda msg: None), \
         patch("pmev.pollers.pinnacle.datetime", FakeDate):
        pinnacle_poller.run_cycle({})

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1, f"expected 1 snapshot file, got {len(files)}"
    stem = files[0].stem
    assert stem == T_WRITE.strftime("%Y%m%dT%H%M%SZ"), (
        f"filename uses cycle-start ({T_START.strftime('%Y%m%dT%H%M%SZ')!r}); "
        f"expected write-time ({T_WRITE.strftime('%Y%m%dT%H%M%SZ')!r})"
    )


def test_kalshi_filename_uses_write_time(tmp_path):
    """Kalshi snap path filename stem must equal T_WRITE (second datetime.now call)."""
    FakeDate = _fake_datetime(T_START, T_WRITE)

    with patch("pmev.pollers.kalshi.fetch_markets_for_series", return_value=[_kalshi_market()]), \
         patch("pmev.pollers.kalshi.SNAPSHOT_DIR", str(tmp_path)), \
         patch("pmev.pollers.kalshi.log", lambda msg: None), \
         patch("pmev.pollers.kalshi.datetime", FakeDate):
        kalshi_poller.run_cycle({}, [{"ticker": "KXNBA-GAME"}], {}, 1)

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1, f"expected 1 snapshot file, got {len(files)}"
    stem = files[0].stem
    assert stem == T_WRITE.strftime("%Y%m%dT%H%M%SZ"), (
        f"filename uses cycle-start ({T_START.strftime('%Y%m%dT%H%M%SZ')!r}); "
        f"expected write-time ({T_WRITE.strftime('%Y%m%dT%H%M%SZ')!r})"
    )


def test_polymarket_filename_uses_write_time(tmp_path):
    """Polymarket snapshot_once filename stem must equal the write-time datetime.now call."""
    FakeDate = _fake_datetime(T_WRITE)

    with patch("pmev.pollers.polymarket.fetch_league_events", return_value=[_poly_event()]), \
         patch("pmev.pollers.polymarket.SNAPSHOT_DIR", str(tmp_path)), \
         patch("pmev.pollers.polymarket._log", lambda msg: None), \
         patch("pmev.pollers.polymarket.datetime", FakeDate):
        polymarket_poller.snapshot_once()

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1, f"expected 1 snapshot file, got {len(files)}"
    stem = files[0].stem
    assert stem == T_WRITE.strftime("%Y%m%dT%H%M%SZ"), (
        f"expected write-time {T_WRITE.strftime('%Y%m%dT%H%M%SZ')!r}, got {stem!r}"
    )


# ---------------------------------------------------------------------------
# Group 1b — captured_at stamped on sidecar + rows (#5)
# ---------------------------------------------------------------------------

def _read_one_jsonl(tmp_path):
    import json
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    with open(files[0]) as f:
        return [json.loads(l) for l in f if l.strip()]


def _read_one_meta(tmp_path):
    import json
    files = list(tmp_path.glob("*.meta.json"))
    assert len(files) == 1
    with open(files[0]) as f:
        return json.load(f)


def test_pinnacle_sidecar_has_captured_at(tmp_path):
    """The .meta.json sidecar carries captured_at == write-time ISO."""
    start = T_START + timedelta(hours=1)
    FakeDate = _fake_datetime(T_START, T_WRITE)
    with patch("pmev.pollers.pinnacle.fetch_sports", return_value=[{"id": 1, "name": "Soccer", "matchupCount": 1}]), \
         patch("pmev.pollers.pinnacle.fetch_raw_matchups", return_value=[_pin_matchup(start)]), \
         patch("pmev.pollers.pinnacle.fetch_bulk_markets", return_value=[_pin_market()]), \
         patch("pmev.pollers.pinnacle.SNAPSHOT_DIR", str(tmp_path)), \
         patch("pmev.pollers.pinnacle.log", lambda msg: None), \
         patch("pmev.pollers.pinnacle.datetime", FakeDate):
        pinnacle_poller.run_cycle({})

    assert _read_one_meta(tmp_path).get("captured_at") == T_WRITE.isoformat()


def test_pinnacle_rows_have_captured_at(tmp_path):
    """Every snapshot row carries captured_at == write-time ISO (fresh fetch)."""
    start = T_START + timedelta(hours=1)
    FakeDate = _fake_datetime(T_START, T_WRITE)
    with patch("pmev.pollers.pinnacle.fetch_sports", return_value=[{"id": 1, "name": "Soccer", "matchupCount": 1}]), \
         patch("pmev.pollers.pinnacle.fetch_raw_matchups", return_value=[_pin_matchup(start)]), \
         patch("pmev.pollers.pinnacle.fetch_bulk_markets", return_value=[_pin_market()]), \
         patch("pmev.pollers.pinnacle.SNAPSHOT_DIR", str(tmp_path)), \
         patch("pmev.pollers.pinnacle.log", lambda msg: None), \
         patch("pmev.pollers.pinnacle.datetime", FakeDate):
        pinnacle_poller.run_cycle({})

    rows = _read_one_jsonl(tmp_path)
    assert rows and all(r.get("captured_at") == T_WRITE.isoformat() for r in rows)


# ---------------------------------------------------------------------------
# Group 2 — Sort-order invariant: prune_snapshots works with write-time filenames
# ---------------------------------------------------------------------------

def test_prune_keeps_newest_by_filename(tmp_path):
    """prune_snapshots deletes oldest filename (chronological = alphabetical)."""
    names = [
        "20260612T120000Z.jsonl",
        "20260612T120100Z.jsonl",
        "20260612T120200Z.jsonl",
    ]
    for n in names:
        (tmp_path / n).write_text("{}")
        (tmp_path / (n[:-6] + ".meta.json")).write_text("{}")

    data_utils.prune_snapshots(str(tmp_path), keep_n=2)

    remaining = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert remaining == ["20260612T120100Z.jsonl", "20260612T120200Z.jsonl"]
    # Oldest sidecar also gone
    assert not (tmp_path / "20260612T120000Z.meta.json").exists()
    # Kept sidecars still present
    assert (tmp_path / "20260612T120100Z.meta.json").exists()
    assert (tmp_path / "20260612T120200Z.meta.json").exists()


def test_prune_companion_meta_removed(tmp_path):
    """prune_snapshots removes .meta.json sidecar together with its .jsonl."""
    (tmp_path / "20260612T120000Z.jsonl").write_text("{}")
    (tmp_path / "20260612T120000Z.meta.json").write_text("{}")
    (tmp_path / "20260612T120100Z.jsonl").write_text("{}")

    data_utils.prune_snapshots(str(tmp_path), keep_n=1)

    assert not (tmp_path / "20260612T120000Z.jsonl").exists()
    assert not (tmp_path / "20260612T120000Z.meta.json").exists()
    assert (tmp_path / "20260612T120100Z.jsonl").exists()


# ---------------------------------------------------------------------------
# Group 3 — Sidecar co-location: .meta.json name follows snap_path
# ---------------------------------------------------------------------------

def test_meta_sidecar_shares_write_time_stem(tmp_path):
    """write_snapshot_meta places .meta.json beside the .jsonl, deriving name from argument."""
    snap = tmp_path / "20260612T123415Z.jsonl"
    snap.write_text("{}")
    data_utils.write_snapshot_meta(str(snap), {"cycle_elapsed_sec": 1.0})

    meta = tmp_path / "20260612T123415Z.meta.json"
    assert meta.exists(), "sidecar not created beside the snapshot"
    # No stale cycle-start sidecar created
    assert not (tmp_path / "20260612T123400Z.meta.json").exists()


# ---------------------------------------------------------------------------
# Group 4 — Market-window filtering unaffected by adding write_now
# ---------------------------------------------------------------------------

def test_pinnacle_window_filtering_uses_cycle_start(tmp_path):
    """A matchup outside the cycle-start window is excluded even when write_now differs."""
    # Matchup starts 1 hour BEFORE cycle start — outside [now, latest]
    past_start = T_START - timedelta(hours=1)
    FakeDate = _fake_datetime(T_START, T_WRITE)

    with patch("pmev.pollers.pinnacle.fetch_sports", return_value=[{"id": 1, "name": "Soccer", "matchupCount": 1}]), \
         patch("pmev.pollers.pinnacle.fetch_raw_matchups", return_value=[_pin_matchup(past_start)]), \
         patch("pmev.pollers.pinnacle.fetch_bulk_markets", return_value=[_pin_market()]), \
         patch("pmev.pollers.pinnacle.SNAPSHOT_DIR", str(tmp_path)), \
         patch("pmev.pollers.pinnacle.log", lambda msg: None), \
         patch("pmev.pollers.pinnacle.datetime", FakeDate):
        pinnacle_poller.run_cycle({})

    # Out-of-window matchup produces no snapshot file
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 0, (
        "out-of-window matchup was included in snapshot; "
        "write_now may have replaced now for window filtering"
    )
