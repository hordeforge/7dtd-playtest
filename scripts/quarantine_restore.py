#!/usr/bin/env python3
"""Put back what a `--fresh-save` run moved into the quarantine.

A managed run never hard-deletes: the zdtd world state (`players.zsv`,
`containers.zct`, `blockmeta.zbm`), its chunk overlays and the previous
client log move under `<logdir>/quarantine/<UTC-stamp>-<kind>/`, and the
newest `playtest_run.QUARANTINE_KEEP` entries are kept. That is the only copy
of a world a mispointed `--world` or a wrong log path swept aside, and it
lives on one local disk with no off-host copy, so the entry records where
each file came from: every move appends `{src, dest}` to `restore.jsonl` in
its entry and this tool reads it back.

  quarantine_restore.py list
  quarantine_restore.py show <entry>
  quarantine_restore.py restore <entry> [--apply] [--force] [--move]

`restore` prints the plan and writes nothing until `--apply`. An existing
file at the original path is kept unless `--force` overwrites it, and
`--move` deletes the quarantined copy after a successful copy-back.

Everything here works off the recorded absolute paths, so a restore on a
different machine writes where the run said it wrote. That is the point of
the manifest: no operator has to remember which `--world` produced an entry.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
from pathlib import Path

# Recorded per move, one JSON object per line. Appended and fsynced by
# :func:`record` before the caller reports success, so a crash cannot leave a
# moved file with no record of where it went.
MANIFEST_NAME = "restore.jsonl"


def default_quarantine_root() -> Path:
    """`<orchestrator logdir>/quarantine`, resolved by the orchestrator itself.

    Imported inside the call so the module stays importable from
    playtest_run, which records through it.
    """
    import playtest_run

    return playtest_run.default_logdir() / playtest_run.QUARANTINE_DIRNAME

_SRC = "src"
_DEST = "dest"


def manifest_path(entry: Path) -> Path:
    return entry / MANIFEST_NAME


def record(entry: Path, src: Path, dest: Path) -> None:
    """Append one `{src, dest}` pair to the entry manifest, durably.

    The line is fsynced before returning: a file already moved aside with no
    manifest line is unrecoverable evidence, because the operator cannot tell
    which world or log it came from.
    """
    line = json.dumps({_SRC: str(src), _DEST: str(dest)}) + "\n"
    path = manifest_path(entry)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def read_manifest(entry: Path) -> list[tuple[Path, Path]]:
    """Recorded (original, quarantined) pairs, oldest first.

    A line that is not a JSON object with two string paths is skipped rather
    than guessed at: restoring to a path this file did not record is a
    write the operator did not ask for. The caller reports the skip count.
    """
    path = manifest_path(entry)
    if not path.is_file():
        return []
    pairs: list[tuple[Path, Path]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as ex:
        print(f"WARNING: cannot read {path}: {ex}", file=sys.stderr)
        return pairs
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        src, dest = row.get(_SRC), row.get(_DEST)
        if isinstance(src, str) and isinstance(dest, str) and src and dest:
            pairs.append((Path(src), Path(dest)))
    return pairs


def skipped_manifest_lines(entry: Path) -> int:
    """Recorded lines the reader had to drop, so `show` can say so."""
    path = manifest_path(entry)
    if not path.is_file():
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    dropped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            dropped += 1
            continue
        if not isinstance(row, dict):
            dropped += 1
            continue
        src, dest = row.get(_SRC), row.get(_DEST)
        if not (isinstance(src, str) and isinstance(dest, str) and src and dest):
            dropped += 1
    return dropped


def resolve_entry(qroot: Path, name: str) -> Path | None:
    """The entry directory a name refers to, or None.

    An exact directory name wins; otherwise the newest entry whose name
    starts with `name`, so an operator can pass the timestamp prefix. A
    `..` segment never resolves: the entry must sit directly in the
    quarantine root this tool was pointed at.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    direct = qroot / name
    if direct.is_dir():
        return direct
    try:
        matches = sorted(p for p in qroot.glob(f"{name}*") if p.is_dir())
    except OSError:
        return None
    return matches[-1] if matches else None


def entries(qroot: Path) -> list[Path]:
    """Quarantine entries, oldest first (their names sort by UTC stamp)."""
    try:
        return sorted(p for p in qroot.iterdir() if p.is_dir())
    except OSError as ex:
        print(f"ERROR: cannot list {qroot}: {ex}", file=sys.stderr)
        return []


def _describe(pairs: list[tuple[Path, Path]], entry: Path, dropped: int) -> list[str]:
    lines = [f"entry {entry}"]
    for src, dest in pairs:
        state = "present" if dest.is_file() else "MISSING"
        lines.append(f"  {src}  <-  {dest}  [{state}]")
    if not pairs:
        lines.append(f"  (no {MANIFEST_NAME}: nothing to restore from this entry)")
    if dropped:
        lines.append(f"  ({dropped} unreadable manifest line(s) skipped)")
    return lines


def restore(entry: Path, apply: bool, force: bool, move: bool) -> int:
    pairs = read_manifest(entry)
    dropped = skipped_manifest_lines(entry)
    print("\n".join(_describe(pairs, entry, dropped)))
    if not pairs:
        return 2
    if not apply:
        print("dry run: nothing written; pass --apply to restore")
        return 0

    restored = 0
    blocked = 0
    for src, dest in pairs:
        if not dest.is_file():
            print(f"SKIP {src}: {dest} is gone", file=sys.stderr)
            blocked += 1
            continue
        if src.exists() and not force:
            print(
                f"SKIP {src}: already exists (pass --force to overwrite)",
                file=sys.stderr,
            )
            blocked += 1
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            tmp = src.with_name(f".{src.name}.restore.{os.getpid()}")
            shutil.copy2(dest, tmp)
            os.replace(tmp, src)
        except OSError as ex:
            print(f"ERROR: could not restore {src}: {ex}", file=sys.stderr)
            blocked += 1
            continue
        restored += 1
        if move:
            with contextlib.suppress(OSError):
                dest.unlink()
    print(f"restored={restored} blocked={blocked}")
    return 0 if restored and not blocked else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "action",
        choices=("list", "show", "restore"),
        help="list entries, show one entry's restore plan, or restore it",
    )
    ap.add_argument("entry", nargs="?", help="entry name or timestamp prefix")
    ap.add_argument(
        "--quarantine",
        type=Path,
        default=None,
        help="quarantine root (default: the orchestrator logdir/quarantine)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="restore writes files; without it the plan is printed only",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="overwrite a file that already sits at the original path",
    )
    ap.add_argument(
        "--move",
        action="store_true",
        help="delete the quarantined copy after a successful restore",
    )
    args = ap.parse_args(argv)

    qroot = args.quarantine
    if qroot is None:
        qroot = default_quarantine_root()
    if not qroot.is_dir():
        print(f"ERROR: no quarantine at {qroot}", file=sys.stderr)
        return 2

    if args.action == "list":
        found = entries(qroot)
        if not found:
            print(f"no quarantine entries under {qroot}")
            return 0
        for qentry in found:
            pairs = read_manifest(qentry)
            print(f"{qentry.name}  files={len(pairs)}  manifest={manifest_path(qentry).name}")
        return 0

    if not args.entry:
        print("ERROR: this action needs an entry name or timestamp prefix",
              file=sys.stderr)
        return 2
    entry = resolve_entry(qroot, args.entry)
    if entry is None:
        print(f"ERROR: no quarantine entry matches {args.entry!r} under {qroot}",
              file=sys.stderr)
        return 2

    if args.action == "show":
        pairs = read_manifest(entry)
        print("\n".join(_describe(pairs, entry, skipped_manifest_lines(entry))))
        return 0 if pairs else 1
    return restore(entry, args.apply, args.force, args.move)


if __name__ == "__main__":
    sys.exit(main())
