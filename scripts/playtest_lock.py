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
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_LOCK_REL = Path(".cache") / "7dtd-playtest" / "playtest_running"
SESSION_RE = re.compile(r"^[a-z][a-z0-9]*-[0-9]{8}-[0-9]{6}-[0-9a-f]+$")
# Lock-file field safety: a session id is written verbatim into a
# line-oriented key=value file every agent on the host parses, so it must
# never carry newlines (field injection) or '=' / a leading '#' (key
# spoofing). Deliberately looser than SESSION_RE so a conforming external
# holder id still passes while injection stays impossible.
SESSION_FIELD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DEFAULT_STALE_SEC = 120
DEFAULT_HEARTBEAT_INTERVAL_SEC = 30

PROC_ROOT = Path("/proc")
STOCK_CLIENT_EXECUTABLES = ("7DaysToDie.exe", "DaysToDie.exe")
STOCK_DEDICATED_EXECUTABLES = ("7DaysToDieServer.x86_64",)
ZDTD_EXECUTABLES = ("zdtd",)
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

    def canonical_path(self, path: Path) -> Path:
        """One inode identity for the lock file and its flock sidecar.

        Acquire serializes on ``flock_path_for(path)``. Two spellings of the
        same file (relative vs absolute, ``~``, a symlink) must not take
        different sidecars and both publish ``running=yes``.
        """
        p = path.expanduser()
        try:
            return p.resolve()
        except OSError:
            return p if p.is_absolute() else Path.cwd() / p

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
        path = self.canonical_path(path)
        self.mkdir_parents(path)
        flock_path = flock_path_for(path)
        # flock is per-process on some platforms (and a no-op on some NFS).
        # Threads in this process still need a mutex around the read-modify
        # publish, or two HeartbeatThread/acquire callers can both win.
        gate = _thread_gate(flock_path)
        with gate, open(flock_path, "a+", encoding="utf-8") as lf:
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
_THREAD_GATES: dict[str, threading.Lock] = {}
_THREAD_GATES_GUARD = threading.Lock()


def _thread_gate(flock_path: Path) -> threading.Lock:
    key = str(flock_path)
    with _THREAD_GATES_GUARD:
        gate = _THREAD_GATES.get(key)
        if gate is None:
            gate = threading.Lock()
            _THREAD_GATES[key] = gate
        return gate


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


def _require_session(session: str) -> str:
    """Strip and validate a caller-supplied session id, or refuse.

    Single gate for acquire/heartbeat/release so none of them can write a
    field-breaking id into the lock file.
    """
    if not session or not str(session).strip():
        raise PlaytestLockError("session id is required", reason="bad_session")
    session = str(session).strip()
    if not SESSION_FIELD_RE.fullmatch(session):
        raise PlaytestLockError(
            "session id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127} "
            f"(single line, no '=' or control chars); got {session!r}",
            reason="bad_session",
        )
    return session


@dataclass(frozen=True)
class LockState:
    running: bool
    session: str | None
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



def default_lock_path(instance: str | None = None) -> Path:
    """Lock file for the client this run drives.

    The lock exists because a client is exclusive, so it is scoped to the
    client that is exclusive. A Safehouse client instance has its own game
    tree, Proton prefix and window, so two runs on different instances share
    nothing and both may proceed; two runs on the same instance must not.
    Without an instance (the operator's single Steam client) the scope is the
    machine, as before.

    ``PLAYTEST_LOCK_FILE`` still overrides everything, so a shared-machine
    convention that points several stacks at one file keeps working.
    """
    env = os.environ.get("PLAYTEST_LOCK_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    base = Path.home() / DEFAULT_LOCK_REL
    if not instance:
        return base
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", instance)
    return base.with_name(f"{base.name}-{safe}")


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


def _lock_path(path: Path | None, env: LockEnv | None) -> Path:
    p = default_lock_path() if path is None else path
    return _env(env).storage.canonical_path(p)


def utc_now_iso(env: LockEnv | None = None) -> str:
    """UTC timestamp with second precision, always Z-suffixed.

    Reads the injected clock, so a simulated run stamps virtual time.
    """
    return format_utc(_env(env).now())


def format_utc(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, UTC)
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
            epoch = float(s)
        except ValueError:
            return None
        # A syntactically numeric but out-of-range epoch becomes ``inf``.
        # Treat it as corrupt just like an unparseable heartbeat: otherwise
        # ``now - inf`` is forever negative and a dead holder can never be
        # reclaimed.
        return epoch if math.isfinite(epoch) else None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
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
    e = _env(env)
    path = _lock_path(path, e)
    store = e.storage
    if not store.is_file(path):
        return LockState(running=False, session=None)
    text = store.read_text(path)
    if text is None:
        return LockState(running=False, session=None)
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
    path = _lock_path(path, e)
    store = e.storage
    store.mkdir_parents(path)
    if running:
        if not session:
            raise ValueError("session is required when running=yes")
        if not SESSION_FIELD_RE.fullmatch(session):
            raise ValueError(f"session id breaks lock-file fields: {session!r}")
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
            with suppress(OSError):
                store.unlink(tmp)


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


def _process_environ(pid_dir: Path) -> str:
    """NUL-separated environment of one process, "" when unreadable."""
    try:
        return (pid_dir / "environ").read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


def client_running_for_compat(compat: Path, *, proc_root: Path = PROC_ROOT) -> bool:
    """True when a client is running against this Proton prefix specifically.

    Matches on ``STEAM_COMPAT_DATA_PATH``, the same discriminator `sb stop`
    uses, so one sandbox client never reads as another. This is what lets
    several sandbox runs share a machine: disjoint instances, disjoint port
    blocks, disjoint prefixes, disjoint locks.
    """
    wanted = f"STEAM_COMPAT_DATA_PATH={Path(compat).resolve()}"
    for pid_dir in _runtime_pids(proc_root):
        if wanted in _process_environ(pid_dir).split("\0"):
            return True
    return False


def client_running(*, proc_root: Path = PROC_ROOT) -> bool:
    """True when a stock/Proton client is present.

    This is what the lock is for. There is one stock client, one display and
    one GPU on a machine, so two orchestrated runs cannot share it; a dedicated
    server is not scarce in the same way. Inspect the executable each process
    is running, so a shell, terminal history or agent prompt that merely
    mentions these names cannot satisfy the check.
    """
    return _any_executable_running(
        STOCK_CLIENT_EXECUTABLES, proc_root=proc_root
    ) or _any_preloader_running_game(proc_root=proc_root)


def dedicated_running(*, proc_root: Path = PROC_ROOT) -> bool:
    """True when a stock dedicated server is present, on any instance.

    Not a lock input. A managed run's dedicated belongs to its Safehouse
    instance, which owns a unique port block and refuses a second start of
    itself (`sb up`), so someone else's dedicated is no reason to refuse a run.
    Kept for diagnostics and for callers that genuinely need the fact.
    """
    return _any_executable_running(STOCK_DEDICATED_EXECUTABLES, proc_root=proc_root)


def zdtd_running(*, proc_root: Path = PROC_ROOT) -> bool:
    """True when a zdtd server is present.

    Unlike the stock dedicated, zdtd is still started by the orchestrator on a
    port the caller chose, so a second zdtd run would double-bind it. A managed
    zdtd run therefore adds this to its lock gate; nothing else does.
    """
    return _any_executable_running(ZDTD_EXECUTABLES, proc_root=proc_root)


def client_or_zdtd_running() -> bool:
    """Lock gate for a managed zdtd run: the shared client, or a zdtd holding
    the port this run is about to bind."""
    return client_running() or zdtd_running()


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
    path = _lock_path(path, e)
    # Default: client OR dedicated/zdtd (full playtest runtime).
    probe = live_probe if live_probe is not None else client_running
    state = read_lock(path, env=e)
    if state.running and state.session and state.session != session:
        # A foreign claim only frees up once stale AND no runtime is alive.
        return is_stale(state, max_age_sec=max_age_sec, env=e) and not probe()
    return not (probe() and not (state.running and state.session == session))


def wait_until_can_start(
    session: str,
    *,
    path: Path | None = None,
    timeout_sec: float = 1800,
    interval_sec: float = 10,
    live_probe: Callable[[], bool] | None = None,
    env: LockEnv | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> bool:
    """Poll :func:`can_start` until True, or return False at ``timeout_sec``.

    Consumers (matrix runners, agents) must wait here instead of parsing
    ``running=`` / ``heartbeat=`` themselves. Missing heartbeat is stale,
    matching :func:`is_stale`.
    """
    e = _env(env)
    sleep = time.sleep if sleeper is None else sleeper
    deadline = e.now() + timeout_sec
    while True:
        if can_start(
            session, path=path, live_probe=live_probe, env=e
        ):
            return True
        now_t = e.now()
        if now_t >= deadline:
            return False
        sleep(min(interval_sec, max(0.0, deadline - now_t)))


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
    session = _require_session(session)
    e = _env(env)
    path = _lock_path(path, e)
    probe = live_probe if live_probe is not None else client_running
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
        # Free lock but client and/or dedicated already up: refuse unless the
        # file also carries a stale foreign claim, which the block above
        # already rejected as stale_but_live.
        if live and not (state.running and state.session == session) and not (
            state.running
            and state.session
            and state.session != session
            and is_stale(state, max_age_sec=max_age_sec, env=e)
        ):
            raise PlaytestLockError(
                f"live playtest runtime (DaysToDie client and/or dedicated/"
                f"zdtd server) present; refusing start (file {path}"
                + (f", lock session={state.session}" if state.session else ", lock free")
                + ")",
                held_by=state.session,
                reason="live_runtime",
            )
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
    session = _require_session(session)
    e = _env(env)
    path = _lock_path(path, e)
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
    session = _require_session(session)
    e = _env(env)
    path = _lock_path(path, e)
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
        self.path = _lock_path(path, env)
        self.interval_sec = (
            e.heartbeat_interval_sec() if interval_sec is None else interval_sec
        )
        self.on_error = on_error
        self._mu = threading.RLock()
        self.last_touch: float | None = None
        self.touches = 0
        self.errors = 0
        # Set when the record stopped naming us while we still believe we
        # hold it (someone else took over, or the shared file was clobbered).
        # Exclusivity is no longer guaranteed once this is true; the policy
        # decision of what a run does about it belongs to the orchestrator.
        self._lost_event = threading.Event()

    @property
    def lost_claim(self) -> bool:
        return self._lost_event.is_set()

    def due(self, now: float | None = None) -> bool:
        t = _env(self.env).now() if now is None else now
        with self._mu:
            if self.last_touch is None:
                return True
            return (t - self.last_touch) >= self.interval_sec

    def tick(self, now: float | None = None, *, force: bool = False) -> bool:
        """Refresh if due. Returns True when a refresh was attempted."""
        e = _env(self.env)
        t = e.now() if now is None else now
        with self._mu:
            if not force and not self.due(t):
                return False
            self.last_touch = t
        try:
            # File publish is serialized by exclusive(); keep I/O off this lock.
            heartbeat(self.session, path=self.path, env=self.env)
            with self._mu:
                self.touches += 1
        except BaseException as ex:
            with self._mu:
                self.errors += 1
            if isinstance(ex, PlaytestLockError) and ex.reason == "foreign_holder":
                self._lost_event.set()
            if self.on_error is not None:
                # A misbehaving callback must not break the heartbeat loop.
                with suppress(Exception):
                    self.on_error(ex)
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
        # Cleanup paths call stop() unconditionally; join on a thread that
        # never started raises RuntimeError and would abort playtest_run's
        # finally (skipping the lock release). ident is set inside
        # Thread.start() before it returns, so a signal between that return
        # and `_started = True` still joins instead of leaking the daemon
        # across release.
        if self._thread.ident is None:
            return
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # Immediate first touch so age stays low even if interval is long,
        # unless stop() already won the race before this body ran.
        if self._stop.is_set():
            return
        self.loop.tick(force=True)
        while not self._stop.wait(self.loop.interval_sec):
            self.loop.tick(force=True)


def _cli_seconds(opt: str, raw: str, *, minimum: float) -> float | None:
    """Parse a CLI duration. nan/inf would make ``wait`` never expire
    (``now >= nan`` is False) or ``time.sleep`` raise; reject them here.
    """
    try:
        val = float(raw)
    except (TypeError, ValueError, OverflowError):
        sys.stderr.write(opt + " requires a number, got " + raw + "\n")
        return None
    if not math.isfinite(val) or val < minimum:
        sys.stderr.write(
            f"{opt} must be a finite number >= {minimum:g}, got {raw}\n"
        )
        return None
    return val


def main(argv: list[str] | None = None) -> int:
    """CLI for consumers that cannot import this module from bash.

    ``wait`` polls :func:`wait_until_can_start` and exits 0 when a new
    session could acquire, 1 on timeout, 2 on bad usage.
    ``live`` probes :func:`client_running` and exits 0 when the shared client
    is free, 1 when it is up, 2 when the probe itself fails; the exit code is
    the whole interface, so it prints nothing. A dedicated server is not
    consulted: it belongs to a Safehouse instance with its own port block, so
    it blocks nobody.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(
            "usage: playtest_lock.py wait [--timeout SEC] [--interval SEC] "
            "[--path FILE]\n"
            "       playtest_lock.py live\n"
        )
        return 0
    if args[0] == "live":
        try:
            live = client_running()
        except Exception as ex:
            sys.stderr.write(f"cannot inspect the live client: {ex}\n")
            return 2
        return 1 if live else 0
    if args[0] != "wait":
        sys.stderr.write("unknown command; expected 'wait' or 'live'\n")
        return 2
    timeout_sec = 1800.0
    interval_sec = 10.0
    path: Path | None = None
    rest = args[1:]
    i = 0
    while i < len(rest):
        opt = rest[i]
        if opt in ("--timeout", "--interval", "--path"):
            if i + 1 >= len(rest):
                sys.stderr.write(opt + " requires a value\n")
                return 2
            val = rest[i + 1]
            if opt == "--path":
                path = Path(val)
            else:
                parsed = _cli_seconds(opt, val, minimum=0.0 if opt == "--timeout" else 0.05)
                if parsed is None:
                    return 2
                if opt == "--timeout":
                    timeout_sec = parsed
                else:
                    interval_sec = parsed
            i += 2
            continue
        sys.stderr.write("unknown option " + opt + "\n")
        return 2
    sid = new_session_id("waiter")
    ok = wait_until_can_start(
        sid, path=path, timeout_sec=timeout_sec, interval_sec=interval_sec
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
