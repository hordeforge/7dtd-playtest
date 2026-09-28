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
`--move` deletes the quarantined copy after a successful copy-back. A second
run of the same command is a no-op over what it already put back: a pair
whose original is in place and whose quarantined copy is gone is reported as
already restored, not as a blocked one, so re-running after an interrupted
restore never reports a file that is in place as missing.

Everything here works off the recorded absolute paths, so a restore on a
different machine writes where the run said it wrote. That is the point of
the manifest: no operator has to remember which `--world` produced an entry.

Exit codes:
  0  the requested listing or plan is complete, or the restore left every file
     in place (written now or already there)
  1  the entry recorded no files, or a restore left one blocked (a path
     already occupied without --force, or a copy that failed, or neither
     copy of the file can be found)
  2  bad usage, or the named quarantine root or entry does not exist
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


class ManifestUnreadableError(OSError):
    """An entry's manifest exists but cannot be read.

    Distinct from "the entry has no manifest": the first is a fault on this
    host, the second is a fact about the entry. A caller that treats the two
    alike (a prune that deletes on an empty read) destroys the only copy of
    whatever the entry held while reporting nothing.
    """


def manifest_path(entry: Path) -> Path:
    return entry / MANIFEST_NAME


def fsync_file(path: Path) -> None:
    """Flush a file's bytes to disk, so a caller may report it preserved.

    `copy2` returns once the kernel has the data, not once the device does.
    A power loss between the copy and the caller's next destructive step
    (truncating the source log) leaves the source gone and the copy empty.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    """Flush a directory's entries, so a rename into it survives power loss.

    A file's own fsync covers its bytes; the name it is reachable under is
    held by the parent directory, and a move the parent never acknowledged
    can be lost while the manifest that names it survives. Filesystems that
    refuse O_RDONLY on a directory (some network mounts) are not a data
    loss, so the failure is swallowed.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def record(entry: Path, src: Path, dest: Path) -> None:
    """Append one `{src, dest}` pair to the entry manifest, durably.

    The line and the entry's directory entry are fsynced before returning: a
    file already moved aside with no manifest line is unrecoverable evidence,
    because the operator cannot tell which world or log it came from. Callers
    record *before* the move or copy, since a recorded pair whose destination
    is absent costs a "nothing to restore" line while an unrecorded move
    costs the only copy of a world.
    """
    line = json.dumps({_SRC: str(src), _DEST: str(dest)}) + "\n"
    path = manifest_path(entry)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    fsync_dir(entry)


def _read_manifest(entry: Path) -> tuple[list[tuple[Path, Path]], int]:
    """Recorded (original, quarantined) pairs oldest first, plus lines dropped.

    A line that is not a JSON object with two string paths is skipped rather
    than guessed at: restoring to a path this file did not record is a
    write the operator did not ask for. The caller reports the skip count.

    A manifest that exists but will not read raises
    :class:`ManifestUnreadableError` rather than returning no pairs: the
    difference between "nothing was recorded" and "the record is
    unreachable" decides whether the caller may delete the entry.
    """
    path = manifest_path(entry)
    pairs: list[tuple[Path, Path]] = []
    if not path.is_file():
        return pairs, 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as ex:
        raise ManifestUnreadableError(
            f"cannot read quarantine manifest {path}: {ex}"
        ) from ex
    dropped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            dropped += 1
            continue
        src, dest = (row.get(_SRC), row.get(_DEST)) if isinstance(row, dict) else (None, None)
        if isinstance(src, str) and isinstance(dest, str) and src and dest:
            pairs.append((Path(src), Path(dest)))
        else:
            dropped += 1
    return pairs, dropped


def read_manifest(entry: Path) -> list[tuple[Path, Path]]:
    """Recorded (original, quarantined) pairs, oldest first."""
    return _read_manifest(entry)[0]


def skipped_manifest_lines(entry: Path) -> int:
    """Recorded lines the reader had to drop, so `show` can say so."""
    return _read_manifest(entry)[1]


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
    except OSError as ex:
        # Name the cause: without it the caller reports "no entry matches"
        # and the operator goes looking for a typo in the entry name.
        print(f"ERROR: cannot list {qroot}: {ex}", file=sys.stderr)
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
        lines.append(
            f"  (no {MANIFEST_NAME} under {entry}: this entry recorded no moves)"
        )
    if dropped:
        lines.append(f"  ({dropped} unreadable manifest line(s) skipped)")
    return lines


# Read size for the two-file comparison, so a world file of a different size
# is not read end to end to learn that it differs.
COMPARE_CHUNK = 1 << 20


def same_content(a: Path, b: Path) -> bool:
    """True when two regular files hold the same bytes.

    Used only to tell a restore that already happened from one whose
    original path is occupied by a different file (this run's log, say), so
    it must not raise on anything it cannot read: an unreadable file is not a
    match.
    """
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ca = fa.read(COMPARE_CHUNK)
                cb = fb.read(COMPARE_CHUNK)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def restore(entry: Path, apply: bool, force: bool, move: bool) -> int:
    pairs, dropped = _read_manifest(entry)
    print("\n".join(_describe(pairs, entry, dropped)))
    if not pairs:
        # 1, like `show` on the same entry: nothing about the invocation was
        # wrong, the entry records nothing to restore. 2 would read as a
        # malformed command line, which it is not.
        return 1
    if not apply:
        print("dry run: nothing written; pass --apply to restore")
        return 0

    restored = 0
    already = 0
    blocked = 0
    for src, dest in pairs:
        if not dest.is_file():
            if src.exists():
                # A copy the run had already put back, and (with --move) then
                # deleted the quarantined one. Re-running the same command is
                # the normal thing to do after an interrupted restore, and it
                # must not read as a failure over a file that is in place.
                print(f"SKIP {src}: already restored")
                already += 1
                continue
            print(f"SKIP {src}: {dest} is gone", file=sys.stderr)
            blocked += 1
            continue
        if src.exists() and not force:
            if same_content(src, dest):
                # The original already holds what the quarantine copy holds,
                # so the copy-back this invocation was asked for has happened
                # (a rerun, or a run whose earlier attempt was interrupted).
                # Reporting it as blocked would make the recovery path for an
                # interrupted restore exit non-zero over nothing.
                print(f"SKIP {src}: already restored")
                already += 1
                continue
            print(
                f"SKIP {src}: already exists (pass --force to overwrite)",
                file=sys.stderr,
            )
            blocked += 1
            continue
        tmp = src.with_name(f".{src.name}.restore.{os.getpid()}")
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, tmp)
            fsync_file(tmp)
            os.replace(tmp, src)
            fsync_dir(src.parent)
        except OSError as ex:
            # A half-written copy2 or a refused replace leaves the temp file
            # sitting in the world directory, where the next sweep can mistake
            # it for world state. Remove it, and say so when that also fails.
            leftover = ""
            with contextlib.suppress(OSError):
                tmp.unlink()
            if tmp.exists():
                leftover = f"; remove the leftover copy at {tmp} by hand"
            print(f"ERROR: could not restore {src}: {ex}{leftover}", file=sys.stderr)
            blocked += 1
            continue
        restored += 1
        if move:
            with contextlib.suppress(OSError):
                dest.unlink()
                fsync_dir(dest.parent)
    print(f"restored={restored} already={already} blocked={blocked}")
    return 0 if (restored or already) and not blocked else 1


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
        unreadable = 0
        for qentry in found:
            try:
                pairs = read_manifest(qentry)
            except ManifestUnreadableError as ex:
                print(f"  {qentry.name}  files=?  {ex}", file=sys.stderr)
                unreadable += 1
                continue
            print(f"{qentry.name}  files={len(pairs)}  manifest={manifest_path(qentry).name}")
        # An entry whose manifest cannot be read is not an empty entry; exit 0
        # here would read as "this entry recorded nothing to restore".
        return 2 if unreadable else 0

    if not args.entry:
        print("ERROR: this action needs an entry name or timestamp prefix",
              file=sys.stderr)
        return 2
    entry = resolve_entry(qroot, args.entry)
    if entry is None:
        print(f"ERROR: no quarantine entry matches {args.entry!r} under {qroot}",
              file=sys.stderr)
        return 2

    try:
        if args.action == "show":
            pairs, dropped = _read_manifest(entry)
        else:
            return restore(entry, args.apply, args.force, args.move)
    except ManifestUnreadableError as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        return 2
    print("\n".join(_describe(pairs, entry, dropped)))
    return 0 if pairs else 1


if __name__ == "__main__":
    sys.exit(main())
