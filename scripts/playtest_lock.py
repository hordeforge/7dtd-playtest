"""Exclusive playtest runtime lock (host-side, no game I/O).

Covers the shared **client and dedicated/zdtd server** on one machine, not
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
import math
import os
import re
import secrets
import sys
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

PROC_ROOT = Path("/proc")
STOCK_CLIENT_EXECUTABLES = ("7DaysToDie.exe", "DaysToDie.exe")
STOCK_SERVER_EXECUTABLES = ("7DaysToDieServer.x86_64", "zdtd")
WINE_PRELOADERS = ("wine-preloader", "wine64-preloader")
GAME_CLIENT_ARG_RE = re.compile(r"(?:^|[/\\\\])7DaysToDie\.exe(?:\0|$)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Nondeterminism seams (deterministic simulation testing)
#
# Everything this module cannot reproduce on its own - wall clock, OS entropy,
# pid, the filesystem, and the cross-process mutex - goes through one injected
# ``LockEnv``. Production uses :data:`SYSTEM_ENV`; the simulator in
# ``dst_sim.py`` substitutes a virtual clock, a seeded RNG, and an in-memory
# filesystem so a whole multi-agent run is a pure function of one seed.
# ---------------------------------------------------------------------------


class LockStorage:
    """Filesystem port. Only these calls may touch durable state."""

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def read_text(self, path: Path) -> str | None:
        try:
            # Foreign helpers share this file through plain shell redirects;
            # nothing forces their bytes to be valid UTF-8. Decode with
            # replacement like every other reader of externally written
            # bytes: U+FFFD never matches a key or session, so a mangled
            # record degrades exactly like the truncated-corruption case
            # instead of raising UnicodeDecodeError out of acquire/release.
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def write_text(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")

    def replace(self, src: Path, dst: Path) -> None:
        os.replace(src, dst)

    def exists(self, path: Path) -> bool:
        return path.exists()

    def unlink(self, path: Path) -> None:
        path.unlink()

    def mkdir_parents(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    def exclusive(self, path: Path, fn: Callable[[], None]) -> None:
        """Run ``fn`` while holding the cross-process lock for ``path``."""
        self.mkdir_parents(path)
        flock_path = flock_path_for(path)
        with open(flock_path, "a+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                fn()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


class LockEnv:
    """Injected clock / entropy / storage. Default binds to the real host."""

    def __init__(self, storage: LockStorage | None = None) -> None:
        self.storage = storage or LockStorage()

    def now(self) -> float:
        """Epoch seconds. Virtual in simulation."""
        return time.time()

    def token_hex(self, nbytes: int) -> str:
        return secrets.token_hex(nbytes)

    def pid(self) -> int:
        return os.getpid()

    def stale_sec(self) -> float:
        return _stale_sec_from_environ()

    def heartbeat_interval_sec(self) -> float:
        return _heartbeat_interval_from_environ()


SYSTEM_ENV = LockEnv()
_ENV: LockEnv = SYSTEM_ENV


def current_env() -> LockEnv:
    return _ENV


def set_env(env: LockEnv | None) -> LockEnv:
    """Swap the process-wide default env (simulation entry point).

    Returns the previous env so callers can restore it.
    """
    global _ENV
    previous = _ENV
    _ENV = env or SYSTEM_ENV
    return previous


def _env(env: LockEnv | None) -> LockEnv:
    return env if env is not None else _ENV


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

    def heartbeat_age_sec_at(self, now: float) -> float | None:
        """Age against an explicit clock read (simulation-safe)."""
        ep = self.heartbeat_epoch
        if ep is None:
            return None
        return max(0.0, now - ep)



def default_lock_path() -> Path:
    env = os.environ.get("PLAYTEST_LOCK_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / DEFAULT_LOCK_REL


def _warn_invalid_env(name: str, raw: str, fallback: float) -> None:
    """A set-but-unparseable override must not silently change behaviour."""
    print(
        f"playtest-lock warn: invalid {name}={raw!r}; using default {fallback:g}s",
        file=sys.stderr,
    )


def _seconds_from_environ(name: str, fallback: float) -> float:
    """Read a seconds override; unparseable or non-finite values warn+default.

    ``float("nan")`` would collapse to a 1s clamp through ``max`` and make a
    fresh live holder look stale instantly; ``inf`` makes the lock never stale
    (or freezes the heartbeat wait). Both silently corrupt exclusivity, so
    they are rejected like unparseable text.
    """
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            val = float(raw)
        except ValueError:
            _warn_invalid_env(name, raw, fallback)
        else:
            if math.isfinite(val):
                return max(1.0, val)
            _warn_invalid_env(name, raw, fallback)
    return float(fallback)


def _stale_sec_from_environ() -> float:
    return _seconds_from_environ("PLAYTEST_LOCK_STALE_SEC", DEFAULT_STALE_SEC)


def _heartbeat_interval_from_environ() -> float:
    return _seconds_from_environ(
        "PLAYTEST_LOCK_HEARTBEAT_SEC", DEFAULT_HEARTBEAT_INTERVAL_SEC
    )


def flock_path_for(lock_path: Path) -> Path:
    return Path(str(lock_path) + ".flock")


def utc_now_iso(env: LockEnv | None = None) -> str:
    """UTC timestamp with second precision, always Z-suffixed.

    Reads the injected clock, so a simulated run stamps virtual time.
    """
    return format_utc(_env(env).now())


def format_utc(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_utc_timestamp(value: str | None) -> float | None:
    """Parse ISO-8601 (…Z or offset) or unix epoch seconds → epoch float.

    An offset-less stamp violates the documented ``<UTC ISO8601 Z>`` format,
    but must not flip meaning with the host timezone: it is read as UTC so
    staleness stays deterministic across hosts and containers.
    """
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
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def new_session_id(prefix: str = "playtest", *, env: LockEnv | None = None) -> str:
    """Same shape as Atomic scripts/new-session-id.sh: prefix-UTC-hex."""
    if not re.fullmatch(r"[a-z][a-z0-9]*", prefix):
        raise ValueError(
            "prefix must start with a lowercase letter and contain only "
            "lowercase letters and digits"
        )
    e = _env(env)
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(e.now()))
    suffix = e.token_hex(6)
    return f"{prefix}-{ts}-{suffix}"


def read_lock(path: Path | None = None, *, env: LockEnv | None = None) -> LockState:
    path = path or default_lock_path()
    store = _env(env).storage
    if not store.is_file(path):
        return LockState(running=False, session=None, path=path)
    text = store.read_text(path)
    if text is None:
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
    env: LockEnv | None = None,
) -> None:
    e = _env(env)
    store = e.storage
    store.mkdir_parents(path)
    if running:
        if not session:
            raise ValueError("session is required when running=yes")
        now = utc_now_iso(e)
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
    tmp = path.with_name(f".{path.name}.tmp.{e.pid()}.{e.token_hex(4)}")
    try:
        # Atomic publish: a crash between these two calls must leave the old
        # payload intact, never a half-written one. The simulator injects a
        # crash here on purpose.
        store.write_text(tmp, body)
        store.replace(tmp, path)
    finally:
        if store.exists(tmp):
            try:
                store.unlink(tmp)
            except OSError:
                pass


def is_stale(
    state: LockState | None = None,
    *,
    path: Path | None = None,
    max_age_sec: float | None = None,
    now: float | None = None,
    env: LockEnv | None = None,
) -> bool:
    """True when running=yes but heartbeat is missing or older than max age.

    Free locks are not stale. Used by agents to see if a holder is still
    refreshing, and by acquire to reclaim crashed holders (with process check).
    """
    e = _env(env)
    state = state if state is not None else read_lock(path, env=e)
    if not state.running:
        return False
    limit = e.stale_sec() if max_age_sec is None else max_age_sec
    now_t = e.now() if now is None else now
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


def _with_flock(path: Path, fn: Callable[[], None], *, env: LockEnv | None = None) -> None:
    _env(env).storage.exclusive(path, fn)


def can_start(
    session: str,
    *,
    path: Path | None = None,
    live_probe: Callable[[], bool] | None = None,
    max_age_sec: float | None = None,
    env: LockEnv | None = None,
) -> bool:
    """Dry-run of acquire rules without writing."""
    if not session:
        return False
    e = _env(env)
    path = path or default_lock_path()
    # Default: client OR dedicated/zdtd (full playtest runtime).
    probe = live_probe if live_probe is not None else default_live_runtime_running
    state = read_lock(path, env=e)
    if state.running and state.session and state.session != session:
        if is_stale(state, max_age_sec=max_age_sec, env=e) and not probe():
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
    env: LockEnv | None = None,
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
    e = _env(env)
    path = path or default_lock_path()
    probe = live_probe if live_probe is not None else default_live_runtime_running
    result: dict[str, LockState | None] = {"state": None}

    def _body() -> None:
        state = read_lock(path, env=e)
        live = probe()
        if state.running and state.session and state.session != session:
            stale = is_stale(state, max_age_sec=max_age_sec, env=e)
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
                age = state.heartbeat_age_sec_at(e.now())
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
                and is_stale(state, max_age_sec=max_age_sec, env=e)
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
        now = utc_now_iso(e)
        # Preserve original acquired time on re-entrant refresh by same session.
        acq = (
            state.acquired
            if state.running and state.session == session and state.acquired
            else now
        )
        write_lock(
            path,
            running=True,
            session=session,
            acquired=acq,
            heartbeat=now,
            env=e,
        )
        result["state"] = read_lock(path, env=e)

    _with_flock(path, _body, env=e)
    assert result["state"] is not None
    return result["state"]


def heartbeat(
    session: str,
    *,
    path: Path | None = None,
    env: LockEnv | None = None,
) -> LockState:
    """Refresh heartbeat for the owning session. No-op fail if not owner."""
    if not session or not str(session).strip():
        raise PlaytestLockError("session id is required", reason="bad_session")
    session = str(session).strip()
    e = _env(env)
    path = path or default_lock_path()
    result: dict[str, LockState | None] = {"state": None}

    def _body() -> None:
        state = read_lock(path, env=e)
        if not state.running or state.session != session:
            raise PlaytestLockError(
                f"cannot heartbeat: lock not owned by session={session} "
                f"(holder={state.session!r}, file {path})",
                held_by=state.session,
                reason="foreign_holder",
            )
        now = utc_now_iso(e)
        write_lock(
            path,
            running=True,
            session=session,
            acquired=state.acquired or now,
            heartbeat=now,
            env=e,
        )
        result["state"] = read_lock(path, env=e)

    _with_flock(path, _body, env=e)
    assert result["state"] is not None
    return result["state"]


def release(
    session: str,
    *,
    path: Path | None = None,
    env: LockEnv | None = None,
) -> LockState:
    """Release only if the file actually names us.

    A release that does not name the current holder writes nothing. Writing
    ``running=no`` whenever the record merely fails to name someone *else*
    turns a late or duplicated release into a claim wipe: deterministic
    simulation reaches this by corrupting the shared file (it is documented
    as shared with the Atomic / 7dtd-mods helpers) so it momentarily reads
    free, then letting a stale exit handler publish ``running=no`` over a
    live holder. Refusing to write unless we are the recorded holder removes
    that window and keeps release idempotent.
    """
    if not session or not str(session).strip():
        raise PlaytestLockError("session id is required", reason="bad_session")
    session = str(session).strip()
    e = _env(env)
    path = path or default_lock_path()
    result: dict[str, LockState | None] = {"state": None}

    def _body() -> None:
        state = read_lock(path, env=e)
        if state.running and state.session and state.session != session:
            raise PlaytestLockError(
                f"playtest lock owned by session={state.session}; not releasing "
                f"(file {path})",
                held_by=state.session,
                reason="foreign_holder",
            )
        if not (state.running and state.session == session):
            # Free, unparseable, or claimed by a record that does not name
            # us. Nothing of ours to clear, so write nothing: publishing
            # running=no here would erase a live holder's claim.
            result["state"] = state
            return
        write_lock(path, running=False, session=None, env=e)
        result["state"] = read_lock(path, env=e)

    _with_flock(path, _body, env=e)
    assert result["state"] is not None
    return result["state"]


class HeartbeatLoop:
    """Heartbeat refresh policy, driven by whoever owns the clock.

    Production drives this from :class:`HeartbeatThread`; the deterministic
    simulator drives the very same object by stepping virtual time. Only the
    *when* differs between the two, so a bug in the refresh policy is
    reachable from simulation.
    """

    def __init__(
        self,
        session: str,
        *,
        path: Path | None = None,
        interval_sec: float | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        env: LockEnv | None = None,
    ) -> None:
        self.session = session
        self.env = env
        e = _env(env)
        self.path = path or default_lock_path()
        self.interval_sec = (
            e.heartbeat_interval_sec() if interval_sec is None else interval_sec
        )
        self.on_error = on_error
        self.last_touch: float | None = None
        self.touches = 0
        self.errors = 0
        # Set when the record stopped naming us while we still believe we
        # hold it (someone else took over, or the shared file was clobbered).
        # Exclusivity is no longer guaranteed once this is true; the policy
        # decision of what a run does about it belongs to the orchestrator.
        self.lost_claim = False

    def due(self, now: float | None = None) -> bool:
        t = _env(self.env).now() if now is None else now
        if self.last_touch is None:
            return True
        return (t - self.last_touch) >= self.interval_sec

    def tick(self, now: float | None = None, *, force: bool = False) -> bool:
        """Refresh if due. Returns True when a refresh was attempted."""
        e = _env(self.env)
        t = e.now() if now is None else now
        if not force and not self.due(t):
            return False
        self.last_touch = t
        try:
            heartbeat(self.session, path=self.path, env=self.env)
            self.touches += 1
        except BaseException as ex:  # noqa: BLE001 - report and keep trying
            self.errors += 1
            if isinstance(ex, PlaytestLockError) and ex.reason == "foreign_holder":
                self.lost_claim = True
            if self.on_error is not None:
                try:
                    self.on_error(ex)
                except Exception:
                    pass
        return True


class HeartbeatThread:
    """Daemon thread that refreshes the lock heartbeat until stopped."""

    def __init__(
        self,
        session: str,
        *,
        path: Path | None = None,
        interval_sec: float | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        env: LockEnv | None = None,
    ) -> None:
        self.loop = HeartbeatLoop(
            session,
            path=path,
            interval_sec=interval_sec,
            on_error=on_error,
            env=env,
        )
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
        # Cleanup paths call stop() unconditionally; if start() itself failed
        # (thread resource exhaustion) join would raise and abort whatever
        # cleanup follows, e.g. the lock release in playtest_run's finally.
        if not self._started:
            return
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # Immediate first touch so age stays low even if interval is long.
        self.loop.tick(force=True)
        while not self._stop.wait(self.loop.interval_sec):
            self.loop.tick(force=True)
