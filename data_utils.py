"""Shared poller utilities: logging, snapshot rotation, atomic writes,
shutdown handling, and cycle pacing.

Open when: editing poller cross-cutting behavior (how logs are formatted,
how snapshots rotate, how SIGTERM is honored, how cycle timing clamps).
"""
import json
import os
import signal
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


def make_logger(_log_path=None):
    """Return a log(msg) closure that prints `[UTC-timestamp] msg` to stdout.
    The `_log_path` arg is accepted for backwards-compatibility with callers
    but unused — systemd routes stdout to /dev/null in prod."""

    def log(msg):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[{ts}] {msg}", flush=True)

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
