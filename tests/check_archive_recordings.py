"""Offline self-check for archive_recordings.py — tmp dirs, no real data."""
import gzip
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archive_recordings import GRACE_SEC, archive_day, closed_days, day_key


def check() -> None:
    assert day_key("08-04-2026") == "2026-08-04"
    assert day_key("intl") is None
    assert day_key("not-a-date") is None

    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        root, dest = Path(td) / "recordings", Path(td) / "nfs"
        old = root / "08-01-2026"      # closed: old date, quiet files
        recent = root / "08-04-2026"   # old date but written recently
        today_utc = time.strftime("%m-%d-%Y", time.gmtime(now))
        live = root / today_utc        # today: never archived
        intl = root / "intl"           # not a day folder: left alone
        for d in (old, recent, live, intl):
            d.mkdir(parents=True)
        line = json.dumps({"type": "meta", "slug": "x"}) + "\n"
        (old / "a.jsonl").write_text(line * 3)
        (old / "b.jsonl").write_text(line)
        (recent / "c.jsonl").write_text(line)
        (live / "d.jsonl").write_text(line)
        (intl / "e.jsonl").write_text(line)
        quiet = now - GRACE_SEC - 60
        import os
        for f in (old / "a.jsonl", old / "b.jsonl", live / "d.jsonl",
                  intl / "e.jsonl"):
            os.utime(f, (quiet, quiet))   # recent/c.jsonl stays fresh

        assert [d.name for d in closed_days(root, now)] == ["08-01-2026"]

        # archive: gzip to dest, source removed, content survives round-trip
        n = archive_day(old, dest)
        assert n == 2
        assert sorted(p.name for p in (dest / "08-01-2026").iterdir()) == \
            ["a.jsonl.gz", "b.jsonl.gz"]
        assert list(old.glob("*.jsonl")) == []
        with gzip.open(dest / "08-01-2026" / "a.jsonl.gz", "rt") as fh:
            assert fh.read() == line * 3
        # idempotent: an emptied day no longer qualifies
        assert closed_days(root, now) == []

        # replay reads the archived file transparently
        game = dest / "08-01-2026" / "g.jsonl.gz"
        rec = [{"type": "meta", "slug": "g", "commence_ts": 100.0},
               {"type": "pinnacle", "ts": 50.0, "fv": 0.5}]
        with gzip.open(game, "wt") as fh:
            fh.write("\n".join(json.dumps(r) for r in rec))
        import replay
        meta, events = replay.load(str(game))
        assert meta["slug"] == "g" and events[0]["fv"] == 0.5
        # expand picks up .gz alongside .jsonl (still skipping intl/)
        got = replay.expand([str(dest)])
        assert str(game) in got and len(got) == 3, got
    print("ok")


if __name__ == "__main__":
    check()
