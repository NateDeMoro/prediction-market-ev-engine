"""Shared fixtures for the close-capture tests.

These are the first automated tests in the repo. They drive paper_tracker's
close-capture path against on-disk snapshot fixtures and seeded module state —
no network, no live API. config paths are monkeypatched at runtime (paper_tracker
reads config.PIN_SNAPSHOT_DIR / config.PAPER_CLOSES_PATH at call time, so patching
the attributes is sufficient).
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import paper_tracker as pt


@pytest.fixture
def pin_dir(tmp_path, monkeypatch):
    d = tmp_path / "snapshots"
    d.mkdir()
    monkeypatch.setattr(config, "PIN_SNAPSHOT_DIR", str(d))
    return d


@pytest.fixture
def closes_path(tmp_path, monkeypatch):
    p = tmp_path / "paper_closes.jsonl"
    monkeypatch.setattr(config, "PAPER_CLOSES_PATH", str(p))
    return p


@pytest.fixture
def write_pin_snapshot(pin_dir):
    """Write rows to a new, lexically-latest *.jsonl in the pin snapshot dir.
    age_sec back-dates the file mtime so freshness can be controlled. The loader
    sorts by filename and takes the last, so the zero-padded counter guarantees
    the most-recently-written file is the one loaded."""
    counter = {"n": 0}

    def _write(rows, age_sec=0.0):
        counter["n"] += 1
        path = pin_dir / f"pin_{counter['n']:04d}.jsonl"
        with open(path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        if age_sec:
            mtime = time.time() - age_sec
            os.utime(path, (mtime, mtime))
        return path

    return _write


# --- builders -------------------------------------------------------------

def pin_total_row(matchup_id=1, line=45.5, over=-110, under=-110,
                  period=0, is_live=False):
    """A Pinnacle 'total' snapshot row (over/under at one line)."""
    return {
        "matchupId": matchup_id,
        "type": "total",
        "period": period,
        "isLive": is_live,
        "prices": [
            {"designation": "over", "points": line, "price": over},
            {"designation": "under", "points": line, "price": under},
        ],
    }


def pin_spread_row(matchup_id=1, line=3.5, home=-110, away=-110,
                   period=0, is_live=False):
    """A Pinnacle 'spread' snapshot row. `line` is the record-line coordinate:
    the home (yes) designation sits at points=-line and away (opp) at points=+line,
    matching _find_pin_prices (yes price is found where pts == -line)."""
    return {
        "matchupId": matchup_id,
        "type": "spread",
        "period": period,
        "isLive": is_live,
        "prices": [
            {"designation": "home", "points": -line, "price": home},
            {"designation": "away", "points": line, "price": away},
        ],
    }


def pin_prop_row(matchup_id=1, prop_matchup_id=99, line=20.5,
                 over=-110, under=-110, is_live=False):
    """A Pinnacle 'player_prop' snapshot row."""
    return {
        "matchupId": matchup_id,
        "type": "player_prop",
        "period": 0,
        "isLive": is_live,
        "prop_matchupId": prop_matchup_id,
        "line": line,
        "prices": [
            {"designation": "over", "points": line, "price": over},
            {"designation": "under", "points": line, "price": under},
        ],
    }


def placement(market_id="KX-1", book="kalshi", side="yes",
              market_type="total", matchup_id=1, period_label="FULL",
              line=45.5, yes_designation="over", opposite_designation="under",
              start_in_sec=30, **extra):
    """A paper placement record, as _open_positions stores it."""
    start = datetime.now(timezone.utc) + timedelta(seconds=start_in_sec)
    rec = {
        "book": book,
        "market_id": market_id,
        "side": side,
        "market_type": market_type,
        "pin_matchup_id": matchup_id,
        "pin_matchup": "Home vs Away",
        "period_label": period_label,
        "line": line,
        "yes_designation": yes_designation,
        "opposite_designation": opposite_designation,
        "pin_start_time": start.isoformat(),
    }
    rec.update(extra)
    return rec


def seed_open(record):
    pt._open_positions[pt._record_key(record)] = record
    return pt._record_key(record)


def seed_close(record, fair_prob_close):
    key = pt._record_key(record)
    pt._closes_by_key[key] = {
        "book": record["book"],
        "market_id": record["market_id"],
        "side": record["side"],
        "market_type": record.get("market_type"),
        "fair_prob_close": fair_prob_close,
    }
    return key


def read_closes(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]
