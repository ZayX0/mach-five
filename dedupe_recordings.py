"""Dedupe duplicate recordings of the same game (INPLAY_FINDINGS item 3).

The recorder opens a SECOND file for a game when the odds feed re-lists
it with a shifted commence time (the filename carries the commence
stamp, so a 2-minute shift = a new path; rain delays produce chains of
four). Every replay.py table over such a day double-weights that game.
The probe over 08-01..08-06 found 20 surplus files across 13 games.

Identity is the market SLUG from the meta line: one US market per game,
so slug == game. gamePk (on lineup events) is used as a cross-check —
a slug group whose lineup events name two different gamePks is left
alone with a warning, since that would mean a slug collision. Groups
are formed WITHIN one day folder only; the same slug in two day folders
is a genuine postponement (a makeup game keeps the original date in its
slug) and both files are kept.

Per group we KEEP the file with the most parsed event lines and MOVE
the rest to a quarantine folder outside the recordings tree (default
`<recordings-root>-dupes/<day>/`), so replay's rglob never sees them
but nothing is destroyed.
ponytail: keeping only the longest file drops the early book/fv
coverage the superseded files hold (a 4-file rain-delay chain loses its
first hours); the upgrade is merging chains with
merge_0805_dupes.merge_lines instead of quarantining.

Safety: dry-run by default (--apply to move); a group with any file
written in the last 30 min is skipped (recorder still on the day).

Usage: dedupe_recordings.py [--apply] [--quarantine DIR] DIR...
  DIR is a recordings root or a single day folder; `intl` subfolders
  are skipped like replay.py does.
"""
import gzip
import json
import shutil
import sys
import time
from pathlib import Path

QUIET_SEC = 30 * 60


def scan(path: Path) -> tuple[str | None, int, int | None]:
    """(slug, parsed-event count, gamePk) for one recording file."""
    op = gzip.open if path.name.endswith(".gz") else open
    slug, n, pk = None, 0, None
    with op(path, "rt") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            n += 1
            t = obj.get("type")
            if t == "meta":
                slug = slug or obj.get("slug")
            elif t == "lineup" and obj.get("game_pk"):
                pk = int(obj["game_pk"])
    return slug, n, pk


def expand(dirs: list[Path]) -> list[Path]:
    """Every recording file under the given roots, `intl` excluded."""
    out: list[Path] = []
    for d in dirs:
        out += sorted(f for pat in ("*.jsonl", "*.jsonl.gz")
                      for f in d.rglob(pat)
                      if "intl" not in f.relative_to(d).parts)
    return out


def plan(files: list[Path], *, now: float | None = None,
         log=print) -> list[tuple[Path, Path]]:
    """[(keeper, loser), ...] — every loser paired with its group's
    keeper. Groups are (day folder, slug); skips fresh and
    pk-inconsistent groups with a logged warning."""
    now = time.time() if now is None else now
    groups: dict[tuple[Path, str], list[tuple[Path, int, int | None]]] = {}
    days_by_slug: dict[str, set[Path]] = {}
    for f in files:
        slug, n, pk = scan(f)
        if slug is None:
            log(f"WARN no meta, skipped: {f}")
            continue
        groups.setdefault((f.parent, slug), []).append((f, n, pk))
        days_by_slug.setdefault(slug, set()).add(f.parent)
    for slug, days in days_by_slug.items():
        if len(days) > 1:
            log(f"WARN {slug} spans {len(days)} day folders — kept in "
                f"all (makeup games keep the original date in the slug)")
    moves: list[tuple[Path, Path]] = []
    for (day, slug), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        pks = {pk for _, _, pk in members if pk is not None}
        if len(pks) > 1:
            log(f"WARN {slug}: gamePks disagree {sorted(pks)} — skipped")
            continue
        if any(now - f.stat().st_mtime < QUIET_SEC for f, _, _ in members):
            log(f"WARN {slug}: written <30min ago (recorder still on the "
                f"day) — skipped")
            continue
        keep, n_keep, _ = max(members,
                              key=lambda m: (m[1], m[0].stat().st_size, m[0].name))
        for f, n, _ in members:
            if f is not keep:
                log(f"{slug}: drop {f.name} ({n} ev) — keeping "
                    f"{keep.name} ({n_keep} ev)")
                moves.append((keep, f))
    return moves


def quarantine_path(loser: Path, override: Path | None) -> Path:
    """recordings/<day>/x.jsonl -> <recordings>-dupes/<day>/x.jsonl"""
    base = override or loser.parent.parent.with_name(
        loser.parent.parent.name + "-dupes")
    return base / loser.parent.name / loser.name


def main(argv: list[str]) -> int:
    apply, override, dirs = False, None, []
    it = iter(argv)
    for a in it:
        if a == "--apply":
            apply = True
        elif a == "--quarantine":
            override = Path(next(it))
        else:
            dirs.append(Path(a))
    if not dirs or not all(d.is_dir() for d in dirs):
        print(__doc__, file=sys.stderr)
        return 1
    moves = plan(expand(dirs))
    for _, loser in moves:
        dest = quarantine_path(loser, override)
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(loser), str(dest))
            print(f"moved {loser} -> {dest}")
        else:
            print(f"would move {loser} -> {dest}")
    verb = "moved" if apply else "would move"
    print(f"done: {verb} {len(moves)} duplicate file(s)"
          + ("" if apply else "   (dry run — pass --apply)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
