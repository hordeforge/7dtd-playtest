#!/usr/bin/env python3
"""Offline gate: pure-logic units of the host orchestrator (playtest_run.py).

The orchestrator's process-driving paths need real game binaries, but some
helpers are fully offline-testable and destructive enough to deserve their
own gate. Today: fresh_save (the --fresh-save implementation; a regression
there either wipes the wrong directories or silently stops wiping) and the
host-fixture suite gate (a regression there either opens the telnet fixture
path for telnet-free suites or strands live cases whose barriers the host
must service).
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_run

ROOT = _SCRIPTS.parent
CATALOG_CS = ROOT / "Source" / "PlayTestMod" / "Catalog.cs"

# Barrier names the orchestrator services only under want_fixtures. The
# unconditional handlers (chat_echo:, spawn_loadgen_*, teleport_persist_pad,
# apm_dump, spawn_vehicle:<class>) are deliberately absent here.
GATED_BARRIERS = frozenset(
    (
        "spawn_zombie",
        "kill_fixture_zombie",
        "spawn_trader",
        "spawn_vehicle",
        "kill_player",
        "settime_bloodmoon",
        "settime_day",
        "bot_spawn",
        "bot_player_near",
    )
)


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


def test_suite_wants_host_fixtures_selection_table() -> None:
    """Whole-suite-token gate: fixture-bearing catalog suites and the legacy
    aliases that include them open the telnet fixture path; pure-client
    suites, fast-gate aliases, and multi-phase suites stay telnet-free."""
    wants = playtest_run.suite_wants_host_fixtures
    for suite in (
        "combat", "economy", "vehicle", "finale", "bot",
        "demo", "full", "all", "live", "benchmark", "bench", "mp", "residual",
    ):
        assert wants(suite), f"{suite} carries live cases needing host fixtures"
    for suite in (
        "smoke", "core", "world", "ui", "quest", "power",
        "persist", "persist_setup", "soak", "soak_long", "apm",
        "demo_min", "gate", "ci",
    ):
        assert not wants(suite), f"{suite} must run without telnet fixtures"
    # Selection lists split into tokens: one fixture suite is enough to arm,
    # and a telnet-free list must not arm via substring accidents.
    assert not wants("smoke,core")
    assert not wants("smoke core")
    assert wants("smoke,combat")
    assert wants("demo;world")
    print("PASS fixture_gate_selection fixture suites arm, telnet-free suites do not")


def test_fixture_gate_covers_every_barrier_emitting_suite() -> None:
    """Catalog<->orchestrator surface: if a suite's Add* function emits any
    host-serviced (gated) barrier, selecting that suite alone must arm the
    fixture path. Catches adding an admin-fixture case without teaching the
    orchestrator's gate about the new suite."""
    text = CATALOG_CS.read_text(encoding="utf-8")
    # Map each Add<Suite> function to the barriers emitted in its body.
    add_bodies = re.split(r"static void (Add\w+)\(List<CaseDef>", text)
    emitters: dict[str, set[str]] = {}
    for i in range(1, len(add_bodies) - 1, 2):
        fn = add_bodies[i]
        barriers = set(re.findall(r'Report\.Barrier\("([^":]+)', add_bodies[i + 1]))
        gated = barriers & GATED_BARRIERS
        if gated:
            emitters[fn] = gated
    assert emitters, "catalog parse failed: no Add* function emits a gated barrier"

    wants = playtest_run.suite_wants_host_fixtures
    for fn, barriers in sorted(emitters.items()):
        suite = fn.removeprefix("Add").lower()
        assert wants(suite), (
            f"{fn} emits host-serviced barriers {sorted(barriers)} but suite "
            f"'{suite}' does not arm fixtures (missing from FIXTURE_SUITE_IDS?)"
        )
    print(
        "PASS fixture_gate_catalog_surface "
        + ", ".join(f"{fn}→{sorted(b)[0]}" for fn, b in sorted(emitters.items()))
    )


def main() -> int:
    failures = 0
    for name, fn in (
        ("fresh_save_named_only", test_fresh_save_removes_only_named_game_saves),
        ("fresh_save_no_saves_dir", test_fresh_save_without_saves_dir_is_noop),
        ("fixture_gate_selection", test_suite_wants_host_fixtures_selection_table),
        ("fixture_gate_catalog_surface", test_fixture_gate_covers_every_barrier_emitting_suite),
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
