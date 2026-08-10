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
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOCK_REL = Path(".cache") / "7dtd-playtest" / "playtest_running"
SESSION_RE = re.compile(r"^[a-z][a-z0-9]*-[0-9]{8}-[0-9]{6}-[0-9a-f]+$")
DEFAULT_STALE_SEC = 120
DEFAULT_HEARTBEAT_INTERVAL_SEC = 30

# Patterns aligned with playtest_run.clean_processes (client + server).
# A second orchestrator must not start while *either* is live.
DEFAULT_CLIENT_PATTERNS = (
    r"[/]7DaysToDie\.exe",
    r"wine64-preloader.*7DaysToDie",
    r"proton.*7DaysToDie",
    r"DaysToDie[.]exe",
)
DEFAULT_SERVER_PATTERNS = (
    r"7DaysToDieServer\.x86_64",
    r"7DaysToDieServe",  # truncated comm
    r"zig-out/bin/zdtd",
    r"[/]zdtd(\s|$)",
)
DEFAULT_RUNTIME_PATTERNS = DEFAULT_CLIENT_PATTERNS + DEFAULT_SERVER_PATTERNS


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
        datetime.now(timezone.utc)
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


def _pgrep_any(patterns: tuple[str, ...]) -> bool:
    for pat in patterns:
        r = subprocess.run(
            ["pgrep", "-f", pat],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if r.returncode == 0:
            return True
    return False


def default_live_client_running() -> bool:
    """True when a stock/Proton **client** process is present."""
    return _pgrep_any(DEFAULT_CLIENT_PATTERNS)


def default_live_server_running() -> bool:
    """True when stock dedicated or zdtd server process is present."""
    return _pgrep_any(DEFAULT_SERVER_PATTERNS)


def default_live_runtime_running() -> bool:
    """True when client **or** dedicated/zdtd server is present.

    Used as the default acquire gate: playtest_run starts both, and a second
    run must not double-bind ports or kill the first holder's processes.
    """
    return _pgrep_any(DEFAULT_RUNTIME_PATTERNS)


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
    if state.running and state.session and state.session != session:
        if is_stale(state, max_age_sec=max_age_sec) and not probe():
            return True
        return False
    if probe() and not (state.running and state.session == session):
        return False
    return True


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
    path = path or default_lock_path()
    probe = live_probe if live_probe is not None else default_live_runtime_running
    result: dict[str, LockState | None] = {"state": None}

    def _body() -> None:
        state = read_lock(path)
        live = probe()
        if state.running and state.session and state.session != session:
            stale = is_stale(state, max_age_sec=max_age_sec)
            if stale and not live:
                # Documented reclaim: holder died without releasing.
                pass
            elif stale and live:
                raise PlaytestLockError(
                    f"playtest lock stale for session={state.session} but live "
                    f"client/server still present (file {path}, "
                    f"heartbeat={state.heartbeat})",
                    held_by=state.session,
                    reason="stale_but_live",
                )
            else:
                age = state.heartbeat_age_sec
                age_s = f"{age:.0f}s" if age is not None else "unknown"
                raise PlaytestLockError(
                    f"playtest lock held by session={state.session} "
                    f"(file {path}, heartbeat_age={age_s}, stale={stale})",
                    held_by=state.session,
                    reason="foreign_holder",
                )
        if live and not (state.running and state.session == session):
            # Free lock but client and/or dedicated already up.
            if not (
                state.running
                and state.session
                and state.session != session
                and is_stale(state, max_age_sec=max_age_sec)
            ):
                raise PlaytestLockError(
                    f"live playtest runtime (DaysToDie client and/or dedicated/"
                    f"zdtd server) present; refusing start (file {path}"
                    + (
                        f", lock session={state.session}"
                        if state.session
                        else ", lock free"
                    )
                    + ")",
                    held_by=state.session,
                    reason="live_runtime",
                )
            # Stale foreign + live: already raised stale_but_live above.
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
        self._thread = threading.Thread(
            target=self._run,
            name="playtest-lock-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
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
                except Exception:
                    pass
