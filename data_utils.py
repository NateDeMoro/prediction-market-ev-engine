"""Shared poller utilities: logging, snapshot rotation, atomic writes,
shutdown handling, and cycle pacing.

Open when: editing poller cross-cutting behavior (how logs are formatted,
how snapshots rotate, how SIGTERM is honored, how cycle timing clamps).
"""
import glob
import json
import os
import signal
import threading
import time
from datetime import datetime, timezone


class RunFlag:
    """Mutable bool sentinel used by long-running loops to check for shutdown
    without a module-level global. Call `flag.stop()` from a signal handler;
    loop body tests `while flag:` or `if not flag: break`."""

    __slots__ = ("running",)

    def __init__(self):
        self.running = True

    def stop(self):
        self.running = False

    def __bool__(self):
        return self.running


class RateGate:
    """Per-poller request-rate gate: monotonic slot spacing + per-cycle 429 counter.

    Use when: a poller needs to space outbound HTTP requests by a minimum interval
    across multiple threads and track 429 back-pressure within a cycle.

    Instantiate once per poller at module level with that poller's inter-request
    interval. The next-slot is persisted across cycles and never reset; only the
    429 counter resets (via reset_and_get_429).
    """

    __slots__ = ("_lock", "_next_slot", "_count_429", "_interval")

    def __init__(self, interval_sec):
        self._lock = threading.Lock()
        self._next_slot = time.monotonic()
        self._count_429 = 0
        self._interval = interval_sec

    def claim_slot(self):
        """Advance the next available slot and sleep until it arrives.

        Holds the lock only for the slot-advance arithmetic, then releases before
        sleeping — callers in other threads can claim their own slot concurrently.
        """
        with self._lock:
            now = time.monotonic()
            wake_at = max(now, self._next_slot)
            self._next_slot = wake_at + self._interval
        gap = wake_at - time.monotonic()
        if gap > 0:
            time.sleep(gap)

    def record_429(self, backoff):
        """Increment the 429 counter and push the shared slot out by backoff seconds."""
        with self._lock:
            self._count_429 += 1
            self._next_slot = max(self._next_slot, time.monotonic() + backoff)

    def reset_and_get_429(self):
        """Return the current 429 count and reset it to zero."""
        with self._lock:
            count = self._count_429
            self._count_429 = 0
        return count


def make_logger(_log_path=None):
    """Return a log(msg) closure that prints `[UTC-timestamp] msg` to stdout
    and, when _log_path is truthy, also appends the same line to that file.
    File append errors are swallowed so a transient FS fault never kills a cycle.
    None path → stdout only."""

    def log(msg):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        if _log_path:
            try:
                with open(_log_path, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    return log


def prune_snapshots(dir_path, keep_n):
    """Delete all but the `keep_n` most recent *.jsonl files under dir_path.

    Ordering is by filename (our timestamp prefix sorts chronologically), so
    this is resilient to mtime skew if snapshots are ever copied between
    machines. `.tmp` files are ignored — they're in-flight atomic writes.
    """
    try:
        names = sorted(n for n in os.listdir(dir_path) if n.endswith(".jsonl"))
    except OSError:
        return
    for stale in names[:-keep_n] if keep_n > 0 else names:
        try:
            os.remove(os.path.join(dir_path, stale))
        except OSError:
            pass
        # Remove companion sidecar; ignore if absent.
        try:
            os.remove(os.path.join(dir_path, stale[:-len(".jsonl")] + ".meta.json"))
        except OSError:
            pass


def atomic_write_jsonl(path, rows, *, dumps_kwargs=None, logger=None):
    """Write each row as a JSON line to `path` via tmp + os.replace so
    consumers never see a partial file. On OSError, logs via `logger` (if
    provided) and tries to remove the tmp file. Returns True on success."""
    dumps_kwargs = dumps_kwargs or {}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            for row in rows:
                f.write(json.dumps(row, **dumps_kwargs) + "\n")
        os.replace(tmp_path, path)
        return True
    except OSError as e:
        if logger:
            logger(f"  ! snapshot write failed: {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


def write_snapshot_meta(snapshot_jsonl_path, meta_dict, logger=None):
    """Atomically write meta_dict as JSON to the .meta.json sidecar for the
    given snapshot path. Uses tmp+os.replace mirroring atomic_write_jsonl.
    Use when: pollers need to persist per-cycle timing beside a snapshot."""
    meta_path = snapshot_jsonl_path[:-len(".jsonl")] + ".meta.json"
    tmp_path = meta_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(meta_path) or ".", exist_ok=True)
        with open(tmp_path, "w") as f:
            json.dump(meta_dict, f)
        os.replace(tmp_path, meta_path)
    except OSError as e:
        if logger:
            logger(f"  ! meta write failed: {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def read_latest_snapshot_meta(dir_path):
    """Return the parsed dict from the newest .meta.json in dir_path, or None.
    Use when: dashboard needs the most recent per-cycle timing for a book."""
    try:
        files = sorted(glob.glob(os.path.join(dir_path, "*.meta.json")))
    except OSError:
        return None
    if not files:
        return None
    try:
        with open(files[-1]) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def install_shutdown_handlers(flag, logger=None, on_shutdown=None):
    """Install SIGTERM + SIGINT handlers that flip `flag` to False so the
    main loop exits cleanly after its current cycle. `on_shutdown()` (if
    provided) runs inside the handler, useful for unlinking a pid file."""

    def handler(signum, _frame):
        flag.stop()
        if logger:
            logger(f"shutdown signal {signum} received, exiting after current cycle")
        if on_shutdown:
            try:
                on_shutdown()
            except Exception:
                pass

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def sleep_until_next_cycle(cycle_start, interval_sec, flag):
    """Sleep until `cycle_start + interval_sec`, waking every 1s to check
    `flag` so shutdown latency is bounded. Guarantees at least 1s of sleep
    even when a cycle runs long."""
    end = cycle_start + max(1, interval_sec)
    while flag and time.time() < end:
        time.sleep(min(1.0, max(0.0, end - time.time())))
