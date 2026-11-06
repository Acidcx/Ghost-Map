"""Ghost Map watching its own CPU use, and the guard against asyncio's self-pipe spin on Windows."""

import asyncio
import logging
import socket
import sys
import threading
import time

import pytest

from ghostmap import selfwatch
from ghostmap.analysis.alerts import host_alerts
from ghostmap.selfwatch import SelfWatch, guard_self_pipe


class Clock:
    def __init__(self):
        self.t, self.cpu, self.wall = 0.0, 0.0, 1_000_000.0

    def run(self, seconds: float, core: float):
        self.t += seconds
        self.wall += seconds
        self.cpu += seconds * core


def watch(clock, **kw):
    return SelfWatch(sample_s=5, busy_core=0.6, busy_for_s=60, clock=lambda: clock.t, cpu=lambda: clock.cpu,
                     wall=lambda: clock.wall, stacks=lambda: "--- thread MainThread\n  spinning here", **kw)


def test_quiet_process_is_not_busy():
    c = Clock()
    w = watch(c)
    for _ in range(30):
        c.run(5, 0.05)
        w.tick()
    snap = w.snapshot()
    assert not snap["busy"] and snap["now_core"] == 5.0
    assert host_alerts({"ghostmap": snap}) == []


def test_runaway_is_logged_with_stacks_once_and_alerted(caplog):
    """One pinned core on a two-core HMI is 50% in Task Manager: under the HMI's 90% CPU alert, so Ghost Map
    flags its own runaway and writes what its threads were doing."""
    c = Clock()
    w = watch(c)
    w.cores = 2
    caplog.set_level(logging.WARNING, "ghostmap.selfwatch")
    for _ in range(11):  # 55 s: not yet
        c.run(5, 1.0)
        w.tick()
    assert not w.snapshot()["busy"] and not caplog.records
    for _ in range(10):
        c.run(5, 1.0)
        w.tick()
    dumps = [r for r in caplog.records if "spinning here" in r.getMessage()]
    assert len(dumps) == 1 and w.runaways == 1  # at most once per DUMP_EVERY_S
    snap = w.snapshot()
    assert snap["busy"] and snap["now"] == 50.0 and snap["now_core"] == 100.0
    [a] = host_alerts({"ghostmap": snap})
    assert a["code"] == "hmi.ghostmap_busy" and "debug bundle" in a["hint"]
    c.run(5, 0.02)
    w.tick()
    assert not w.snapshot()["busy"] and host_alerts({"ghostmap": w.snapshot()}) == []


def test_brief_burst_is_not_a_runaway(caplog):
    c = Clock()
    w = watch(c)
    caplog.set_level(logging.WARNING, "ghostmap.selfwatch")
    for core in [1.0] * 8 + [0.1] + [1.0] * 8:  # a scan or a big export, with a pause between
        c.run(5, core)
        w.tick()
    assert not w.snapshot()["busy"] and not caplog.records


def test_real_thread_samples_this_process():
    w = SelfWatch(sample_s=0.05)
    w.start()
    try:
        end = time.time() + 0.5
        while time.time() < end:
            sum(range(10000))
    finally:
        w.stop()
    assert w.samples and w.snapshot()["now"] is not None
    got = []
    t = threading.Thread(target=lambda: got.append(selfwatch.thread_stacks()))
    t.start()
    t.join()
    assert "--- thread MainThread" in got[0] and "test_real_thread_samples_this_process" in got[0]


def test_host_snapshot_includes_ghostmap_cpu(tmp_path):
    from ghostmap.collectors.host import HostMonitor

    snap = HostMonitor(tmp_path).snapshot()
    assert set(snap["ghostmap"]) >= {"now", "now_core", "avg_5m", "busy", "cores"}


def test_one_powershell_read_at_a_time(tmp_path, monkeypatch):
    """Several pages opened at once (Home, the alert bar, the warm-up just after a restart) share one read."""
    from ghostmap.collectors import host

    mon = host.HostMonitor(tmp_path)
    calls = []

    async def slow_read():
        calls.append(1)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(mon, "_read_windows", slow_read)

    async def go():
        await asyncio.gather(*(mon.read_windows() for _ in range(5)))
        await mon.read_windows()  # a later one reads again

    asyncio.run(go())
    assert len(calls) == 2


# ------------------------------------------------------------------- the self-pipe guard
class FakeLoop:
    """Just the self-pipe parts of asyncio's BaseProactorEventLoop."""

    def __init__(self):
        self.rearmed = []
        self._self_reading_future = None
        self._internal_fds = 0
        self._make_self_pipe()

    def _make_self_pipe(self):
        self._ssock, self._csock = socket.socketpair()
        self._internal_fds += 1

    def _close_self_pipe(self):
        self._ssock.close()
        self._csock.close()
        self._ssock = self._csock = None
        self._internal_fds -= 1

    def _loop_self_reading(self, f=None):  # stands in for asyncio's: re-arm a read of the self-pipe
        self.rearmed.append(f)
        fut = asyncio.Future(loop=asyncio.new_event_loop())
        self._self_reading_future = fut
        return fut


def done(result):
    f = asyncio.Future(loop=asyncio.new_event_loop())
    f.set_result(result)
    return f


def test_guard_rebuilds_a_self_pipe_at_eof(caplog):
    class Loop(FakeLoop):
        pass

    assert guard_self_pipe(Loop) and guard_self_pipe(Loop)  # idempotent
    loop = Loop()
    old = loop._csock
    caplog.set_level(logging.WARNING, "ghostmap.selfwatch")

    f = loop._self_reading_future = done(b"\0")  # a normal wake-up: passed through untouched
    loop._loop_self_reading(f)
    assert loop.rearmed == [f] and loop._csock is old

    f = loop._self_reading_future = done(b"")  # EOF: the pair was shut down from outside
    loop._loop_self_reading(f)
    assert loop.rearmed[-1] is None  # started afresh on the new pair, not re-armed on the dead one
    assert loop._csock is not old and old.fileno() == -1 and loop._internal_fds == 1
    assert "gh-156333" in caplog.text


def test_guard_is_windows_only_by_default(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert guard_self_pipe() is False


@pytest.mark.skipif(sys.platform != "win32", reason="ProactorEventLoop is Windows only")
def test_proactor_survives_a_shut_down_self_pipe():  # pragma: no cover - runs on the Windows CI job
    """The CPython gh-156333 reproducer: shut down the write end of the loop's self-pipe. Unguarded, every read
    returns b'' at once and the loop spins at 100% of a core; guarded, it makes a new pair and stays idle."""
    from asyncio.proactor_events import BaseProactorEventLoop

    guard_self_pipe()
    loop = asyncio.ProactorEventLoop()
    calls = []
    original = BaseProactorEventLoop._loop_self_reading

    def counting(self, f=None):
        calls.append(f)
        return original(self, f)

    BaseProactorEventLoop._loop_self_reading = counting
    try:
        async def main():
            await asyncio.sleep(0.05)
            loop._csock.shutdown(socket.SHUT_WR)
            await asyncio.sleep(0.5)
            woke = asyncio.Event()
            loop.call_soon_threadsafe(woke.set)  # wake-ups from other threads still work
            await asyncio.wait_for(woke.wait(), 2)

        before = time.process_time()
        loop.run_until_complete(main())
        assert len(calls) < 20, f"self-pipe re-armed {len(calls)} times: spinning"
        assert time.process_time() - before < 0.4
    finally:
        BaseProactorEventLoop._loop_self_reading = original
        loop.close()


def test_cpu_alert_needs_minutes_not_one_boot_second(tmp_path, monkeypatch):
    """Seen on the Test HMI (2026-11-06): the service started 2 min after boot, the first CPU sample covered the
    one second since it started (100% while Windows and FactoryTalk were still starting) and the alert list said
    "CPU 100% busy for 5 minutes" 12 s later. Every sample now spans at least 20 s, and the 5-minute average
    needs four of them."""
    from ghostmap.collectors import host

    t = {"mono": 1000.0, "busy": 0.0, "total": 0.0}
    monkeypatch.setattr(host.time, "monotonic", lambda: t["mono"])
    monkeypatch.setattr(host.time, "time", lambda: 1_790_000_000 + t["mono"])
    monkeypatch.setattr(host, "cpu_times", lambda: (t["busy"], t["total"]))

    def run(seconds, pct):
        t["mono"] += seconds
        t["total"] += seconds
        t["busy"] += seconds * pct / 100

    mon = host.HostMonitor(tmp_path)
    run(1, 100)  # boot: everything starting at once
    mon.sample()
    snap = mon.snapshot()
    assert snap["cpu"]["now"] is None and snap["cpu"]["avg_5m"] is None
    assert not [a for a in host_alerts(snap) if a["code"] == "hmi.cpu_high"]
    for _ in range(4):  # then the normal 5-12% the HMI ran at
        run(60, 7)
        mon.sample()
    snap = mon.snapshot()
    assert 7 <= snap["cpu"]["avg_5m"] < 20  # the boot second is folded into the first minute
    assert not [a for a in host_alerts(snap) if a["code"] == "hmi.cpu_high"]
    for _ in range(6):  # really busy for 5 minutes still alerts
        run(60, 97)
        mon.sample()
    assert [a for a in host_alerts(mon.snapshot()) if a["code"] == "hmi.cpu_high"]
