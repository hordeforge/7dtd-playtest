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

import contextlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PROVISIONS = ("managed", "attach")
BACKENDS = ("stock", "zdtd")

# How long `sb up` may take to bind the game port before the run gives up.
SANDBOX_UP_TIMEOUT_SEC = 240

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
    server_config: Path | None = None
    server_log: Path | None = None
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
    pair = (sandbox_name or os.environ.get("PLAYTEST_SANDBOX_NAME") or "playtest").strip()
    srv_name = f"srv-{pair}"
    cli_name = f"client-{pair}"
    env_map = load_sandbox_env(sb_root, srv_name)
    return TargetPlan(
        provision="managed",
        backend="stock",
        sandbox_server=srv_name,
        sandbox_client=cli_name,
        sandbox_root=sb_root,
        game_srv=_optional_path(env_map.get("SERVER_GAME")),
        userdata=_optional_path(env_map.get("SERVER_USERDATA")),
        port=_optional_int(env_map.get("SERVER_PORT")),
        telnet_port=_optional_int(env_map.get("SERVER_TELNET_PORT")),
        server_config=_optional_path(env_map.get("SERVER_CONFIG")),
        server_log=_optional_path(env_map.get("SERVER_LOG")),
        notes=("Safehouse owns isolation, ports, fresh save and teardown",),
    )


def _optional_int(raw: str | None) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _optional_path(raw: str | None) -> Path | None:
    if raw is None or not str(raw).strip():
        return None
    return Path(str(raw).strip())


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
            return parse_sb_env_output(inst_env.read_text(encoding="utf-8"))
        except OSError:
            return {}
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
            "instance; check out 7dtd-sandbox beside this repo"
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
    if plan.sandbox_root is None or plan.sandbox_server is None:
        raise TargetError("managed stock plan is missing sandbox_root/server name")
    sb = sb_path(plan.sandbox_root)
    if not sb.is_file():
        raise TargetError(
            f"Safehouse CLI missing: {sb}. A managed stock run is a sandbox "
            "instance; check out 7dtd-sandbox beside this repo or pass "
            "--sandbox-root"
        )

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


def stop_sandbox_server(plan: TargetPlan) -> None:
    """Best-effort ``sb stop`` for the sandbox server instance."""
    if not plan.is_sandbox or plan.sandbox_root is None or plan.sandbox_server is None:
        return
    if not sb_path(plan.sandbox_root).is_file():
        return
    # Teardown runs on the way out of a run that may already be failing; a stop
    # that cannot even start is reported by the caller's own exit path.
    with contextlib.suppress(TargetError):
        _run_sb(plan, ["stop", plan.sandbox_server], check=False)


def _run_sb(
    plan: TargetPlan,
    argv: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    assert plan.sandbox_root is not None
    sb = sb_path(plan.sandbox_root)
    try:
        proc = subprocess.run(
            ["bash", str(sb), *argv],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(plan.sandbox_root),
        )
    except OSError as ex:
        raise TargetError(f"could not run sb {' '.join(argv)}: {ex}") from ex
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        raise TargetError(f"sb {' '.join(argv)} failed: {tail}")
    return proc


def apply_plan_to_args(args: object, plan: TargetPlan) -> None:
    """Mutate an argparse Namespace-like object from a TargetPlan.

    Sets the lifecycle flags and overlays the instance's paths and ports.
    Explicit operator flags still win: only unset values are filled in.
    """
    args.no_server = plan.is_attach  # type: ignore[attr-defined]
    args.provision = plan.provision  # type: ignore[attr-defined]
    args.server = plan.backend  # type: ignore[attr-defined]
    args.readonly = plan.readonly  # type: ignore[attr-defined]
    if plan.port is not None and getattr(args, "port", None) is None:
        args.port = plan.port  # type: ignore[attr-defined]
    if plan.telnet_port is not None and not getattr(args, "_admin_port_explicit", False):
        args.admin_port = plan.telnet_port  # type: ignore[attr-defined]
    if plan.game_srv is not None:
        args.game_srv = plan.game_srv  # type: ignore[attr-defined]
    if plan.userdata is not None:
        args.userdata = plan.userdata  # type: ignore[attr-defined]


def overlay_instance_env(args: object, env_map: dict[str, str]) -> None:
    """Overlay the live instance.env onto args after `sb up` allocated it."""
    port = _optional_int(env_map.get("SERVER_PORT"))
    telnet = _optional_int(env_map.get("SERVER_TELNET_PORT"))
    game = _optional_path(env_map.get("SERVER_GAME"))
    userdata = _optional_path(env_map.get("SERVER_USERDATA"))
    if port is not None:
        args.port = port  # type: ignore[attr-defined]
    if telnet is not None:
        args.admin_port = telnet  # type: ignore[attr-defined]
    if game is not None:
        args.game_srv = game  # type: ignore[attr-defined]
    if userdata is not None:
        args.userdata = userdata  # type: ignore[attr-defined]


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
