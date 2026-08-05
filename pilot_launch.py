"""Pilot session launcher — the cross-platform replacement for the shell
launchers (pilot_run.sh / pilot2_run.sh, whose BSD-only `date -u -r`
broke on Linux and whose watchdog logic had no check script).

Timeline (session 2's shape, decide time now a flag):

  arm -> wait until --decide -> pilot_pick ranks the slate, the volume
  leader wins -> wait until first pitch - LEAD_SEC -> spawn
  mach_five.run() live on that one slug -> SIGTERM at first pitch +
  STOP_AFTER_SEC -> mach_five's finally-shutdown cancels every tracked
  order. If the launch time is already past at decision time (a pick
  inside the 3.5h runway), the pilot starts immediately.

Arm it detached before a slate; all output lands in pilot.log:

  nohup .venv/bin/python3 pilot_launch.py --decide 2026-08-06T18:00Z \
      >> pilot.log 2>&1 &

The whole arc lives in session() with injected clock/sleep/spawn/log so
tests/check_pilot_launch.py drives it offline.
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

LEAD_SEC = 3.5 * 3600     # quote runway before first pitch
STOP_AFTER_SEC = 15 * 60  # SIGTERM this long after first pitch
POLL_SEC = 20             # watchdog / wait cadence

LOG_PATH = "pilot.log"


def _hms(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M:%S")


def parse_when(text: str) -> float:
    """Epoch seconds from a raw epoch or an ISO-8601 stamp ('Z' accepted;
    a naive stamp is taken as UTC, never local time)."""
    try:
        return float(text)
    except ValueError:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


def wait_until(epoch: float, clock, sleep, poll: float = POLL_SEC) -> None:
    """Sleep in short slices until clock() reaches epoch; returns
    immediately if the moment is already past. Slices are capped at the
    remaining time so the wake-up never overshoots the target."""
    while True:
        remaining = epoch - clock()
        if remaining <= 0:
            return
        sleep(min(poll, remaining))


def file_logger(path: str = LOG_PATH):
    def log(msg: str) -> None:
        with open(path, "a") as f:
            f.write(f"[launcher {_hms(time.time())}] {msg}\n")
    return log


def spawn_pilot(slug: str, log_path: str = LOG_PATH):
    """mach_five.run() live on one slug, as a SIGTERM-able child whose
    output appends to the log. Runs the same interpreter from the repo
    directory so imports and .env resolve regardless of launch cwd."""
    logf = open(log_path, "ab")
    env = dict(os.environ, MACH_FIVE_LIVE="1", MACH_FIVE_SLUGS=slug)
    return subprocess.Popen(
        [sys.executable, "-c", "import mach_five; mach_five.run()"],
        env=env, stdout=logf, stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.abspath(__file__)))


def session(decide: float, pick, spawn, log, clock, sleep) -> int:
    """The full launcher arc. `pick()` -> [(slug, pitch_epoch, cents)]
    best-first (pilot_pick.rank's shape); `spawn(slug)` -> a Popen-like
    object (pid/poll/terminate/wait). Returns the process exit code."""
    log(f"armed: deciding at {_hms(decide)}Z")
    wait_until(decide, clock, sleep)

    try:
        rows = pick()
    except Exception as e:
        log(f"ABORT: pick failed: {e!r}")
        return 1
    for slug, pitch, cents in rows:
        log(f"  {slug:40s} pitch {_hms(pitch)}Z   traded ${cents / 100:,.0f}")
    if not rows:
        log("ABORT: no eligible game — no session today")
        return 1
    slug, pitch, _ = rows[0]
    launch, stop = pitch - LEAD_SEC, pitch + STOP_AFTER_SEC
    log(f"picked {slug} (pitch {_hms(pitch)}Z); "
        f"launch {_hms(launch)}Z, stop {_hms(stop)}Z")

    wait_until(launch, clock, sleep)
    log("starting mach_five.run()")
    proc = spawn(slug)
    log(f"pilot pid {proc.pid}")

    while clock() < stop and proc.poll() is None:
        sleep(POLL_SEC)
    if proc.poll() is None:
        log("sending SIGTERM")
        proc.terminate()
        proc.wait()
        log("pilot exited cleanly")
    else:
        rc = proc.wait()
        log(f"pilot exited on its own before the stop time (rc {rc}) — "
            "check the log above")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Arm a one-shot live pilot session (pick -> launch -> "
                    "SIGTERM watchdog).")
    ap.add_argument("--decide", default=None,
                    help="when to run the pick: epoch or ISO-8601, naive "
                         "stamps read as UTC (default: immediately)")
    args = ap.parse_args(argv)
    decide = parse_when(args.decide) if args.decide else time.time()

    import pilot_pick
    return session(decide, lambda: pilot_pick.ranked_rows(time.time()),
                   spawn_pilot, file_logger(), time.time, time.sleep)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
