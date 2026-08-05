"""Archive closed recording days: gzip each day-folder's *.jsonl to a
destination (the NFS mount on the server) and delete the originals.

Why this exists: the recorder writes to fast local disk so an NFS hiccup
can never stall the tape (books/fv are unbackfillable), and the server's
internal disk is small — so closed slates move to bulk storage nightly.
`replay.py` reads .jsonl.gz transparently, so archived days replay by
pointing at the destination.

A day-folder is CLOSED when its date (MM-DD-YYYY folder name, the
recorder's convention) is before today UTC AND none of its files were
modified in the last GRACE_SEC — belt and braces so a slate that runs
past midnight UTC (west-coast games) is never archived mid-write.
Anything that isn't a day-named folder (e.g. `intl/`) is left alone.

Usage:  python3 archive_recordings.py <recordings_dir> <dest_dir>
Runs from cron/systemd-timer nightly; idempotent — a partially archived
day resumes (per-file: gzip to a temp name, rename, then unlink source).
"""
import gzip
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

GRACE_SEC = 6 * 3600.0   # a "closed" day must also be quiet this long


def day_key(name: str) -> str | None:
    """MM-DD-YYYY folder name -> sortable YYYY-MM-DD, else None."""
    try:
        return datetime.strptime(name, "%m-%d-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def closed_days(root: Path, now: float) -> list[Path]:
    """Day-folders safe to archive, oldest first."""
    today = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    out = []
    for d in root.iterdir():
        key = day_key(d.name) if d.is_dir() else None
        if key is None or key >= today:
            continue
        files = list(d.glob("*.jsonl"))
        if not files:
            continue
        if max(f.stat().st_mtime for f in files) > now - GRACE_SEC:
            continue
        out.append(d)
    return sorted(out, key=lambda d: day_key(d.name))


def archive_day(day: Path, dest_root: Path) -> int:
    """Gzip-move one day-folder's recordings; returns files moved.
    Crash-safe per file: write .gz to a temp name, rename into place,
    only then delete the source. Empties (but keeps) the source folder."""
    dest = dest_root / day.name
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src in sorted(day.glob("*.jsonl")):
        final = dest / (src.name + ".gz")
        tmp = dest / (src.name + ".gz.part")
        with open(src, "rb") as fi, gzip.open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        tmp.rename(final)
        src.unlink()
        moved += 1
    return moved


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-6], file=sys.stderr)
        return 2
    root, dest = Path(argv[0]), Path(argv[1])
    now = time.time()
    total = 0
    for day in closed_days(root, now):
        n = archive_day(day, dest)
        print(f"archived {day.name}: {n} file(s) -> {dest / day.name}")
        total += n
    if not total:
        print("nothing to archive")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
