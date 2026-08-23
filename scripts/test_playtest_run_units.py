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
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import time
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


def _spawn_detached(body: str) -> subprocess.Popen:
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
    fh = tempfile.TemporaryFile()
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
        assert playtest_run._MUTE_HELPER_PROCS == [live], (
            "exited helpers must be reaped away; live ones kept"
        )
    finally:
        playtest_run._MUTE_HELPER_PROCS[:] = saved
        live.kill()
        live.wait()
    print("PASS reap_finished_helpers exited helpers reaped, live ones kept")


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

    zdtd = argparse.Namespace(**{**vars(args), "server": "zdtd"})
    assert "world_name" not in playtest_run.config_summary(zdtd), (
        "stock-only GameName must not masquerade as the zdtd world"
    )
    peer = argparse.Namespace(**{**vars(args), "peer_client_name": "atomic-peer"})
    assert "peer=atomic-peer" in playtest_run.config_summary(peer)
    print("PASS config_summary_redaction effective options logged, password redacted")


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


def main() -> int:
    failures = 0
    for name, fn in (
        ("fresh_save_named_only", test_fresh_save_removes_only_named_game_saves),
        ("fresh_save_no_saves_dir", test_fresh_save_without_saves_dir_is_noop),
        ("fresh_save_quarantine", test_fresh_save_quarantines_named_saves_recoverably),
        (
            "fresh_save_quarantine_unavailable",
            test_fresh_save_unusable_quarantine_keeps_data_in_place,
        ),
        ("fresh_zdtd_world", test_fresh_zdtd_world_moves_state_and_overlays_recoverably),
        ("prune_quarantine", test_prune_quarantine_keeps_newest_entries),
        ("snapshot_previous_log", test_snapshot_previous_log_copies_before_truncate),
        ("fixture_gate_selection", test_suite_wants_host_fixtures_selection_table),
        ("fixture_gate_catalog_surface", test_fixture_gate_covers_every_barrier_emitting_suite),
        ("stop_proc_sigkill_reap", test_stop_proc_reaps_after_sigkill_escalation),
        ("stop_proc_exited_child", test_stop_proc_exited_child_closes_log_handle),
        ("reap_finished_helpers", test_reap_finished_helpers_drops_only_exited),
        ("timeout_validation", test_positive_seconds_type_and_env_reader),
        ("tcp_port_range", test_tcp_port_type_range),
        ("config_summary_redaction", test_config_summary_redacts_telnet_password),
        ("stock_config_permissions", test_write_stock_config_restricts_file_mode),
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
