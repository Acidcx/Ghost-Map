"""Ghost Map watching itself: its own CPU use, and a guard against a known Windows asyncio spin.

**CPU watch.** A small thread samples this process's CPU time every ``SAMPLE_S``. When Ghost Map keeps
``BUSY_CORE`` of a core busy for ``BUSY_FOR_S``, it writes what every thread was doing to the debug log (once per
``DUMP_EVERY_S``) and the alert list says so. Task Manager shows one pinned core as 50% on a two-core HMI and 25%
on four, under the HMI's 90% CPU alert, so a runaway would otherwise go unnoticed. A thread, not an asyncio task,
so it still runs when the event loop itself is what spins.

**Self-pipe guard (Windows).** asyncio's ProactorEventLoop wakes itself through a socket pair. If something
outside Python (a network filter driver, security software) shuts that pair down, every read of it returns
zero bytes at once and the loop re-arms it forever: one core at 100% until the process restarts
(CPython gh-156333, open as of 2026-11). ``guard_self_pipe()`` makes the loop build a new pair instead, and
logs that it happened.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import traceback
from collections import deque
from typing import Callable, Optional

log = logging.getLogger("ghostmap.selfwatch")

SAMPLE_S = 5.0
BUSY_CORE = 0.6        # fraction of one CPU core
BUSY_FOR_S = 60.0      # ... for this long counts as a runaway
DUMP_EVERY_S = 1800.0  # thread stacks written to the debug log at most this often
KEPT_S = 3600.0        # samples kept for the This PC page


def thread_stacks() -> str:
    """Every thread's current stack, named, for the debug log."""
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        if ident == threading.get_ident():
            continue  # the watcher itself
        parts.append(f"--- thread {names.get(ident, '?')} ({ident})\n" + "".join(traceback.format_stack(frame)))
    return "\n".join(parts)


class SelfWatch:
    """Samples this process's CPU time (all threads). ``tick()`` is one sample; ``start()`` runs it in a thread."""

    def __init__(self, sample_s: float = SAMPLE_S, busy_core: float = BUSY_CORE, busy_for_s: float = BUSY_FOR_S,
                 clock: Callable[[], float] = time.monotonic, cpu: Callable[[], float] = time.process_time,
                 wall: Callable[[], float] = time.time, stacks: Callable[[], str] = thread_stacks):
        self.sample_s, self.busy_core, self.busy_for_s = sample_s, busy_core, busy_for_s
        self.clock, self.cpu, self.wall, self.stacks = clock, cpu, wall, stacks
        self.cores = os.cpu_count() or 1
        self.samples: deque = deque(maxlen=max(2, int(KEPT_S / sample_s)))  # (wall time, fraction of one core)
        self.busy_since: Optional[float] = None  # wall time
        self.dumped_at: Optional[float] = None   # wall time of the last stack dump
        self.runaways = 0
        self._last = (clock(), cpu())
        self._busy_mono: Optional[float] = None
        self._dump_mono: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def tick(self) -> Optional[float]:
        now, used = self.clock(), self.cpu()
        t0, c0 = self._last
        self._last = (now, used)
        if now <= t0:
            return None
        core = max(0.0, (used - c0) / (now - t0))
        self.samples.append((self.wall(), round(core, 3)))
        if core < self.busy_core:
            if self._busy_mono is not None and now - self._busy_mono >= self.busy_for_s:
                log.info("Ghost Map's CPU use is back to normal (%.0f%% of one core)", core * 100)
            self._busy_mono = self.busy_since = None
            return core
        if self._busy_mono is None:
            self._busy_mono, self.busy_since = now, self.wall()
        busy_for = now - self._busy_mono
        if busy_for >= self.busy_for_s and (self._dump_mono is None or now - self._dump_mono >= DUMP_EVERY_S):
            if self._dump_mono is None or self.dumped_at is None or self.dumped_at < self.busy_since:
                self.runaways += 1
            self._dump_mono, self.dumped_at = now, self.wall()
            log.warning("Ghost Map has kept %.0f%% of a CPU core busy for %.0f s (%.0f%% of this PC's %d cores). "
                        "What each thread is doing:\n%s", core * 100, busy_for, core * 100 / self.cores, self.cores,
                        self.stacks())
        return core

    def snapshot(self) -> dict:
        """For /api/host: percent of the whole PC (as Task Manager shows it) and of one core."""
        now = self.wall()
        recent = [c for t, c in self.samples if t >= now - 300]
        last = self.samples[-1][1] if self.samples else None
        busy = self.busy_since is not None and now - self.busy_since >= self.busy_for_s
        return {"cores": self.cores,
                "now": None if last is None else round(last * 100 / self.cores, 1),
                "now_core": None if last is None else round(last * 100, 1),
                "avg_5m": round(sum(recent) * 100 / self.cores / len(recent), 1) if recent else None,
                "busy": busy, "busy_since": self.busy_since if busy else None, "dumped_at": self.dumped_at}

    def _run(self) -> None:
        while not self._stop.wait(self.sample_s):
            try:
                self.tick()
            except Exception:  # never let the watcher die quietly or take anything down
                log.exception("self-watch sample failed")

    def start(self) -> None:
        if self._thread is None:
            self._last = (self.clock(), self.cpu())
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="ghostmap-selfwatch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=2)
            self._thread = None


# ------------------------------------------------------------------- the Windows self-pipe guard
_guarded = False


def guard_self_pipe(loop_cls=None) -> bool:
    """Patch ``loop_cls._loop_self_reading`` (default: asyncio's ProactorEventLoop, on Windows only) so a self-pipe
    read that returns end-of-file rebuilds the pair instead of spinning. Returns whether the guard is in place."""
    global _guarded
    if loop_cls is None:
        if sys.platform != "win32":
            return False
        from asyncio.proactor_events import BaseProactorEventLoop as loop_cls  # noqa: N813
    if getattr(loop_cls._loop_self_reading, "_ghostmap_guard", False):
        return True
    original = loop_cls._loop_self_reading

    def _loop_self_reading(self, f=None):
        if (f is not None and f is self._self_reading_future and f.done() and not f.cancelled()
                and f.exception() is None and not f.result()):
            log.warning("asyncio's wake-up socket was shut down from outside Ghost Map (CPython gh-156333); "
                        "made a new one instead of spinning")
            rebuild_self_pipe(self)
            return original(self, None)
        return original(self, f)

    _loop_self_reading._ghostmap_guard = True
    loop_cls._loop_self_reading = _loop_self_reading
    _guarded = True
    return True


def rebuild_self_pipe(loop) -> None:
    """Close the loop's dead self-pipe and make a new one (and point the signal wake-up at it, as the loop did)."""
    loop._self_reading_future = None
    try:
        loop._close_self_pipe()
    except Exception:  # half-closed sockets: what matters is the new pair
        log.debug("closing the old self-pipe failed", exc_info=True)
        loop._ssock = loop._csock = None
    loop._make_self_pipe()
    if threading.current_thread() is threading.main_thread():
        try:
            signal.set_wakeup_fd(loop._csock.fileno())
        except (ValueError, OSError):
            pass
