"""Offline checks for dedupe_recordings: grouping by (day, slug),
keep-longest, gamePk cross-check, cross-day immunity, freshness gate,
gz handling, and that --apply quarantines without destroying data."""
import gzip
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dedupe_recordings as dr


def write_rec(path: Path, slug: str, n_books: int, pk: int | None = None,
              old: bool = True) -> Path:
    lines = [{"type": "meta", "slug": slug, "venue": "us"}]
    lines += [{"type": "book", "ts": float(i)} for i in range(n_books)]
    if pk is not None:
        lines.append({"type": "lineup", "side": "home", "game_pk": pk})
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(o) for o in lines) + "\n"
    if path.name.endswith(".gz"):
        with gzip.open(path, "wt") as fh:
            fh.write(text)
    else:
        path.write_text(text)
    if old:  # predate the freshness gate
        os.utime(path, (time.time() - 3600, time.time() - 3600))
    return path


def _check() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "recordings"
        day1, day2 = root / "08-04-2026", root / "08-05-2026"

        # dup chain: longest kept, both others dropped; gz counts too
        a1 = write_rec(day1 / "2026-08-04-2240_ath-cin.jsonl", "mlb-ath-cin", 50, pk=7)
        a2 = write_rec(day1 / "2026-08-04-2241_ath-cin.jsonl", "mlb-ath-cin", 90, pk=7)
        a3 = write_rec(day1 / "2026-08-04-2242_ath-cin.jsonl.gz", "mlb-ath-cin", 20)
        # singleton: untouched
        write_rec(day1 / "2026-08-04-1810_pit-mil.jsonl", "mlb-pit-mil", 30, pk=9)
        # same slug across two day folders: a makeup game, both kept
        write_rec(day1 / "2026-08-04-2300_stl-nyy.jsonl", "mlb-stl-nyy", 40, pk=3)
        write_rec(day2 / "2026-08-05-2300_stl-nyy.jsonl", "mlb-stl-nyy", 40, pk=3)
        # gamePk mismatch inside one day: slug collision, skipped
        write_rec(day1 / "2026-08-04-1100_nym-cle.jsonl", "mlb-nym-cle", 10, pk=1)
        write_rec(day1 / "2026-08-04-1200_nym-cle.jsonl", "mlb-nym-cle", 60, pk=2)
        # fresh file (recorder still writing): group skipped
        write_rec(day1 / "2026-08-04-1500_det-sea.jsonl", "mlb-det-sea", 10, pk=5)
        write_rec(day1 / "2026-08-04-1501_det-sea.jsonl", "mlb-det-sea", 60, pk=5,
                  old=False)
        # intl subfolder: never scanned
        write_rec(root / "intl" / "08-04-2026" / "x.jsonl", "mlb-ath-cin", 999)

        files = dr.expand([root])
        assert all("intl" not in str(f) for f in files)

        warnings: list[str] = []
        moves = dr.plan(files, log=warnings.append)
        losers = {loser for _, loser in moves}
        assert losers == {a1, a3}, losers          # a2 (90 ev) is the keeper
        assert all(k == a2 for k, _ in moves)
        assert any("spans 2 day folders" in w for w in warnings), warnings
        assert any("gamePks disagree" in w for w in warnings), warnings
        assert any("<30min" in w for w in warnings), warnings

        # quarantine path lands OUTSIDE the recordings tree, day preserved
        q = dr.quarantine_path(a1, None)
        assert q == root.with_name("recordings-dupes") / "08-04-2026" / a1.name

        # apply: losers moved, keeper + singletons intact, then idempotent
        n_before = len(dr.expand([root]))
        assert dr.main(["--apply", str(root)]) == 0
        assert a2.exists() and not a1.exists() and not a3.exists()
        assert q.exists()
        assert len(dr.expand([root])) == n_before - 2
        assert dr.plan(dr.expand([root]), log=lambda *_: None) == []

    print("check_dedupe_recordings OK")


if __name__ == "__main__":
    _check()
