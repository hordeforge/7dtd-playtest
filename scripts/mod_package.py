#!/usr/bin/env python3
"""Build the release zip from the compiled mod, byte-identical per source tree.

`make build` leaves the shippable folder in `dist/7dtd-playtest/`; this wraps
it in the archive a release attaches. The entry list is the same two files
`make install` copies, named here rather than globbed, so whatever else a build
drops in that folder (a .deps.json, a stray pdb, a previous version's output)
cannot ride along with a release.

Everything a zip container would otherwise take from the build host is pinned
here: entry order is sorted, every entry carries the same timestamp (from
SOURCE_DATE_EPOCH, or the zip epoch when the variable is unset, never the
clock), the mode is 0644 for all of them, and create_system is fixed to Unix so
an archive built on Windows does not record a different host. Two builds of the
same commit produce the same file, which is what lets a rebuild be compared
against a release instead of trusted.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path

MOD_NAME = "7dtd-playtest"
MOD_FILES = ("ModInfo.xml", f"{MOD_NAME}.dll")
VERSION_RE = re.compile(r'<Version\s+value="([^"]+)"')

# 1980-01-01T00:00:00Z, the earliest timestamp a zip entry can record. It is
# the fallback so a build that does not set SOURCE_DATE_EPOCH still produces
# the same bytes on every day; a release that wants the real build date sets
# the variable to the commit's timestamp.
DEFAULT_EPOCH = 315532800
ZIP_EPOCH_YEAR = 1980

# zip stores the mode in the high half of external_attr and the MS-DOS
# attribute byte in the low half; a fixed pair keeps the host's umask and its
# filesystem out of the archive.
FILE_MODE = 0o644
CREATE_SYSTEM_UNIX = 3

# zlib's own level, pinned rather than left to the interpreter default.
COMPRESS_LEVEL = 6


class PackageError(Exception):
    """A dist tree that cannot be packaged, named rather than asserted."""


def shipped_version(mod_info: Path) -> str:
    """The version the archive is named for, read from the manifest it ships."""
    text = mod_info.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if match is None:
        raise PackageError(f"{mod_info} has no <Version value=\"...\"> to name the archive")
    return match.group(1)


def entry_datetime(epoch: int) -> tuple[int, int, int, int, int, int]:
    if epoch < 0:
        raise PackageError(f"SOURCE_DATE_EPOCH must not be negative, got {epoch}")
    # Zip records local time with no zone, so a build on a machine set to
    # another zone would record a different wall clock. UTC is the only
    # reading that means the same thing everywhere.
    parts = time.gmtime(epoch)
    if parts.tm_year < ZIP_EPOCH_YEAR:
        raise PackageError(
            f"epoch {epoch} is before {ZIP_EPOCH_YEAR}, which a zip entry cannot record"
        )
    return (parts.tm_year, parts.tm_mon, parts.tm_mday, parts.tm_hour, parts.tm_min, parts.tm_sec)


def resolve_epoch(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_EPOCH
    try:
        return int(raw)
    except ValueError:
        raise PackageError(f"SOURCE_DATE_EPOCH is not an integer: {raw!r}") from None


def package(dist: Path, out: Path, epoch: int) -> Path:
    """Write the archive and return its path. Refuses a dist that cannot ship."""
    sources = [dist / name for name in MOD_FILES]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise PackageError(
            "dist is not a complete build: " + ", ".join(missing) + " (run `make build`)"
        )
    version = shipped_version(dist / MOD_FILES[0])
    target = out if out.suffix == ".zip" else out / f"{MOD_NAME}-{version}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = entry_datetime(epoch)

    # Publish by rename: a reader never sees a half-written archive, and a run
    # killed here leaves the previous release's file intact.
    handle, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".mod-package-", suffix=".zip")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as raw_file, zipfile.ZipFile(
            raw_file, "w", zipfile.ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL
        ) as archive:
            for source in sorted(sources, key=lambda path: path.name):
                info = zipfile.ZipInfo(f"{MOD_NAME}/{source.name}", date_time=stamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = CREATE_SYSTEM_UNIX
                info.external_attr = FILE_MODE << 16
                archive.writestr(info, source.read_bytes())
        os.chmod(tmp, stat.S_IMODE(FILE_MODE))
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "examples:\n"
            "  mod_package.py --dist dist/7dtd-playtest\n"
            "  SOURCE_DATE_EPOCH=1780000000 mod_package.py --dist dist/7dtd-playtest\n"
            "  mod_package.py --dist dist/7dtd-playtest --out dist/release\n"
            "exit codes: 0 archive written, 1 the dist tree cannot be packaged\n"
            "(a missing shipped file, a manifest with no version, an epoch a zip\n"
            "cannot record), 2 bad usage"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dist", required=True, type=Path, help="compiled mod directory")
    parser.add_argument(
        "--out",
        type=Path,
        help="archive path, or a directory to name from the manifest (default: the dist dir)",
    )
    parser.add_argument(
        "--epoch",
        type=int,
        help="recorded timestamp; overrides SOURCE_DATE_EPOCH (default: the zip epoch)",
    )
    args = parser.parse_args(argv)

    try:
        epoch = args.epoch
        if epoch is None:
            epoch = resolve_epoch(os.environ.get("SOURCE_DATE_EPOCH"))
        target = package(args.dist, args.out or args.dist, epoch)
    except PackageError as exc:
        print(f"mod_package: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"mod_package: {exc}", file=sys.stderr)
        return 1
    print(f"mod_package: {target} ({', '.join(MOD_FILES)}, epoch={epoch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
