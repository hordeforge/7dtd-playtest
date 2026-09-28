#!/usr/bin/env python3
"""Playtest target adapters: who owns the server process, and which server.

Two independent axes replace the old five-value ``--target``:

  provision  managed  Safehouse (7dtd-sandbox) brings the server up and tears
                      it down; the run owns an isolated instance.
             attach   the server is already running; playtest never touches
                      its lifecycle, config, mods or save.
  backend    stock    the stock 7DaysToDieServer dedicated
             zdtd     the Zig dedi under test

``readonly`` is attach-only and names a host playtest must not write to at all
(a 7dtd-server-container production LAN server). Production deploy stays in
7dtd-server-container; this module never deploys, stages or restarts it.

A managed stock run is always a sandbox instance. There is no path that starts
a dedicated inside the user's Steam install: isolation, fresh save, port
allocation and process teardown all belong to `sb`, and a second implementation
here is what let a playtest rewrite the install's platform.cfg.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _warn(message: str) -> None:
    """Report a failure that must not raise. This module has no log channel."""
    print(f"playtest-targets: {message}", file=sys.stderr)


PROVISIONS = ("managed", "attach")
BACKENDS = ("stock", "zdtd")

# How long `sb up` may take to bind the game port before the run gives up.
SANDBOX_UP_TIMEOUT_SEC = 240

# Wall-clock bound on one `sb` invocation. `sb up` gets its own shorter
# SANDBOX_UP_TIMEOUT_SEC for the port wait; this is the outer bound for a sb
# that never returns at all (a wedged Proton download, a lock prompt on a
# second instance, a hung `dotnet build` inside `sb stage`). Without it the
# orchestrator's poll loop never starts, so the run's deadline cannot fire and
# the live client and exclusive-lock claim are stranded.
SB_COMMAND_TIMEOUT_SEC = 900.0

# The pair name is one directory under <sandbox_root>/instances and one
# argument to every `sb` call, so it must stay a single path component: a
# name carrying a separator walks out of the instance directory (`srv-../..`
# is the sandbox root's parent) and hands `sb` a path the run never named.
# A leading `-` would be read as a flag instead, and a leading `.` names a
# hidden or relative component, so neither starts the name.
SANDBOX_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

# Long enough for any readable instance pair, short enough that a pasted
# sentence is refused rather than turned into a directory name.
SANDBOX_NAME_MAX_LEN = 64

# The contract ports the run connects to. A value outside the range is a
# corrupt contract line, not a port the run may carry into a connect attempt.
MIN_PORT = 1
MAX_PORT = 65535

_ENV_ASSIGN = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(?:'([^']*)'|\"([^\"]*)\"|(.*))$"
)


class TargetError(RuntimeError):
    """Bring-up or teardown of the target failed."""


@dataclass(frozen=True)
class TargetPlan:
    """Resolved bring-up plan for one playtest run."""

    provision: str
    backend: str
    readonly: bool = False
    # Sandbox instance names (managed stock only).
    sandbox_server: str | None = None
    sandbox_client: str | None = None
    sandbox_root: Path | None = None
    # Filled in from instance.env once the instance exists.
    game_srv: Path | None = None
    userdata: Path | None = None
    port: int | None = None
    telnet_port: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def start_server(self) -> bool:
        return self.provision == "managed"

    @property
    def is_attach(self) -> bool:
        return self.provision == "attach"

    @property
    def is_sandbox(self) -> bool:
        """Managed stock runs on a Safehouse instance."""
        return self.provision == "managed" and self.backend == "stock"

    @property
    def label(self) -> str:
        suffix = " readonly" if self.readonly else ""
        return f"{self.provision}/{self.backend}{suffix}"


def normalize_provision(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        return "managed"
    value = str(raw).strip().lower()
    if value not in PROVISIONS:
        raise ValueError(
            f"unknown provision {raw!r}; expected one of: {', '.join(PROVISIONS)}"
        )
    return value


def normalize_backend(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        return "stock"
    value = str(raw).strip().lower()
    if value not in BACKENDS:
        raise ValueError(
            f"unknown backend {raw!r}; expected one of: {', '.join(BACKENDS)}"
        )
    return value


def resolve_target(
    *,
    provision: str | None = None,
    backend: str | None = None,
    readonly: bool = False,
    no_server: bool = False,
    sandbox_name: str | None = None,
    sandbox_root: Path | None = None,
    workspace: Path | None = None,
) -> TargetPlan:
    """Map CLI/env knobs onto a TargetPlan.

    ``--no-server`` is the long-standing shorthand for attach and still wins
    over an explicit ``--provision managed``: a caller that says "do not start
    a server" must never get one started.
    """
    resolved_provision = normalize_provision(
        provision if provision is not None and str(provision).strip()
        else os.environ.get("PLAYTEST_PROVISION")
    )
    if no_server:
        resolved_provision = "attach"
    resolved_backend = normalize_backend(
        backend if backend is not None and str(backend).strip()
        else os.environ.get("PLAYTEST_BACKEND")
    )
    if readonly and resolved_provision != "attach":
        raise ValueError(
            "--readonly names a host playtest must not write to, so it is "
            "attach-only; pass --no-server / --provision attach"
        )

    if resolved_provision == "attach":
        notes = ["attach to an already-running server; lifecycle stays with its owner"]
        if readonly:
            notes.append(
                "readonly: never wipe, stage, restart or rewrite config on this host"
            )
        return TargetPlan(
            provision="attach",
            backend=resolved_backend,
            readonly=readonly,
            notes=tuple(notes),
        )

    if resolved_backend == "zdtd":
        return TargetPlan(
            provision="managed",
            backend="zdtd",
            notes=("zdtd binary under test; orchestrator owns its process",),
        )

    ws = workspace or Path(__file__).resolve().parents[2]
    sb_root = sandbox_root or (ws / "7dtd-sandbox")
    pair = normalize_sandbox_name(
        sandbox_name or os.environ.get("PLAYTEST_SANDBOX_NAME") or "playtest"
    )
    srv_name = f"srv-{pair}"
    cli_name = f"client-{pair}"
    env_map = load_sandbox_env(sb_root, srv_name)
    return TargetPlan(
        provision="managed",
        backend="stock",
        sandbox_server=srv_name,
        sandbox_client=cli_name,
        sandbox_root=sb_root,
        game_srv=_optional_path(env_map.get("SERVER_GAME"), "SERVER_GAME"),
        userdata=_optional_path(env_map.get("SERVER_USERDATA"), "SERVER_USERDATA"),
        port=_optional_int(env_map.get("SERVER_PORT"), "SERVER_PORT"),
        telnet_port=_optional_int(env_map.get("SERVER_TELNET_PORT"), "SERVER_TELNET_PORT"),
        notes=("Safehouse owns isolation, ports, fresh save and teardown",),
    )


def normalize_sandbox_name(raw: str) -> str:
    """A pair name that can only ever name one instance directory.

    Raised as a ValueError like the other argument checks, so a name the
    caller cannot have meant stops the run before it stages, wipes or stops
    anything: the name reaches `sb` as an argv element and every instance
    path is built from it.
    """
    name = str(raw).strip()
    if not SANDBOX_NAME_RE.fullmatch(name) or len(name) > SANDBOX_NAME_MAX_LEN:
        raise ValueError(
            f"invalid sandbox name {raw!r}; expected one path component of "
            f"[A-Za-z0-9_.-] starting alphanumeric, at most "
            f"{SANDBOX_NAME_MAX_LEN} characters"
        )
    return name


def _optional_int(raw: str | None, key: str) -> int | None:
    """A contract integer: unset is None, set-but-malformed is a TargetError.

    A port that fails to parse used to read as absent, which left the run on
    the pre-`sb up` placeholder (or the lab default) and reported the unusable
    number rather than the corrupt line that caused it. A number outside the
    port range is the same fault: the run would carry it into every connect
    attempt and time out against an address no listener can own.
    """
    if raw is None or not str(raw).strip():
        return None
    value = str(raw).strip()
    try:
        number = int(value)
    except ValueError:
        raise TargetError(
            f"{key}={value!r} in the instance contract is not an integer"
        ) from None
    if not MIN_PORT <= number <= MAX_PORT:
        raise TargetError(
            f"{key}={value!r} in the instance contract is outside the port "
            f"range {MIN_PORT}-{MAX_PORT}"
        )
    return number


def _optional_path(raw: str | None, key: str) -> Path | None:
    """A contract path: unset is None, relative or unusable is a TargetError.

    `sb` writes the absolute location it created. A relative one resolves
    against whatever directory the step runs in, so a run would wipe or
    stage a path the contract never named.
    """
    if raw is None or not str(raw).strip():
        return None
    value = str(raw).strip()
    path = Path(value)
    if not path.is_absolute():
        raise TargetError(
            f"{key}={value!r} in the instance contract is not an absolute path"
        )
    return path


def parse_sb_env_output(text: str) -> dict[str, str]:
    """Parse `sb env` / instance.env style KEY=VALUE lines into a dict."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_ASSIGN.match(stripped)
        if not match:
            continue
        key = match.group(1)
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4) or ""
        out[key] = value
    return out


def sb_path(sandbox_root: Path) -> Path:
    return sandbox_root / "scripts" / "sb"


def load_sandbox_env(sandbox_root: Path, name: str) -> dict[str, str]:
    """Load the sandbox instance contract for ``name``; {} when it is absent."""
    inst_env = sandbox_root / "instances" / name / "instance.env"
    if inst_env.is_file():
        try:
            text = inst_env.read_text(encoding="utf-8")
        except UnicodeDecodeError as ex:
            # sb writes UTF-8; anything else is a corrupted contract, and
            # reading it as if it were absent would hand the run someone
            # else's ports. Name it instead.
            raise TargetError(f"{inst_env} is not valid UTF-8: {ex}") from ex
        except OSError as ex:
            # The file is there (is_file() passed) but will not read: EACCES,
            # EIO, or a race with sb rewriting it. An empty contract means no
            # SERVER_PORT, so the run plans onto a pre-`sb up` placeholder,
            # which is the same wrong-port outcome the decode case avoids.
            raise TargetError(f"cannot read {inst_env}: {ex}") from ex
        return parse_sb_env_output(text)
    return {}


def check_sandbox_available(plan: TargetPlan) -> None:
    """Fail early and by name when a managed stock run has no Safehouse CLI.

    Read-only on purpose: resolving a run must never create an instance. The
    instance (and its 5-port block) comes into being in ensure_sandbox_server,
    on the live path, so an offline gate that calls main() cannot leave a
    multi-gigabyte game copy behind.
    """
    if not plan.is_sandbox:
        return
    if plan.sandbox_root is None or plan.sandbox_server is None:
        raise TargetError("managed stock plan is missing sandbox_root/server name")
    sb = sb_path(plan.sandbox_root)
    if not sb.is_file():
        raise TargetError(
            f"Safehouse CLI missing: {sb}. A managed stock run is a sandbox "
            "instance; check out 7dtd-sandbox beside this repo or pass "
            "--sandbox-root"
        )


def ensure_sandbox_server(
    plan: TargetPlan,
    *,
    wipe: bool = True,
    mods: list[Path] | None = None,
    config: dict[str, str] | None = None,
    timeout: int = SANDBOX_UP_TIMEOUT_SEC,
) -> dict[str, str]:
    """Wipe, stage, configure and start the sandbox server; block until ready.

    Returns the instance.env map. Every step is an `sb` call: the orchestrator
    owns no isolation logic of its own. Raises TargetError on failure.
    """
    if not plan.is_sandbox:
        return {}
    check_sandbox_available(plan)
    assert plan.sandbox_root is not None
    assert plan.sandbox_server is not None

    name = plan.sandbox_server
    inst = plan.sandbox_root / "instances" / name
    if not inst.is_dir():
        _run_sb(plan, ["create-server", name])
    elif wipe:
        # Fresh save is a playtest hard rule; wipe resets userdata and Mods.
        _run_sb(plan, ["wipe", name])

    if mods:
        _run_sb(plan, ["stage", name, *[str(m) for m in mods]])
    if config:
        _run_sb(plan, ["render-config", name, *[f"{k}={v}" for k, v in config.items()]])

    _run_sb(plan, ["up", name, "--timeout", str(timeout)])
    env_map = load_sandbox_env(plan.sandbox_root, name)
    if not env_map.get("SERVER_PORT"):
        raise TargetError(f"sandbox instance {name} has no SERVER_PORT after `sb up`")
    return env_map


def sandbox_client_paths(plan: TargetPlan) -> dict[str, Path]:
    """Where the client instance will live, derived from its name alone.

    No disk access and no side effect, so a run can validate paths and pick its
    lock file before it is allowed to create anything. The instance layout is
    Safehouse's contract: <root>/instances/<name>/{game,compatdata}.
    """
    if not plan.is_sandbox or plan.sandbox_root is None or plan.sandbox_client is None:
        return {}
    inst = plan.sandbox_root / "instances" / plan.sandbox_client
    return {"instance": inst, "game": inst / "game", "compat": inst / "compatdata"}


# Windowed 1280x720 unless the operator overrides SB_RES / SB_FULLSCREEN for
# the `sb env` call. Kept as a fallback string so a run still launches windowed
# when `sb env` cannot be read; Safehouse's sandbox_screen_args is the source.
DEFAULT_SCREEN_ARGS = "-screen-fullscreen 0 -screen-width 1280 -screen-height 720"


def sandbox_screen_args(plan: TargetPlan) -> str:
    """The instance's window contract, as `sb env` reports it.

    A sandbox client must never take the display fullscreen: it is a test
    fixture, and several of them have to be visible at once. The command line
    wins over whatever the Proton prefix last saved, so these are passed at
    every launch rather than seeded once.
    """
    if not plan.is_sandbox or plan.sandbox_root is None or plan.sandbox_client is None:
        return DEFAULT_SCREEN_ARGS
    if not sb_path(plan.sandbox_root).is_file():
        return DEFAULT_SCREEN_ARGS
    try:
        proc = _run_sb(plan, ["env", plan.sandbox_client], check=False)
    except TargetError as ex:
        # A default window command line is the right answer, but say why: a
        # silent default reads as "the instance asked for no screen args".
        _warn(f"sb env {plan.sandbox_client} failed ({ex}); using the default screen args")
        return DEFAULT_SCREEN_ARGS
    return parse_sb_env_output(proc.stdout).get("SB_SCREEN_ARGS") or DEFAULT_SCREEN_ARGS


def ensure_sandbox_client(
    plan: TargetPlan,
    *,
    wipe: bool = True,
    mods: list[Path] | None = None,
) -> dict[str, str]:
    """Create, wipe, stage and resolve the Safehouse client instance.

    A managed run drives the instance's own client, not the operator's Steam
    install: the instance is the Windows depot under Proton with no Steam auth,
    its mods are the ones the suite declared, and `sb wipe` gives every run the
    same starting prefix. The Steam install may be a different build entirely
    (the Linux native client has no 7DaysToDie.exe for Proton to launch).

    Returns the `sb env` contract (GAME, COMPAT, PROTON, LOGFILE).
    """
    if not plan.is_sandbox:
        return {}
    if plan.sandbox_root is None or plan.sandbox_client is None:
        raise TargetError("managed stock plan is missing sandbox_root/client name")
    check_sandbox_available(plan)

    name = plan.sandbox_client
    if not (plan.sandbox_root / "instances" / name).is_dir():
        _run_sb(plan, ["create", name])
    elif wipe:
        _run_sb(plan, ["wipe", name])
    if mods:
        _run_sb(plan, ["stage", name, *[str(m) for m in mods]])

    proc = _run_sb(plan, ["env", name])
    env_map = parse_sb_env_output(proc.stdout)
    if not env_map.get("GAME"):
        raise TargetError(f"sandbox client {name} has no GAME in `sb env` output")
    return env_map


def _stop_sandbox_instance(plan: TargetPlan, name: str | None) -> None:
    if not plan.is_sandbox or plan.sandbox_root is None or name is None:
        return
    if not sb_path(plan.sandbox_root).is_file():
        return
    # Teardown runs on the way out of a run that may already be failing, so a
    # stop must not raise. It must still be reported, and a non-zero exit has
    # to be read: an instance left up is a live process holding its port
    # block, and swallowing the reason leaves the operator no way to tell a
    # clean teardown from an abandoned one.
    try:
        proc = _run_sb(plan, ["stop", name], check=False)
    except TargetError as ex:
        _warn(f"sandbox {name} could not be stopped: {ex}")
        return
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        _warn(f"sb stop {name} failed, the instance may still be running: {tail}")


def stop_sandbox_client(plan: TargetPlan) -> None:
    """Best-effort ``sb stop`` for the sandbox client instance."""
    _stop_sandbox_instance(plan, plan.sandbox_client)


def stop_sandbox_server(plan: TargetPlan) -> None:
    """Best-effort ``sb stop`` for the sandbox server instance."""
    _stop_sandbox_instance(plan, plan.sandbox_server)


def _run_sb(
    plan: TargetPlan,
    argv: list[str],
    *,
    check: bool = True,
    timeout: float = SB_COMMAND_TIMEOUT_SEC,
) -> subprocess.CompletedProcess[str]:
    assert plan.sandbox_root is not None
    sb = sb_path(plan.sandbox_root)
    try:
        proc = subprocess.run(
            ["bash", str(sb), *argv],
            check=False,
            capture_output=True,
            text=True,
            # sb echoes sandbox and instance paths, which carry the operator's
            # home directory name; a locale-default decode raises on the first
            # non-ASCII byte instead of reporting the failure.
            encoding="utf-8",
            errors="replace",
            cwd=str(plan.sandbox_root),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as ex:
        # sb was killed partway, so whatever it printed before the deadline is
        # the only clue about where it wedged; keep it in the message. The
        # capture is text-mode, but TimeoutExpired is typed as carrying
        # whatever the pipe held, so bytes have to be decoded, not str()'d.
        raw: bytes | str = ex.stderr or ex.stdout or b""
        detail: str = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        raise TargetError(
            f"sb {' '.join(argv)} did not finish within {timeout:g}s: "
            f"{(detail.strip() or 'no output')[-200:]}"
        ) from ex
    except OSError as ex:
        raise TargetError(f"could not run sb {' '.join(argv)}: {ex}") from ex
    if check and proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else f"exit {proc.returncode}"
        raise TargetError(f"sb {' '.join(argv)} failed: {tail}")
    return proc


def apply_plan_to_args(args: argparse.Namespace, plan: TargetPlan) -> None:
    """Mutate a parsed argparse namespace from a TargetPlan.

    Sets the lifecycle flags, then overlays the instance's paths and ports.
    ``game_srv``, ``userdata`` and (absent an explicit marker) the telnet port
    are overwritten outright: the Safehouse instance allocated them, so an
    operator value for the same slot cannot be honoured and silently keeping it
    would point the run at a server the instance is not running.
    """
    args.no_server = plan.is_attach
    args.provision = plan.provision
    args.server = plan.backend
    args.readonly = plan.readonly
    if plan.port is not None and getattr(args, "port", None) is None:
        args.port = plan.port
    if plan.telnet_port is not None and not getattr(args, "_admin_port_explicit", False):
        args.admin_port = plan.telnet_port
    if plan.game_srv is not None:
        args.game_srv = plan.game_srv
    if plan.userdata is not None:
        args.userdata = plan.userdata


def overlay_instance_env(args: argparse.Namespace, env_map: dict[str, str]) -> None:
    """Overlay the live instance.env onto args after `sb up` allocated it.

    Raises TargetError naming the key and value: the ports this map carries
    are the ones the run connects to, and a silently skipped line leaves the
    pre-`sb up` placeholder in place.
    """
    port = _optional_int(env_map.get("SERVER_PORT"), "SERVER_PORT")
    telnet = _optional_int(env_map.get("SERVER_TELNET_PORT"), "SERVER_TELNET_PORT")
    game = _optional_path(env_map.get("SERVER_GAME"), "SERVER_GAME")
    userdata = _optional_path(env_map.get("SERVER_USERDATA"), "SERVER_USERDATA")
    if port is not None:
        args.port = port
    if telnet is not None:
        args.admin_port = telnet
    if game is not None:
        args.game_srv = game
    if userdata is not None:
        args.userdata = userdata


def target_report_fields(plan: TargetPlan) -> dict[str, object]:
    """JSON-serializable fields for the run report payload."""
    return {
        "provision": plan.provision,
        "backend": plan.backend,
        "readonly": plan.readonly,
        "start_server": plan.start_server,
        "sandbox_server": plan.sandbox_server,
        "sandbox_client": plan.sandbox_client,
        "notes": list(plan.notes),
    }
