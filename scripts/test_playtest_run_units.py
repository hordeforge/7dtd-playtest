#!/usr/bin/env python3
"""Offline gate: pure-logic units of the host orchestrator (playtest_run.py).

The orchestrator's process-driving paths need real game binaries, but some
helpers are fully offline-testable and destructive enough to deserve their
own gate. Today: the fresh-save quarantine (stock saves, zdtd world state,
and prior-run log evidence move under <logdir>/quarantine instead of being
hard-deleted; a regression there either wipes the wrong directories or
silently stops wiping), the host-fixture suite gate, and the startup config
validators (a bad timeout / port env value must fail fast with a named error
instead of crashing at argparse setup or timing out instantly), plus the
config summary redaction.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_lock as pl  # noqa: E402
import playtest_run  # noqa: E402

ROOT = _SCRIPTS.parent
CATALOG_CS = ROOT / "Source" / "PlayTestMod" / "Catalog.cs"
PLAYTEST_RUN = _SCRIPTS / "playtest_run.py"

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


def test_loadgen_structured_events_and_expectations() -> None:
    with tempfile.TemporaryDirectory(prefix="playtest-loadgen-events-") as td:
        path = Path(td) / "events.jsonl"
        path.write_text(
            "not json\n"
            '{"schema":"7dtd.loadgen.event.v1","type":"joined","botId":1,"entityId":171}\n'
            '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,"kind":"cvar","name":"protection","value":1}\n'
            '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,"kind":"cvar","name":"raw","value":4}\n'
            '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,"kind":"cvar","name":"net","value":4}\n'
            '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,"kind":"buff","name":"protected","active":true}\n',
            encoding="utf-8",
        )
        events = playtest_run.read_loadgen_events(path)
        assert playtest_run.loadgen_joined_entity(events) == 171
        assert playtest_run.loadgen_expectation_failures(
            events, ["protection=1"], ["protected=true"], ["net"], ["raw=net"]
        ) == []
        failures = playtest_run.loadgen_expectation_failures(
            events, ["protection=0.5"], ["protected=false"]
        )
        assert len(failures) == 2 and "CVar protection" in failures[0]
        assert "buff protected" in failures[1]

        # Every rejection path must surface as a named failure instead of a
        # silent pass: no joined event, unparseable expectation text, and
        # positive/equality expectations against missing state.
        assert playtest_run.loadgen_expectation_failures([], [], []) == [
            "no structured joined event"
        ]
        rejected = playtest_run.loadgen_expectation_failures(
            events,
            ["protection=abc"],
            ["protected=maybe"],
            ["missing_cvar"],
            ["net=also_missing"],
        )
        assert len(rejected) == 4, f"rejection paths drifted: {rejected}"
        assert "invalid CVar expectation" in rejected[0], rejected
        assert "invalid buff expectation" in rejected[1], rejected
        assert "missing_cvar expected positive" in rejected[2], rejected
        assert "net and also_missing expected equal" in rejected[3], rejected
        assert playtest_run.parse_cvar_value(
            "Player 171: protection = 1.25", "protection"
        ) == 1.25
        assert playtest_run.parse_cvar_value(
            "Player EntityPlayer has cvar protection: True. Value: 9.940732",
            "protection",
        ) == 9.940732
        assert playtest_run.parse_cvar_value(
            "Executing command 'cvar get protection -p 172'", "protection"
        ) is None

        class Oracle(playtest_run.TelnetAdmin):
            def get_cvar(
                self, name: str, entity_id: int, timeout: float = 8.0
            ) -> float | None:
                return 4.0

        _, latest = playtest_run.loadgen_latest_state(events)
        assert playtest_run.server_cvar_oracle_failures(
            Oracle("127.0.0.1", 1, ""), 171, ["raw", "net"], latest
        ) == []

        class DriftOracle(Oracle):
            def get_cvar(
                self, name: str, entity_id: int, timeout: float = 8.0
            ) -> float | None:
                return 4.02

        assert playtest_run.server_cvar_oracle_failures(
            DriftOracle("127.0.0.1", 1, ""), 171, ["net"], latest, 0.05
        ) == []

        # Rejection direction: the oracle decides mp-suite verdicts, so an
        # implementation that stopped consulting the server or comparing
        # values would otherwise stay green here forever.
        class BeyondToleranceOracle(Oracle):
            def get_cvar(
                self, name: str, entity_id: int, timeout: float = 8.0
            ) -> float | None:
                return 4.5

        drift = playtest_run.server_cvar_oracle_failures(
            BeyondToleranceOracle("127.0.0.1", 1, ""), 171, ["raw", "net"], latest, 0.05
        )
        assert len(drift) == 2 and "CVar raw" in drift[0], f"drift missed: {drift}"
        assert "expected peer value" in drift[0] and "4.5" in drift[0], drift

        class DeadOracle(Oracle):
            def get_cvar(
                self, name: str, entity_id: int, timeout: float = 8.0
            ) -> float | None:
                return None

        dead = playtest_run.server_cvar_oracle_failures(
            DeadOracle("127.0.0.1", 1, ""), 171, ["net"], latest
        )
        assert len(dead) == 1 and "observed None" in dead[0], f"None not flagged: {dead}"

        # A name with no peer state event has nothing to compare against and
        # must fail, not silently pass.
        unobserved = playtest_run.server_cvar_oracle_failures(
            Oracle("127.0.0.1", 1, ""), 171, ["never_reported"], latest
        )
        assert len(unobserved) == 1 and "never_reported" in unobserved[0], unobserved


def test_loadgen_expectations_reject_non_finite_values() -> None:
    """NaN/inf must fail expectations, never pass them: every comparison on
    NaN is False, so an '=nan' typo or a non-finite observed value would
    otherwise read as a green verdict against any state."""
    nan = float("nan")
    inf = float("inf")
    events = [
        {"type": "joined", "botId": 1, "entityId": 171},
        {"type": "state", "entityId": 171, "kind": "cvar", "name": "x",
         "value": 4.0},
        # Python JSON producers may emit bare NaN/Infinity tokens.
        {"type": "state", "entityId": 171, "kind": "cvar", "name": "nan_cvar",
         "value": nan},
        {"type": "state", "entityId": 171, "kind": "cvar", "name": "inf_cvar",
         "value": inf},
    ]

    # Non-finite expected text is a bad expectation, not a wildcard match-all.
    rejected = playtest_run.loadgen_expectation_failures(
        events, ["x=nan", "x=inf", "x=1e999"], [], [], []
    )
    assert len(rejected) == 3, rejected
    assert all("invalid CVar expectation" in r for r in rejected), rejected

    # Non-finite observed values fail exact / positive / equal expectations.
    failed = playtest_run.loadgen_expectation_failures(
        events,
        ["nan_cvar=4"],
        [],
        ["nan_cvar", "inf_cvar"],
        ["nan_cvar=inf_cvar", "x=x"],
    )
    assert len(failed) == 4, failed

    # The server oracle must flag a non-finite peer value instead of letting
    # the NaN comparison (always False) pass any server value.
    class FixedOracle(playtest_run.TelnetAdmin):
        def get_cvar(
            self, name: str, entity_id: int, timeout: float = 8.0
        ) -> float | None:
            return 4.0

    _, latest = playtest_run.loadgen_latest_state(events)
    oracle_failures = playtest_run.server_cvar_oracle_failures(
        FixedOracle("127.0.0.1", 1, ""), 171, ["nan_cvar"], latest
    )
    assert len(oracle_failures) == 1 and "nan_cvar" in oracle_failures[0], (
        oracle_failures
    )
    print(
        "PASS loadgen_expectations_non_finite nan/inf expectations and states rejected"
    )


def test_loadgen_observer_wiring_is_generic() -> None:
    source = PLAYTEST_RUN.read_text(encoding="utf-8")
    for flag in (
        "--loadgen-observe-cvar",
        "--loadgen-observe-buff",
        "--loadgen-expect-cvar",
        "--loadgen-expect-cvar-positive",
        "--loadgen-expect-cvar-equal",
        "--loadgen-expect-buff",
        "--loadgen-server-cvar-oracle",
        "--loadgen-server-cvar-tolerance",
        "--loadgen-teleport",
    ):
        assert flag in source
    # The poll loop must drain loadgen events incrementally (LoadgenEventReader),
    # not re-read + re-parse the whole JSONL file every iteration.
    assert "loadgen_joined_entity(loadgen_event_reader.drain())" in source
    assert (
        "loadgen_event_reader = LoadgenEventReader(loadgen_events_path)" in source
    )
    # The final-verdict snapshot stays a single whole-file read.
    assert "read_loadgen_events(loadgen_events_path)" in source
    assert "teleportplayer {joined_entity}" in source


def test_loadgen_rebuilds_when_source_is_newer() -> None:
    source = PLAYTEST_RUN.read_text(encoding="utf-8")
    assert "exe.stat().st_mtime >= source_mtime" in source
    assert 'log("loadgen source is newer than its executable; rebuilding")' in source


def test_fresh_save_removes_only_named_game_saves() -> None:
    """Layout UserData/Saves/<World>/<GameName>: every world's copy of the
    named game must go; sibling saves, stray files, and other worlds stay."""
    with tempfile.TemporaryDirectory(prefix="playtest-fresh-") as td:
        ud = Path(td) / "userdata"
        qroot = Path(td) / "logdir" / "quarantine"
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

        playtest_run.fresh_save(ud, "PlaytestNav", qroot)

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

        playtest_run.fresh_save(ud, "PlaytestNav", Path(td) / "q")

        assert ud.is_dir() and (ud / "not_a_dir").is_file(), (
            "no-op fresh-save must leave userdata untouched"
        )
        print("PASS fresh_save_no_saves_dir noop without creating anything")


def test_fresh_save_quarantines_named_saves_recoverably() -> None:
    """Removed saves land under <quarantine> intact: soft-delete window, so a
    mispointed --userdata costs a copy-back instead of unrecoverable loss."""
    with tempfile.TemporaryDirectory(prefix="playtest-fresh-") as td:
        ud = Path(td) / "userdata"
        qroot = Path(td) / "logdir" / "quarantine"
        game = ud / "Saves" / "Navezgane" / "PlaytestNav"
        game.mkdir(parents=True)
        (game / "main.ttw").write_text("save-bytes", encoding="utf-8")

        playtest_run.fresh_save(ud, "PlaytestNav", qroot)

        assert not game.exists(), "save must leave the live Saves tree"
        entries = [p for p in qroot.iterdir() if p.is_dir()]
        assert len(entries) == 1, f"want one quarantine entry, got {entries}"
        rescued = entries[0] / "Navezgane--PlaytestNav" / "PlaytestNav" / "main.ttw"
        assert rescued.is_file() and rescued.read_text(encoding="utf-8") == (
            "save-bytes"
        ), "quarantined save must keep its content for copy-back"
        print("PASS fresh_save_quarantine removed save recoverable from quarantine")


def test_fresh_save_unusable_quarantine_keeps_data_in_place() -> None:
    """If the quarantine cannot take the save, it stays (stale-run warning)
    rather than being destroyed without a recovery path."""
    with tempfile.TemporaryDirectory(prefix="playtest-fresh-") as td:
        ud = Path(td) / "userdata"
        qroot_file = Path(td) / "not-a-dir"
        qroot_file.write_text("blocker", encoding="utf-8")
        game = ud / "Saves" / "Navezgane" / "PlaytestNav"
        game.mkdir(parents=True)
        (game / "main.ttw").write_text("precious", encoding="utf-8")

        errbuf = io.StringIO()
        with contextlib.redirect_stderr(errbuf):
            playtest_run.fresh_save(ud, "PlaytestNav", qroot_file)

        assert game.is_dir() and (game / "main.ttw").is_file(), (
            "unusable quarantine must keep the save in place"
        )
        assert "stale save will be reused" in errbuf.getvalue()
        print("PASS fresh_save_quarantine_unavailable data kept, stale warned")


def test_fresh_zdtd_world_moves_state_and_overlays_recoverably() -> None:
    """zdtd wipe: persisted state + chunk overlays go to quarantine; unrelated
    files stay; missing world dir is a noop."""
    with tempfile.TemporaryDirectory(prefix="playtest-fresh-") as td:
        qroot = Path(td) / "logdir" / "quarantine"
        world = Path(td) / "playtest_auto"
        world.mkdir()
        payloads = {
            "players.zsv": "players",
            "containers.zct": "containers",
            "c_0_0.zch": "chunk",
            "c_1_0.zch.bak": "chunk-bak",
        }
        for name, body in payloads.items():
            (world / name).write_text(body, encoding="utf-8")
        (world / "unrelated.txt").write_text("keep", encoding="utf-8")

        playtest_run.fresh_zdtd_world(world, qroot)

        for name in payloads:
            assert not (world / name).exists(), f"{name} must leave the world dir"
        assert (world / "unrelated.txt").is_file()
        entries = [p for p in qroot.iterdir() if p.is_dir()]
        assert len(entries) == 1, f"want one quarantine entry, got {entries}"
        for name, body in payloads.items():
            rel = "state" if not name.startswith("c_") else "chunks"
            moved = entries[0] / rel / name
            assert moved.is_file() and moved.read_text(encoding="utf-8") == body, (
                f"quarantined {name} must keep its content"
            )
        # Noop path: absent world must not create anything.
        before = sorted(p.name for p in qroot.iterdir())
        playtest_run.fresh_zdtd_world(Path(td) / "missing", qroot)
        assert sorted(p.name for p in qroot.iterdir()) == before
        print("PASS fresh_zdtd_world state+chunks quarantined, unrelated kept")


def test_prune_quarantine_keeps_newest_entries() -> None:
    """Prune deletes oldest entries past QUARANTINE_KEEP (files included)."""
    with tempfile.TemporaryDirectory(prefix="playtest-fresh-") as td:
        qroot = Path(td) / "q"
        qroot.mkdir()
        names = [f"{i:04d}-entry" for i in range(6)]
        for i, name in enumerate(names):
            if i == 0:
                (qroot / name).write_text("old-log", encoding="utf-8")
            else:
                (qroot / name).mkdir()

        playtest_run.prune_quarantine(qroot, keep=5)

        left = sorted(p.name for p in qroot.iterdir())
        assert left == names[1:], f"oldest entry (a file) must go first: {left}"
        print("PASS prune_quarantine newest kept, oldest file+dirs dropped")


def test_prune_run_artifacts_keeps_newest_per_pattern() -> None:
    """Run artifacts accumulate one report+junit pair per run forever without
    a bound. Prune is per pattern so a kept report never loses its junit
    twin, and unrelated logdir files are untouched."""
    with tempfile.TemporaryDirectory(prefix="playtest-artifacts-") as td:
        logdir = Path(td) / "cache"
        logdir.mkdir()
        reports = [f"report-{1700000000 + i}.json" for i in range(7)]
        junits = [f"junit-{1700000000 + i}.xml" for i in range(6)]
        for name in reports + junits:
            (logdir / name).write_text("{}", encoding="utf-8")
        (logdir / "server-orch.log").write_text("keep me", encoding="utf-8")

        playtest_run.prune_run_artifacts(logdir, keep=5)

        left_reports = sorted(p.name for p in logdir.glob("report-*.json"))
        assert left_reports == reports[-5:], f"newest 5 reports kept: {left_reports}"
        left_junits = sorted(p.name for p in logdir.glob("junit-*.xml"))
        assert left_junits == junits[-5:], f"newest 5 junits kept: {left_junits}"
        assert (logdir / "server-orch.log").is_file(), "unrelated files untouched"

        # keep<=0 disables pruning entirely (opt-out for scripted evidence).
        playtest_run.prune_run_artifacts(logdir, keep=0)
        assert len(list(logdir.glob("report-*.json"))) == 5
        print("PASS prune_run_artifacts newest kept per pattern, others untouched")


def test_prune_run_artifacts_wired_into_main() -> None:
    """Every path that writes timestamped artifacts into <logdir> must prune,
    or the bound silently stops covering new writers (rejoin-abort path and
    the final report path both land there)."""
    text = PLAYTEST_RUN.read_text(encoding="utf-8")
    calls = len(re.findall(r"prune_run_artifacts\(args\.logdir\)", text))
    # One definition reference inside main() per artifact-writing exit path;
    # the def line itself does not match this exact call shape.
    assert calls == 2, (
        f"expected prune after both report-writing paths, found {calls} calls"
    )
    print("PASS prune_run_artifacts wired after both report-writing paths")


def test_main_finally_reaps_mute_helpers() -> None:
    """reap_finished_helpers must run from main()'s finally, not only inside
    the poll loops: the rejoin-abort return, exception unwind, and post-DONE
    teardown all bypass the loops and would otherwise leave exited detached
    helpers as zombies for the rest of the orchestrator run."""
    tree = ast.parse(PLAYTEST_RUN.read_text(encoding="utf-8"))
    mains = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"
    ]
    assert len(mains) == 1, "expected exactly one main() definition"

    def _call_names(stmts: list[ast.stmt]) -> set[str]:
        return {
            n.func.id
            for stmt in stmts
            for n in ast.walk(stmt)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }

    # The nested service_barrier helper has its own try/finally (telnet
    # close); the teardown finally is the one driving stop_proc.
    teardown_finallys = [
        n.finalbody
        for n in ast.walk(mains[0])
        if isinstance(n, ast.Try) and "stop_proc" in _call_names(n.finalbody)
    ]
    assert len(teardown_finallys) == 1, (
        "expected exactly one teardown finally (stop_proc-driven) in main()"
    )
    calls = [
        n
        for stmt in teardown_finallys[0]
        for n in ast.walk(stmt)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "reap_finished_helpers"
    ]
    assert calls, "main()'s finally must call reap_finished_helpers()"
    print("PASS main_finally_reap_helpers teardown reaps detached helpers")


def test_snapshot_previous_log_copies_before_truncate() -> None:
    """Log evidence of the previous run is copied aside; original untouched
    (the caller still truncates for the incremental readers)."""
    with tempfile.TemporaryDirectory(prefix="playtest-fresh-") as td:
        qroot = Path(td) / "q"
        log_path = Path(td) / "client.log"
        log_path.write_text("previous crash stacktrace", encoding="utf-8")

        playtest_run.snapshot_previous_log(log_path, qroot, "client-log")

        entries = list(qroot.iterdir())
        assert len(entries) == 1
        copy = entries[0] / "client.log"
        assert copy.is_file()
        assert copy.read_text(encoding="utf-8") == "previous crash stacktrace"
        assert log_path.read_text(encoding="utf-8") == "previous crash stacktrace"
        # Missing logs are a noop.
        playtest_run.snapshot_previous_log(None, qroot, "client-log")
        playtest_run.snapshot_previous_log(Path(td) / "absent.log", qroot, "x")
        assert len(list(qroot.iterdir())) == 1
        print("PASS snapshot_previous_log prior run preserved, noop when absent")


def test_wait_file_contains_incremental() -> None:
    """wait_file_contains must find needles incrementally: one already in the
    log is found before any sleep, and one landing in a later append is found
    on a subsequent poll without re-reading earlier content (server startup
    logs reach tens of MB while the poll shares the machine with the game)."""
    with tempfile.TemporaryDirectory(prefix="playtest-wait-") as td:
        log = Path(td) / "unity.log"
        log.write_text("boot noise\n", encoding="utf-8")

        t0 = time.monotonic()
        assert playtest_run.wait_file_contains(log, "boot noise", timeout=5.0)
        assert time.monotonic() - t0 < 5.0, "pre-existing needle must not wait"

        assert not playtest_run.wait_file_contains(
            log, "absent-token", timeout=0.05
        ), "missing needle must time out False"

        result: list[bool] = []

        def waiter() -> None:
            result.append(playtest_run.wait_file_contains(log, "StartGame done", 30.0))

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.7)  # let at least one poll miss pass before appending
        with log.open("a", encoding="utf-8") as fh:
            fh.write("StartGame done\n")
        th.join(timeout=30)
        assert result == [True], f"late append never matched: {result}"
        print("PASS wait_file_contains incremental pre-existing/late-append/timeout")


def test_suite_wants_host_fixtures_selection_table() -> None:
    """Whole-suite-token gate: fixture-bearing catalog suites and the legacy
    aliases that include them open the telnet fixture path; pure-client
    suites, fast-gate aliases, and multi-phase suites stay telnet-free."""
    wants = playtest_run.suite_wants_host_fixtures
    for suite in (
        "combat", "economy", "vehicle", "finale", "bot",
        # demo_mode / residual_light are ExpandSuites synonyms of demo /
        # residual: every spelling of one selection must arm identically.
        "demo", "demo_mode", "full", "all", "live",
        "benchmark", "bench", "mp", "residual", "residual_light",
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
    enabled = playtest_run.host_fixtures_enabled
    assert enabled("external-provider-suite", disabled=False, requested=True)
    assert not enabled("external-provider-suite", disabled=False, requested=False)
    assert not enabled("combat", disabled=True, requested=True)
    print("PASS fixture_gate_selection fixture suites arm, telnet-free suites do not")


def test_new_barrier_tables_fresh_pair_per_generation() -> None:
    """Fired and seen tables are created (and reset) as one pair: both cover
    every barrier name at zero, and a fresh pair is independent of the old
    one. A generation boundary that reset only one side would keep stale
    fired counts that swallow the first verify-generation emission of an
    already-serviced name."""
    fired, seen = playtest_run.new_barrier_tables()
    assert fired.keys() == set(playtest_run.BARRIER_NAMES)
    assert seen.keys() == set(playtest_run.BARRIER_NAMES)
    assert all(v == 0 for v in fired.values())
    assert all(v == 0 for v in seen.values())
    # Simulate one setup generation servicing a barrier, then take the next
    # generation's pair: it must start clean, not inherit the counts.
    seen["teleport_persist_pad"] += 1
    fired["teleport_persist_pad"] += 1
    fired2, seen2 = playtest_run.new_barrier_tables()
    assert fired2["teleport_persist_pad"] == 0, "stale fired count crossed generations"
    assert seen2["teleport_persist_pad"] == 0, "stale seen count crossed generations"
    # And the new tables are distinct objects, not aliased views.
    seen2["spawn_zombie"] += 5
    assert seen["spawn_zombie"] == 0
    print("PASS new_barrier_tables paired zeroed fresh per generation")


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


def _expand_suites_alias_map(text: str) -> dict[str, tuple[str, ...]]:
    """Parse Catalog.ExpandSuites switch arms into {alias: expansion ids}.

    Only alias arms (explicit ``case`` labels with an AddUnique) are
    returned; the pass-through default arm and the early-return list/catalog
    arm yield no ids and are skipped by callers.
    """
    start = text.index("public static string[] ExpandSuites")
    end = text.index("static void AddUnique", start)
    body = re.sub(r"//[^\n]*", "", text[start:end])
    aliases: dict[str, tuple[str, ...]] = {}
    arm_re = re.compile(
        r'((?:case\s+"[^"]+":\s*)+)'
        r"(?:return new\[\]\s*\{[^}]*\}\s*;|AddUnique\(list,(?P<ids>[^)]*)\))"
    )
    for m in arm_re.finditer(body):
        names = re.findall(r'case\s+"([^"]+)"', m.group(1))
        ids = tuple(re.findall(r'"([^"]+)"', m.group("ids") or ""))
        for name in names:
            aliases[name] = ids
    return aliases


def test_fixture_gate_covers_every_expand_suites_alias() -> None:
    """Alias parity: if Catalog.ExpandSuites maps an alias onto an expansion
    containing any suite id that arms fixtures, every spelling of that
    selection must arm fixtures too. Catches adding a client synonym (the
    residual_light class) without teaching FIXTURE_SUITE_IDS, where one
    spelling of the same run opens the telnet path and the other leaves its
    barriers unserviced."""
    text = CATALOG_CS.read_text(encoding="utf-8")
    aliases = _expand_suites_alias_map(text)
    assert "residual" in aliases and "residual_light" in aliases, (
        "catalog parse failed: residual synonyms not recognized"
    )
    wants = playtest_run.suite_wants_host_fixtures
    for alias, ids in sorted(aliases.items()):
        if not ids:
            continue
        if set(ids) & set(playtest_run.FIXTURE_SUITE_IDS):
            assert wants(alias), (
                f"alias '{alias}' expands into fixture-armed suites "
                f"{sorted(set(ids) & set(playtest_run.FIXTURE_SUITE_IDS))} but "
                "does not arm fixtures (missing from FIXTURE_SUITE_IDS?)"
            )
    print(
        f"PASS fixture_gate_alias_surface {len(aliases)} ExpandSuites aliases"
        " agree with FIXTURE_SUITE_IDS"
    )


def _spawn_detached(body: str) -> subprocess.Popen[bytes]:
    """Real child in its own session (same shape as orchestrator launches)."""
    return subprocess.Popen(
        ["bash", "-c", body],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def test_stop_proc_reaps_after_sigkill_escalation() -> None:
    """A child that ignores SIGTERM gets SIGKILLed and then reaped: after
    stop_proc the Popen must carry a returncode, not linger as a zombie."""
    proc = _spawn_detached("trap '' TERM; sleep 30")
    old_term = playtest_run._STOP_TERM_WAIT_SEC
    old_kill = playtest_run._STOP_KILL_WAIT_SEC
    playtest_run._STOP_TERM_WAIT_SEC = 0.3
    playtest_run._STOP_KILL_WAIT_SEC = 0.3
    try:
        t0 = time.monotonic()
        playtest_run.stop_proc(proc)
        elapsed = time.monotonic() - t0
    finally:
        playtest_run._STOP_TERM_WAIT_SEC = old_term
        playtest_run._STOP_KILL_WAIT_SEC = old_kill
    assert proc.returncode is not None, (
        "SIGKILL escalation must reap the child (returncode stays None on a zombie)"
    )
    assert elapsed < 5, f"stop_proc waited too long: {elapsed:.1f}s"
    print("PASS stop_proc_sigkill_reap SIGKILLed child reaped, no zombie")


def test_stop_proc_exited_child_closes_log_handle() -> None:
    """stop_proc on an already-exited child must not raise and must close the
    attached log handle (fd released deterministically)."""
    proc = _spawn_detached("exit 0")
    while proc.poll() is None:
        time.sleep(0.02)
    # Raw handle on purpose: mirrors the orchestrator's proc._log_fh ownership.
    fh = tempfile.TemporaryFile()  # noqa: SIM115
    try:
        proc._log_fh = fh  # type: ignore[attr-defined]
        playtest_run.stop_proc(proc)
        assert fh.closed, "log handle of an exited child must be closed"
    finally:
        if not fh.closed:
            fh.close()
    print("PASS stop_proc_exited_child exited child reaped, log handle closed")


def test_reap_finished_helpers_drops_only_exited() -> None:
    """reap_finished_helpers removes exited detached helpers (no zombie pileup
    during long soaks) and keeps live ones registered."""
    done = _spawn_detached("exit 0")
    while done.poll() is None:
        time.sleep(0.02)
    live = _spawn_detached("sleep 30")
    saved = list(playtest_run._MUTE_HELPER_PROCS)
    playtest_run._MUTE_HELPER_PROCS[:] = [done, live]
    try:
        playtest_run.reap_finished_helpers()
        assert [live] == playtest_run._MUTE_HELPER_PROCS, (
            "exited helpers must be reaped away; live ones kept"
        )
    finally:
        playtest_run._MUTE_HELPER_PROCS[:] = saved
        live.kill()
        live.wait()
    print("PASS reap_finished_helpers exited helpers reaped, live ones kept")


def test_loadgen_peer_rebind_reaps_exited_instance() -> None:
    """The spawn_loadgen_peer rebind must route the prior (exited) instance
    through stop_proc: dropping the Popen after only closing its log handle
    leaves the child unreaped, so every peer barrier fire adds one zombie that
    lives until orchestrator exit (long soak / mp runs)."""
    source = PLAYTEST_RUN.read_text(encoding="utf-8")
    start = source.index("# Prior instance already exited")
    end = source.index("loadgen_proc = start_loadgen(", start)
    block = source[start:end]
    assert "stop_proc(loadgen_proc)" in block, (
        "peer rebind must reap the exited instance via stop_proc"
    )
    print("PASS loadgen_peer_rebind_reap exited instance routed through stop_proc")


def test_positive_seconds_type_and_env_reader() -> None:
    """--timeout and PLAYTEST_TIMEOUT_SEC accept only finite seconds > 0; a
    bad env value exits 2 naming the variable instead of a raw traceback or
    an instant timeout later."""
    assert playtest_run.positive_seconds("900") == 900.0
    assert playtest_run.positive_seconds("0.5") == 0.5
    for bad in ("abc", "", "0", "-5", "inf", "nan", "1e999"):
        try:
            playtest_run.positive_seconds(bad)
        except argparse.ArgumentTypeError:
            pass
        else:
            raise AssertionError(f"positive_seconds accepted {bad!r}")

    name = "PLAYTEST_TIMEOUT_SEC"
    old = os.environ.get(name)
    try:
        os.environ.pop(name, None)
        assert playtest_run.seconds_from_env(name, 900.0) == 900.0, (
            "unset env must fall back to the documented default"
        )
        os.environ[name] = "120"
        assert playtest_run.seconds_from_env(name, 900.0) == 120.0
        os.environ[name] = "not-a-number"
        errbuf = io.StringIO()
        try:
            with contextlib.redirect_stderr(errbuf):
                playtest_run.seconds_from_env(name, 900.0)
        except SystemExit as ex:
            assert ex.code == 2, f"bad env value must exit 2 (harness error): {ex.code}"
        else:
            raise AssertionError("bad env value must exit nonzero")
        out = errbuf.getvalue()
        assert name in out and "not-a-number" in out, (
            f"error must name the variable and the bad value: {out!r}"
        )
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old
    print("PASS timeout_validation bad env values fail fast with a named error")


def test_tcp_port_type_range() -> None:
    """--port / --admin-port must be real TCP ports: out-of-range values are
    config errors at startup, not late server-bind failures."""
    assert playtest_run.tcp_port("26900") == 26900
    assert playtest_run.tcp_port("65535") == 65535
    for bad in ("0", "65536", "-1", "abc", ""):
        try:
            playtest_run.tcp_port(bad)
        except argparse.ArgumentTypeError:
            pass
        else:
            raise AssertionError(f"tcp_port accepted {bad!r}")
    print("PASS tcp_port_range ports outside 1..65535 rejected at startup")


def test_litenet_port_room_guard() -> None:
    """--port feeds the derived loadgen join port ServerPort+2, so a value
    above 65533 must be rejected at startup (tcp_port alone accepts it)."""
    source = PLAYTEST_RUN.read_text(encoding="utf-8")
    assert "require_litenet_room(args.port)" in source
    for ok in (26900, 27025, 65533):
        playtest_run.require_litenet_room(ok)
    for bad in (65534, 65535):
        try:
            playtest_run.require_litenet_room(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"require_litenet_room accepted {bad}")
    print("PASS litenet_port_room ports without room for port+2 rejected")


def test_main_default_port_reaches_preflight_refusal() -> None:
    """Omitting --port must resolve the backend default before the
    require_litenet_room guard compares it: make playtest leaves PORT= empty,
    so every default-port invocation reached main() and died on a None > int
    TypeError instead of running its preflight refusals. The patched install
    paths make main() return 2 at the missing-dedicated check, before any
    lock acquisition or process work."""
    with tempfile.TemporaryDirectory(prefix="playtest-default-port-") as td:
        root = Path(td)
        orig_game_srv = playtest_run.DEFAULT_GAME_SRV
        orig_connect = playtest_run.CONNECT
        playtest_run.DEFAULT_GAME_SRV = root / "no-dedicated"
        playtest_run.CONNECT = root / "no-connect"
        try:
            rc = playtest_run.main(
                ["--server", "stock", "--logdir", str(root / "log"), "--timeout", "900"]
            )
        finally:
            playtest_run.DEFAULT_GAME_SRV = orig_game_srv
            playtest_run.CONNECT = orig_connect
    assert rc == 2, rc
    print("PASS default_port_preflight omitted --port reaches preflight refusal")


def test_config_summary_redacts_telnet_password() -> None:
    """The startup config line lists the effective options but never the
    telnet password value, so run logs stay shareable."""
    args = argparse.Namespace(
        server="stock",
        suite=" smoke ",
        port=26900,
        admin_port=8081,
        timeout=900.0,
        world_name="Navezgane",
        world=None,
        game_name="PlaytestNav",
        logdir=Path("/tmp/logdir"),
        fresh_save=True,
        no_server=False,
        no_fixtures=False,
        telnet_password="hunter2-secret",
        peer_client_name="",
    )
    line = playtest_run.config_summary(args)
    assert "hunter2-secret" not in line, f"password leaked into config line: {line}"
    assert "telnet_password=set" in line, line
    assert "suite=smoke" in line and "port=26900" in line and "server=stock" in line, line
    assert "fresh_save=True" in line, line

    # Attach mode without env: the state is named (legacy fallback) but no
    # value ever appears. An own-server run without env generates instead,
    # which still counts as "set".
    attach = argparse.Namespace(
        **{**vars(args), "telnet_password": "", "no_server": True}
    )
    line = playtest_run.config_summary(attach)
    assert "telnet_password=legacy-attach-default" in line, line
    generated = argparse.Namespace(**{**vars(args), "telnet_password": ""})
    assert "telnet_password=set" in playtest_run.config_summary(generated)

    zdtd = argparse.Namespace(**{**vars(args), "server": "zdtd"})
    assert "world_name" not in playtest_run.config_summary(zdtd), (
        "stock-only GameName must not masquerade as the zdtd world"
    )
    peer = argparse.Namespace(**{**vars(args), "peer_client_name": "atomic-peer"})
    assert "peer=atomic-peer" in playtest_run.config_summary(peer)
    print("PASS config_summary_redaction effective options logged, password redacted")


def test_telnet_admin_ai_and_player_parsing() -> None:
    """TelnetAdmin's parsers decide which entities die on the live server: a
    regression either kills the player entity or clears nothing, and neither
    is visible until a real suite runs. Text in, entity ids out (docstring
    contract: shared AI keyword table, stock `listplayers` / zdtd `list`
    styles, `(entity N)` console form, non-positive ids dropped)."""
    tn = playtest_run.TelnetAdmin("127.0.0.1", 1, "")
    listents_out = "\n".join(
        (
            "2. zombieSteve (id=3877, pos=(520.0, 62.0, 950.0))",
            "3. zombieYo (ID=3878)",
            "4. animalStag (id=3890)",
            "5. bandit (id=3901)",
            "zombieBoe",
            "Remote 'maci' (id=171, hp=100)",
        )
    )
    assert tn._ai_entity_ids(listents_out) == ["3877", "3878", "3890"], (
        f"AI table picked wrong entities: {tn._ai_entity_ids(listents_out)}"
    )

    class CannedTelnet(playtest_run.TelnetAdmin):
        """Replays canned server replies instead of opening a socket."""

        def __init__(self, replies: list[str]) -> None:
            self._replies = list(replies)
            self.sent: list[str] = []
            self.host = ""
            self.port = 0
            self.password = ""
            self._sock = None

        def exec(self, cmd: str) -> str:
            self.sent.append(cmd)
            return self._replies.pop(0)

        def _recv(self, settle: float) -> str:
            return ""  # the lag re-read after a complete reply sees no more data

    # kill_non_player_ai must kill exactly the listed non-player AI ids and
    # never fall back to killall-style behavior while players are alive.
    # Reply order matches the call order: listents first, then listplayers.
    killer = CannedTelnet(
        [
            "2. zombieSteve (id=3877)\n3. animalStag (id=3890)",
            "Total of 1 in the game\n'maci' (id=171, pos=(520.0, 62.0, 950.0))",
            "killed 3877",
            "killed 3890",
        ]
    )
    assert killer.kill_non_player_ai() == 2, (
        f"killed~={killer.sent} (player or fallback kill leaked in)"
    )
    assert killer.sent == [
        "listents",
        "listplayers",
        "kill 3877",
        "kill 3890",
    ], f"unexpected commands sent: {killer.sent}"

    # zdtd style: listplayers is unknown, `list` answers with (entity N);
    # id=0 entries must be dropped, duplicates collapsed.
    zdtd = CannedTelnet(
        [
            "unknown command: listplayers",
            "(entity 107) maci\n(entity 0) ghost\n(entity 107) dup",
        ]
    )
    assert zdtd.list_player_ids() == [107], zdtd.list_player_ids()
    assert zdtd.sent == ["listplayers", "list"], zdtd.sent

    # Persist flow: every listed player is teleported to the pad and the
    # command reaches the wire in the raw PERSIST_PAD_COORDS form (the same
    # numbers spawnentityat consumers use), so a formatting drift cannot
    # strand players off-pad with an opaque "far from pad" failure.
    tp = CannedTelnet(
        [
            "Total of 2 in the game\n'maci' (id=171, pos=(0, 0, 0))\n"
            "'ghost' (id=172, pos=(0, 0, 0))",
            "teleported",
            "teleported",
        ]
    )
    assert tp.teleport_players_to(520, 62, 950) == 2, f"sent={tp.sent}"
    assert tp.sent == [
        "listplayers",
        "teleportplayer 171 520 62 950",
        "teleportplayer 172 520 62 950",
    ], f"pad coords must reach the wire verbatim: {tp.sent}"

    # Nobody online: warn + no teleport commands, never a blind send.
    errbuf = io.StringIO()
    with contextlib.redirect_stderr(errbuf):
        empty = CannedTelnet(["", ""])
        assert empty.teleport_players_to(520, 62, 950) == 0
    assert empty.sent == ["listplayers", "list"], empty.sent
    assert "no players from listplayers" in errbuf.getvalue(), errbuf.getvalue()


def test_telnet_broken_session_degrades_to_empty_reply() -> None:
    """A telnet session that dies mid-poll (server restart, pipe reset) must
    degrade like every other telnet failure: exec closes the socket and
    returns "", and list_player_ids' lag re-read then sees no session. The
    old `assert self._sock` turned that expected operating error into an
    AssertionError that escaped the poll loop and killed the whole run with
    a traceback instead of a warn-and-retry next poll."""
    class BrokenPipe:
        """Socket stub whose transport is already gone."""

        def settimeout(self, _v: float) -> None:
            return None

        def sendall(self, _b: bytes) -> None:
            raise OSError("broken pipe")

        def recv(self, _n: int) -> bytes:
            raise OSError("connection reset")

        def close(self) -> None:
            return None

    tn = playtest_run.TelnetAdmin("127.0.0.1", 1, "")
    tn._sock = BrokenPipe()  # type: ignore[assignment]
    errbuf = io.StringIO()
    with contextlib.redirect_stderr(errbuf):
        ids = tn.list_player_ids()
    assert ids == [], f"broken session must parse no players: {ids}"
    assert tn._sock is None, "exec must close the broken session"
    assert "telnet exec fail" in errbuf.getvalue(), errbuf.getvalue()

    # The same path with no socket at all (post-close callers): empty reply,
    # never an assert, and no 1.5s settle sleep on the dead handle.
    gone = playtest_run.TelnetAdmin("127.0.0.1", 1, "")
    gone._sock = None
    t0 = time.monotonic()
    assert gone.list_player_ids() == []
    assert time.monotonic() - t0 < 1.0, "dead-session read must not settle-wait"
    print("PASS telnet_broken_session dead session degrades to empty reply")


def test_spawn_near_players_trusts_only_live_sessions() -> None:
    """spawn_near_players books a fixture fire only when the telnet session
    survived the exchange: exec() closes the socket on transport failure and
    returns "", indistinguishable from silence, so a dead session must book
    zero (the barrier retries next poll) instead of counting a spawn that
    never reached the server (docstring contract on connected())."""
    class _LiveSock:
        """Non-None stand-in for an open session socket."""

        def settimeout(self, _v: float) -> None:
            return None

    class SpawnTelnet(playtest_run.TelnetAdmin):
        """Replays canned replies and can kill the session mid-exchange."""

        def __init__(self, replies: list[str], die_on: str | None = None) -> None:
            self._replies = list(replies)
            self.die_on = die_on
            self.sent: list[str] = []
            self.host = ""
            self.port = 0
            self.password = ""
            self._sock = None

        def open(self) -> None:
            self._sock = _LiveSock()  # type: ignore[assignment]

        def exec(self, cmd: str) -> str:
            self.sent.append(cmd)
            if self._sock is None:
                return ""  # real exec sends nothing without a live session
            if self.die_on is not None and cmd == self.die_on:
                # Mirror exec(): a broken transport closes the session and
                # the caller cannot tell silence from failure.
                self._sock = None
                return ""
            return self._replies.pop(0)

    players = "Total of 1 in the game\n'maci' (id=171, pos=(520.0, 62.0, 950.0))"

    # Session survived the exchange and the reply is not a miss: book one.
    ok = SpawnTelnet([players, "zombieBoe spawned id=3877"])
    ok.open()
    assert ok.spawn_near_players("zombieBoe") == 1, f"sent={ok.sent}"
    assert ok.sent == ["listplayers", "spawnentity 171 zombieBoe"], ok.sent

    # Session died on the spawn command itself: "" is indistinguishable from
    # silence, so nothing may be booked (the barrier retries next poll). The
    # fallback attempts run on the dead session and must stay unbooked too.
    died = SpawnTelnet([players], die_on="spawnentity 171 zombieBoe")
    died.open()
    assert died.spawn_near_players("zombieBoe") == 0, (
        f"a dead session was booked as a spawn: sent={died.sent}"
    )
    assert died.sent[:2] == ["listplayers", "spawnentity 171 zombieBoe"]
    assert all(cmd.startswith("spawnentityat ") for cmd in died.sent[2:]), died.sent

    # Alive session but every shape misses ("No spawn point" primary, ERR /
    # Unknown fallback replies): zero bookings, all three pad offsets tried.
    misses = SpawnTelnet(
        [
            players,
            "No spawn point found",
            "ERR: invalid position",
            "Unknown entity name",
            "ERR: invalid position",
        ]
    )
    misses.open()
    assert misses.spawn_near_players("zombieBoe") == 0, f"sent={misses.sent}"
    assert len(misses.sent) == 2 + 3, f"fallback stopped early: {misses.sent}"

    # Fallback success books exactly one, then stops issuing commands.
    rescued = SpawnTelnet([players, "No spawn point found", "spawned at pad"])
    rescued.open()
    assert rescued.spawn_near_players("zombieBoe") == 1, f"sent={rescued.sent}"
    assert rescued.sent[-1].startswith("spawnentityat zombieBoe "), rescued.sent
    print("PASS spawn_trust_only_live_sessions dead sessions never book spawns")


def test_safe_barrier_param_rejects_command_shapes() -> None:
    """Barrier parameters are lifted from client-log lines (attacker-reachable
    via remote chat) and interpolated into telnet console commands. Only
    identifier-shaped tokens may cross: whitespace would smuggle a second
    command onto the next telnet line, quotes break out of the quoted
    `say "<token>"` form."""
    for good in (
        "ptchat12345",
        "zombieBoe",
        "vehicleMotorcycle",
        "npcTraderJoel",
        "a" * 64,
    ):
        assert playtest_run.safe_barrier_param(good), f"{good!r} must pass"
    for bad in (
        "",
        "two words",
        'say" hacked',
        "x\nsay hacked",
        "x\rsay hacked",
        "semi;colon",
        "$(...)",
        "`cmd`",
        "a" * 65,
        "ptchat-1;kill 4",
        "tab\tsep",
    ):
        assert not playtest_run.safe_barrier_param(bad), f"{bad!r} must be dropped"
    print("PASS barrier_param_validation identifiers only, injection shapes dropped")


def test_safe_barrier_param_gates_both_telnet_handlers() -> None:
    """Wiring gate: every log-derived barrier parameter forwarded into a
    telnet command (chat_echo token, spawn_vehicle class) must pass through
    safe_barrier_param inside main(). A new handler that skips the check
    reopens the log-to-console injection path."""
    tree = ast.parse(PLAYTEST_RUN.read_text(encoding="utf-8"))
    mains = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"
    ]
    assert len(mains) == 1
    calls = sum(
        1
        for n in ast.walk(mains[0])
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "safe_barrier_param"
    )
    assert calls >= 2, (
        f"chat_echo and spawn_vehicle handlers must validate via "
        f"safe_barrier_param, found {calls} call(s) in main()"
    )
    print("PASS barrier_param_wiring both parameterised handlers validated")


def test_scrub_strips_control_chars_from_echoed_log_text() -> None:
    """Log bytes echoed to the operator terminal carry remote chat text;
    control characters (ESC introducing terminal escapes, CR rewriting
    lines) must not survive into orchestrator stdout. Tab/LF stay so dumps
    remain readable."""
    scrub = playtest_run.scrub
    assert scrub("normal line") == "normal line"
    assert scrub("\x1b[31mred\x1b[0m") == "[31mred[0m"
    assert scrub("hide\x0bme\x00\x07") == "hideme"
    assert scrub("cr\rinjected") == "crinjected", "CR must go (line-rewrite)"
    assert scrub("keep\ttabs\nand\nlines") == "keep\ttabs\nand\nlines"
    print("PASS log_scrub control chars stripped from terminal echoes")


def test_telnet_recv_scrubs_control_chars() -> None:
    """Telnet replies echo remote-peer-chosen names (listplayers player
    names, listents entity names) and every caller logs slices of them to
    the operator terminal. _recv is the single control-char boundary: ESC,
    NUL, BEL, and CR must be stripped there so no exec/get_cvar/list echo
    can emit terminal escapes, while printable text survives for the id
    regexes, keyword checks, and cvar number extraction."""
    class EscapedSocket:
        """Replays one payload then acts closed."""

        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self.sent: list[bytes] = []

        def settimeout(self, _v: float) -> None:
            return None

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def recv(self, _n: int) -> bytes:
            chunk, self._payload = self._payload, b""
            return chunk

        def close(self) -> None:
            return None

    tn = playtest_run.TelnetAdmin("127.0.0.1", 1, "")
    tn._sock = EscapedSocket(
        b"\x1b[2J\x00'p\x07layer' (id=171)\r\nTotal of 1 in the game\n"
    )  # type: ignore[assignment]
    out = tn._recv(0.0)
    for bad in ("\x1b", "\x00", "\x07", "\r"):
        assert bad not in out, f"{bad!r} survived the telnet boundary: {out!r}"
    assert "'player' (id=171)" in out, f"visible reply text lost: {out!r}"
    assert "Total of 1" in out, f"reply lines lost: {out!r}"
    # Verdict parsing still works on the scrubbed form (the escape debris
    # stays clear of the token so the id regexes see the same boundaries).
    scrubbed_cvar = EscapedSocket(b"\x1b[0m\nHoldingController = 3\r\n")
    tn._sock = scrubbed_cvar  # type: ignore[assignment]
    assert tn.get_cvar("HoldingController", 171) == 3.0
    assert scrubbed_cvar.sent == [b"cvar get HoldingController -p 171\n"]
    print("PASS telnet_recv_scrub replies scrubbed once at the socket boundary")


def test_result_echo_line_scrubs_parsed_rows() -> None:
    """Result rows echo parsed client-log fields (case ids, details carrying
    remote chat text) to the operator terminal: the same control-char scrub
    as every other interactive echo must apply, and the row shapes must stay
    byte-identical for clean input."""
    line = playtest_run.result_echo_line(
        {"status": "PASS", "case": "smoke/join", "detail": "ok"}
    )
    assert line == "  PASS smoke/join ok", f"clean shape drifted: {line!r}"
    peer = playtest_run.result_echo_line(
        {"status": "FAIL", "case": "mp/s", "detail": "d"}, peer=True
    )
    assert peer == "  peer FAIL mp/s d", f"peer shape drifted: {peer!r}"
    dirty = playtest_run.result_echo_line(
        {"status": "FAIL", "case": "chat/echo", "detail": "\x1b[2J\x00cr\rinj"}
    )
    assert "\x1b" not in dirty and "\r" not in dirty and "\x00" not in dirty, (
        f"control chars reached the terminal echo: {dirty!r}"
    )
    assert "[2Jcrinj" in dirty, f"visible text must survive the scrub: {dirty!r}"
    print("PASS result_echo_line parsed rows scrubbed before terminal echo")


def test_result_row_echoes_all_routed_through_helper() -> None:
    """A direct f-string echo of a parsed row reintroduces the escape path;
    every row echo in main() must call the scrubbed helper instead."""
    src = Path(playtest_run.__file__).read_text(encoding="utf-8")
    direct = re.findall(r'log\(f"  \{r\[.status.\]\}', src)
    assert not direct, (
        f"{len(direct)} result row echo(es) bypass result_echo_line"
    )
    calls = len(re.findall(r"\bresult_echo_line\(", src))
    # 1 definition + 1 docstring mention aside: 4 call sites in main().
    assert calls >= 5, f"helper defined but unwired: {calls} reference(s)"
    print("PASS result_row_echo_wiring every row echo routed through helper")


def test_resolve_telnet_password_paths() -> None:
    """Operator-provided wins verbatim; --no-server attach falls back to the
    documented lab default (the running dedicated's config was written by
    someone else); servers this orchestrator starts get an ephemeral secret,
    unique per run and never equal to the published default."""
    resolve = playtest_run.resolve_telnet_password
    legacy = playtest_run.LEGACY_TELNET_PASSWORD

    assert resolve("operator-pw", no_server=False) == "operator-pw"
    assert resolve("operator-pw", no_server=True) == "operator-pw"

    assert resolve("", no_server=True) == legacy, (
        "attach mode without env must use the documented lab default"
    )

    generated = [resolve("", no_server=False) for _ in range(2)]
    assert all(pw != legacy for pw in generated), (
        "the static published default must never serve as the run password"
    )
    assert len(set(generated)) == 2, "generated secrets must differ per call"
    for pw in generated:
        # Command-safe alphabet (token_urlsafe): survives the generated XML
        # attribute and the telnet wire unescaped.
        assert re.fullmatch(r"[A-Za-z0-9_-]{10,40}", pw), f"bad shape: {pw!r}"
    print("PASS telnet_password_resolution operator/attach/generated split")


def test_write_stock_config_restricts_file_mode() -> None:
    """The generated serverconfig carries TelnetPassword: it must not inherit
    a world-readable umask."""
    src = (
        "<ServerSettings>\n"
        '  <property name="TelnetPassword" value="old"/>\n'
        "</ServerSettings>\n"
    )
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src_cfg = tdp / "serverconfig.xml"
        out_cfg = tdp / "out" / "serverconfig_playtest.xml"
        src_cfg.write_text(src, encoding="utf-8")
        errbuf = io.StringIO()
        with contextlib.redirect_stderr(errbuf):
            playtest_run.write_stock_config(
                src_cfg,
                out_cfg,
                tdp / "userdata",
                world_name="Navezgane",
                game_name="PlaytestNav",
                port=26900,
                telnet_port=8081,
                telnet_password="pw",
            )
        mode = out_cfg.stat().st_mode & 0o777
        assert mode == 0o600, f"generated serverconfig mode {oct(mode)}, want 0o600"
    print("PASS stock_config_permissions generated config is user-only")


def test_client_install_is_discovered_from_steam_libraries() -> None:
    """A library on another disk is read out of Steam's own catalogue.

    Hardcoding a library is what sends a run at the wrong install, or at none:
    the launcher then exits with "Game not found" into a log the orchestrator
    is not reading yet, and the run spends its whole timeout waiting.
    """
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        native = home / ".local/share/Steam/steamapps"
        (native / "common").mkdir(parents=True)
        game = home / "second-disk/Steam/steamapps/common/7 Days To Die"
        game.mkdir(parents=True)
        (game / playtest_run.CLIENT_EXECUTABLE).write_text("", encoding="utf-8")
        library = home / "second-disk/Steam"
        (native / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"' + str(library) + '"\n\t}\n}\n',
            encoding="utf-8",
        )
        found = playtest_run.client_game_dir(env={}, home=home)
        assert found == game, f"expected {game}, got {found}"
    print("PASS client_install_discovery found via libraryfolders.vdf")


def test_no_client_install_is_a_refusal_not_a_guess() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        (home / ".local/share/Steam/steamapps/common").mkdir(parents=True)
        assert playtest_run.client_game_dir(env={}, home=home) is None
    print("PASS client_install_refusal no install found reports None")


def test_game_env_wins_over_discovery() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        explicit = home / "explicit"
        found = playtest_run.client_game_dir(env={"GAME": str(explicit)}, home=home)
        assert found == explicit, found
    print("PASS client_install_env_wins GAME overrides discovery")


def test_client_compat_follows_the_install_library() -> None:
    """The prefix belongs to the library the client is in, not to a fixed one."""
    game = pathlib.Path("/data/games/Steam/steamapps/common/7 Days To Die")
    compat = playtest_run.client_compat_for_game(game, env={})
    expected = pathlib.Path(
        "/data/games/Steam/steamapps/compatdata/" + playtest_run.STEAM_APPID
    )
    assert compat == expected, compat
    override = pathlib.Path("/elsewhere/prefix")
    assert playtest_run.client_compat_for_game(game, env={"COMPAT": str(override)}) == override
    print("PASS client_compat_follows_library prefix derived from the library")

def test_write_stock_config_activates_commented_userdata_folder() -> None:
    """Stock ships UserDataFolder commented out. Rewriting the value inside
    that comment leaves the server saving under its default tree, so
    --fresh-save wiped nothing and state carried over between runs (the
    regression documented at the write_stock_config call site). Both shapes
    must yield an ACTIVE property pointing at the resolved --userdata."""
    ud = "userdata"

    commented_only = (
        "<ServerSettings>\n"
        '  <!-- <property name="UserDataFolder" value="/default/7DaysToDie"/> -->\n'
        '  <property name="GameWorld" value="Navezgane"/>\n'
        "</ServerSettings>\n"
    )
    active_stale = (
        "<ServerSettings>\n"
        '  <property name="UserDataFolder" value="/stale/path"/>\n'
        '  <property name="GameWorld" value="Navezgane"/>\n'
        "</ServerSettings>\n"
    )
    with tempfile.TemporaryDirectory() as td_str:
        for label, src in (
            ("commented-out", commented_only),
            ("active-stale", active_stale),
        ):
            side = Path(td_str) / label
            side.mkdir()
            src_cfg = side / "serverconfig.xml"
            src_cfg.write_text(src, encoding="utf-8")
            out_cfg = side / "out" / "serverconfig_playtest.xml"
            userdata = side / ud
            playtest_run.write_stock_config(
                src_cfg,
                out_cfg,
                userdata,
                world_name="Navezgane",
                game_name="PlaytestNav",
                port=26900,
                telnet_port=8081,
                telnet_password="pw",
            )
            want_ud = str(userdata.resolve())
            root = ElementTree.parse(out_cfg).getroot()
            props = {p.get("name"): p.get("value") for p in root.iter("property")}
            assert props.get("UserDataFolder") == want_ud, (
                f"{label}: UserDataFolder must be active and point at "
                f"{want_ud}, got {props.get('UserDataFolder')!r}"
            )
            assert '<!-- <property name="UserDataFolder"' not in out_cfg.read_text(
                encoding="utf-8"
            ), f"{label}: commented form survived next to the active property"
    print("PASS stock_config_userdata_folder commented form activated, stale value rewritten")


def test_write_stock_config_unreadable_template_names_the_file() -> None:
    """A template that exists but cannot be decoded (UTF-16 editor save) or
    read (permissions) must raise a named error carrying the path and cause,
    not a bare UnicodeDecodeError/OSError traceback after --fresh-save."""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src_cfg = tdp / "serverconfig.xml"
        # UTF-16-encoded bytes are not valid UTF-8: read_text raises
        # UnicodeDecodeError, the realistic user-edit failure shape.
        src_cfg.write_bytes(
            "<ServerSettings/>\n".encode("utf-16")
        )
        try:
            playtest_run.write_stock_config(
                src_cfg,
                tdp / "out" / "serverconfig_playtest.xml",
                tdp / "userdata",
                world_name="Navezgane",
                game_name="PlaytestNav",
                port=26900,
                telnet_port=8081,
                telnet_password="pw",
            )
        except RuntimeError as ex:
            assert str(src_cfg) in str(ex), f"path missing from error: {ex}"
            assert "cannot read serverconfig template" in str(ex)
        except Exception as ex:
            raise AssertionError(f"wrong error type: {type(ex).__name__}: {ex}") from ex
        else:
            raise AssertionError("undecodable template accepted silently")


def test_write_stock_config_unwritable_output_names_the_file() -> None:
    """An output path that cannot be created (parent exists as a file) must
    raise a named RuntimeError carrying the destination and cause, not a bare
    OSError traceback: this fires after --fresh-save already moved the save
    aside, mirroring the read-side guard right above it."""
    src = (
        "<ServerSettings>\n"
        '  <property name="TelnetPassword" value="old"/>\n'
        "</ServerSettings>\n"
    )
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src_cfg = tdp / "serverconfig.xml"
        src_cfg.write_text(src, encoding="utf-8")
        blocker = tdp / "out"
        blocker.write_text("a file where the output dir must go", encoding="utf-8")
        out_cfg = blocker / "serverconfig_playtest.xml"
        try:
            playtest_run.write_stock_config(
                src_cfg,
                out_cfg,
                tdp / "userdata",
                world_name="Navezgane",
                game_name="PlaytestNav",
                port=26900,
                telnet_port=8081,
                telnet_password="pw",
            )
        except RuntimeError as ex:
            assert str(out_cfg) in str(ex), f"destination missing from error: {ex}"
            assert "cannot write generated serverconfig" in str(ex)
        except Exception as ex:
            raise AssertionError(f"wrong error type: {type(ex).__name__}: {ex}") from ex
        else:
            raise AssertionError("unwritable generated config accepted silently")
    print(
        "PASS stock_config_unwritable_output named RuntimeError carries destination"
    )


def test_wait_stock_ready_early_exit_survives_unreadable_log() -> None:
    """When the dedicated exits before StartGame done, the diagnostic tail of
    its unity log is best-effort: a readable log must be echoed scrubbed,
    and an unreadable one (rotation, EIO, permissions) must warn and still
    return False instead of raising out of main()'s startup path."""
    class DeadProc:
        returncode = 137

        def poll(self) -> int:
            return 137

    orig_timeout = playtest_run.STOCK_READY_TIMEOUT_SEC
    playtest_run.STOCK_READY_TIMEOUT_SEC = 0.05
    try:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            unity_log = tdp / "server_playtest.txt"

            # Readable log: tail reaches stderr, verdict is False.
            unity_log.write_text(
                "line one\nERROR: world load failed\nline three\n",
                encoding="utf-8",
            )
            errbuf = io.StringIO()
            with contextlib.redirect_stderr(errbuf):
                ready = playtest_run.wait_stock_dedicated_ready(DeadProc(), unity_log)  # type: ignore[arg-type]
            assert ready is False, "exited-early dedicated must not read as ready"
            errs = errbuf.getvalue()
            assert "exited early code=137" in errs, errs
            assert "tail server log" in errs and "world load failed" in errs, errs

            # Unreadable log: warn + same False verdict, no exception.
            if os.geteuid() != 0:
                unity_log.chmod(0o000)
                try:
                    errbuf = io.StringIO()
                    with contextlib.redirect_stderr(errbuf):
                        ready = playtest_run.wait_stock_dedicated_ready(DeadProc(), unity_log)  # type: ignore[arg-type]
                    assert ready is False
                    assert "could not read server log tail" in errbuf.getvalue(), (
                        errbuf.getvalue()
                    )
                finally:
                    unity_log.chmod(0o644)
    finally:
        playtest_run.STOCK_READY_TIMEOUT_SEC = orig_timeout
    print("PASS stock_ready_unreadable_log tail read is best-effort, verdict stands")


def test_acquire_exclusive_lock_undoes_published_claim_on_interrupt() -> None:
    """A signal-driven SystemExit escaping after the claim was published must
    not leave it standing: main() has not set lock_held yet, so its finally
    would skip release and the orphan claim would sit unheartbeated until
    the stale window passes, blocking every other agent."""
    with tempfile.TemporaryDirectory(prefix="playtest-acq-") as td:
        lock = Path(td) / "playtest_running"
        sid = "grok-20260810-231500-a1b2c3d4e5f6"

        # Captured before patching so the fake can publish a real claim
        # without recursing into itself.
        real_acquire = pl.acquire

        def publish_then_die(
            session: str,
            *,
            path: Path | None = None,
            live_probe: Callable[[], bool] | None = None,
            max_age_sec: float | None = None,
            env: pl.LockEnv | None = None,
        ) -> pl.LockState:
            state = real_acquire(session, path=path, live_probe=live_probe)
            if not (state.running and state.session == session):
                raise AssertionError(f"fake acquire failed to publish: {state}")
            # Model the signal landing after publication but before main()
            # records lock_held.
            raise SystemExit(128 + 15)

        orig = pl.acquire
        pl.acquire = publish_then_die
        raised = False
        try:
            try:
                playtest_run.acquire_exclusive_lock(sid, lock)
            except SystemExit:
                raised = True
        finally:
            pl.acquire = orig
        assert raised, "interrupt must propagate out of acquire_exclusive_lock"
        state = pl.read_lock(lock)
        assert not state.running and state.session is None, (
            f"published claim survived interrupt: {state}"
        )
    print("PASS acquire_exclusive_lock_undo interrupt releases published claim")


def test_acquire_exclusive_lock_refusal_leaves_foreign_record() -> None:
    """A refused acquire fails like a bare acquire and leaves the foreign
    record exactly as it was: the interrupt-undo release refuses to write
    for a session the file does not name."""
    with tempfile.TemporaryDirectory(prefix="playtest-acqr-") as td:
        lock = Path(td) / "playtest_running"
        owner = "owner-20260810-000000-aaaaaaaaaaaa"
        other = "other-20260810-000001-bbbbbbbbbbbb"
        pl.acquire(owner, path=lock, live_probe=lambda: False)
        try:
            playtest_run.acquire_exclusive_lock(other, lock)
            raise AssertionError("foreign acquire must refuse")
        except pl.PlaytestLockError as ex:
            assert ex.reason == "foreign_holder", f"reason={ex.reason}"
        state = pl.read_lock(lock)
        assert state.running and state.session == owner, (
            f"refusal disturbed the foreign record: {state}"
        )
    print("PASS acquire_exclusive_lock_refusal foreign record untouched")


def test_acquire_exclusive_lock_marks_held_inside_guarded_region() -> None:
    """mark_held must run inside the undo-release guard, before the wrapper
    returns: main's lock_held flag has to flip in the same region that
    releases on interrupt, or a signal landing between publication and the
    flag write would exit through a finally that still sees lock_held=False
    and strand the fresh claim unheartbeated."""
    with tempfile.TemporaryDirectory(prefix="playtest-acqm-") as td:
        lock = Path(td) / "playtest_running"
        sid = "grok-20260810-231500-a1b2c3d4e5f6"
        marks: list[int] = []
        playtest_run.acquire_exclusive_lock(
            sid, lock, mark_held=lambda: marks.append(1)
        )
        assert marks == [1], f"successful publish must mark held once: {marks}"
        state = pl.read_lock(lock)
        assert state.running and state.session == sid, (
            f"successful acquire must leave the claim standing: {state}"
        )

    # Refusal: the flag must stay down so main's finally keeps skipping
    # process teardown for a lock we never held.
    with tempfile.TemporaryDirectory(prefix="playtest-acqn-") as td:
        lock = Path(td) / "playtest_running"
        owner = "owner-20260810-000000-aaaaaaaaaaaa"
        other = "other-20260810-000001-bbbbbbbbbbbb"
        pl.acquire(owner, path=lock, live_probe=lambda: False)
        marks = []
        try:
            playtest_run.acquire_exclusive_lock(
                other, lock, mark_held=lambda: marks.append(1)
            )
            raise AssertionError("foreign acquire must refuse")
        except pl.PlaytestLockError:
            pass
        assert marks == [], f"refused acquire must not mark held: {marks}"
    print("PASS acquire_exclusive_lock_mark_held flag flips inside the guard")


def test_block_termination_signals_blocks_then_restores() -> None:
    """The finally's first action must mask TERM/HUP on this thread so a
    first-ever signal landing between cleanup entry and the SIG_IGN disarm
    is pended instead of raising SystemExit mid-teardown; the helper must be
    transparent about the prior mask so tests (and any future caller) can
    restore it."""
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    playtest_run._block_termination_signals()
    try:
        after = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            assert sig in after, f"{name} not blocked by the teardown mask"
            assert sig not in before, f"{name} unexpectedly blocked beforehand"
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
    print("PASS block_termination_signals masks TERM/HUP until restored")


def test_rewrite_platform_cfg_backs_up_once_and_forces_surface() -> None:
    """start_stock_dedicated rewrites the user's platform.cfg in place: the
    backup must carry the original bytes and be written exactly once (it is
    the only copy of the user's config), the live file must end at the forced
    local-auth surface, and the atomic publish must leave no temp droppings."""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        pcfg = tdp / "platform.cfg"
        original = b"platform=EAC\ncrossplatform=EOS\n"
        pcfg.write_bytes(original)

        playtest_run._rewrite_platform_cfg(pcfg)

        bak = pcfg.with_name(pcfg.name + ".playtest-bak")
        assert bak.is_file(), "first rewrite must create the backup"
        assert bak.read_bytes() == original, "backup must hold the untouched original"
        forced = b"platform=Steam\ncrossplatform=None\nserverplatforms=Steam,LAN,Local,\n"
        assert pcfg.read_bytes() == forced, f"forced surface drifted: {pcfg.read_bytes()!r}"

        # Backup-once: a second run (already-forced live content) must never
        # overwrite the only copy of the user's original.
        playtest_run._rewrite_platform_cfg(pcfg)
        assert bak.read_bytes() == original, "backup was overwritten on the second run"
        assert pcfg.read_bytes() == forced

        leftovers = sorted(
            p.name for p in tdp.iterdir()
            if p.name not in {pcfg.name, bak.name}
        )
        assert not leftovers, f"temp files leaked by the atomic write: {leftovers}"
    print("PASS platform_cfg_rewrite backup once, forced surface, no temp files")


def test_client_mute_env_contract() -> None:
    """CLIENT_MUTE defaults on and only the documented off-spellings disable
    it; PLAYTEST_MUTE / SEVEN_DAYS_TO_DIE_CLIENT_MUTE are fallbacks in order.
    The mute helper silences a real player's audio session when this
    misreads, so pin the behavior, not just the source text."""
    names = ("CLIENT_MUTE", "PLAYTEST_MUTE", "SEVEN_DAYS_TO_DIE_CLIENT_MUTE")
    saved = {n: os.environ.get(n) for n in names}
    try:
        for n in names:
            os.environ.pop(n, None)
        assert playtest_run.client_mute_enabled() is True, "default must be muted"
        for off in ("0", "false", "No", "OFF", " off "):
            os.environ["CLIENT_MUTE"] = off
            assert playtest_run.client_mute_enabled() is False, f"CLIENT_MUTE={off!r}"
            os.environ["CLIENT_MUTE"] = "1"
        for on in ("1", "true", ""):
            os.environ["CLIENT_MUTE"] = on
            assert playtest_run.client_mute_enabled() is True, f"CLIENT_MUTE={on!r}"

        # First set name wins: an opt-out beats a later alias's default-on,
        # and each alias disables on its own when the earlier ones are unset.
        os.environ["CLIENT_MUTE"] = "0"
        os.environ["PLAYTEST_MUTE"] = "1"
        assert playtest_run.client_mute_enabled() is False, "CLIENT_MUTE lost to alias"
        os.environ.pop("CLIENT_MUTE")
        assert playtest_run.client_mute_enabled() is True, "PLAYTEST_MUTE=1 must keep mute"
        os.environ["PLAYTEST_MUTE"] = "0"
        assert playtest_run.client_mute_enabled() is False, "alias opt-out ignored"
        os.environ.pop("PLAYTEST_MUTE")
        os.environ["SEVEN_DAYS_TO_DIE_CLIENT_MUTE"] = "off"
        assert playtest_run.client_mute_enabled() is False, "legacy alias opt-out ignored"
    finally:
        for n, old in saved.items():
            if old is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = old
    print("PASS client_mute_env_contract default on, documented opt-outs, alias order")


def test_write_zdtd_apm_dump_fails_closed_without_markers() -> None:
    """The APM evidence writer must never invent a live snapshot: only output
    carrying real zdtd markers becomes a dump (prefixed with run_id for
    correlation); marker-less or empty output writes the failure sentinel so
    the client cannot soft-pass synthetic data, and a missing binary writes
    nothing at all."""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        dump_path = tdp / "apm" / "dump.txt"

        def fake_zdtd(body: str) -> Path:
            exe = tdp / "zdtd"
            exe.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}EOF\n", encoding="utf-8")
            exe.chmod(0o755)
            return exe

        # Live markers: dump written, run_id prefixed, verdict True.
        exe = fake_zdtd("zdtd-apm tick_total=3 wall_ns=100\n")
        assert playtest_run.write_zdtd_apm_dump(
            exe, tdp / "world", tdp / "srv", dump_path, run_id="run-42"
        )
        assert dump_path.read_text(encoding="utf-8") == (
            "run_id=run-42\nzdtd-apm tick_total=3 wall_ns=100\n"
        ), dump_path.read_text(encoding="utf-8")

        # No markers: fail closed with the sentinel, not synthetic evidence.
        dump_path.unlink()
        exe = fake_zdtd("boot noise only\n")
        assert not playtest_run.write_zdtd_apm_dump(
            exe, tdp / "world", tdp / "srv", dump_path
        ), "marker-less output must not read as an APM snapshot"
        assert dump_path.read_text(encoding="utf-8") == (
            "APM_DUMP_FAILED no markers from zdtd --ticks\n"
        )

        # Missing binary: warn + False, nothing is written anywhere.
        dump_path.unlink()
        assert not playtest_run.write_zdtd_apm_dump(
            tdp / "absent-zdtd", tdp / "world", tdp / "srv", dump_path
        )
        assert not dump_path.exists(), "a failed run must leave no dump behind"
    print("PASS apm_dump_fail_closed markers required, sentinel on miss")


def main() -> int:
    failures = 0
    for name, fn in (
        ("loadgen_events", test_loadgen_structured_events_and_expectations),
        (
            "loadgen_expectations_non_finite",
            test_loadgen_expectations_reject_non_finite_values,
        ),
        ("loadgen_observer_wiring", test_loadgen_observer_wiring_is_generic),
        ("loadgen_stale_rebuild", test_loadgen_rebuilds_when_source_is_newer),
        ("fresh_save_named_only", test_fresh_save_removes_only_named_game_saves),
        ("fresh_save_no_saves_dir", test_fresh_save_without_saves_dir_is_noop),
        ("fresh_save_quarantine", test_fresh_save_quarantines_named_saves_recoverably),
        (
            "fresh_save_quarantine_unavailable",
            test_fresh_save_unusable_quarantine_keeps_data_in_place,
        ),
        ("fresh_zdtd_world", test_fresh_zdtd_world_moves_state_and_overlays_recoverably),
        ("prune_quarantine", test_prune_quarantine_keeps_newest_entries),
        (
            "prune_run_artifacts",
            test_prune_run_artifacts_keeps_newest_per_pattern,
        ),
        ("prune_run_artifacts_wiring", test_prune_run_artifacts_wired_into_main),
        ("snapshot_previous_log", test_snapshot_previous_log_copies_before_truncate),
        ("fixture_gate_selection", test_suite_wants_host_fixtures_selection_table),
        ("fixture_gate_catalog_surface", test_fixture_gate_covers_every_barrier_emitting_suite),
        ("fixture_gate_alias_surface", test_fixture_gate_covers_every_expand_suites_alias),
        ("barrier_tables_pair", test_new_barrier_tables_fresh_pair_per_generation),
        ("stop_proc_sigkill_reap", test_stop_proc_reaps_after_sigkill_escalation),
        ("stop_proc_exited_child", test_stop_proc_exited_child_closes_log_handle),
        (
            "loadgen_peer_rebind_reap",
            test_loadgen_peer_rebind_reaps_exited_instance,
        ),
        ("reap_finished_helpers", test_reap_finished_helpers_drops_only_exited),
        ("main_finally_reap_helpers", test_main_finally_reaps_mute_helpers),
        ("wait_file_contains", test_wait_file_contains_incremental),
        ("client_install_discovery", test_client_install_is_discovered_from_steam_libraries),
        ("client_install_refusal", test_no_client_install_is_a_refusal_not_a_guess),
        ("client_install_env_wins", test_game_env_wins_over_discovery),
        ("client_compat_follows_library", test_client_compat_follows_the_install_library),
        ("timeout_validation", test_positive_seconds_type_and_env_reader),
        ("tcp_port_range", test_tcp_port_type_range),
        ("litenet_port_room", test_litenet_port_room_guard),
        ("default_port_preflight", test_main_default_port_reaches_preflight_refusal),
        ("config_summary_redaction", test_config_summary_redacts_telnet_password),
        ("telnet_admin_parsing", test_telnet_admin_ai_and_player_parsing),
        (
            "telnet_broken_session",
            test_telnet_broken_session_degrades_to_empty_reply,
        ),
        (
            "telnet_recv_scrub",
            test_telnet_recv_scrubs_control_chars,
        ),
        (
            "spawn_trust_only_live_sessions",
            test_spawn_near_players_trusts_only_live_sessions,
        ),
        (
            "stock_ready_unreadable_log",
            test_wait_stock_ready_early_exit_survives_unreadable_log,
        ),
        ("barrier_param_validation", test_safe_barrier_param_rejects_command_shapes),
        (
            "barrier_param_wiring",
            test_safe_barrier_param_gates_both_telnet_handlers,
        ),
        ("log_scrub", test_scrub_strips_control_chars_from_echoed_log_text),
        (
            "result_row_echo",
            test_result_echo_line_scrubs_parsed_rows,
        ),
        (
            "result_row_echo_wiring",
            test_result_row_echoes_all_routed_through_helper,
        ),
        ("telnet_password_resolution", test_resolve_telnet_password_paths),
        ("stock_config_permissions", test_write_stock_config_restricts_file_mode),
        (
            "stock_config_userdata_folder",
            test_write_stock_config_activates_commented_userdata_folder,
        ),
        (
            "stock_config_unreadable_template",
            test_write_stock_config_unreadable_template_names_the_file,
        ),
        (
            "stock_config_unwritable_output",
            test_write_stock_config_unwritable_output_names_the_file,
        ),
        (
            "acquire_exclusive_lock_undo",
            test_acquire_exclusive_lock_undoes_published_claim_on_interrupt,
        ),
        (
            "acquire_exclusive_lock_refusal",
            test_acquire_exclusive_lock_refusal_leaves_foreign_record,
        ),
        (
            "acquire_exclusive_lock_mark_held",
            test_acquire_exclusive_lock_marks_held_inside_guarded_region,
        ),
        (
            "block_termination_signals",
            test_block_termination_signals_blocks_then_restores,
        ),
        (
            "platform_cfg_rewrite",
            test_rewrite_platform_cfg_backs_up_once_and_forces_surface,
        ),
        ("client_mute_env_contract", test_client_mute_env_contract),
        (
            "apm_dump_fail_closed",
            test_write_zdtd_apm_dump_fails_closed_without_markers,
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
