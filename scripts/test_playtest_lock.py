#!/usr/bin/env python3
"""Unit tests for shipped scripts/playtest_lock.py (no game launch)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import playtest_lock as pl  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_free_acquire_release(tmp: Path) -> None:
    lock = tmp / "playtest_running"
    sid = "grok-20260810-231500-a1b2c3d4e5f6"
    _assert(pl.can_start(sid, path=lock, live_probe=lambda: False), "can start free")
    state = pl.acquire(sid, path=lock, live_probe=lambda: False)
    _assert(state.running is True, "running after acquire")
    _assert(state.session == sid, "session recorded")
    text = lock.read_text(encoding="utf-8")
    _assert("running=yes" in text, "payload running=yes")
    _assert(f"session={sid}" in text, "payload session")
    _assert("heartbeat=" in text, "payload heartbeat")
    _assert("acquired=" in text, "payload acquired")
    _assert(state.heartbeat is not None, "state.heartbeat set")
    _assert(not pl.is_stale(state, max_age_sec=120), "fresh lock not stale")
    _assert(
        pl.SESSION_RE.match(sid) is not None,
        "session matches documented shape",
    )
    # Foreign refuse
    other = "codex-20260810-120000-deadbeefcafe"
    _assert(
        not pl.can_start(other, path=lock, live_probe=lambda: False),
        "foreign cannot start",
    )
    try:
        pl.acquire(other, path=lock, live_probe=lambda: False)
        raise AssertionError("foreign acquire should raise")
    except pl.PlaytestLockError as e:
        _assert(e.held_by == sid, f"held_by should be owner, got {e.held_by}")
        _assert(e.reason == "foreign_holder", f"reason={e.reason}")
    # Foreign release refused; file unchanged
    try:
        pl.release(other, path=lock)
        raise AssertionError("foreign release should raise")
    except pl.PlaytestLockError as e:
        _assert(e.held_by == sid, "foreign release held_by")
    _assert(pl.read_lock(lock).running is True, "still held after foreign release")
    # Owner re-entrant + release
    pl.acquire(sid, path=lock, live_probe=lambda: False)
    free = pl.release(sid, path=lock)
    _assert(free.running is False, "free after release")
    _assert(lock.read_text(encoding="utf-8").strip() == "running=no", "payload no")


def test_live_client_blocks_free_lock(tmp: Path) -> None:
    lock = tmp / "playtest_running"
    sid = "claude-20260810-010203-aabbccddeeff"
    try:
        pl.acquire(sid, path=lock, live_probe=lambda: True)
        raise AssertionError("live runtime must block free acquire")
    except pl.PlaytestLockError as e:
        _assert(e.reason == "live_runtime", f"reason={e.reason}")
    _assert(not lock.is_file() or pl.read_lock(lock).running is False, "still free")
    _assert(
        not pl.can_start(sid, path=lock, live_probe=lambda: True),
        "can_start false when live",
    )


def test_runtime_patterns_include_server() -> None:
    _assert(
        "7DaysToDieServer.x86_64" in pl.STOCK_SERVER_EXECUTABLES,
        "stock dedicated in runtime probe",
    )
    _assert("zdtd" in pl.STOCK_SERVER_EXECUTABLES, "zdtd in runtime probe")
    _assert(
        "7DaysToDie.exe" in pl.STOCK_CLIENT_EXECUTABLES,
        "client in runtime probe",
    )
    # Structural: tcp_port_in_use is real (binds ephemeral, expects free high port free)
    _assert(
        pl.tcp_port_in_use(1) is False or isinstance(pl.tcp_port_in_use(65530), bool),
        "tcp_port_in_use returns bool",
    )


def _fake_process(proc_root: Path, pid: str, exe: str, cmdline: str = "") -> None:
    process = proc_root / pid
    process.mkdir()
    (process / "exe").symlink_to(exe)
    (process / "cmdline").write_bytes(cmdline.encode("utf-8"))


def test_runtime_detection_checks_executables_not_shell_text(tmp: Path) -> None:
    proc = tmp / "proc"
    proc.mkdir(parents=True)
    # An agent shell can cite both runtime paths without becoming the runtime.
    _fake_process(
        proc,
        "100",
        "/usr/bin/bash",
        "bash\0echo 7DaysToDieServer.x86_64 zig-out/bin/zdtd 7DaysToDie.exe\0",
    )
    _assert(
        not pl._any_executable_running(pl.STOCK_SERVER_EXECUTABLES, proc_root=proc),
        "shell text must not count as a server",
    )
    _assert(
        not pl._any_preloader_running_game(proc_root=proc),
        "shell text must not count as a client",
    )

    _fake_process(proc, "101", "/games/7DaysToDieServer.x86_64")
    _fake_process(proc, "102", "/games/zdtd")
    _fake_process(
        proc,
        "103",
        "/usr/bin/wine64-preloader",
        "wine64-preloader\0Z:\\\\game\\\\7DaysToDie.exe\0",
    )
    _assert(
        pl._any_executable_running(pl.STOCK_SERVER_EXECUTABLES, proc_root=proc),
        "real dedicated or zdtd executable must count",
    )
    _assert(
        pl._any_preloader_running_game(proc_root=proc),
        "Wine preloader with game argument must count as client",
    )


def test_owner_reacquire_with_live_client(tmp: Path) -> None:
    lock = tmp / "playtest_running"
    sid = "playtest-20260810-111111-001122334455"
    pl.acquire(sid, path=lock, live_probe=lambda: False)
    # Holder may refresh while their client is up
    pl.acquire(sid, path=lock, live_probe=lambda: True)
    _assert(pl.read_lock(lock).session == sid, "still owner")
    pl.release(sid, path=lock)


def test_atomic_contention(tmp: Path) -> None:
    """Two threads: at most one foreign-free acquire succeeds as first holder."""
    lock = tmp / "playtest_running"
    winners: list[str] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        try:
            pl.acquire(name, path=lock, live_probe=lambda: False)
            winners.append(name)
        except pl.PlaytestLockError as e:
            errors.append(f"{name}:{e.reason}:{e.held_by}")

    a = "agenta-20260810-000001-aaaaaaaaaaaa"
    b = "agentb-20260810-000002-bbbbbbbbbbbb"
    t1 = threading.Thread(target=worker, args=(a,))
    t2 = threading.Thread(target=worker, args=(b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    _assert(len(winners) == 1, f"exactly one winner, got {winners!r} errors={errors!r}")
    _assert(len(errors) == 1, f"exactly one refuse, got {errors!r}")
    holder = pl.read_lock(lock).session
    _assert(holder == winners[0], "file holder matches winner")
    pl.release(holder, path=lock)


def test_env_override_path(tmp: Path) -> None:
    lock = tmp / "from-env" / "playtest_running"
    old = os.environ.get("PLAYTEST_LOCK_FILE")
    os.environ["PLAYTEST_LOCK_FILE"] = str(lock)
    try:
        _assert(pl.default_lock_path() == lock, "env path honored")
        sid = pl.new_session_id("grok")
        _assert(pl.SESSION_RE.match(sid), f"generated session shape: {sid}")
        pl.acquire(sid, live_probe=lambda: False)
        _assert(lock.is_file(), "wrote env path")
        pl.release(sid)
    finally:
        if old is None:
            os.environ.pop("PLAYTEST_LOCK_FILE", None)
        else:
            os.environ["PLAYTEST_LOCK_FILE"] = old


def test_heartbeat_and_stale_takeover(tmp: Path) -> None:
    lock = tmp / "playtest_running"
    owner = "owner-20260810-000000-aaaaaaaaaaaa"
    other = "other-20260810-000001-bbbbbbbbbbbb"
    pl.acquire(owner, path=lock, live_probe=lambda: False)
    # Force an old heartbeat then touch — second resolution UTC does not move in 50ms.
    pl.write_lock(
        lock,
        running=True,
        session=owner,
        acquired="2020-01-01T00:00:00Z",
        heartbeat="2020-01-01T00:00:00Z",
    )
    touched = pl.heartbeat(owner, path=lock)
    _assert(touched.heartbeat != "2020-01-01T00:00:00Z", "heartbeat advances")
    _assert(touched.session == owner, "heartbeat keeps owner")

    # Backdate heartbeat far into the past → stale.
    pl.write_lock(
        lock,
        running=True,
        session=owner,
        acquired="2020-01-01T00:00:00Z",
        heartbeat="2020-01-01T00:00:00Z",
    )
    st = pl.read_lock(lock)
    _assert(pl.is_stale(st, max_age_sec=60), "old heartbeat is stale")
    _assert(
        pl.can_start(other, path=lock, live_probe=lambda: False, max_age_sec=60),
        "can start when foreign lock is stale and no client",
    )
    # Live client blocks takeover even when stale.
    _assert(
        not pl.can_start(other, path=lock, live_probe=lambda: True, max_age_sec=60),
        "stale + live client cannot start",
    )
    try:
        pl.acquire(other, path=lock, live_probe=lambda: True, max_age_sec=60)
        raise AssertionError("stale+live must refuse")
    except pl.PlaytestLockError as e:
        _assert(e.reason == "stale_but_live", f"reason={e.reason}")

    # Takeover when stale and no live client.
    taken = pl.acquire(other, path=lock, live_probe=lambda: False, max_age_sec=60)
    _assert(taken.session == other, "stale takeover by other")
    _assert(not pl.is_stale(taken, max_age_sec=60), "new owner heartbeat fresh")
    pl.release(other, path=lock)


def test_playtest_run_wiring() -> None:
    """Structural: orchestrator acquires before clean_processes and releases."""
    src = (SCRIPTS / "playtest_run.py").read_text(encoding="utf-8")
    _assert("import playtest_lock" in src, "imports playtest_lock")
    _assert("playtest_lock.acquire" in src, "calls playtest_lock.acquire")
    _assert("playtest_lock.release" in src, "calls playtest_lock.release")
    _assert("HeartbeatThread" in src, "starts heartbeat thread")
    main_i = src.find("def main(")
    _assert(main_i >= 0, "main defined")
    acq_in_main = src.find("playtest_lock.acquire", main_i)
    clean_in_main = src.find("clean_processes(", main_i)
    _assert(
        0 <= acq_in_main < clean_in_main,
        f"acquire (pos {acq_in_main}) must precede clean_processes "
        f"(pos {clean_in_main}) in main",
    )
    finally_i = src.rfind("finally:")
    rel_i = src.find("playtest_lock.release", finally_i if finally_i >= 0 else 0)
    _assert(finally_i >= 0 and rel_i > finally_i, "release in finally block")
    # Foreign refuse must not pkill: process teardown is gated on lock_held
    fin_body = src[finally_i : rel_i + 80]
    _assert(
        "if lock_held" in fin_body or "if lock_held:" in src[finally_i:],
        "finally process teardown must be gated on lock_held",
    )
    # pkill in finally should appear only under the lock_held branch
    pkill_after_finally = src.find("pkill_patterns", finally_i)
    lock_gate = src.find("if lock_held", finally_i)
    _assert(
        0 <= lock_gate < pkill_after_finally,
        "pkill_patterns in finally must sit under if lock_held",
    )


def test_heartbeat_thread_stop_before_start(tmp: Path) -> None:
    """stop() must be safe when start() never ran (or failed).

    Cleanup paths stop the heartbeat unconditionally; a RuntimeError from
    join-on-unstarted would abort the rest of the cleanup, including the
    lock release in playtest_run's finally.
    """
    lock = tmp / "playtest_running"
    th = pl.HeartbeatThread(
        "owner-20260810-000000-aaaaaaaaaaaa", path=lock, interval_sec=3600
    )
    th.stop()
    th.stop()  # idempotent
    _assert(not lock.is_file(), "no tick ran, so no lock file")


def test_sigterm_becomes_graceful_exit() -> None:
    """SIGTERM must convert to SystemExit so orchestrator cleanup unwinds.

    Default SIGTERM action kills without running finally: the detached
    runtime survives and a stale-but-live lock wedges exclusivity. The
    handler turns it into SystemExit(128+sig) instead.
    """
    code = (
        "import sys, time;"
        f"sys.path.insert(0, {str(SCRIPTS)!r});"
        "import playtest_run;"
        "playtest_run.install_signal_handlers();"
        "print('armed', flush=True);"
        "time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        line = proc.stdout.readline().strip()
        _assert(line == "armed", f"child did not arm handlers: {line!r}")
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    _assert(rc == 128 + signal.SIGTERM, f"exit {rc}, expected {128 + signal.SIGTERM}")


def main() -> int:
    fails = 0
    with tempfile.TemporaryDirectory(prefix="playtest-lock-") as td:
        tmp = Path(td)
        cases: list[tuple[str, object]] = [
            ("free_acquire_release", lambda: test_free_acquire_release(tmp / "free")),
            (
                "live_client_blocks",
                lambda: test_live_client_blocks_free_lock(tmp / "live"),
            ),
            ("runtime_patterns_include_server", test_runtime_patterns_include_server),
            (
                "runtime_detection_checks_executables_not_shell_text",
                lambda: test_runtime_detection_checks_executables_not_shell_text(
                    tmp / "runtime_detection"
                ),
            ),
            (
                "owner_reacquire_live",
                lambda: test_owner_reacquire_with_live_client(tmp / "owner"),
            ),
            ("atomic_contention", lambda: test_atomic_contention(tmp / "race")),
            ("env_override_path", lambda: test_env_override_path(tmp / "env")),
            (
                "heartbeat_and_stale_takeover",
                lambda: test_heartbeat_and_stale_takeover(tmp / "hb"),
            ),
            (
                "heartbeat_thread_stop_before_start",
                lambda: test_heartbeat_thread_stop_before_start(tmp / "hbstopped"),
            ),
            ("playtest_run_wiring", test_playtest_run_wiring),
            ("sigterm_becomes_graceful_exit", test_sigterm_becomes_graceful_exit),
        ]
        for name, fn in cases:
            try:
                if name != "playtest_run_wiring":
                    (tmp / name).mkdir(exist_ok=True)
                fn()  # type: ignore[operator]
                print(f"PASS {name}")
            except Exception as ex:  # noqa: BLE001 — report each test
                fails += 1
                print(f"FAIL {name}: {ex}", file=sys.stderr)

    if fails:
        print(f"RESULT FAIL ({fails})", file=sys.stderr)
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
