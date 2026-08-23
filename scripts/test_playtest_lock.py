#!/usr/bin/env python3
"""Unit tests for shipped scripts/playtest_lock.py (no game launch)."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import playtest_lock as pl


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


def test_tcp_port_in_use_tracks_listener() -> None:
    """Bound loopback port reports in use; released port reports free."""
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        _assert(
            pl.tcp_port_in_use(port) is True,
            f"tcp_port_in_use must be True while {port} accepts",
        )
    finally:
        srv.close()
    _assert(
        pl.tcp_port_in_use(port) is False,
        f"tcp_port_in_use must be False after {port} closed",
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


def test_can_start_matches_acquire_matrix(tmp: Path) -> None:
    """can_start must agree with acquire for every lock-state × liveness cell.

    Both read the same policy (_start_blocker); this pins that agreement so
    the dry-run can never drift from the real acquire.
    """
    owner = "owner-20260810-000000-aaaaaaaaaaaa"
    other = "other-20260810-000001-bbbbbbbbbbbb"

    def fresh_lock(name: str) -> Path:
        lock = tmp / name / "playtest_running"
        return lock

    def foreign_state(stale: bool) -> tuple[Path, float]:
        lock = fresh_lock(f"foreign-{stale}")
        pl.acquire(owner, path=lock, live_probe=lambda: False)
        if stale:
            pl.write_lock(
                lock,
                running=True,
                session=owner,
                acquired="2020-01-01T00:00:00Z",
                heartbeat="2020-01-01T00:00:00Z",
            )
        return lock, 60

    cells = [
        ("free-idle", fresh_lock("free-idle"), False),
        ("free-live", fresh_lock("free-live"), True),
        ("own-idle", None, False),  # filled in below
        ("foreign-fresh-idle", *foreign_state(False)),
        ("foreign-stale-idle", *foreign_state(True)),
    ]

    # own lock: holder may re-acquire even with a live runtime present.
    lock = fresh_lock("own")
    pl.acquire(other, path=lock, live_probe=lambda: False)
    cells[2] = ("own-live", lock, True)
    own_session = other

    for label, lock, live in cells:
        session = own_session if label.startswith("own") else "probe-20260810-000002-cccccccccccc"
        allowed = pl.can_start(
            session,
            path=lock,
            live_probe=lambda v=live: v,
            max_age_sec=60,
        )
        try:
            pl.acquire(session, path=lock, live_probe=lambda v=live: v, max_age_sec=60)
            acquired = True
        except pl.PlaytestLockError as e:
            acquired = False
            _assert(
                e.reason
                in ("foreign_holder", "stale_but_live", "live_runtime"),
                f"{label}: unexpected reason {e.reason}",
            )
        _assert(
            allowed == acquired,
            f"{label}: can_start={allowed} but acquire "
            f"{'succeeded' if acquired else 'refused'}",
        )
        if acquired:
            pl.release(session, path=lock)


def test_env_overrides_parse_and_clamp(tmp: Path) -> None:
    """Bad numeric overrides fall back to defaults; zero clamps to 1s floor."""
    specs = (
        ("PLAYTEST_LOCK_STALE_SEC", pl.stale_sec, pl.DEFAULT_STALE_SEC),
        (
            "PLAYTEST_LOCK_HEARTBEAT_SEC",
            pl.heartbeat_interval_sec,
            pl.DEFAULT_HEARTBEAT_INTERVAL_SEC,
        ),
    )
    for env_name, getter, default in specs:
        old = os.environ.get(env_name)
        try:
            for raw, want in (
                ("5", 5.0),
                ("abc", default),
                ("   ", default),
                ("0", 1.0),
                ("-3", 1.0),
            ):
                os.environ[env_name] = raw
                got = getter()
                _assert(
                    got == want,
                    f"{env_name}={raw!r}: want {want}, got {got}",
                )
        finally:
            if old is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = old


def test_new_session_id_shape_and_prefix_validation() -> None:
    sid = pl.new_session_id("grok")
    _assert(pl.SESSION_RE.match(sid) is not None, f"generated shape: {sid}")
    for bad in ("", "9lead", "Has-Upper", "has-dash"):
        try:
            pl.new_session_id(bad)
            raise AssertionError(f"prefix {bad!r} must be rejected")
        except ValueError:
            pass


def test_parse_utc_timestamp_shapes() -> None:
    now = time.time()
    zulul = pl.parse_utc_timestamp(pl.utc_now_iso())
    epoch = pl.parse_utc_timestamp(f"{now:.3f}")
    _assert(zulul is not None and abs(zulul - now) < 1.5, "Z-suffixed ISO parses")
    _assert(epoch is not None and abs(epoch - now) < 1.5, "epoch seconds parse")
    _assert(
        pl.parse_utc_timestamp(None) is None
        and pl.parse_utc_timestamp("") is None
        and pl.parse_utc_timestamp("not-a-time") is None,
        "missing/garbage timestamps are None, not crashes",
    )


def test_heartbeat_foreign_or_free_refused(tmp: Path) -> None:
    lock = tmp / "playtest_running"
    owner = "owner-20260810-000000-aaaaaaaaaaaa"
    other = "other-20260810-000001-bbbbbbbbbbbb"
    pl.acquire(owner, path=lock, live_probe=lambda: False)
    try:
        pl.heartbeat(other, path=lock)
        raise AssertionError("foreign heartbeat must refuse")
    except pl.PlaytestLockError as e:
        _assert(e.reason == "foreign_holder", f"reason={e.reason}")
        _assert(e.held_by == owner, f"held_by={e.held_by}")
    st = pl.read_lock(lock)
    _assert(st.session == owner, "owner unchanged after refused heartbeat")
    # Heartbeat on a free/absent lock refuses too (nothing to refresh).
    pl.release(owner, path=lock)
    try:
        pl.heartbeat(owner, path=lock)
        raise AssertionError("heartbeat without held lock must refuse")
    except pl.PlaytestLockError as e:
        _assert(e.reason == "foreign_holder", f"free-lock reason={e.reason}")


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
    rel_i = src.find("playtest_lock.release", max(finally_i, 0))
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


def test_playtest_run_child_bounds() -> None:
    """Structural: spawned children have release paths and hold bounds.

    - The synchronous loadgen build runs while the exclusivity lock is held
      with a live heartbeat, so it must carry a timeout (a wedged build would
      otherwise keep the lock fresh past stale takeover forever).
    - The detached mute helper has no parent-side wait unless teardown reaps
      it: stop_proc must handle the helper riding the client proc.
    """
    src = (SCRIPTS / "playtest_run.py").read_text(encoding="utf-8")
    build_i = src.find("def ensure_loadgen_built(")
    _assert(build_i >= 0, "ensure_loadgen_built defined")
    build_end = src.find("\ndef ", build_i + 1)
    build_body = src[build_i:build_end]
    _assert(
        "timeout=" in build_body,
        "loadgen dotnet build must pass timeout= to subprocess.run",
    )
    _assert(
        "TimeoutExpired" in build_body,
        "loadgen build must catch subprocess.TimeoutExpired",
    )
    stop_i = src.find("def stop_proc(")
    stop_end = src.find("\ndef ", stop_i + 1)
    stop_body = src[stop_i:stop_end]
    _assert(
        "_mute_helper" in stop_body,
        "stop_proc must reap/stop the client mute helper riding the proc",
    )
    start_client_i = src.find("def start_client(")
    start_client_end = src.find("\ndef ", start_client_i + 1)
    start_client_body = src[start_client_i:start_client_end]
    _assert(
        "proc._mute_helper=" in start_client_body.replace(" ", ""),
        "start_client must attach the mute helper to the returned proc",
    )


def test_heartbeat_stop_before_start_is_safe(tmp: Path) -> None:
    """stop() before/without start() must not raise: it runs in finally blocks
    where a RuntimeError would skip process cleanup and lock release."""
    hb = pl.HeartbeatThread(
        "hb-20260810-000000-cccccccccccc",
        path=tmp / "playtest_running",
        interval_sec=0.05,
    )
    hb.stop()
    hb.stop()  # idempotent

    # Started thread still stops promptly and refreshes while alive.
    lock = tmp / "live" / "playtest_running"
    owner = "hblive-20260810-000000-dddddddddddd"
    pl.acquire(owner, path=lock, live_probe=lambda: False)
    hb2 = pl.HeartbeatThread(owner, path=lock, interval_sec=0.05)
    hb2.start()
    time.sleep(0.15)
    hb2.stop()
    state = pl.read_lock(lock)
    _assert(state.running and state.session == owner, "heartbeat kept owner")
    _assert(
        (state.heartbeat_age_sec or 99) < 5,
        f"heartbeat fresh after stop: age={state.heartbeat_age_sec}",
    )
    pl.release(owner, path=lock)


def test_session_field_injection_rejected(tmp: Path) -> None:
    """Session ids are written verbatim into a shared key=value file that
    every agent parses; newlines or '=' must not be able to forge fields."""
    lock = tmp / "playtest_running"
    evil = "evil-20260810-000000-ffffff\nrunning=no"
    try:
        pl.acquire(evil, path=lock, live_probe=lambda: False)
        raise AssertionError("newline-bearing session must be refused")
    except pl.PlaytestLockError as e:
        _assert(e.reason == "bad_session", f"reason={e.reason}")
    _assert(
        not lock.exists() or pl.read_lock(lock).running is False,
        "lock payload not forged by injected fields",
    )

    # Sink-side guard also covers writers that bypass acquire().
    try:
        pl.write_lock(lock, running=True, session="x=y\nheartbeat=nope")
        raise AssertionError("write_lock must refuse field-injecting sessions")
    except ValueError:
        pass
    _assert(pl.read_lock(lock).running is False, "injected write produced no lock")

    # Documented-shape ids keep working end to end.
    sid = "grok-20260810-231500-a1b2c3d4e5f6"
    pl.acquire(sid, path=lock, live_probe=lambda: False)
    _assert(pl.read_lock(lock).session == sid, "conforming session recorded")
    pl.release(sid, path=lock)

    for fn in (pl.heartbeat, pl.release):
        try:
            fn("bad id\n", path=lock)
            raise AssertionError(f"{fn.__name__} must refuse malformed session")
        except pl.PlaytestLockError as e:
            _assert(e.reason == "bad_session", f"{fn.__name__} reason={e.reason}")


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
            ("tcp_port_in_use_tracks_listener", test_tcp_port_in_use_tracks_listener),
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
                "can_start_matches_acquire_matrix",
                lambda: test_can_start_matches_acquire_matrix(tmp / "matrix"),
            ),
            (
                "heartbeat_stop_before_start_is_safe",
                lambda: test_heartbeat_stop_before_start_is_safe(tmp / "hbsafe"),
            ),
            ("playtest_run_wiring", test_playtest_run_wiring),
            ("playtest_run_child_bounds", test_playtest_run_child_bounds),
            (
                "env_overrides_parse_and_clamp",
                lambda: test_env_overrides_parse_and_clamp(tmp / "envparse"),
            ),
            (
                "new_session_id_shape_and_prefix_validation",
                test_new_session_id_shape_and_prefix_validation,
            ),
            ("parse_utc_timestamp_shapes", test_parse_utc_timestamp_shapes),
            (
                "heartbeat_foreign_or_free_refused",
                lambda: test_heartbeat_foreign_or_free_refused(tmp / "hbreject"),
            ),
            (
                "session_field_injection_rejected",
                lambda: test_session_field_injection_rejected(tmp / "injection"),
            ),
        ]
        for name, fn in cases:
            try:
                if name not in ("playtest_run_wiring", "playtest_run_child_bounds"):
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
