#!/usr/bin/env python3
"""Unit tests for shipped scripts/playtest_lock.py (no game launch)."""

from __future__ import annotations

import contextlib
import io
import os
import signal
import socket
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


def test_lock_gate_is_the_client_not_a_server(tmp: Path) -> None:
    """The lock covers what is scarce: one client, one display, one GPU.

    A managed stock run's dedicated belongs to its Safehouse instance, which
    holds a unique port block and refuses a second start of itself, so another
    instance's server must not block this run. Two server-only suites on
    disjoint instances are meant to run at once.
    """
    live_dedicated = tmp / "dedicated-only"
    _assert(
        pl.acquire(
            "a-20260901-000000-aaaaaaaaaaaa",
            path=live_dedicated,
            live_probe=lambda: False,
        ).running,
        "a run acquires while only a dedicated is up",
    )
    pl.release("a-20260901-000000-aaaaaaaaaaaa", path=live_dedicated)

    # The client is still exclusive: a live client refuses a fresh claim.
    blocked = tmp / "client-up"
    try:
        pl.acquire(
            "b-20260901-000000-bbbbbbbbbbbb",
            path=blocked,
            live_probe=lambda: True,
        )
    except pl.PlaytestLockError as ex:
        _assert(ex.reason == "live_runtime", f"want live_runtime, got {ex.reason}")
    else:
        raise AssertionError("a live client must refuse a fresh claim")

    # zdtd is still started by the orchestrator on a caller-chosen port, so its
    # gate is the union; the stock dedicated has no such gate.
    _assert(
        pl.client_or_zdtd_running.__doc__ is not None,
        "client_or_zdtd_running must document why zdtd is different",
    )
    print("PASS lock_gate_is_the_client_not_a_server")


def test_parallel_sandboxes_do_not_share_a_lock(tmp: Path) -> None:
    """Two sandbox runs on different client instances must both proceed.

    Disjoint instances mean disjoint game trees, Proton prefixes, port blocks
    and windows, so nothing is actually shared and a machine-wide lock would
    serialise runs for no reason. Same instance still collides.
    """
    a = pl.default_lock_path("client-alpha")
    b = pl.default_lock_path("client-beta")
    _assert(a != b, f"two instances must not share a lock file: {a}")
    _assert(a == pl.default_lock_path("client-alpha"), "lock path must be stable")
    _assert(
        pl.default_lock_path(None) not in (a, b),
        "the shared-client lock must be distinct from any instance's",
    )
    _assert("/" not in a.name, f"instance name must not escape the file name: {a.name}")
    tricky = pl.default_lock_path("../../etc/passwd")
    _assert(
        tricky.parent == pl.default_lock_path(None).parent,
        f"a hostile instance name must stay in the lock dir: {tricky}",
    )

    # PLAYTEST_LOCK_FILE still wins, so a shared-machine convention that points
    # several stacks at one file keeps working.
    shared = tmp / "shared-lock"
    os.environ["PLAYTEST_LOCK_FILE"] = str(shared)
    try:
        _assert(
            pl.default_lock_path("client-alpha") == shared,
            "PLAYTEST_LOCK_FILE must override instance scoping",
        )
    finally:
        del os.environ["PLAYTEST_LOCK_FILE"]

    # Two runs on different instances hold their own locks at the same time.
    sa, sb_ = "a-20260901-000000-aaaaaaaaaaaa", "b-20260901-000000-bbbbbbbbbbbb"
    pa, pb = tmp / "lock-alpha", tmp / "lock-beta"
    _assert(pl.acquire(sa, path=pa, live_probe=lambda: False).running, "alpha acquires")
    _assert(pl.acquire(sb_, path=pb, live_probe=lambda: False).running, "beta acquires")
    try:
        pl.acquire("c-20260901-000000-cccccccccccc", path=pa, live_probe=lambda: False)
    except pl.PlaytestLockError as ex:
        _assert(ex.reason == "foreign_holder", f"want foreign_holder, got {ex.reason}")
    else:
        raise AssertionError("a third run must not take alpha's held lock")
    pl.release(sa, path=pa)
    pl.release(sb_, path=pb)
    print("PASS parallel_sandboxes_do_not_share_a_lock")


def test_client_probe_is_scoped_to_one_prefix(tmp: Path) -> None:
    """A client is matched by its own STEAM_COMPAT_DATA_PATH.

    The same discriminator `sb stop` uses. Without it one sandbox client reads
    as another and the first run to start locks the machine.
    """
    proc = tmp / "proc-scoped"
    proc.mkdir(parents=True, exist_ok=True)
    mine = tmp / "instances" / "client-mine" / "compatdata"
    theirs = tmp / "instances" / "client-theirs" / "compatdata"
    mine.mkdir(parents=True)
    theirs.mkdir(parents=True)

    _assert(
        not pl.client_running_for_compat(mine, proc_root=proc),
        "no processes means no client",
    )

    proc.mkdir(parents=True, exist_ok=True)
    _fake_process(
        proc, "4242", "/usr/bin/wine64-preloader",
        "wine64-preloader\0Z:\\game\\7DaysToDie.exe\0",
    )
    (proc / "4242" / "environ").write_text(
        f"USER=maci\0STEAM_COMPAT_DATA_PATH={theirs.resolve()}\0", encoding="utf-8"
    )
    _assert(
        not pl.client_running_for_compat(mine, proc_root=proc),
        "another instance's client must not count as ours",
    )
    _assert(
        pl.client_running_for_compat(theirs, proc_root=proc),
        "the instance's own client must be found",
    )

    # In the prefix is not the same as being the client. Seeding an instance
    # runs `proton run reg.exe` and a wineserver lingers after any prefix use;
    # both carry STEAM_COMPAT_DATA_PATH and neither is a run. Counting them
    # refused a run against the very instance it had just prepared.
    _fake_process(proc, "4243", "/usr/bin/wineserver", "wineserver\0-p\0")
    (proc / "4243" / "environ").write_text(
        f"STEAM_COMPAT_DATA_PATH={mine.resolve()}\0", encoding="utf-8"
    )
    _assert(
        not pl.client_running_for_compat(mine, proc_root=proc),
        "a wineserver in the prefix must not count as a running client",
    )
    print("PASS client_probe_is_scoped_to_one_prefix")


def test_runtime_patterns_include_server() -> None:
    _assert(
        "7DaysToDieServer.x86_64" in pl.STOCK_DEDICATED_EXECUTABLES,
        "stock dedicated recognised",
    )
    _assert("zdtd" in pl.ZDTD_EXECUTABLES, "zdtd recognised")
    _assert(
        "7DaysToDieServer.x86_64" not in pl.ZDTD_EXECUTABLES,
        "the dedicated and zdtd tables are separate; only zdtd gates the lock",
    )
    _assert(
        "7DaysToDie.exe" in pl.STOCK_CLIENT_EXECUTABLES,
        "client in runtime probe",
    )
    # Behavioral: the probe must see a real listener and then see it gone
    # (a connect-based probe that always returned False would pass here).
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    bound = srv.getsockname()[1]
    _assert(pl.tcp_port_in_use(bound), f"listening port {bound} reported in use")
    srv.close()
    _assert(not pl.tcp_port_in_use(bound), f"closed port {bound} reported free")


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
        not pl.dedicated_running(proc_root=proc),
        "shell text must not count as a dedicated",
    )
    _assert(
        not pl.zdtd_running(proc_root=proc),
        "shell text must not count as a zdtd",
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
        pl.dedicated_running(proc_root=proc),
        "real dedicated executable must be detectable",
    )
    _assert(
        pl.zdtd_running(proc_root=proc),
        "real zdtd executable must be detectable",
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
    # _assert just proved equality with the winning session id (a str).
    pl.release(winners[0], path=lock)


def test_env_override_path(tmp: Path) -> None:
    lock = tmp / "from-env" / "playtest_running"
    old = os.environ.get("PLAYTEST_LOCK_FILE")
    os.environ["PLAYTEST_LOCK_FILE"] = str(lock)
    try:
        _assert(pl.default_lock_path() == lock, "env path honored")
        sid = pl.new_session_id("grok")
        _assert(pl.SESSION_RE.match(sid) is not None, f"generated session shape: {sid}")
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
    # Force an old heartbeat then touch; second resolution UTC does not move in 50ms.
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


def test_wait_until_can_start(tmp: Path) -> None:
    """wait_until_can_start uses can_start, including missing-heartbeat stale."""
    tmp.mkdir(parents=True, exist_ok=True)
    lock = tmp / "playtest_running"
    waiter = "waiter-20260824-000000-aaaaaaaaaaaa"
    owner = "owner-20260824-000000-bbbbbbbbbbbb"
    sleeps: list[float] = []

    def sleeper(dt: float) -> None:
        sleeps.append(dt)

    _assert(
        pl.wait_until_can_start(
            waiter, path=lock, live_probe=lambda: False, sleeper=sleeper
        ),
        "free lock is immediately startable",
    )
    _assert(sleeps == [], "no wait when already free")

    # Missing heartbeat while claimed is stale (not free). The local matrix
    # clone treated that as free; wait_until_can_start must not.
    lock.write_text(
        "running=yes\nsession=" + owner + "\nacquired=2020-01-01T00:00:00Z\n",
        encoding="utf-8",
    )
    st = pl.read_lock(lock)
    _assert(st.heartbeat is None, "fixture has no heartbeat field")
    _assert(pl.is_stale(st), "missing heartbeat is stale")
    _assert(
        pl.can_start(waiter, path=lock, live_probe=lambda: False),
        "stale missing-heartbeat + no runtime can start",
    )
    _assert(
        pl.wait_until_can_start(
            waiter, path=lock, live_probe=lambda: False, sleeper=sleeper
        ),
        "wait returns immediately on stale missing-heartbeat",
    )

    sleeps.clear()
    clock = [0.0]

    class FakeEnv(pl.LockEnv):
        def now(self) -> float:
            return clock[0]

    def advancing_sleep(dt: float) -> None:
        sleeps.append(dt)
        clock[0] += dt

    _assert(
        not pl.wait_until_can_start(
            waiter,
            path=lock,
            timeout_sec=25,
            interval_sec=10,
            live_probe=lambda: True,
            env=FakeEnv(),
            sleeper=advancing_sleep,
        ),
        "live runtime times out instead of spinning forever",
    )
    _assert(sleeps != [], "slept while blocked")
    _assert(clock[0] >= 25, f"clock reached timeout, got {clock[0]}")

    if not pl.client_running():
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "playtest_lock.py"),
                "wait",
                "--timeout",
                "2",
                "--interval",
                "1",
                "--path",
                str(tmp / "cli-free"),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _assert(
            proc.returncode == 0,
            "CLI wait on a free path exits 0: " + proc.stderr,
        )

    # The capture scripts consume only the exit code, so it must track the
    # probe exactly: 1 with the shared client up, 0 without. A dedicated server
    # is deliberately not consulted; it belongs to a Safehouse instance.
    expected_live_rc = 1 if pl.client_running() else 0
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "playtest_lock.py"), "live"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    _assert(
        proc.returncode == expected_live_rc,
        f"CLI live exit {proc.returncode} must match probed state "
        f"{expected_live_rc}: {proc.stderr}",
    )


def test_wait_cli_rejects_non_finite() -> None:
    """``wait --timeout nan`` used to never return: now >= nan is False, so
    the poll loop had no deadline. inf never expired either; a zero interval
    busy-looped and a negative interval made time.sleep raise instead of
    exiting 2.
    """
    py = sys.executable
    script = str(SCRIPTS / "playtest_lock.py")

    def run(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [py, script, "wait", *extra],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    for raw in ("nan", "inf", "-inf", "NaN", "1e999"):
        proc = run("--timeout", raw)
        _assert(proc.returncode == 2, f"--timeout {raw!r} rc={proc.returncode}")
        _assert("finite" in proc.stderr, proc.stderr)
    proc = run("--interval", "nan")
    _assert(proc.returncode == 2, f"--interval nan rc={proc.returncode}")
    _assert("finite" in proc.stderr, proc.stderr)
    proc = run("--timeout", "-1")
    _assert(proc.returncode == 2, f"--timeout -1 rc={proc.returncode}")
    proc = run("--interval", "0")
    _assert(proc.returncode == 2, f"--interval 0 rc={proc.returncode}")


def test_playtest_run_wiring() -> None:
    """Structural: orchestrator acquires before clean_processes and releases."""
    src = (SCRIPTS / "playtest_run.py").read_text(encoding="utf-8")
    _assert("import playtest_lock" in src, "imports playtest_lock")
    _assert("playtest_lock.acquire" in src, "calls playtest_lock.acquire")
    _assert("playtest_lock.release" in src, "calls playtest_lock.release")
    _assert("HeartbeatThread" in src, "starts heartbeat thread")
    main_i = src.find("def main(")
    _assert(main_i >= 0, "main defined")
    # main() goes through the interrupt-safe wrapper; the wrapper itself
    # calls playtest_lock.acquire with the undo-on-interrupt cleanup.
    acq_in_main = src.find("acquire_exclusive_lock(", main_i)
    clean_in_main = src.find("clean_processes(", main_i)
    _assert(
        0 <= acq_in_main < clean_in_main,
        f"acquire (pos {acq_in_main}) must precede clean_processes "
        f"(pos {clean_in_main}) in main",
    )
    finally_i = src.rfind("finally:")
    rel_i = src.find("playtest_lock.release", finally_i if finally_i >= 0 else 0)
    _assert(finally_i >= 0 and rel_i > finally_i, "release in finally block")
    # The teardown must disarm TERM/HUP before its first step: delivery while
    # cleanup runs must not raise SystemExit mid-finally and skip the
    # stop_proc/release statements below it (a live runtime would be stranded
    # under a published claim).
    disarm_i = src.find("_ignore_termination_signals()", finally_i)
    hb_stop_i = src.find("lock_heartbeat.stop()", finally_i)
    _assert(
        0 <= disarm_i < hb_stop_i,
        f"finally must disarm signals (at {disarm_i}) before teardown "
        f"(stop at {hb_stop_i})",
    )
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


def test_heartbeat_thread_ticks_until_stopped(tmp: Path) -> None:
    """A started thread must actually refresh the claim and stop() must join
    it for good: a heartbeat thread that never ticks (or keeps ticking after
    stop) either lets every other agent watch the hold go stale and take
    over a live run, or writes behind a released lock."""
    lock = tmp / "playtest_running"
    sid = "grok-20260810-231500-a1b2c3d4e5f6"
    pl.acquire(sid, path=lock, live_probe=lambda: False)
    th = pl.HeartbeatThread(sid, path=lock, interval_sec=0.05)
    th.start()
    try:
        deadline = time.monotonic() + 5.0
        while th.loop.touches < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        _assert(
            th.loop.touches >= 2,
            f"started thread never refreshed the claim: touches={th.loop.touches}",
        )
        _assert(pl.read_lock(lock).session == sid, "heartbeat kept the owner")
    finally:
        th.stop()
    ticks_at_stop = th.loop.touches
    _assert(not th._thread.is_alive(), "stop() returned before heartbeat thread exited")
    _assert(
        th.loop.touches == ticks_at_stop,
        f"thread kept writing after stop(): {ticks_at_stop} -> {th.loop.touches}",
    )


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
        assert proc.stdout is not None, "Popen was created with stdout=PIPE"
        line = proc.stdout.readline().strip()
        _assert(line == "armed", f"child did not arm handlers: {line!r}")
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    _assert(rc == 128 + signal.SIGTERM, f"exit {rc}, expected {128 + signal.SIGTERM}")


def test_sigterm_during_cleanup_is_ignored(tmp: Path) -> None:
    """SIGTERM delivered while the finally teardown is running must be
    ignored, not converted into SystemExit from inside the cleanup.

    Raising there skips the remaining teardown statements (stop_proc,
    release) and strands a live runtime under a published claim. The child
    models main()'s shape: handlers armed, claim taken, then a teardown body
    that disarms first, does slow work, and releases.
    """
    lock = tmp / "playtest_running"
    code = f"""\
import sys, time
from pathlib import Path
sys.path.insert(0, {str(SCRIPTS)!r})
import playtest_lock as pl, playtest_run
playtest_run.install_signal_handlers()
sid = 'sigterm-20260824-000000-000000000001'
lock = Path({str(lock)!r})
pl.acquire(sid, path=lock, live_probe=lambda: False)
print('armed', flush=True)
try:
    pass
finally:
    # Teardown shape of playtest_run.main(): disarm first, then slow work.
    playtest_run._ignore_termination_signals()
    print('disarmed', flush=True)
    time.sleep(2)  # window: SIGTERM lands mid-teardown here
    pl.release(sid, path=lock)
    print('released', flush=True)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert proc.stdout is not None, "Popen was created with stdout=PIPE"
        _assert(proc.stdout.readline().strip() == "armed", "child did not arm")
        # Wait until execution is already inside the disarmed teardown body,
        # then fire: delivery must not abort the remaining steps.
        _assert(proc.stdout.readline().strip() == "disarmed", "child not in teardown")
        proc.send_signal(signal.SIGTERM)
        rest = proc.stdout.read().split()
        rc = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    _assert(rc == 0, f"cleanup aborted by signal: exit {rc}, tail {rest!r}")
    _assert("released" in rest, f"release never ran, tail {rest!r}")
    state = pl.read_lock(lock)
    _assert(
        not state.running and state.session is None,
        f"claim survived interrupted-looking teardown: {state}",
    )


def test_parse_utc_timestamp_zones() -> None:
    """Timestamps parse as instants regardless of host TZ or stamp style."""
    z = pl.parse_utc_timestamp("2020-01-01T00:00:00Z")
    offset = pl.parse_utc_timestamp("2020-01-01T02:00:00+02:00")
    _assert(z is not None and offset is not None, "Z and offset stamps parse")
    # Z and an explicit +02:00 naming the same instant must agree exactly.
    _assert(z == offset, "Z equals the same instant with explicit offset")
    # A naive stamp violates the documented <UTC ISO8601 Z> format; it must
    # still be read as UTC, not host-local, so staleness does not shift when
    # the lock file is shared across hosts with different TZ settings.
    # Force a non-UTC zone so the pin fails against local-time interpretation
    # even when the test machine runs UTC.
    naive = None
    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        naive = pl.parse_utc_timestamp("2020-01-01T00:00:00")
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    _assert(naive == z, "naive stamp read as UTC, not host-local")
    epoch = pl.parse_utc_timestamp("1577836800.5")
    _assert(epoch == 1577836800.5, "epoch seconds pass through")
    for bad in (
        None,
        "",
        "not-a-time",
        "2020-13-40T99:00:00Z",
        "9" * 1000,
    ):
        _assert(pl.parse_utc_timestamp(bad) is None, f"garbage {bad!r} → None")


def test_seconds_env_overrides_reject_non_finite() -> None:
    """inf/nan overrides must warn+default, not poison staleness decisions.

    nan collapses through max(1.0, nan) to a 1s stale window (instant takeover
    of live holders); inf makes the lock never stale and freezes the heartbeat
    wait. Both are rejected like unparseable text.
    """
    names = ("PLAYTEST_LOCK_STALE_SEC", "PLAYTEST_LOCK_HEARTBEAT_SEC")
    saved = {n: os.environ.get(n) for n in names}
    try:
        for name, fallback in (
            ("PLAYTEST_LOCK_STALE_SEC", pl.DEFAULT_STALE_SEC),
            ("PLAYTEST_LOCK_HEARTBEAT_SEC", pl.DEFAULT_HEARTBEAT_INTERVAL_SEC),
        ):
            for raw in ("nan", "inf", "-inf", "NaN", "Infinity"):
                os.environ[name] = raw
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    val = pl._seconds_from_environ(name, fallback)
                _assert(val == float(fallback), f"{name}={raw!r} → default, got {val}")
                _assert("warn" in err.getvalue(), f"{name}={raw!r} warns")
            # Valid finite values still honored; small ones clamp to 1s.
            os.environ[name] = "45"
            _assert(
                pl._seconds_from_environ(name, fallback) == 45.0,
                f"{name}=45 honored",
            )
            os.environ[name] = "-5"
            _assert(
                pl._seconds_from_environ(name, fallback) == 1.0,
                f"{name}=-5 clamps to 1s",
            )
            # Unset falls back without warning.
            os.environ.pop(name)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                _assert(
                    pl._seconds_from_environ(name, fallback) == float(fallback),
                    f"unset {name} → default",
                )
                _assert(err.getvalue() == "", f"unset {name} stays quiet")
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def test_non_utf8_lock_bytes_survive_read(tmp: Path) -> None:
    """The lock file is shared with foreign helpers writing plain shell
    redirects; nothing forces valid UTF-8 (latin-1 names, binary junk from a
    torn write). Reading must degrade like every other corruption path:
    replacement chars parse as a foreign record and acquire/release refuse
    cleanly, never UnicodeDecodeError out of the orchestrator's finally."""
    lock = tmp / "playtest_running"
    tmp.mkdir(exist_ok=True)
    now = pl.format_utc(time.time())
    # 0xE9 is 'é' in latin-1 and invalid as any UTF-8 byte position.
    lock.write_bytes(
        f"running=yes\nsession=caf\xe9-holder\nacquired={now}\nheartbeat={now}\n".encode(
            "latin-1"
        )
    )
    state = pl.read_lock(lock)
    _assert(state.running is True, "intact running=yes still parses")
    _assert(state.session != "caf\xe9-holder", "raw latin-1 session not resurrected")
    sid = "grok-20260810-231500-a1b2c3d4e5f6"
    try:
        pl.acquire(sid, path=lock, live_probe=lambda: False)
        raise AssertionError("garbled foreign record must refuse acquire")
    except pl.PlaytestLockError as e:
        _assert(e.reason == "foreign_holder", f"reason={e.reason}")
        _assert(e.held_by == state.session, "held_by names the replaced record")
    # A non-owner release refuses instead of wiping the garbled record.
    try:
        pl.release(sid, path=lock)
        raise AssertionError("non-owner release should raise")
    except pl.PlaytestLockError as e:
        _assert(e.reason == "foreign_holder", f"reason={e.reason}")
    _assert(pl.read_lock(lock).running is True, "refused release left record alone")



def test_session_field_injection_refused(tmp: Path) -> None:
    """A session id must never be able to forge lock-file fields.

    acquire/heartbeat/release all route through _require_session, and
    write_lock refuses as a last line, so no caller can smuggle a newline
    ("running=no" on its own line) or an '=' past the parser.
    """
    lock = tmp / "playtest_running"
    bad = [
        "sess\nrunning=no",       # field injection
        "sess=other",             # key spoofing
        "#sess",                  # comment/leading-hash
        "sess with space",
        "sess\x00nul",
        "s" * 129,                # over length
    ]
    for sid in bad:
        try:
            pl.acquire(sid, path=lock, live_probe=lambda: False)
            raise AssertionError(f"acquire accepted {sid!r}")
        except pl.PlaytestLockError as e:
            _assert(e.reason == "bad_session", f"{sid!r} reason={e.reason}")
        _assert(not lock.exists(), f"{sid!r} must not create a lock file")

    ok = "agent-20260823-120000-abc123"
    pl.acquire(ok, path=lock, live_probe=lambda: False)
    _assert(pl.read_lock(lock).session == ok, "valid session still acquires")
    pl.release(ok, path=lock)


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
                "lock_gate_is_the_client_not_a_server",
                lambda: test_lock_gate_is_the_client_not_a_server(tmp / "gate"),
            ),
            (
                "parallel_sandboxes_do_not_share_a_lock",
                lambda: test_parallel_sandboxes_do_not_share_a_lock(tmp / "parallel"),
            ),
            (
                "client_probe_is_scoped_to_one_prefix",
                lambda: test_client_probe_is_scoped_to_one_prefix(tmp / "scoped"),
            ),
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
                "wait_until_can_start",
                lambda: test_wait_until_can_start(tmp / "wait"),
            ),
            ("wait_cli_rejects_non_finite", test_wait_cli_rejects_non_finite),
            (
                "heartbeat_thread_stop_before_start",
                lambda: test_heartbeat_thread_stop_before_start(tmp / "hbstopped"),
            ),
            (
                "heartbeat_thread_ticks_until_stopped",
                lambda: test_heartbeat_thread_ticks_until_stopped(
                    tmp / "hbticks"
                ),
            ),
            (
                "session_field_injection_refused",
                lambda: test_session_field_injection_refused(tmp / "sessfield"),
            ),
            ("playtest_run_wiring", test_playtest_run_wiring),
            ("parse_utc_timestamp_zones", test_parse_utc_timestamp_zones),
            (
                "seconds_env_overrides_reject_non_finite",
                test_seconds_env_overrides_reject_non_finite,
            ),
            ("sigterm_becomes_graceful_exit", test_sigterm_becomes_graceful_exit),
            (
                "sigterm_during_cleanup_ignored",
                lambda: test_sigterm_during_cleanup_is_ignored(tmp / "sigcleanup"),
            ),
            (
                "non_utf8_lock_bytes_survive_read",
                lambda: test_non_utf8_lock_bytes_survive_read(tmp / "nonutf8"),
            ),
        ]
        for name, fn in cases:
            try:
                if name != "playtest_run_wiring":
                    (tmp / name).mkdir(exist_ok=True)
                fn()  # type: ignore[operator]
                print(f"PASS {name}")
            except Exception as ex:
                fails += 1
                print(f"FAIL {name}: {ex}", file=sys.stderr)

    if fails:
        print(f"RESULT FAIL ({fails})", file=sys.stderr)
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
