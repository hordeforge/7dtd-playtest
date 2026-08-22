#!/usr/bin/env python3
"""Offline gate: pure-logic units of the host orchestrator (playtest_run.py).

The orchestrator's process-driving paths need real game binaries, but some
helpers are fully offline-testable and destructive enough to deserve their
own gate. Today: fresh_save, the --fresh-save implementation. A regression
there either wipes the wrong directories (data loss beyond the named save)
or silently stops wiping (saves accumulate holes between runs, which the
Makefile FRESH contract depends on preventing).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_run


def test_fresh_save_removes_only_named_game_saves() -> None:
    """Layout UserData/Saves/<World>/<GameName>: every world's copy of the
    named game must go; sibling saves, stray files, and other worlds stay."""
    with tempfile.TemporaryDirectory(prefix="playtest-fresh-") as td:
        ud = Path(td) / "userdata"
        saves = ud / "Saves"
        removed_markers: list[Path] = []
        kept = []
        for world in ("Navezgane", "CustomWorld"):
            game = saves / world / "PlaytestNav"
            game.mkdir(parents=True)
            (game / "region").mkdir()
            (game / "main.ttw").write_text("save", encoding="utf-8")
            removed_markers.append(game)
        sibling_game = saves / "Navezgane" / "SomeOtherGame"
        sibling_game.mkdir(parents=True)
        stray_file = saves / "Navezgane" / "stray.txt"
        stray_file.write_text("keep", encoding="utf-8")
        kept += [sibling_game, stray_file]

        playtest_run.fresh_save(ud, "PlaytestNav")

        for target in removed_markers:
            assert not target.exists(), f"named save must be wiped: {target}"
        for survivor in kept:
            assert survivor.is_file() or survivor.is_dir(), (
                f"fresh-save deleted something outside the named game: {survivor}"
            )
        assert saves.is_dir(), "Saves root itself must survive"
        print("PASS fresh_save_named_only worlds' named saves gone, siblings kept")


def test_fresh_save_without_saves_dir_is_noop() -> None:
    """No Saves directory (first run, wrong userdata): do nothing, do not raise,
    do not create anything."""
    with tempfile.TemporaryDirectory(prefix="playtest-fresh-") as td:
        ud = Path(td) / "userdata"
        ud.mkdir()
        (ud / "not_a_dir").write_text("keep", encoding="utf-8")

        playtest_run.fresh_save(ud, "PlaytestNav")

        assert ud.is_dir() and (ud / "not_a_dir").is_file(), (
            "no-op fresh-save must leave userdata untouched"
        )
        print("PASS fresh_save_no_saves_dir noop without creating anything")


def main() -> int:
    failures = 0
    for name, fn in (
        ("fresh_save_named_only", test_fresh_save_removes_only_named_game_saves),
        ("fresh_save_no_saves_dir", test_fresh_save_without_saves_dir_is_noop),
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
