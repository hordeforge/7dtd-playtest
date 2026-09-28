#!/usr/bin/env python3
"""The release archive is a function of the built mod, not of the build host.

`make package` is what a maintainer attaches to a tagged release, so the file
that ships has to be the one a rebuild of the same source produces. The gate
runs the real CLI over a stand-in dist tree and reads the bytes back out of
the zip, because every claim here is about what lands in the container: entry
set, entry order, recorded timestamp, mode, and the create_system byte that
says which platform packed it. A source tree's mtime, the local timezone and
the local locale are varied between the two runs, since those are exactly the
three things a zip records by default.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "mod_package.py"
MOD_FILES = ("ModInfo.xml", "7dtd-playtest.dll")
# Recorded when SOURCE_DATE_EPOCH is unset: the earliest a zip entry can hold.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
MANIFEST = (
    '<?xml version="1.0" encoding="UTF-8" ?>\n'
    '<xml>\n'
    '  <Name value="7dtd-playtest" />\n'
    '  <Version value="9.9.9" />\n'
    "</xml>\n"
)


def make_dist(root: Path) -> Path:
    """A stand-in build output: the two shipped files and nothing else."""
    dist = root / "dist" / "7dtd-playtest"
    dist.mkdir(parents=True)
    (dist / "ModInfo.xml").write_text(MANIFEST, encoding="utf-8")
    (dist / "7dtd-playtest.dll").write_bytes(bytes(range(256)) * 8)
    return dist


def run(
    dist: Path, out: Path, *, epoch: str | None = None, tz: str = "UTC"
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "TZ": tz, "HOME": "/nonexistent"}
    if epoch is not None:
        env["SOURCE_DATE_EPOCH"] = epoch
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--dist", str(dist), "--out", str(out)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_archive_carries_exactly_the_installed_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dist = make_dist(root)
        # Anything else a build drops in the folder must not ride along.
        (dist / "7dtd-playtest.deps.json").write_text("{}", encoding="utf-8")
        out = root / "release.zip"
        run(dist, out)
        with zipfile.ZipFile(out) as archive:
            names = archive.namelist()
            assert names == [f"7dtd-playtest/{name}" for name in sorted(MOD_FILES)], names
            for name in MOD_FILES:
                assert archive.read(f"7dtd-playtest/{name}") == (dist / name).read_bytes(), (
                    f"{name} does not round-trip out of the archive"
                )
    print("OK the archive holds the two installed files and nothing else")


def test_two_runs_agree() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dist = make_dist(root)
        first = root / "first.zip"
        run(dist, first, tz="Asia/Tokyo")
        # A rebuild writes fresh files: newer mtimes, another timezone.
        for name in MOD_FILES:
            path = dist / name
            path.touch()
        second = root / "second.zip"
        run(dist, second, tz="UTC")
        assert digest(first) == digest(second), (
            "two builds of one source produced different archives: "
            f"{digest(first)} vs {digest(second)}"
        )
    print("OK two builds of one source produce the same bytes")


def test_entry_metadata_is_pinned() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dist = make_dist(root)
        out = root / "release.zip"
        run(dist, out)
        with zipfile.ZipFile(out) as archive:
            infos = archive.infolist()
        names = [info.filename for info in infos]
        assert names == sorted(names), f"entries are not in a fixed order: {names}"
        for info in infos:
            assert info.date_time == ZIP_EPOCH, (
                f"{info.filename} records the wall clock: {info.date_time}"
            )
            assert info.external_attr >> 16 == 0o644, (
                f"{info.filename} records mode {oct(info.external_attr >> 16)}"
            )
            assert info.create_system == 3, (
                f"{info.filename} records create_system {info.create_system}, which "
                "names the packing host"
            )
    print("OK every entry carries a pinned timestamp, mode and host byte")


def test_source_date_epoch_is_honored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dist = make_dist(root)
        out = root / "release.zip"
        run(dist, out, epoch="1700000000")
        with zipfile.ZipFile(out) as archive:
            stamps = {info.date_time for info in archive.infolist()}
        assert stamps == {(2023, 11, 14, 22, 13, 20)}, stamps
    print("OK SOURCE_DATE_EPOCH sets the recorded timestamp")


def test_out_directory_names_from_the_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dist = make_dist(root)
        target = root / "out"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--dist", str(dist), "--out", str(target)],
            env={"PATH": "/usr/bin:/bin", "TZ": "UTC", "HOME": "/nonexistent"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert (target / "7dtd-playtest-9.9.9.zip").is_file(), f"missing archive: {proc.stdout}"
    print("OK an --out directory is named from the shipped manifest version")


def test_incomplete_dist_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dist = make_dist(root)
        (dist / "7dtd-playtest.dll").unlink()
        out = root / "release.zip"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--dist", str(dist), "--out", str(out)],
            env={"PATH": "/usr/bin:/bin", "TZ": "UTC", "HOME": "/nonexistent"},
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1, f"an incomplete dist must fail, got {proc.returncode}"
        assert "7dtd-playtest.dll" in proc.stderr, proc.stderr
        assert not out.exists(), "a refused package still wrote an archive"
    print("OK an incomplete dist fails by name and writes nothing")


def main() -> int:
    test_archive_carries_exactly_the_installed_files()
    test_two_runs_agree()
    test_entry_metadata_is_pinned()
    test_source_date_epoch_is_honored()
    test_out_directory_names_from_the_manifest()
    test_incomplete_dist_is_refused()
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
