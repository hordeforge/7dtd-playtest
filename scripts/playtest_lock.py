"""Exclusive playtest runtime lock (host-side, no game I/O).

Covers the shared **client and dedicated/zdtd server** on one machine — not
only the client. Deterministic, parseable lock shared across agents and
orchestrators. Compatible with the 7dtd-mods monorepo convention:

    running=yes|no
    session=<agent>-<UTC YYYYMMDD-HHMMSS>-<hex>
    acquired=<UTC ISO8601 Z>     # set on acquire (when running=yes)
    heartbeat=<UTC ISO8601 Z>    # refreshed while the holder is still active

Default path: ~/.cache/7dtd-playtest/playtest_running
Override: PLAYTEST_LOCK_FILE (same env name as Atomic playtest-run helpers).
Point Atomic and this harness at the **same** path on a shared machine.

Acquire is serialized with fcntl.flock on a sidecar file and writes the
holder payload via os.replace so two processes cannot both win under normal
local FS conditions.

Staleness: if running=yes but heartbeat is older than PLAYTEST_LOCK_STALE_SEC
(default 120s), and no live runtime process is present, another session may
take over (documented reclaim). Fresh heartbeat means the holder is still
alive; agents should wait.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_LOCK_REL = Path(".cache") / "7dtd-playtest" / "playtest_running"
SESSION_RE = re.compile(r"^[a-z][a-z0-9]*-[0-9]{8}-[0-9]{6}-[0-9a-f]+$")
# Lock-file field safety: session ids are written verbatim into a line-oriented
# key=value file that every agent on the host parses. A session id must never
# carry newlines (field injection) or '=' / leading '#' (key spoofing). This is
# deliberately looser than SESSION_RE so conforming external holder ids still
# pass while injection stays impossible.
SESSION_FIELD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DEFAULT_STALE_SEC = 120
DEFAULT_HEARTBEAT_INTERVAL_SEC = 30

PROC_ROOT = Path("/proc")
STOCK_CLIENT_EXECUTABLES = ("7DaysToDie.exe", "DaysToDie.exe")
STOCK_SERVER_EXECUTABLES = ("7DaysToDieServer.x86_64", "zdtd")
WINE_PRELOADERS = ("wine-preloader", "wine64-preloader")
GAME_CLIENT_ARG_RE = re.compile(r"(?:^|[/\\\\])7DaysToDie\.exe(?:\0|$)", re.IGNORECASE)


class PlaytestLockError(RuntimeError):
    """Acquire/release refused. ``held_by`` is the foreign session if known."""

    def __init__(
        self,
        message: str,
        *,
        held_by: str | None = None,
        reason: str = "refused",
    ) -> None:
        super().__init__(message)
        self.held_by = held_by
        self.reason = reason


@dataclass(frozen=True)
class LockState:
    running: bool
    session: str | None
    path: Path
    acquired: str | None = None
    heartbeat: str | None = None

    @property
    def heartbeat_epoch(self) -> float | None:
        return parse_utc_timestamp(self.heartbeat) if self.heartbeat else None

    @property
    def heartbeat_age_sec(self) -> float | None:
        ep = self.heartbeat_epoch
        if ep is None:
            return None
        return max(0.0, time.time() - ep)


def default_lock_path() -> Path:
    env = os.environ.get("PLAYTEST_LOCK_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / DEFAULT_LOCK_REL


def stale_sec() -> float:
    raw = os.environ.get("PLAYTEST_LOCK_STALE_SEC", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return float(DEFAULT_STALE_SEC)


def heartbeat_interval_sec() -> float:
    raw = os.environ.get("PLAYTEST_LOCK_HEARTBEAT_SEC", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return float(DEFAULT_HEARTBEAT_INTERVAL_SEC)


def flock_path_for(lock_path: Path) -> Path:
    return Path(str(lock_path) + ".flock")


def utc_now_iso() -> str:
    """UTC timestamp with second precision, always Z-suffixed."""
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_utc_timestamp(value: str | None) -> float | None:
    """Parse ISO-8601 (…Z or offset) or unix epoch seconds → epoch float."""
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", s):
        try:
            return float(s)
        except ValueError:
            return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def new_session_id(prefix: str = "playtest") -> str:
    """Same shape as Atomic scripts/new-session-id.sh: prefix-UTC-hex."""
    if not re.fullmatch(r"[a-z][a-z0-9]*", prefix):
        raise ValueError(
            "prefix must start with a lowercase letter and contain only "
            "lowercase letters and digits"
        )
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    suffix = secrets.token_hex(6)
    return f"{prefix}-{ts}-{suffix}"


def validate_session_field(session: str) -> str:
    """Reject session ids that could inject or spoof lock-file fields.

    Raises ValueError; acquire/heartbeat/release translate this into
    PlaytestLockError(reason="bad_session").
    """
    if not SESSION_FIELD_RE.fullmatch(session):
        raise ValueError(
            "session id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127} "
            f"(single line, no '=' or control chars); got {session!r}"
        )
    return session


def read_lock(path: Path | None = None) -> LockState:
    path = path or default_lock_path()
    if not path.is_file():
        return LockState(running=False, session=None, path=path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return LockState(running=False, session=None, path=path)
    running = False
    session: str | None = None
    acquired: str | None = None
    heartbeat: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if key == "running":
            running = val.lower() in ("yes", "true", "1")
        elif key == "session" and val:
            session = val
        elif key == "acquired" and val:
            acquired = val
        elif key == "heartbeat" and val:
            heartbeat = val
    if not running:
        session = None
        acquired = None
        heartbeat = None
    return LockState(
        running=running,
        session=session,
        path=path,
        acquired=acquired,
        heartbeat=heartbeat,
    )


def write_lock(
    path: Path,
    *,
    running: bool,
    session: str | None = None,
    acquired: str | None = None,
    heartbeat: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if running:
        if not session:
            raise ValueError("session is required when running=yes")
        validate_session_field(session)
        now = utc_now_iso()
        acq = acquired or now
        hb = heartbeat or now
        body = (
            f"running=yes\n"
            f"session={session}\n"
            f"acquired={acq}\n"
            f"heartbeat={hb}\n"
        )
    else:
        body = "running=no\n"
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def is_stale(
    state: LockState | None = None,
    *,
    path: Path | None = None,
    max_age_sec: float | None = None,
    now: float | None = None,
) -> bool:
    """True when running=yes but heartbeat is missing or older than max age.

    Free locks are not stale. Used by agents to see if a holder is still
    refreshing, and by acquire to reclaim crashed holders (with process check).
    """
    state = state if state is not None else read_lock(path)
    if not state.running:
        return False
    limit = stale_sec() if max_age_sec is None else max_age_sec
    now_t = time.time() if now is None else now
    ep = state.heartbeat_epoch
    if ep is None:
        # Legacy / corrupt: no heartbeat while claimed → treat as stale.
        return True
    return (now_t - ep) > limit


def _runtime_pids(proc_root: Path = PROC_ROOT) -> list[Path]:
    """Return numeric process directories without matching shell command text."""
    try:
        return [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return []


def _process_executable_name(pid_dir: Path) -> str | None:
    try:
        return Path(os.readlink(pid_dir / "exe")).name
    except OSError:
        return None


def _process_cmdline(pid_dir: Path) -> str:
    try:
        return (pid_dir / "cmdline").read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _any_executable_running(
    names: tuple[str, ...], *, proc_root: Path = PROC_ROOT
) -> bool:
    wanted = set(names)
    return any(
        _process_executable_name(pid_dir) in wanted
        for pid_dir in _runtime_pids(proc_root)
    )


def _any_preloader_running_game(
    *, proc_root: Path = PROC_ROOT
) -> bool:
    """True for a Wine game process, not a shell that merely cites its path."""
    for pid_dir in _runtime_pids(proc_root):
        if _process_executable_name(pid_dir) not in WINE_PRELOADERS:
            continue
        if GAME_CLIENT_ARG_RE.search(_process_cmdline(pid_dir)):
            return True
    return False


def default_live_client_running() -> bool:
    """True when a stock/Proton **client** process is present."""
    return _any_executable_running(STOCK_CLIENT_EXECUTABLES) or _any_preloader_running_game()


def default_live_server_running() -> bool:
    """True when stock dedicated or zdtd server process is present.

    Inspect the executable each process is running. A shell, terminal history,
    or agent prompt may mention these names, but cannot satisfy this check.
    """
    return _any_executable_running(STOCK_SERVER_EXECUTABLES)


def default_live_runtime_running() -> bool:
    """True when client **or** dedicated/zdtd server is present.

    Used as the default acquire gate: playtest_run starts both, and a second
    run must not double-bind ports or kill the first holder's processes.
    """
    return default_live_client_running() or default_live_server_running()


def tcp_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """True if something accepts TCP on host:port (quick connect probe)."""
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def _with_flock(path: Path, fn: Callable[[], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flock_path = flock_path_for(path)
    with open(flock_path, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            fn()
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _start_blocker(
    state: LockState,
    session: str,
    *,
    live: bool,
    max_age_sec: float | None = None,
) -> tuple[str, str | None] | None:
    """Single source of truth for the start policy.

    Returns ``(reason, held_by)`` for the first rule that refuses
    ``session``, or None when starting is allowed. Used by both
    :func:`acquire` (raises) and :func:`can_start` (dry run) so the two
    cannot drift.
    """
    if state.running and state.session and state.session != session:
        if is_stale(state, max_age_sec=max_age_sec):
            # Documented reclaim when the holder died without releasing;
            # a live runtime still blocks takeover (stale_but_live).
            return ("stale_but_live", state.session) if live else None
        return ("foreign_holder", state.session)
    if live and not (state.running and state.session == session):
        # Free lock (or corrupt payload) but client/server already up.
        return ("live_runtime", state.session)
    return None


def can_start(
    session: str,
    *,
    path: Path | None = None,
    live_probe: Callable[[], bool] | None = None,
    max_age_sec: float | None = None,
) -> bool:
    """Dry-run of acquire rules without writing."""
    if not session:
        return False
    path = path or default_lock_path()
    # Default: client OR dedicated/zdtd (full playtest runtime).
    probe = live_probe if live_probe is not None else default_live_runtime_running
    state = read_lock(path)
    return _start_blocker(state, session, live=probe(), max_age_sec=max_age_sec) is None


def acquire(
    session: str,
    *,
    path: Path | None = None,
    live_probe: Callable[[], bool] | None = None,
    max_age_sec: float | None = None,
) -> LockState:
    """Acquire the lock for ``session``. Re-entrant for the same session.

    Raises PlaytestLockError when held by another fresh session or a live
    playtest runtime (client and/or dedicated server) blocks start. A **stale**
    foreign lock (old/missing heartbeat) may be taken over only when no live
    runtime process is present.
    """
    if not session or not str(session).strip():
        raise PlaytestLockError("session id is required", reason="bad_session")
    session = str(session).strip()
    try:
        validate_session_field(session)
    except ValueError as ex:
        raise PlaytestLockError(str(ex), reason="bad_session") from ex
    path = path or default_lock_path()
    probe = live_probe if live_probe is not None else default_live_runtime_running
    result: dict[str, LockState | None] = {"state": None}

    def _body() -> None:
        state = read_lock(path)
        live = probe()
        blocker = _start_blocker(state, session, live=live, max_age_sec=max_age_sec)
        if blocker is not None:
            reason, held_by = blocker
            if reason == "stale_but_live":
                raise PlaytestLockError(
                    f"playtest lock stale for session={held_by} but live "
                    f"client/server still present (file {path}, "
                    f"heartbeat={state.heartbeat})",
                    held_by=held_by,
                    reason=reason,
                )
            if reason == "foreign_holder":
                age = state.heartbeat_age_sec
                age_s = f"{age:.0f}s" if age is not None else "unknown"
                raise PlaytestLockError(
                    f"playtest lock held by session={held_by} "
                    f"(file {path}, heartbeat_age={age_s}, "
                    f"stale={is_stale(state, max_age_sec=max_age_sec)})",
                    held_by=held_by,
                    reason=reason,
                )
            raise PlaytestLockError(
                f"live playtest runtime (DaysToDie client and/or dedicated/"
                f"zdtd server) present; refusing start (file {path}"
                + (
                    f", lock session={held_by}"
                    if held_by
                    else ", lock free"
                )
                + ")",
                held_by=held_by,
                reason=reason,
            )
        now = utc_now_iso()
        # Preserve original acquired time on re-entrant refresh by same session.
        acq = (
            state.acquired
            if state.running and state.session == session and state.acquired
            else now
        )
        write_lock(path, running=True, session=session, acquired=acq, heartbeat=now)
        result["state"] = read_lock(path)

    _with_flock(path, _body)
    assert result["state"] is not None
    return result["state"]


def heartbeat(
    session: str,
    *,
    path: Path | None = None,
) -> LockState:
    """Refresh heartbeat for the owning session. No-op fail if not owner."""
    if not session or not str(session).strip():
        raise PlaytestLockError("session id is required", reason="bad_session")
    session = str(session).strip()
    try:
        validate_session_field(session)
    except ValueError as ex:
        raise PlaytestLockError(str(ex), reason="bad_session") from ex
    path = path or default_lock_path()
    result: dict[str, LockState | None] = {"state": None}

    def _body() -> None:
        state = read_lock(path)
        if not state.running or state.session != session:
            raise PlaytestLockError(
                f"cannot heartbeat: lock not owned by session={session} "
                f"(holder={state.session!r}, file {path})",
                held_by=state.session,
                reason="foreign_holder",
            )
        now = utc_now_iso()
        write_lock(
            path,
            running=True,
            session=session,
            acquired=state.acquired or now,
            heartbeat=now,
        )
        result["state"] = read_lock(path)

    _with_flock(path, _body)
    assert result["state"] is not None
    return result["state"]


def release(
    session: str,
    *,
    path: Path | None = None,
) -> LockState:
    """Release only if we own the lock (or it is already free)."""
    if not session or not str(session).strip():
        raise PlaytestLockError("session id is required", reason="bad_session")
    session = str(session).strip()
    try:
        validate_session_field(session)
    except ValueError as ex:
        raise PlaytestLockError(str(ex), reason="bad_session") from ex
    path = path or default_lock_path()
    result: dict[str, LockState | None] = {"state": None}

    def _body() -> None:
        state = read_lock(path)
        if state.running and state.session and state.session != session:
            raise PlaytestLockError(
                f"playtest lock owned by session={state.session}; not releasing "
                f"(file {path})",
                held_by=state.session,
                reason="foreign_holder",
            )
        write_lock(path, running=False, session=None)
        result["state"] = read_lock(path)

    _with_flock(path, _body)
    assert result["state"] is not None
    return result["state"]


class HeartbeatThread:
    """Daemon thread that refreshes the lock heartbeat until stopped."""

    def __init__(
        self,
        session: str,
        *,
        path: Path | None = None,
        interval_sec: float | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self.session = session
        self.path = path or default_lock_path()
        self.interval_sec = (
            heartbeat_interval_sec() if interval_sec is None else interval_sec
        )
        self.on_error = on_error
        self._stop = threading.Event()
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name="playtest-lock-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._started:
            # join raises RuntimeError on a never-started thread; stop() runs in
            # orchestrator finally blocks where that would skip lock release.
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # Immediate first touch so age stays low even if interval is long.
        self._touch()
        while not self._stop.wait(self.interval_sec):
            self._touch()

    def _touch(self) -> None:
        try:
            heartbeat(self.session, path=self.path)
        except BaseException as ex:  # noqa: BLE001 — report and keep trying
            if self.on_error is not None:
                try:
                    self.on_error(ex)
                except Exception:  # noqa: BLE001, S110 — heartbeat must never die from its own callback
                    pass
