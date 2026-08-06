"""Offline self-check for pilot_launch.py — fake clock/process, no
network, no subprocesses, no real sleeping."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_launch import (LEAD_SEC, POLL_SEC, STOP_AFTER_SEC, parse_when,
                          session, wait_until)


class Clock:
    """time.time/time.sleep pair where sleeping is what advances time."""
    def __init__(self, start: float):
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, sec: float) -> None:
        assert sec >= 0, f"negative sleep {sec}"
        self.sleeps.append(sec)
        self.now += sec


class Proc:
    """Popen-alike: alive until a scripted death time (None = immortal
    until terminate())."""
    def __init__(self, clock: Clock, dies_at: float | None = None, rc: int = 0):
        self.pid = 424242
        self.clock = clock
        self.dies_at = dies_at
        self.rc = rc
        self.terminated = False

    def _dead(self) -> bool:
        return (self.terminated
                or (self.dies_at is not None and self.clock.now >= self.dies_at))

    def poll(self):
        return self.rc if self._dead() else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self) -> int:
        assert self._dead(), "wait() on a live fake proc would hang"
        return self.rc


def check() -> None:
    T0 = 1_785_952_800.0  # decide time; pitch 5h later
    PITCH = T0 + 5 * 3600
    ROW = ("aec-mlb-nyy-bos-2026-08-06", PITCH, 385_000_00)

    # wait_until: lands exactly on target (capped final slice), never early
    c = Clock(T0)
    wait_until(T0 + 65, c.time, c.sleep)
    assert c.now == T0 + 65 and c.sleeps == [POLL_SEC, POLL_SEC, POLL_SEC, 5]
    # already past -> no sleep at all
    wait_until(T0, c.time, c.sleep)
    assert len(c.sleeps) == 3 + 1

    # normal arc: pick at decide, spawn at pitch - LEAD_SEC, SIGTERM at stop
    c = Clock(T0 - 100)
    logs: list[str] = []
    spawned: list[tuple[str, float]] = []
    proc = Proc(c)

    def spawn(slug):
        spawned.append((slug, c.now))
        return proc

    rc = session(T0, lambda: [ROW], spawn, logs.append, c.time, c.sleep)
    assert rc == 0
    assert spawned == [(ROW[0], PITCH - LEAD_SEC)]
    assert proc.terminated and c.now >= PITCH + STOP_AFTER_SEC
    # the SIGTERM fired within one poll of the stop time
    assert c.now < PITCH + STOP_AFTER_SEC + POLL_SEC
    assert any("exited cleanly" in m for m in logs), logs

    # early self-exit: no SIGTERM, watchdog stops at death not stop time
    c = Clock(T0)
    logs = []
    proc = Proc(c, dies_at=PITCH - 3600, rc=1)
    rc = session(T0, lambda: [ROW], lambda s: proc, logs.append,
                 c.time, c.sleep)
    assert rc == 0 and not proc.terminated
    assert c.now < PITCH, "watchdog kept waiting after the pilot died"
    assert any("on its own" in m and "rc 1" in m for m in logs), logs

    # games=2: top two picks in ONE spawn (comma allowlist); launch keys
    # off the EARLIEST pitch, the SIGTERM off the LATEST
    ROW2 = ("aec-mlb-sd-az-2026-08-06", PITCH + 2 * 3600, 300_000_00)
    c = Clock(T0 - 100)
    logs = []
    spawned = []
    proc = Proc(c)
    rc = session(T0, lambda: [ROW, ROW2, ("ignored", PITCH, 1)],
                 lambda s: spawned.append((s, c.now)) or proc,
                 logs.append, c.time, c.sleep, games=2)
    assert rc == 0
    assert spawned == [(f"{ROW[0]},{ROW2[0]}", PITCH - LEAD_SEC)], spawned
    assert proc.terminated
    assert c.now >= ROW2[1] + STOP_AFTER_SEC, "stop must key off the LAST pitch"
    assert c.now < ROW2[1] + STOP_AFTER_SEC + POLL_SEC

    # launch already inside the runway -> immediate spawn, no negative sleep
    c = Clock(PITCH - LEAD_SEC + 600)
    spawned = []
    proc = Proc(c)
    session(c.now, lambda: [ROW], lambda s: spawned.append(c.now) or proc,
            logs.append, c.time, c.sleep)
    assert spawned == [PITCH - LEAD_SEC + 600]

    # empty ranking and a raising pick both abort without spawning
    for pick in (lambda: [], lambda: (_ for _ in ()).throw(RuntimeError("api"))):
        c = Clock(T0)
        logs = []
        rc = session(T0, pick, lambda s: (_ for _ in ()).throw(
            AssertionError("spawned on abort")), logs.append, c.time, c.sleep)
        assert rc == 1
        assert any("ABORT" in m for m in logs), logs

    # parse_when: epoch, Z-suffixed ISO, and naive ISO all read as UTC
    assert parse_when("1785952800") == 1_785_952_800.0
    assert parse_when("2026-08-05T18:00:00Z") == 1_785_952_800.0
    assert parse_when("2026-08-05T18:00:00+00:00") == 1_785_952_800.0
    assert parse_when("2026-08-05T18:00:00") == 1_785_952_800.0
    print("ok")


if __name__ == "__main__":
    check()
