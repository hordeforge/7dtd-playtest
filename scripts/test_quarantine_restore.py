#!/usr/bin/env python3
"""Offline gate: the fresh-save quarantine restore path.

`--fresh-save` never hard-deletes: the zdtd world state, its chunk overlays
and the previous client log move under `<logdir>/quarantine/`, and every
move records `{src, dest}` in the entry's `restore.jsonl`. That manifest is
the only way back for a world a mispointed `--world` swept aside, so this
gate drives the real recorder and the shipped CLI: a file moved aside comes
back byte for byte at its original path, and nothing is written without
`--apply`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_run  # noqa: E402
import quarantine_restore as qr  # noqa: E402


def test_wiped_world_is_restorable_from_its_manifest() -> None:
    """fresh_zdtd_world + restore: every moved file returns to its own path."""
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        root = Path(td)
        qroot = root / "logdir" / "quarantine"
        world = root / "worlds" / "playtest_auto"
        world.mkdir(parents=True)
        payloads = {
            "players.zsv": "players",
            "containers.zct": "containers",
            "blockmeta.zbm": "blockmeta",
            "c_0_0.zch": "chunk",
        }
        for name, body in payloads.items():
            (world / name).write_text(body, encoding="utf-8")
        (world / "map.png").write_text("keep", encoding="utf-8")

        playtest_run.fresh_zdtd_world(world, qroot)

        entries = qr.entries(qroot)
        assert len(entries) == 1, f"want one quarantine entry, got {entries}"
        pairs = qr.read_manifest(entries[0])
        assert {src for src, _ in pairs} == {world / name for name in payloads}, (
            "every moved file must be recorded with its original path"
        )
        for _src, dest in pairs:
            assert dest.is_file(), f"{dest} must hold the quarantined bytes"

        rc = qr.main([
            "restore", entries[0].name, "--quarantine", str(qroot), "--apply",
        ])
        assert rc == 0, f"restore should succeed, got {rc}"
        for name, body in payloads.items():
            restored = world / name
            assert restored.is_file(), f"{name} must be back at its original path"
            assert restored.read_text(encoding="utf-8") == body, (
                f"{name} must come back byte for byte"
            )
        assert (world / "map.png").read_text(encoding="utf-8") == "keep"
        print("PASS wiped world restored from its manifest")


def test_manifest_record_is_durable_and_parseable() -> None:
    """One JSON object per line, fsynced, and a torn line costs only itself."""
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        entry = Path(td) / "entry"
        entry.mkdir()
        a, b = Path(td) / "a.log", Path(td) / "b.log"
        qr.record(entry, a, entry / "a.log")
        qr.record(entry, b, entry / "b.log")
        with open(qr.manifest_path(entry), "a", encoding="utf-8") as fh:
            fh.write('{"src": 1}\nnot json\n')  # a half-written tail

        pairs = qr.read_manifest(entry)
        assert [src for src, _ in pairs] == [a, b], f"got {pairs}"
        assert qr.skipped_manifest_lines(entry) == 2
        first = json.loads(qr.manifest_path(entry).read_text(encoding="utf-8").splitlines()[0])
        assert set(first) == {"src", "dest"}
        print("PASS manifest lines are durable JSON, unreadable ones skipped")


def test_restore_is_dry_run_by_default_and_refuses_to_clobber() -> None:
    """No bytes without --apply; an occupied original path is kept."""
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        root = Path(td)
        qroot = root / "quarantine"
        qroot.mkdir()
        entry = qroot / "20260101T000000Z-client-log"
        entry.mkdir()
        original = root / "client.log"
        kept = entry / "client.log"
        kept.write_text("previous run", encoding="utf-8")
        qr.record(entry, original, kept)

        base = ["restore", entry.name, "--quarantine", str(qroot)]
        assert qr.main(base) == 0, "dry run must succeed"
        assert not original.exists(), "a dry run must not write anything"

        original.write_text("this run", encoding="utf-8")
        assert qr.main([*base, "--apply"]) == 1, "an occupied path is a blocked restore"
        assert original.read_text(encoding="utf-8") == "this run", (
            "the current run's log must not be clobbered without --force"
        )
        assert qr.main([*base, "--apply", "--force", "--move"]) == 0
        assert original.read_text(encoding="utf-8") == "previous run"
        assert not kept.exists(), "--move reclaims the quarantined copy"
        print("PASS restore is dry-run by default and never clobbers silently")


def test_cli_fails_closed_on_a_missing_quarantine_or_entry() -> None:
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        root = Path(td)
        missing = root / "absent"
        assert qr.main(["list", "--quarantine", str(missing)]) == 2
        qroot = root / "q"
        (qroot / "20260101T000000Z-x").mkdir(parents=True)
        assert qr.main(["restore", "nope", "--quarantine", str(qroot)]) == 2
        # A traversal segment never resolves to an entry.
        assert qr.main(["restore", "../etc", "--quarantine", str(qroot)]) == 2
        assert qr.main(["restore", "20260101T000000Z-x", "--quarantine", str(qroot)]) == 1, (
            "an entry with no manifest has nothing to restore"
        )
        assert qr.main(["show", "20260101T000000Z-x", "--quarantine", str(qroot)]) == 1
        print("PASS restore CLI fails closed on a missing root, entry or manifest")


def test_prune_names_the_last_copy_it_deletes() -> None:
    """An entry past the keep window is the only copy of a swept-aside world.

    The prune is bounded on purpose; what is not acceptable is deleting that
    copy silently, so the paths it held are named on stderr before it goes.
    """
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        qroot = Path(td) / "q"
        old = qroot / "20200101T000000Z-zdtd-world--gone"
        old.mkdir(parents=True)
        (old / "state").mkdir()
        (old / "state" / "players.zsv").write_text("players", encoding="utf-8")
        qr.record(old, Path("/worlds/gone/players.zsv"), old / "state" / "players.zsv")
        for i in range(1, 3):
            (qroot / f"20200101T00000{i}Z-client-log").mkdir()

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            playtest_run.prune_quarantine(qroot, keep=2)
        assert not old.exists(), "the entry is past the keep window and must go"
        assert "/worlds/gone/players.zsv" in stderr.getvalue(), (
            f"prune must name what it deleted, got: {stderr.getvalue()!r}"
        )
        print("PASS prune names the last copy it deletes")


def test_prune_keeps_an_entry_whose_manifest_cannot_be_read() -> None:
    """An unreadable manifest is a fault, not an empty record.

    Both read as "no pairs", but only one of them licenses deleting the entry
    that holds the only copy of a swept-aside world. The prune must keep it
    and say why.
    """
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        qroot = Path(td) / "q"
        old = qroot / "20200101T000000Z-zdtd-world--gone"
        old.mkdir(parents=True)
        (old / "state").mkdir()
        (old / "state" / "players.zsv").write_text("players", encoding="utf-8")
        qr.record(old, Path("/worlds/gone/players.zsv"), old / "state" / "players.zsv")
        for i in range(1, 3):
            (qroot / f"20200101T00000{i}Z-client-log").mkdir()

        real_read_text = Path.read_text
        manifest = qr.manifest_path(old)

        def refuse(self: Path, *a: object, **kw: object) -> str:
            if self == manifest:
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *a, **kw)  # type: ignore[arg-type]

        stderr = io.StringIO()
        with (
            mock.patch.object(Path, "read_text", refuse),
            contextlib.redirect_stderr(stderr),
        ):
            playtest_run.prune_quarantine(qroot, keep=2)
        assert old.exists(), (
            "an entry whose manifest will not read must survive the prune; it "
            "may be the only copy of the world it holds"
        )
        assert str(manifest) in stderr.getvalue(), (
            f"the prune must name the manifest it could not read, got: {stderr.getvalue()!r}"
        )
        print("PASS prune keeps an entry whose manifest cannot be read")


def test_manifest_read_failure_is_not_an_empty_manifest() -> None:
    """`show` on an unreadable manifest fails closed instead of reporting none."""
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        entry = Path(td) / "q" / "20260101T000000Z-x"
        entry.mkdir(parents=True)
        manifest = qr.manifest_path(entry)
        manifest.write_text('{"src": "/a", "dest": "/b"}\n', encoding="utf-8")

        with mock.patch.object(
            Path, "read_text", side_effect=PermissionError(13, "Permission denied")
        ):
            try:
                qr.read_manifest(entry)
            except qr.ManifestUnreadableError as ex:
                assert str(manifest) in str(ex), f"the error must name the file: {ex}"
            else:
                raise AssertionError("an unreadable manifest must raise, not return []")
        print("PASS manifest read failure is not an empty manifest")


def test_a_failed_restore_leaves_no_temp_copy_in_the_world_directory() -> None:
    """copy2 is interrupted: its partial temp file must not outlive the run."""
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        root = Path(td)
        qroot = root / "q"
        entry = qroot / "20260101T000000Z-x"
        entry.mkdir(parents=True)
        (entry / "players.zsv").write_text("players", encoding="utf-8")
        src = root / "worlds" / "playtest_auto" / "players.zsv"
        src.parent.mkdir(parents=True)
        qr.record(entry, src, entry / "players.zsv")

        def interrupt(*a: object, **kw: object) -> None:
            raise OSError(28, "No space left on device")

        stderr = io.StringIO()
        with mock.patch("shutil.copy2", interrupt), contextlib.redirect_stderr(stderr):
            rc = qr.restore(entry, apply=True, force=False, move=False)
        assert rc == 1, f"a blocked restore must exit nonzero, got {rc}"
        leftovers = [p.name for p in src.parent.iterdir() if p.name.startswith(".")]
        assert not leftovers, f"the temp copy must not survive the failure: {leftovers}"
        print("PASS a failed restore leaves no temp copy in the world directory")


def test_move_is_recorded_before_the_bytes_leave_their_path() -> None:
    """The manifest line lands before the rename, not after it.

    Order is the whole point: a recorded pair whose destination never
    arrived costs one "nothing to restore" line on the copy-back, while a
    move the manifest never heard about costs the only copy of a world.
    """
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        root = Path(td)
        qroot = root / "quarantine"
        world = root / "world"
        world.mkdir()
        (world / "players.zsv").write_text("players", encoding="utf-8")

        seen: list[tuple[bool, list[str]]] = []
        real_move = shutil.move

        def spy(src: str, dst: str) -> str:
            entry = qr.entries(qroot)[0]
            seen.append((
                Path(dst).is_file(),
                [src_path.name for src_path, _ in qr.read_manifest(entry)],
            ))
            return real_move(src, dst)

        with mock.patch("playtest_run.shutil.move", side_effect=spy):
            playtest_run.fresh_zdtd_world(world, qroot)

        assert seen == [(False, ["players.zsv"])], (
            f"at move time the pair must be recorded and the bytes not yet moved, got {seen}"
        )
        assert (world / "players.zsv").is_file() is False
        print("PASS the move is recorded before it happens")


def test_unrecordable_move_leaves_the_world_in_place() -> None:
    """No manifest line means no move: the data stays and the run refuses."""
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        root = Path(td)
        qroot = root / "quarantine"
        world = root / "world"
        world.mkdir()
        (world / "players.zsv").write_text("players", encoding="utf-8")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), mock.patch.object(
            qr, "record", side_effect=OSError("manifest unwritable")
        ):
            try:
                playtest_run.fresh_zdtd_world(world, qroot)
                raised = None
            except playtest_run.FreshSaveError as ex:
                raised = ex

        assert raised is not None, "a run must refuse rather than lose the world"
        assert (world / "players.zsv").is_file(), "the world must be left untouched"
        assert (world / "players.zsv").read_text(encoding="utf-8") == "players"
        assert "restore path" in stderr.getvalue(), (
            f"the operator must be told why, got: {stderr.getvalue()!r}"
        )
        print("PASS an unrecordable move leaves the world in place")


def test_the_log_is_kept_when_its_preserved_copy_is_unrecordable() -> None:
    """snapshot_previous_log reports failure so the caller skips truncation."""
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        root = Path(td)
        qroot = root / "quarantine"
        log = root / "client.log"
        log.write_text("previous run", encoding="utf-8")

        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(
            qr, "record", side_effect=OSError("manifest unwritable")
        ):
            assert playtest_run.snapshot_previous_log(log, qroot, "client-log") is False, (
                "an unrecordable preserved copy must not license truncation"
            )
        assert log.read_text(encoding="utf-8") == "previous run"
        assert not list(qroot.glob("*/client.log")), "nothing may be copied unrecorded"

        assert playtest_run.snapshot_previous_log(log, qroot, "client-log") is True
        log.write_text("this run", encoding="utf-8")  # the caller's truncation
        entry = qr.entries(qroot)[-1]  # the failed attempt left an empty entry
        assert qr.main([
            "restore", entry.name, "--quarantine", str(qroot), "--apply", "--force",
        ]) == 0
        assert log.read_text(encoding="utf-8") == "previous run"
        print("PASS the client log is preserved before it may be truncated")


def test_prune_reports_an_entry_it_could_not_delete() -> None:
    """A half-deleted entry must not read as a lost world on the next restore."""
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        qroot = Path(td) / "q"
        old = qroot / "20200101T000000Z-zdtd-world--stuck"
        old.mkdir(parents=True)
        (old / "state").mkdir()
        kept = old / "state" / "players.zsv"
        kept.write_text("players", encoding="utf-8")
        qr.record(old, Path("/worlds/stuck/players.zsv"), kept)
        (qroot / "20260101T000000Z-client-log").mkdir()

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), mock.patch(
            "playtest_run.shutil.rmtree", side_effect=OSError("device busy")
        ):
            playtest_run.prune_quarantine(qroot, keep=1)

        assert old.is_dir(), "a failed prune must not look like a successful one"
        assert kept.is_file(), "the only copy must survive a failed prune"
        assert "could not prune 20200101T000000Z-zdtd-world--stuck" in stderr.getvalue(), (
            f"the failure must be reported, got: {stderr.getvalue()!r}"
        )
        print("PASS a prune that cannot delete says so")


def test_entry_lookup_accepts_a_timestamp_prefix() -> None:
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        qroot = Path(td) / "q"
        for name in ("20260101T000000Z-a", "20260101T000000Z-b"):
            (qroot / name).mkdir(parents=True)
        found = qr.resolve_entry(qroot, "20260101T000000Z")
        assert found is not None and found.name == "20260101T000000Z-b", (
            "a prefix must resolve to the newest matching entry"
        )
        exact = qr.resolve_entry(qroot, "20260101T000000Z-a")
        assert exact is not None and exact.name == "20260101T000000Z-a"
        assert qr.resolve_entry(qroot, "") is None
        print("PASS entry lookup takes an exact name or a timestamp prefix")


def test_default_root_follows_the_orchestrator_logdir() -> None:
    """The CLI's default root is the same logdir a run writes to."""
    with tempfile.TemporaryDirectory(prefix="playtest-quarantine-") as td:
        with mock.patch.dict(os.environ, {"LOGDIR": td}):
            assert qr.default_quarantine_root() == Path(td) / "quarantine"
        assert qr.main(["list", "--quarantine", str(Path(td) / "nope")]) == 2
        print("PASS default quarantine root follows LOGDIR")


def main() -> int:
    failures = 0
    for name, fn in (
        (
            "wiped_world_is_restorable_from_its_manifest",
            test_wiped_world_is_restorable_from_its_manifest,
        ),
        (
            "manifest_record_is_durable_and_parseable",
            test_manifest_record_is_durable_and_parseable,
        ),
        (
            "restore_is_dry_run_by_default_and_refuses_to_clobber",
            test_restore_is_dry_run_by_default_and_refuses_to_clobber,
        ),
        (
            "cli_fails_closed_on_a_missing_quarantine_or_entry",
            test_cli_fails_closed_on_a_missing_quarantine_or_entry,
        ),
        (
            "prune_names_the_last_copy_it_deletes",
            test_prune_names_the_last_copy_it_deletes,
        ),
        (
            "prune_keeps_an_entry_whose_manifest_cannot_be_read",
            test_prune_keeps_an_entry_whose_manifest_cannot_be_read,
        ),
        (
            "manifest_read_failure_is_not_an_empty_manifest",
            test_manifest_read_failure_is_not_an_empty_manifest,
        ),
        (
            "a_failed_restore_leaves_no_temp_copy_in_the_world_directory",
            test_a_failed_restore_leaves_no_temp_copy_in_the_world_directory,
        ),
        (
            "move_is_recorded_before_the_bytes_leave_their_path",
            test_move_is_recorded_before_the_bytes_leave_their_path,
        ),
        (
            "unrecordable_move_leaves_the_world_in_place",
            test_unrecordable_move_leaves_the_world_in_place,
        ),
        (
            "the_log_is_kept_when_its_preserved_copy_is_unrecordable",
            test_the_log_is_kept_when_its_preserved_copy_is_unrecordable,
        ),
        (
            "prune_reports_an_entry_it_could_not_delete",
            test_prune_reports_an_entry_it_could_not_delete,
        ),
        (
            "entry_lookup_accepts_a_timestamp_prefix",
            test_entry_lookup_accepts_a_timestamp_prefix,
        ),
        (
            "default_root_follows_the_orchestrator_logdir",
            test_default_root_follows_the_orchestrator_logdir,
        ),
    ):
        try:
            fn()
        except AssertionError as ex:
            failures += 1
            print(f"FAIL {name}: {ex}", file=sys.stderr)
    if failures:
        print(f"RESULT FAIL ({failures})", file=sys.stderr)
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
