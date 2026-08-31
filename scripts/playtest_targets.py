#!/usr/bin/env python3
"""Playtest target adapters: where a live suite runs.

Targets (env PLAYTEST_TARGET / --target):

  stock    legacy: orchestrator starts the Steam stock dedicated (default)
  sandbox  Safehouse lab: 7dtd-safehouse brings up the dedicated
  attach   already-running server (sandbox instance or any lab dedi)
  zdtd     Zig dedi under test
  live     attach-only to a production server-container host (never deploy)

server-container stays production-only. live never stages mods or restarts it.
loadgen remains demand-only; it does not own the world under test for these
targets.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TARGETS = ("stock", "sandbox", "attach", "zdtd", "live")

# Legacy --server stock|zdtd maps through resolve_target.
_SERVER_TO_TARGET = {
    "stock": "stock",
    "zdtd": "zdtd",
}

_ENV_ASSIGN = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(?:'([^']*)'|\"([^\"]*)\"|(.*))$"
)


@dataclass(frozen=True)
class TargetPlan:
    """Resolved bring-up plan for one playtest run."""

    target: str
    server_backend: str  # stock | zdtd (existing orchestrator backend switch)
    start_server: bool
    # sandbox instance names (server / optional client pair)
    sandbox_server: str | None = None
    sandbox_client: str | None = None
    sandbox_root: Path | None = None
    # When set, orchestrator prefers these over Steam defaults.
    game_srv: Path | None = None
    userdata: Path | None = None
    port: int | None = None
    telnet_port: int | None = None
    server_config: Path | None = None
    server_log: Path | None = None
    client_game: Path | None = None
    client_compat: Path | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_attach(self) -> bool:
        return not self.start_server


def normalize_target(raw: str | None) -> str:
    """Return a canonical target id or raise ValueError."""
    if raw is None or not str(raw).strip():
        return "stock"
    value = str(raw).strip().lower()
    if value not in TARGETS:
        raise ValueError(
            f"unknown playtest target {raw!r}; expected one of: {', '.join(TARGETS)}"
        )
    return value


def resolve_target(
    *,
    target: str | None = None,
    server: str | None = None,
    no_server: bool = False,
    sandbox_name: str | None = None,
    sandbox_root: Path | None = None,
    workspace: Path | None = None,
) -> TargetPlan:
    """Map CLI/env knobs onto a TargetPlan.

    Precedence:
      1. explicit --target / PLAYTEST_TARGET
      2. legacy --no-server  -> attach
      3. legacy --server zdtd -> zdtd
      4. default stock
    """
    explicit = None
    if target is not None and str(target).strip():
        explicit = normalize_target(target)
    elif os.environ.get("PLAYTEST_TARGET", "").strip():
        explicit = normalize_target(os.environ.get("PLAYTEST_TARGET"))

    if explicit is None:
        if no_server:
            explicit = "attach"
        elif server is not None and str(server).strip():
            mapped = _SERVER_TO_TARGET.get(str(server).strip().lower())
            if mapped is None:
                raise ValueError(
                    f"unknown --server {server!r}; expected stock or zdtd "
                    "(or pass --target)"
                )
            explicit = mapped
        else:
            explicit = "stock"

    if explicit == "live" and not no_server and target is None:
        # live is attach-only by contract; callers must not ask the orch to start it.
        pass

    ws = workspace or Path(__file__).resolve().parents[2]
    sb_root = sandbox_root or (ws / "7dtd-safehouse")
    pair = (sandbox_name or os.environ.get("PLAYTEST_SANDBOX_NAME") or "playtest").strip()
    srv_name = f"srv-{pair}"
    cli_name = f"client-{pair}"

    if explicit == "stock":
        return TargetPlan(
            target="stock",
            server_backend="stock",
            start_server=not no_server,
            notes=("legacy Steam dedicated bring-up",),
        )

    if explicit == "zdtd":
        return TargetPlan(
            target="zdtd",
            server_backend="zdtd",
            start_server=not no_server,
            notes=("zdtd binary under test",),
        )

    if explicit == "attach":
        return TargetPlan(
            target="attach",
            server_backend="stock",
            start_server=False,
            sandbox_server=None,
            notes=("attach to an already-running dedicated",),
        )

    if explicit == "live":
        return TargetPlan(
            target="live",
            server_backend="stock",
            start_server=False,
            notes=(
                "attach-only to server-container / LAN production; "
                "never deploy or restart from playtest",
            ),
        )

    if explicit == "sandbox":
        env_map = load_sandbox_env(sb_root, srv_name, create=False)
        port = _optional_int(env_map.get("SERVER_PORT"))
        telnet = _optional_int(env_map.get("SERVER_TELNET_PORT"))
        game = _optional_path(env_map.get("SERVER_GAME"))
        userdata = _optional_path(env_map.get("SERVER_USERDATA"))
        cfg = _optional_path(env_map.get("SERVER_CONFIG"))
        slog = _optional_path(env_map.get("SERVER_LOG"))
        return TargetPlan(
            target="sandbox",
            server_backend="stock",
            start_server=not no_server,
            sandbox_server=srv_name,
            sandbox_client=cli_name,
            sandbox_root=sb_root,
            game_srv=game,
            userdata=userdata,
            port=port,
            telnet_port=telnet,
            server_config=cfg,
            server_log=slog,
            notes=(
                "sandbox owns isolation; playtest stages mods into the instance",
            ),
        )

    raise ValueError(f"unhandled target {explicit!r}")


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


def load_sandbox_env(
    sandbox_root: Path,
    name: str,
    *,
    create: bool = False,
) -> dict[str, str]:
    """Load the sandbox instance contract for ``name``.

    When create is True and the instance is missing, runs
    ``sb create-server <name>`` first. Missing sb or instance yields {}.
    """
    sb = sandbox_root / "scripts" / "sb"
    if not sb.is_file():
        return {}
    inst_env = sandbox_root / "instances" / name / "instance.env"
    if create and not inst_env.is_file():
        try:
            subprocess.run(
                ["bash", str(sb), "create-server", name],
                check=True,
                capture_output=True,
                text=True,
                cwd=str(sandbox_root),
            )
        except (OSError, subprocess.CalledProcessError):
            return {}
    if inst_env.is_file():
        try:
            return parse_sb_env_output(inst_env.read_text(encoding="utf-8"))
        except OSError:
            return {}
    try:
        proc = subprocess.run(
            ["bash", str(sb), "env", name],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(sandbox_root),
        )
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}
    return parse_sb_env_output(proc.stdout)


def ensure_sandbox_server(
    plan: TargetPlan,
    *,
    wipe: bool = True,
) -> dict[str, str]:
    """Create (if needed), optionally wipe, and launch a sandbox server.

    Returns the instance.env map after launch. Raises RuntimeError on failure.
    Does nothing useful for non-sandbox plans (returns {}).
    """
    if plan.target != "sandbox" or not plan.start_server:
        return {}
    if plan.sandbox_root is None or plan.sandbox_server is None:
        raise RuntimeError("sandbox target missing sandbox_root/server name")
    sb = plan.sandbox_root / "scripts" / "sb"
    if not sb.is_file():
        raise RuntimeError(f"sandbox CLI missing: {sb}")

    name = plan.sandbox_server
    inst = plan.sandbox_root / "instances" / name
    if not inst.is_dir():
        _run_sb(sb, plan.sandbox_root, ["create-server", name])
    elif wipe:
        # Fresh save is a playtest hard rule; wipe resets userdata/Mods to base.
        _run_sb(sb, plan.sandbox_root, ["wipe", name])

    # launch-server is long-running; start detached via sb run server semantics.
    # `sb run server <name>` execs the dedicated; we background it.
    log_path = inst / "logs" / "playtest-orch-launch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as logf:
        subprocess.Popen(
            ["bash", str(sb), "run", "server", name],
            cwd=str(plan.sandbox_root),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return load_sandbox_env(plan.sandbox_root, name, create=False)


def stop_sandbox_server(plan: TargetPlan) -> None:
    """Best-effort ``sb stop`` for the sandbox server instance."""
    if plan.target != "sandbox" or plan.sandbox_root is None:
        return
    if plan.sandbox_server is None:
        return
    sb = plan.sandbox_root / "scripts" / "sb"
    if not sb.is_file():
        return
    with contextlib_suppress():
        _run_sb(sb, plan.sandbox_root, ["stop", plan.sandbox_server], check=False)


def _run_sb(
    sb: Path,
    cwd: Path,
    argv: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(sb), *argv],
        check=check,
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


class contextlib_suppress:
    """Tiny stand-in so this module stays free of a broad import for one call."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


def apply_plan_to_args(args: object, plan: TargetPlan) -> None:
    """Mutate an argparse Namespace-like object from a TargetPlan.

    Sets server/no_server and overlays sandbox paths/ports when present.
    """
    args.server = plan.server_backend  # type: ignore[attr-defined]
    args.no_server = plan.is_attach or not plan.start_server  # type: ignore[attr-defined]
    args.target = plan.target  # type: ignore[attr-defined]
    if plan.port is not None and getattr(args, "port", None) is None:
        args.port = plan.port  # type: ignore[attr-defined]
    if plan.telnet_port is not None and getattr(args, "admin_port", None) in (
        None,
        8087,
        8081,
    ):
        # Only overlay when the caller left the lab default; explicit flags win.
        pass
    if plan.telnet_port is not None and not getattr(args, "_admin_port_explicit", False):
        args.admin_port = plan.telnet_port  # type: ignore[attr-defined]
    if plan.game_srv is not None:
        args.game_srv = plan.game_srv  # type: ignore[attr-defined]
    if plan.userdata is not None:
        args.userdata = plan.userdata  # type: ignore[attr-defined]


def target_report_fields(plan: TargetPlan) -> dict[str, object]:
    """JSON-serializable fields for the run report payload."""
    return {
        "target": plan.target,
        "server_backend": plan.server_backend,
        "start_server": plan.start_server,
        "sandbox_server": plan.sandbox_server,
        "sandbox_client": plan.sandbox_client,
        "notes": list(plan.notes),
    }
