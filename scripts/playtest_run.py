#!/usr/bin/env python3
"""Host orchestrator: stock dedicated (default) or zdtd + stock client playtest.

Exit codes:
  0  all cases pass (DONE with fail=0)
  1  playtest assertion failures
  2  harness error (no DONE, server/client fail, timeout, lock refused,
     lock claim lost mid-run)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_lock  # noqa: E402
from playtest_log import (  # noqa: E402
    ClientLogScan,
    LogTail,
    ParsedClientLog,
    TailSource,
    add_barrier_hits,
    barrier_hits_prefix,
    barrier_line_hits,
    empty_client_log,
)

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
CONNECT = WORKSPACE / "7dtd-fastconnect"
LOADGEN = WORKSPACE / "7dtd-loadgen"
DEFAULT_ZDTD = WORKSPACE / "zdtd-server" / "zig-out" / "bin" / "zdtd"
DEFAULT_GAME_SRV = (
    Path.home() / ".local/share/Steam/steamapps/common/7 Days to Die Dedicated Server"
)
DEFAULT_USERDATA = Path.home() / ".cache" / "7dtd-playtest-dedicated"
STEAM_APPID = "251570"
DEFAULT_COMPAT = (
    Path.home() / f".local/share/Steam/steamapps/compatdata/{STEAM_APPID}"
)


CLIENT_EXECUTABLE = "7DaysToDie.exe"
# Steam's own standard roots, not anyone's particular install: the native data
# directory, the two symlinks Steam maintains, and the Flatpak sandbox's copy.
# Libraries outside these (a second disk, a home-relative games folder) are
# not guessed at; they are read out of steamapps/libraryfolders.vdf below,
# which is where Steam itself records them.
STEAM_ROOTS = (
    ".local/share/Steam",
    ".steam/steam",
    ".steam/root",
    ".var/app/com.valvesoftware.Steam/data/Steam",
)


def client_log_for_compat(compat: Path) -> Path:
    """Stock launch_client.sh's game log location for one Proton profile."""
    return (
        compat
        / "pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/logs"
        / "output_log_client_7dtd_connect.txt"
    )


def steam_library_dirs(home: Path | None = None) -> list[Path]:
    """Every `steamapps` directory Steam knows about, in discovery order.

    Read from `steamapps/libraryfolders.vdf` under each standard Steam root,
    because a library on a second disk or under a different home directory is
    ordinary and unguessable. Parsed with a `"path"` scan rather than a real
    VDF parser: the file is small, the key is stable, and a dependency for one
    field would be its own maintenance.
    """
    base = Path.home() if home is None else home
    libraries: list[Path] = []
    for root in STEAM_ROOTS:
        steamapps = base / root / "steamapps"
        if steamapps.is_dir() and steamapps not in libraries:
            libraries.append(steamapps)
        catalog = steamapps / "libraryfolders.vdf"
        try:
            text = catalog.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            candidate = Path(match.group(1)) / "steamapps"
            if candidate.is_dir() and candidate not in libraries:
                libraries.append(candidate)
    return libraries


def client_game_dir(env: dict[str, str] | None = None, home: Path | None = None) -> Path | None:
    """The client install launch_client.sh will use, resolved its way.

    `GAME` wins, because that is the variable the launcher reads. Otherwise the
    install is *found*: each Steam library is searched for a directory holding
    the client executable. Resolving it here is what lets the orchestrator
    refuse before starting anything, rather than leaving a caller who exported
    nothing, or exported only SEVEN_DAYS_TO_DIE_DIR (which the launcher does
    not read), to wait out the whole timeout on a client that exited
    immediately with "Game not found" into a log nobody was reading yet.

    None means no client install was found, which is a refusal, not a guess.
    """
    environment = os.environ if env is None else env
    configured = (environment.get("GAME") or "").strip()
    if configured:
        return Path(configured)
    for steamapps in steam_library_dirs(home):
        common = steamapps / "common"
        if not common.is_dir():
            continue
        for entry in sorted(common.iterdir()):
            if (entry / CLIENT_EXECUTABLE).is_file():
                return entry
    return None


def client_compat_for_game(game: Path, env: dict[str, str] | None = None) -> Path:
    """The Proton prefix for a client install, derived as the launcher derives it."""
    environment = os.environ if env is None else env
    configured = (environment.get("COMPAT") or "").strip()
    if configured:
        return Path(configured)
    common = game.parent
    steamapps = common.parent
    if common.name == "common" and steamapps.name == "steamapps":
        return steamapps / "compatdata" / STEAM_APPID
    return DEFAULT_COMPAT



# Server-authoritative persist pad: every rejoin/persist flow teleports players
# here before saveworld so the saved position is known and walkable. Tuple for
# teleport_players_to, string form for raw spawnentityat commands.
PERSIST_PAD_XYZ = (520, 62, 950)
PERSIST_PAD_COORDS = " ".join(str(v) for v in PERSIST_PAD_XYZ)

# Client + dedicated process identities shared by every pkill step (pre-run
# clean, rejoin teardown, post-run finally): one table so a new runtime shape
# cannot be added to one step and missed by the others. Site-specific extras
# (truncated comm names, zdtd, loadgen) append to this list.
GAME_PROC_PATTERNS = [
    r"7DaysToDieServer\.x86_64",
    r"[/]7DaysToDie\.exe",
    r"wine64-preloader.*7DaysToDie",
    r"proton.*7DaysToDie",
]

def mod_version() -> str:
    """Version declared by ModInfo.xml (single source of truth), "unknown" if absent."""
    try:
        text = (ROOT / "ModInfo.xml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    m = re.search(r'<Version value="([^"]+)"', text)
    return m.group(1) if m else "unknown"

def positive_seconds(text: str) -> float:
    """argparse type: a finite number of seconds > 0 (--timeout, env reader)."""
    try:
        val = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
    if not math.isfinite(val) or val <= 0:
        raise argparse.ArgumentTypeError(
            f"must be a finite number of seconds > 0, got {text!r}"
        )
    return val


def seconds_from_env(name: str, default: float) -> float:
    """Read a positive-seconds env var; harness error (exit 2) when invalid.

    A typo'd value must fail fast with the variable named, not surface later
    as an instant "timeout after 0s" or a bare float() traceback.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return positive_seconds(raw)
    except argparse.ArgumentTypeError as ex:
        err(f"invalid {name}: {ex}")
        raise SystemExit(2) from None


def tcp_port(text: str) -> int:
    """argparse type: TCP port 1..65535 (--port, --admin-port)."""
    try:
        val = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 1 <= val <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range 1..65535: {val}")
    return val


def require_litenet_room(server_port: int) -> None:
    """LiteNet bots join on ServerPort+2 (start_loadgen): a --port above
    65533 pushes the derived port out of TCP range and would fail late as an
    opaque loadgen join error instead of this startup config error."""
    if server_port > 65535 - 2:
        raise ValueError(
            f"ServerPort {server_port} leaves no room for the derived LiteNet "
            f"bot port (port+2 must be <= 65535)"
        )


def config_summary(args: argparse.Namespace) -> str:
    """Effective top-level options as one startup log line.

    The telnet password appears only as set/unset so run logs stay shareable;
    everything here is already visible in --help or the generated paths.
    """
    # Credential state without the value: operator-supplied or generated
    # per-run both count as set. Stock attach mode is rejected before this
    # function when no explicit credential was supplied.
    pw_state = "set" if (args.telnet_password or not args.no_server) else "unset"
    parts = [
        f"server={args.server}",
        f"suite={args.suite.strip()}",
        f"port={args.port}",
        f"admin_port={args.admin_port}",
        f"timeout_sec={args.timeout:g}",
        f"world={args.world_name if args.server == 'stock' else args.world}",
        f"game_name={args.game_name}",
        f"logdir={args.logdir}",
        "fresh_save=True",
        f"no_server={bool(args.no_server)}",
        f"fixtures={not args.no_fixtures}",
        f"telnet_password={pw_state}",
    ]
    if args.peer_client_name:
        parts.append(f"peer={args.peer_client_name}")
    return " ".join(parts)


def log(msg: str) -> None:
    print(f"[playtest-orch] {msg}", flush=True)


def warn(msg: str) -> None:
    """Recoverable problem: diagnostics belong on stderr, progress stays on stdout."""
    print(f"[playtest-orch] warn: {msg}", file=sys.stderr, flush=True)


def err(msg: str) -> None:
    """Terminal harness error (nonzero exit follows)."""
    print(f"[playtest-orch] {msg}", file=sys.stderr, flush=True)


# Control characters (C0 except tab/LF, plus DEL). ESC (\x1b) and CR (\x0d)
# are covered by the \x0b-\x0d and \x0e-\x1f ranges. Log bytes echoed to the
# operator terminal carry remote chat text verbatim; without stripping, a
# crafted line can emit arbitrary terminal escape sequences into the run's
# stdout or rewrite already-written lines via CR.
_LOG_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def scrub(text: str) -> str:
    """Strip control characters from log-derived text before echoing it.

    Only for interactive stdout/stderr echoes (progress crumbs, failure
    dumps); report JSON/XML keep raw detail and escape it structurally.
    """
    return _LOG_CTRL_RE.sub("", text)


# Bound on each pkill escalation step. These run in the finally teardown
# ahead of the lock release; a wedged pkill must not hold the exclusivity
# lock forever.
_PKILL_TIMEOUT_SEC = 30.0


def pkill_patterns(patterns: list[str], sig: str = "-9") -> None:
    for pat in patterns:
        try:
            subprocess.run(
                ["pkill", sig, "-f", pat],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_PKILL_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            # Skip the stuck pattern so teardown reaches the remaining steps
            # (stop_proc, lock release) instead of hanging while holding the
            # exclusivity lock.
            warn(f"pkill {sig} -f {pat!r} timed out; continuing teardown")


def clean_processes(*, kill_wine: bool = False) -> None:
    """Stop prior servers/clients. Avoid killing whole wineserver by default
    (that drops Steam); only kill game + dedicated + optional zdtd."""
    log("cleaning prior dedicated / client / zdtd")
    patterns = [
        *GAME_PROC_PATTERNS,
        r"7DaysToDieServe",  # truncated comm
        r"zig-out/bin/zdtd",
    ]
    pkill_patterns(patterns, sig="-15")
    time.sleep(2)
    pkill_patterns(patterns, sig="-9")
    if kill_wine:
        log("aggressive wine clean (kill_wine=1)")
        pkill_patterns(
            [
                r"wineserver",
                r"pressure-vessel|pv-adverb|pv-bwrap",
                r"SteamLaunch.*251570",
            ],
            sig="-9",
        )
    time.sleep(2)


def truncate_file(path: Path, what: str) -> None:
    """Empty an append-only log so incremental readers restart from zero."""
    if not path.is_file():
        return
    try:
        path.write_text("", encoding="utf-8")
    except OSError as ex:
        warn(f"could not truncate {what}: {ex}")


def wait_file_contains(path: Path, needle: str, timeout: float) -> bool:
    # Elapsed-time budget: monotonic so an NTP step or manual clock change
    # cannot extend or cut the wait.
    deadline = time.monotonic() + timeout
    # Incremental tail: O(new bytes) per poll instead of re-reading the whole
    # log every 0.5s. This waits on server startup logs that reach tens of MB
    # over a cold load, and the poll shares the machine with the game.
    tail = LogTail(path)
    while time.monotonic() < deadline:
        if needle in tail.poll():
            return True
        time.sleep(0.5)
    return False


# Cold-load wait budgets for the two server backends, shared by the initial
# start and the rejoin restart so one path cannot drift from the other again.
STOCK_READY_TIMEOUT_SEC = 600.0
ZDTD_READY_TIMEOUT_SEC = 60.0


def wait_stock_dedicated_ready(proc: subprocess.Popen, unity_log: Path) -> bool:
    """Wait for stock `StartGame done`; False when the dedicated exited first.

    A backend that dies before becoming ready must fail the harness here on
    every path that starts one: proceeding would only burn the wall clock on
    a client that can never join.
    """
    if wait_file_contains(unity_log, "StartGame done", timeout=STOCK_READY_TIMEOUT_SEC):
        log("stock dedicated ready (StartGame done)")
        return True
    if proc.poll() is not None:
        err(f"stock dedicated exited early code={proc.returncode}")
        try:
            tail = unity_log.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[-40:]
        except OSError as ex:
            # The exit-code verdict stands on its own; a log that cannot be
            # re-read (rotation, EIO) must not become a raw traceback here,
            # same boundary as the client-log grep at the final verdict.
            warn(f"could not read server log tail {unity_log}: {ex}")
        else:
            err(
                "tail server log:\n"
                + "\n".join(scrub(line) for line in tail)
            )
        return False
    warn("no StartGame done yet; server still running, proceeding")
    return True


def wait_zdtd_ready(proc: subprocess.Popen, server_log_path: Path) -> bool:
    """Wait for zdtd `tick=20Hz`; False when zdtd exited before ticking."""
    if wait_file_contains(server_log_path, "tick=20Hz", timeout=ZDTD_READY_TIMEOUT_SEC):
        log("zdtd ready (tick=20Hz)")
        return True
    if proc.poll() is not None:
        err(f"zdtd exited early code={proc.returncode}")
        return False
    warn("no tick=20Hz; proceeding")
    return True


def _literal_replacement(replacement: str) -> Callable[[re.Match[str]], str]:
    """re.sub replacer that inserts ``replacement`` without backslash escapes."""

    def _sub(_m: re.Match[str]) -> str:
        return replacement

    return _sub


def write_stock_config(
    src_cfg: Path,
    out_cfg: Path,
    userdata: Path,
    *,
    world_name: str,
    game_name: str,
    port: int,
    telnet_port: int,
    telnet_password: str,
) -> None:
    try:
        text = src_cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as ex:
        # A user-edited template can be non-UTF-8 or unreadable; name it and
        # the reason here instead of a bare decode/OSError traceback after
        # the save wipe already ran.
        raise RuntimeError(
            f"cannot read serverconfig template {src_cfg}: {ex}"
        ) from ex
    ud = str(userdata.resolve())
    # Values land inside double-quoted XML attributes; a quote or ampersand in
    # any of them would corrupt the property or smuggle extra ones into the
    # generated serverconfig. Lambda replacements keep re.sub from
    # interpreting backslashes in the value.
    ud_attr = xml_attr(ud)
    # The stock serverconfig.xml ships UserDataFolder commented out
    # (`<!-- <property name="UserDataFolder" .../> -->`). Rewriting the value
    # inside that comment leaves the server saving under its default
    # ~/.local/share/7DaysToDie, so --fresh-save wiped an empty tree while
    # player inventory and world state carried over between runs. Drop the
    # commented form first so an active property is always written.
    text = re.sub(
        r'<!--\s*<property\s+name="UserDataFolder"[^>]*/>\s*-->',
        "",
        text,
    )
    if 'name="UserDataFolder"' not in text:
        text = text.replace(
            "<ServerSettings>",
            f'<ServerSettings>\n\t<property name="UserDataFolder" value="{ud_attr}"/>',
        )
    else:
        text = re.sub(
            r'name="UserDataFolder"\s*value="[^"]*"',
            _literal_replacement(f'name="UserDataFolder" value="{ud_attr}"'),
            text,
        )
    repls = {
        "GameWorld": world_name,
        "GameName": game_name,
        "WorldGenSeed": "playtest",
        "WorldGenSize": "4096",
        "ServerPort": str(port),
        "ServerMaxPlayerCount": "8",
        "EACEnabled": "false",
        "ServerAllowCrossplay": "false",
        "ServerDisabledNetworkProtocols": "SteamNetworking",
        "ServerVisibility": "0",
        "WebDashboardEnabled": "false",
        "IgnoreEOSSanctions": "true",
        # No natural hordes during demo (fixtures spawn one zombie on barrier).
        "EnemySpawnMode": "false",
        "MaxSpawnedZombies": "0",
        "MaxSpawnedAnimals": "0",
        "TelnetEnabled": "true",
        "TelnetPort": str(telnet_port),
        # Same password TelnetAdmin authenticates with (--telnet-password /
        # PLAYTEST_TELNET_PASSWORD). Writing a different literal here would
        # make the orchestrator's telnet login fail on non-local auth.
        "TelnetPassword": telnet_password,
        "BuildCreate": "true",
        "ServerPassword": "",
        "PlayerKillingMode": "0",
    }
    for k, v in repls.items():
        text = re.sub(
            rf'name="{k}"\s*value="[^"]*"',
            _literal_replacement(f'name="{k}" value="{xml_attr(v)}"'),
            text,
        )
    try:
        out_cfg.parent.mkdir(parents=True, exist_ok=True)
        out_cfg.write_text(text, encoding="utf-8")
    except OSError as ex:
        # Named like the read failure above: this runs after --fresh-save
        # already moved the save aside, so the operator needs the destination
        # path and cause, not a bare OSError traceback from deep in main().
        raise RuntimeError(
            f"cannot write generated serverconfig {out_cfg}: {ex}"
        ) from ex
    # The generated config carries TelnetPassword; keep it user-only instead
    # of inheriting a world-readable umask.
    try:
        os.chmod(out_cfg, 0o600)
    except OSError as ex:
        warn(f"could not restrict serverconfig permissions to 0600: {ex}")


def _popen_to_logfile(
    cmd: list[str],
    log_path: Path,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start a detached process with stdout+stderr redirected into ``log_path``.

    The handle is attached as ``_log_fh`` for stop_proc to close. If the
    spawn itself fails (missing binary, exec error), the already-opened
    descriptor is closed here instead of leaking until interpreter exit.
    """
    # Long-lived by design: the handle rides on proc._log_fh and stop_proc closes it.
    fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=cwd,
            env=env,
        )
    except OSError:
        fh.close()
        raise
    proc._log_fh = fh  # type: ignore[attr-defined]
    return proc


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Publish ``data`` at ``path`` via temp+os.replace.

    A failure mid-write then leaves the previous content intact instead of a
    truncated file that later runs would treat as good.
    """
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _rewrite_platform_cfg(pcfg: Path) -> None:
    """Back up once, then force the local-auth platform surface, atomically.

    The backup is the only copy of the user's original config: writing it
    in place means a disk-full mid-write silently destroys it (the next run
    sees the backup exists and never retries), and a torn platform.cfg
    breaks every later dedicated start.
    """
    bak = pcfg.with_name(pcfg.name + ".playtest-bak")
    if not bak.is_file():
        _atomic_write_bytes(bak, pcfg.read_bytes())
    _atomic_write_bytes(
        pcfg,
        b"platform=Steam\ncrossplatform=None\nserverplatforms=Steam,LAN,Local,\n",
    )


def start_stock_dedicated(
    game_srv: Path,
    userdata: Path,
    server_log: Path,
    *,
    world_name: str,
    game_name: str,
    port: int,
    telnet_port: int,
    telnet_password: str,
) -> tuple[subprocess.Popen, Path]:
    """Start stock 7DaysToDieServer. Returns (proc, logfile path)."""
    if not (game_srv / "7DaysToDieServer.x86_64").is_file():
        raise FileNotFoundError(f"dedicated server binary missing under {game_srv}")

    userdata.mkdir(parents=True, exist_ok=True)
    (userdata / "Saves").mkdir(exist_ok=True)
    (userdata / "Logs").mkdir(exist_ok=True)

    # Local auth surface (same as loadgen): Steam + LAN only.
    pcfg = game_srv / "platform.cfg"
    if pcfg.is_file():
        try:
            _rewrite_platform_cfg(pcfg)
        except OSError as ex:
            # Not fatal: the dedicated still runs, but the auth surface may
            # keep the user's defaults and reject LAN/local logins.
            warn(f"platform.cfg rewrite failed ({ex}); auth surface may keep user defaults")

    # Quarantine RealEarth if present (stock playtest).
    re_mod = game_srv / "Mods" / "RealEarth"
    if re_mod.is_dir():
        disabled = game_srv / "Mods.disabled"
        disabled.mkdir(exist_ok=True)
        dest = disabled / "RealEarth"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        re_mod.rename(dest)
        log("quarantined RealEarth → Mods.disabled")

    cfg_src = LOADGEN / "scripts" / "serverconfig_loadgen.xml"
    if not cfg_src.is_file():
        cfg_src = game_srv / "serverconfig.xml"
    if not cfg_src.is_file():
        # Named failure instead of a read_text traceback after the save wipe:
        # the operator needs to know both paths that were tried.
        raise FileNotFoundError(
            f"no serverconfig template: tried "
            f"{LOADGEN / 'scripts' / 'serverconfig_loadgen.xml'} and "
            f"{game_srv / 'serverconfig.xml'}"
        )
    cfg_out = userdata / "serverconfig_playtest.xml"
    write_stock_config(
        cfg_src,
        cfg_out,
        userdata,
        world_name=world_name,
        game_name=game_name,
        port=port,
        telnet_port=telnet_port,
        telnet_password=telnet_password,
    )
    log(f"stock config → {cfg_out} world={world_name} port={port}")

    server_log.parent.mkdir(parents=True, exist_ok=True)
    # Unity -logfile path
    unity_log = userdata / "Logs" / f"server_playtest_{world_name}.txt"
    if unity_log.is_file():
        unity_log.write_text("", encoding="utf-8")

    cmd = [
        str(game_srv / "7DaysToDieServer.x86_64"),
        "-logfile",
        str(unity_log),
        "-quit",
        "-batchmode",
        "-nographics",
        "-dedicated",
        f"-configfile={cfg_out}",
    ]
    log("start stock dedicated: " + " ".join(cmd))
    proc = _popen_to_logfile(cmd, server_log, cwd=str(game_srv))
    return proc, unity_log


def start_zdtd(
    zdtd: Path,
    world: Path,
    port: int,
    admin_port: int,
    game_srv: Path,
    server_log: Path,
) -> subprocess.Popen:
    world.mkdir(parents=True, exist_ok=True)
    server_log.parent.mkdir(parents=True, exist_ok=True)
    map_dir = game_srv / "Data" / "Worlds" / "Navezgane"
    cmd = [
        str(zdtd),
        "--port",
        str(port),
        "--world",
        str(world),
        "--map",
        str(map_dir),
        "--game-dir",
        str(game_srv),
        "--world-name",
        "Navezgane",
        "--admin-port",
        str(admin_port),
    ]
    log("start zdtd: " + " ".join(cmd))
    return _popen_to_logfile(cmd, server_log)


# Detached mute helpers started per client launch. They self-exit after their
# poll window, but nothing else waits on them: without an explicit reap each
# one lingers as a zombie for the rest of a long soak run.
_MUTE_HELPER_PROCS: list[subprocess.Popen] = []


def reap_finished_helpers() -> None:
    """Reap exited detached helpers (mute audio) so they cannot accumulate."""
    for p in _MUTE_HELPER_PROCS[:]:
        if p.poll() is not None:
            _MUTE_HELPER_PROCS.remove(p)


def client_mute_enabled() -> bool:
    """Default on: mute client audio for automated runs (opt-out CLIENT_MUTE=0)."""
    raw = (
        os.environ.get("CLIENT_MUTE")
        or os.environ.get("PLAYTEST_MUTE")
        or os.environ.get("SEVEN_DAYS_TO_DIE_CLIENT_MUTE")
        or "1"
    )
    return raw.strip().lower() not in ("0", "false", "no", "off")


def mute_client_audio_async() -> None:
    """Poll PipeWire/Pulse for 7DaysToDie sink-input and mute it (default on).

    Prefer 7dtd-fastconnect's mute_client_audio.sh (same helper launch_client uses).
    Best-effort: missing pactl/jq only logs a warning inside the helper.
    """
    if not client_mute_enabled():
        log("client mute: off (CLIENT_MUTE=0)")
        return
    helper = CONNECT / "scripts" / "mute_client_audio.sh"
    wait_s = os.environ.get(
        "CLIENT_MUTE_TIMEOUT",
        os.environ.get("SEVEN_DAYS_TO_DIE_CLIENT_MUTE_TIMEOUT", "60"),
    )
    if not helper.is_file():
        warn(f"client mute: helper missing ({helper}); skip")
        return
    log(f"client mute: on (opt-out CLIENT_MUTE=0); polling up to {wait_s}s")
    try:
        proc = subprocess.Popen(
            ["bash", str(helper), str(wait_s)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _MUTE_HELPER_PROCS.append(proc)
    except OSError as ex:
        warn(f"client mute: failed to start helper: {ex}")


def start_client(
    port: int,
    suite: str,
    client_launch_log: Path,
    *,
    extra_env: dict[str, str] | None = None,
    run_suite: bool = True,
) -> subprocess.Popen:
    launch = CONNECT / "scripts" / "launch_client.sh"
    env = os.environ.copy()
    env["7DTD_CONNECT"] = f"127.0.0.1:{port}"
    if run_suite:
        env["PLAYTEST_SUITE"] = suite
        env["PLAYTEST"] = "1"
    else:
        # A stock peer must join and remain in the world, not run a duplicate
        # scenario suite or inherit a suite selection from its parent process.
        for key in (
            "PLAYTEST_SUITE",
            "ZDTD_PLAYTEST_SUITE",
            "PLAYTEST",
            "ZDTD_PLAYTEST",
            "PLAYTEST_LAPS",
            "ZDTD_PLAYTEST_LAPS",
        ):
            env.pop(key, None)
    # Propagate mute defaults into connect launch_client (default muted).
    if "CLIENT_MUTE" not in env and "SEVEN_DAYS_TO_DIE_CLIENT_MUTE" not in env:
        env["CLIENT_MUTE"] = "1" if client_mute_enabled() else "0"
    if extra_env:
        env.update(extra_env)
    client_launch_log.parent.mkdir(parents=True, exist_ok=True)
    role = "scenario" if run_suite else "stock-peer"
    log(f"start client role={role} suite={suite or '(none)'} connect={env['7DTD_CONNECT']}")
    proc = _popen_to_logfile(
        ["bash", str(launch)], client_launch_log, cwd=str(CONNECT), env=env
    )
    # Belt-and-suspenders: connect mutes itself; also start poll from orch.
    mute_client_audio_async()
    return proc


def ensure_loadgen_built() -> Path | None:
    exe = LOADGEN / "src" / "LoadGen" / "bin" / "Release" / "net8.0" / "7dtd-loadgen"
    proj = LOADGEN / "src" / "LoadGen" / "LoadGen.csproj"
    if not proj.is_file():
        warn(f"loadgen project missing: {proj}")
        return None
    source_root = proj.parent
    source_mtime = max(
        (path.stat().st_mtime for path in source_root.glob("*.cs")),
        default=proj.stat().st_mtime,
    )
    # The glob default above covers an empty source dir only; a non-empty one
    # can still predate a csproj touch, so fold that in explicitly.
    source_mtime = max(source_mtime, proj.stat().st_mtime)
    if exe.is_file() and exe.stat().st_mtime >= source_mtime:
        return exe
    if exe.is_file():
        log("loadgen source is newer than its executable; rebuilding")
    log("building loadgen…")
    try:
        r = subprocess.run(
            ["dotnet", "build", str(proj), "-c", "Release", "-v", "q"],
            cwd=str(LOADGEN),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_LOADGEN_BUILD_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        warn(
            f"loadgen build timed out after {_LOADGEN_BUILD_TIMEOUT_SEC:g}s; "
            "skipping loadgen barriers this run"
        )
        return None
    except OSError as ex:
        warn(f"loadgen build could not start: {ex}")
        return None
    if r.returncode != 0:
        warn(f"loadgen build failed: {r.stderr[-400:]}")
        return None
    return exe if exe.is_file() else None


def start_loadgen(
    *,
    game_port: int,
    count: int,
    timeout_ms: int,
    log_path: Path,
    events_path: Path | None = None,
    observe_cvars: list[str] | None = None,
    observe_buffs: list[str] | None = None,
) -> subprocess.Popen | None:
    """Join LiteNet bots (port = ServerPort+2)."""
    exe = ensure_loadgen_built()
    if exe is None:
        return None
    litenet = game_port + 2
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(exe),
        "--join",
        "--host",
        "127.0.0.1",
        "--port",
        str(litenet),
        "--count",
        str(count),
        "--timeout",
        str(timeout_ms),
        "--no-spawn-zombies",
        "--min-pass-rate",
        "0.0",
    ]
    if events_path is not None:
        cmd.extend(["--events-jsonl", str(events_path)])
        for name in observe_cvars or []:
            cmd.extend(["--observe-cvar", name])
        for name in observe_buffs or []:
            cmd.extend(["--observe-buff", name])
    log(f"start loadgen count={count} litenet={litenet} timeout_ms={timeout_ms}")
    return _popen_to_logfile(cmd, log_path, cwd=str(LOADGEN))


def read_loadgen_events(path: Path) -> list[dict]:
    """Read complete valid loadgen JSON-lines events; tolerate a growing tail.

    One-shot whole-file snapshot: this is the final-verdict read, run once
    after the suite ends so every observer check sees one consistent view.
    Polling loops must use :class:`LoadgenEventReader` instead, which feeds
    only newly appended lines through the same filter.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return parse_loadgen_event_lines(lines)


def parse_loadgen_event_lines(lines: list[str]) -> list[dict]:
    """Valid loadgen events among ``lines``; shared by the whole-file and
    incremental readers so they cannot drift."""
    return [event for line in lines if (event := parse_loadgen_event_line(line)) is not None]


def parse_loadgen_event_line(line: str) -> dict | None:
    """One valid loadgen JSONL event, or ``None`` for incomplete/noise lines."""
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(event, dict) and event.get("schema") == "7dtd.loadgen.event.v1":
        return event
    return None


def read_loadgen_latest_state(path: Path) -> tuple[int | None, dict[tuple[str, str], dict]]:
    """Stream the final observer snapshot without retaining every event.

    Loadgen emits state snapshots for the complete run. The final observer
    only needs the newest joined entity and its newest state for each observed
    key, so loading the full JSONL file made memory scale with run duration.
    Two sequential disk passes preserve ``loadgen_latest_state`` semantics,
    including state lines that precede the selected joined record, while
    retaining O(observed keys) data instead of O(all snapshots).
    """
    entity_id: int | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                event = parse_loadgen_event_line(line)
                if event is None or event.get("type") != "joined":
                    continue
                candidate = event.get("entityId")
                if (
                    isinstance(candidate, int)
                    and not isinstance(candidate, bool)
                    and candidate > 0
                ):
                    entity_id = candidate
    except OSError:
        return None, {}

    latest: dict[tuple[str, str], dict] = {}
    if entity_id is None:
        return None, latest
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                event = parse_loadgen_event_line(line)
                if (
                    event is None
                    or event.get("type") != "state"
                    or event.get("entityId") != entity_id
                ):
                    continue
                kind, name = event.get("kind"), event.get("name")
                if isinstance(kind, str) and isinstance(name, str):
                    latest[(kind, name)] = event
    except OSError:
        return entity_id, {}
    return entity_id, latest


class LoadgenEventReader:
    """Incremental reader for the growing loadgen events JSONL file.

    The poll loop re-checks for a joined bot entity every iteration while a
    teleport is pending; re-reading and re-parsing the whole file each time
    is quadratic in the events the bot emits (state snapshots keep flowing
    for the rest of the run) on the same CPU as the game under test. This
    mirrors LogTail + ClientLogScan for the client log: only newly appended
    complete lines are parsed, and accumulated events reset when the file is
    truncated, so an id from a finished loadgen generation can never answer
    for the current one.
    """

    def __init__(self, path: Path) -> None:
        self._tail = LogTail(path)
        self.events: list[dict] = []

    def drain(self) -> list[dict]:
        """Fold in newly appended events and return everything seen so far."""
        # A truncation between polls advances the tail's generation while
        # poll() runs, so compare around the call rather than before it.
        generation_before = self._tail.generations
        chunk = self._tail.poll()
        if self._tail.generations != generation_before:
            self.events.clear()
        if chunk:
            self.events.extend(parse_loadgen_event_lines(chunk.splitlines()))
        return self.events


def _finite_number(value: object) -> float | None:
    """The numeric reading of a loadgen event field, or ``None``.

    JSON integers are unbounded, so ``float()`` on a hostile ``"value"``
    raises OverflowError past an isinstance check; booleans pass
    isinstance(int); and parsed NaN/Infinity tokens are not observations.
    Everything that is not a finite non-boolean number is rejected here so
    every oracle below compares floats only.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def loadgen_joined_entity(events: list[dict]) -> int | None:
    """Return the newest positive entity id from a structured joined event."""
    for event in reversed(events):
        if event.get("type") != "joined":
            continue
        entity_id = event.get("entityId")
        # bool is an int subclass; "entityId": true must never become the bot.
        if isinstance(entity_id, int) and not isinstance(entity_id, bool) and entity_id > 0:
            return entity_id
    return None


def loadgen_latest_state(events: list[dict]) -> tuple[int | None, dict[tuple[str, str], dict]]:
    """Return the joined bot id and its latest event for every state key."""
    entity_id = loadgen_joined_entity(events)
    latest: dict[tuple[str, str], dict] = {}
    if entity_id is not None:
        for event in events:
            if event.get("type") == "state" and event.get("entityId") == entity_id:
                kind, name = event.get("kind"), event.get("name")
                if isinstance(kind, str) and isinstance(name, str):
                    latest[(kind, name)] = event
    return entity_id, latest


def loadgen_expectation_failures(
    events: list[dict], cvars: list[str], buffs: list[str],
    positive_cvars: list[str] | None = None,
    equal_cvars: list[str] | None = None,
) -> list[str]:
    """Compare exact and relational expectations with the joined bot state."""
    entity_id, latest = loadgen_latest_state(events)
    return loadgen_expectation_failures_from_latest(
        entity_id, latest, cvars, buffs, positive_cvars, equal_cvars
    )


def loadgen_expectation_failures_from_latest(
    entity_id: int | None,
    latest: dict[tuple[str, str], dict],
    cvars: list[str],
    buffs: list[str],
    positive_cvars: list[str] | None = None,
    equal_cvars: list[str] | None = None,
) -> list[str]:
    """Compare expectations against a compact loadgen observer snapshot."""
    if entity_id is None:
        return ["no structured joined event"]
    failures: list[str] = []
    for raw in cvars:
        try:
            name, expected_text = raw.rsplit("=", 1)
            expected = float(expected_text)
        except (ValueError, TypeError):
            failures.append(f"invalid CVar expectation {raw!r}")
            continue
        # NaN/inf expectations make every comparison below False, so a typo
        # like "=nan" would silently pass against any observed value.
        if not math.isfinite(expected):
            failures.append(f"invalid CVar expectation {raw!r}")
            continue
        state_event = latest.get(("cvar", name))
        observed_raw = state_event.get("value") if state_event else None
        value = _finite_number(observed_raw)
        if value is None or abs(value - expected) > 0.0001:
            failures.append(f"CVar {name} expected {expected:g}, observed {observed_raw!r}")
    for raw in buffs:
        try:
            name, expected_text = raw.rsplit("=", 1)
        except ValueError:
            failures.append(f"invalid buff expectation {raw!r}")
            continue
        # Validate before interpreting: an unknown token must be reported as
        # a bad expectation, never decoded as merely falsy.
        lowered = expected_text.lower()
        if lowered not in ("0", "false", "no", "off", "1", "true", "yes", "on"):
            failures.append(f"invalid buff expectation {raw!r}")
            continue
        expected = lowered in ("1", "true", "yes", "on")
        state_event = latest.get(("buff", name))
        active = state_event.get("active") if state_event else None
        if not isinstance(active, bool) or active != expected:
            failures.append(f"buff {name} expected active={expected}, observed {active!r}")
    for name in positive_cvars or []:
        state_event = latest.get(("cvar", name))
        observed_raw = state_event.get("value") if state_event else None
        # NaN is not positive (<= 0 is False for it) and must not pass here.
        value = _finite_number(observed_raw)
        if value is None or value <= 0:
            failures.append(f"CVar {name} expected positive, observed {observed_raw!r}")
    for raw in equal_cvars or []:
        try:
            left, right = raw.split("=", 1)
        except ValueError:
            failures.append(f"invalid CVar equality {raw!r}")
            continue
        left_event, right_event = latest.get(("cvar", left)), latest.get(("cvar", right))
        left_value = left_event.get("value") if left_event else None
        right_value = right_event.get("value") if right_event else None
        left_num = _finite_number(left_value)
        right_num = _finite_number(right_value)
        if left_num is None or right_num is None or abs(left_num - right_num) > 0.0001:
            failures.append(
                f"CVars {left} and {right} expected equal, observed "
                f"{left_value!r} and {right_value!r}"
            )
    return failures


def parse_cvar_value(reply: str, name: str) -> float | None:
    """Extract NAME's numeric value from stock ``cvar get`` output."""
    match = re.search(
        rf"(?im)\b{re.escape(name)}\b(?:\s*=\s*|:\s*(?:True|False)\.\s*Value:\s*)"
        r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)",
        reply,
    )
    if not match:
        return None
    try:
        return _finite_number(float(match.group(1)))
    except OverflowError:
        return None


def server_cvar_oracle_failures(
    tn: TelnetAdmin, entity_id: int, names: list[str], latest: dict[tuple[str, str], dict],
    tolerance: float = 0.0001,
) -> list[str]:
    """Compare server-authority CVar values with the joined bot's decoded state."""
    failures: list[str] = []
    for name in names:
        event = latest.get(("cvar", name))
        peer_raw = event.get("value") if event else None
        server_value = tn.get_cvar(name, entity_id)
        peer_value = _finite_number(peer_raw)
        if (
            peer_value is None
            or server_value is None
            or abs(peer_value - server_value) > tolerance
        ):
            failures.append(
                f"server CVar {name} expected peer value {peer_value!r}, "
                f"observed {server_value!r}"
            )
    return failures


def write_zdtd_apm_dump(
    zdtd: Path,
    world: Path,
    game_srv: Path,
    dump_path: Path,
    *,
    ticks: int = 80,
    run_id: str = "",
) -> bool:
    """Run short offline zdtd --ticks for APM text snapshot (docs/APM.md)."""
    if not zdtd.is_file():
        warn(f"apm dump: missing zdtd {zdtd}")
        return False
    map_dir = game_srv / "Data" / "Worlds" / "Navezgane"
    cmd = [
        str(zdtd),
        "--ticks",
        str(ticks),
        "--once",
        "--world",
        str(world),
        "--map",
        str(map_dir),
        "--game-dir",
        str(game_srv),
        "--world-name",
        "Navezgane",
    ]
    log(f"apm dump: {' '.join(cmd)}")
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as ex:
        warn(f"apm dump fail: {ex}")
        return False
    out = (r.stdout or "") + (r.stderr or "")
    # Fail closed: do not invent markers. Client would soft-pass a synthetic dump.
    has_marker = "zdtd-apm" in out or "wall_ns" in out or "tick_total" in out

    def _write_dump(text: str) -> bool:
        """Write the dump file; False leaves the retry-next-poll path armed."""
        try:
            dump_path.parent.mkdir(parents=True, exist_ok=True)
            dump_path.write_text(text, encoding="utf-8")
        except OSError as ex:
            warn(f"apm dump: could not write {dump_path}: {ex}")
            return False
        return True

    if not has_marker or not out.strip():
        warn(f"apm dump: no live markers in output (len={len(out)} rc={r.returncode})")
        _write_dump("APM_DUMP_FAILED no markers from zdtd --ticks\n")
        return False
    body = out
    if run_id:
        # Prefix for correlation only; markers must already exist in body.
        body = f"run_id={run_id}\n" + body
    if not _write_dump(body):
        return False
    log(
        f"apm dump → {dump_path} bytes={dump_path.stat().st_size} run_id={run_id or '-'}"
    )
    return True


# Bounded waits around each escalation step of stop_proc. Module-level so the
# offline gate can shrink them instead of waiting out the production values.
_STOP_TERM_WAIT_SEC = 8.0
_STOP_KILL_WAIT_SEC = 8.0

# A cold dotnet build is minutes, not tens of minutes. Without a timeout a
# hung compiler would block the poll loop forever: the wall-clock deadline
# only fires between polls, so the run would never reach it.
_LOADGEN_BUILD_TIMEOUT_SEC = 600.0


def stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        # A process already gone mid-teardown must not fail the others.
        with contextlib.suppress(Exception):
            proc.terminate()
    try:
        proc.wait(timeout=_STOP_TERM_WAIT_SEC)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
        # SIGKILL needs its own reap: without wait() the killed child stays a
        # zombie until this orchestrator exits.
        try:
            proc.wait(timeout=_STOP_KILL_WAIT_SEC)
        except subprocess.TimeoutExpired:
            warn(f"stop_proc: pid {proc.pid} not reaped after SIGKILL")
    fh = getattr(proc, "_log_fh", None)
    if fh:
        with contextlib.suppress(Exception):
            fh.close()


def write_report(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as ex:
        # A full disk must not erase the run's verdict: the caller keeps
        # going so SUMMARY/exit still reflect the playtest result.
        err(f"could not write report {path}: {ex}")
        return
    log(f"report → {path}")


def collect_visual_reviews(directory: Path | None) -> dict[str, str]:
    """Evidence paths keyed by suite/case, from review envelopes under `directory`.

    Discovers `review-*.json` (the default evidence name the deadeye gateway
    and `scripts/review_video.py` write) and maps each envelope to
    `"<suite>/<case>"` from the recorded intent. Only **paths** reach the
    report: no verdict, score, or pass/fail derived from a review is ever
    included, so a review can never change a case's result by existing.
    """
    if directory is None or not directory.is_dir():
        return {}
    reviews: dict[str, str] = {}
    for path in sorted(directory.rglob("review-*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        intent = document.get("intent") or {}
        content = intent.get("content") or {}
        suite = content.get("suite") or ""
        case = content.get("case") or ""
        key = f"{suite}/{case}" if suite and case else path.stem
        reviews[key] = str(path)
    return reviews


# Characters that are illegal anywhere in an XML 1.0 document: C0 controls
# except tab/LF/CR, DEL plus C1 controls, surrogates, and the noncharacters
# U+FFFE/U+FFFF. They cannot be escaped (no numeric reference exists for
# them), so a single NUL surviving from a binary log line would make the
# whole generated document unparseable; they are dropped before escaping.
_XML_ILLEGAL_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]"
)


def xml_attr(value: str) -> str:
    """Escape a value for safe inclusion inside a double-quoted XML attribute.

    Case ids and detail text come from client log lines (game output, mod
    detail strings), so every XML special must be escaped or a crafted line
    breaks out of the attribute into arbitrary report markup. Characters
    illegal in XML 1.0 are dropped first: escaping cannot represent them.
    """
    return xml_escape(_XML_ILLEGAL_RE.sub("", str(value)), {'"': "&quot;", "'": "&apos;"})


def write_junit(path: Path, suite: str, results: list[dict]) -> None:
    """Minimal JUnit XML for CI UIs. Counters derive from ``results``."""
    tests = len(results)
    failures = sum(1 for r in results if r.get("status") == "FAIL")
    skipped = sum(1 for r in results if r.get("status") == "SKIP")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<testsuite name="7dtd-playtest.{xml_attr(suite)}" tests="{tests}" '
            f'failures="{failures}" skipped="{skipped}">'
        ),
    ]
    for r in results:
        case = xml_attr(r.get("case", "unknown"))
        status = r.get("status", "FAIL")
        detail = xml_attr(r.get("detail") or "")
        lines.append(f'  <testcase classname="playtest" name="{case}">')
        if status == "FAIL":
            lines.append(f'    <failure message="{detail}"/>')
        elif status == "SKIP":
            lines.append(f'    <skipped message="{detail}"/>')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as ex:
        err(f"could not write junit {path}: {ex}")
        return
    log(f"junit → {path}")


class TelnetAdmin:
    """Minimal stock dedicated telnet (password prompt)."""

    # Substrings that mark a listents line as non-player AI. One shared table
    # so clear_ai and kill_non_player_ai cannot drift apart again.
    AI_LINE_KEYWORDS = (
        "zombie",
        "animal",
        "vulture",
        "bear",
        "wolf",
        "snake",
        "kind=zombie",
        "kind=animal",
    )

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self._sock: socket.socket | None = None

    def connect(self) -> bool:
        try:
            self.close()
            s = socket.create_connection((self.host, self.port), timeout=5.0)
            s.settimeout(2.0)
            self._sock = s
            banner = self._recv(0.8)
            if "password" in banner.lower():
                self._send(self.password)
                _ = self._recv(0.6)
            log(f"telnet connected {self.host}:{self.port} banner={banner[:60]!r}")
            return True
        except OSError as ex:
            warn(f"telnet connect fail: {ex}")
            self.close()
            return False

    def exec(self, cmd: str) -> str:
        if not self._sock:
            return ""
        try:
            self._send(cmd)
            # zdtd admin is polled on the 20 Hz tick; allow a few frames.
            return self._recv(1.2)
        except OSError as ex:
            # The session is broken (reset pipe, server gone). Close now so
            # later execs fail fast as "no socket" instead of re-raising on a
            # dead fd, and replies stay distinguishable from silence.
            warn(f"telnet exec fail: {ex}")
            self.close()
            return ""

    def get_cvar(self, name: str, entity_id: int, timeout: float = 8.0) -> float | None:
        """Run stock ``cvar get`` and wait past its command echo for a value."""
        if not self._sock:
            return None
        try:
            self._send(f"cvar get {name} -p {entity_id}")
            deadline = time.monotonic() + timeout
            reply = ""
            while time.monotonic() < deadline:
                reply += self._recv(0.5)
                value = parse_cvar_value(reply, name)
                if value is not None:
                    return value
            log(f"telnet cvar reply missing value name={name} tail={reply[-160:]!r}")
            return None
        except OSError as ex:
            warn(f"telnet cvar get fail: {ex}")
            self.close()
            return None

    def _ai_entity_ids(self, out: str) -> list[str]:
        """Entity ids from listents lines matching the shared AI keyword table."""
        ids: list[str] = []
        for line in out.splitlines():
            low = line.lower()
            if not any(k in low for k in self.AI_LINE_KEYWORDS):
                continue
            m = re.search(r"id\s*=\s*(\d+)", line, flags=re.IGNORECASE)
            if m:
                ids.append(m.group(1))
        return ids

    def clear_ai(self) -> None:
        """Remove non-player AI without killing the human player.

        Do **not** use stock `killall` here: it also kills the player entity and
        leaves the demo stuck on a death screen.
        """
        out = self.exec("listents")
        killed = 0
        for eid in self._ai_entity_ids(out):
            self.exec(f"kill {eid}")
            killed += 1
        log(f"telnet clear_ai killed~={killed} (listents sample {out[:100]!r})")

    def kill_non_player_ai(self) -> int:
        """Kill zombie/animal entities from listents (not the player)."""
        out = self.exec("listents")
        killed = 0
        players = {str(i) for i in self.list_player_ids()}
        for eid in self._ai_entity_ids(out):
            if eid in players:
                continue
            r = self.exec(f"kill {eid}")
            log(f"telnet kill {eid} → {r[:80]!r}")
            killed += 1
        if killed == 0:
            # Broader: kill all entity ids in listents that are not players
            for m in re.finditer(r"(?:id|ID)\s*=\s*(\d+)", out):
                eid = m.group(1)
                if eid in players:
                    continue
                if int(eid) < 100:
                    continue
                r = self.exec(f"kill {eid}")
                log(f"telnet kill fallback {eid} → {r[:80]!r}")
                killed += 1
                if killed >= 16:
                    break
        # No killall here either: stock killall also kills the player entity,
        # and this helper is named for leaving players alive.
        log(f"telnet kill_non_player_ai killed~={killed}")
        return killed

    def list_player_ids(self) -> list[int]:
        """Parse stock `listplayers` / zdtd `list` for entity ids."""
        out = self.exec("listplayers")
        if not out or "unknown" in out.lower():
            out = self.exec("list") or out
        if "id=" not in out:
            # The reply can lag the 1.2 s settle right after login; one more
            # read before giving up, and say what came back so a later run
            # is not left with "empty/unparsed".
            out += self._recv(1.5)
            if "id=" not in out:
                log(f"telnet listplayers reply unparsed: {out[-160:]!r}")
        ids = [
            int(x) for x in re.findall(r"(?:id|entity)\s*=\s*(\d+)", out, flags=re.IGNORECASE)
        ]
        # zdtd console style: "(entity 107)"
        ids += [int(x) for x in re.findall(r"\(entity\s+(\d+)\)", out, flags=re.IGNORECASE)]
        ids = [i for i in ids if i > 0]
        return list(dict.fromkeys(ids))

    def teleport_players_to(self, x: float, y: float, z: float) -> int:
        """Teleport every listed player to world coords. Returns how many cmds ran."""
        ids = self.list_player_ids()
        if not ids:
            warn("telnet teleport: no players from listplayers")
            return 0
        n = 0
        for pid in ids:
            r = self.exec(f"teleportplayer {pid} {x:g} {y:g} {z:g}")
            log(f"telnet teleportplayer {pid} {x:g} {y:g} {z:g} → {r[:120]!r}")
            n += 1
        return n

    def spawn_near_players(self, entity: str) -> int:
        ids = self.list_player_ids()
        if not ids:
            log("telnet listplayers empty/unparsed for spawn")
            return 0
        # One passive-ish spawn near first player only (no scouts: they swarm and kill).
        r = self.exec(f"spawnentity {ids[0]} {entity}")
        # A broken session returns "" exactly like a silent success; only
        # trust the fire when the socket survived the exchange (exec closes
        # it on failure), so callers fall back or retry instead of skipping.
        spawned = 1 if self._sock is not None and "No spawn point" not in r else 0
        if spawned == 0:
            # Offset from known pad so the zombie is visible but not on top of the player.
            for pos in (PERSIST_PAD_COORDS, "530 62 960", "515 62 955"):
                r = self.exec(f"spawnentityat {entity} {pos}")
                if r and "ERR" not in r.upper() and "Unknown" not in r:
                    spawned += 1
                    break
        log(f"telnet spawn near players={ids[:1]} units~={spawned} type={entity}")
        return spawned

    def _send(self, line: str) -> None:
        # Same contract as exec(): no session means nothing was sent.
        # list_player_ids calls _recv after exec may have closed a broken
        # socket, so a bare assert here would turn an expected operating
        # error (pipe reset mid-poll) into a run-killing AssertionError
        # instead of the warn-and-retry every other telnet path uses.
        if not self._sock:
            return
        self._sock.sendall((line + "\n").encode("utf-8", errors="replace"))

    def _recv(self, settle: float) -> str:
        if not self._sock:
            return ""
        time.sleep(settle)
        chunks: list[bytes] = []
        self._sock.settimeout(0.25)
        try:
            while True:
                try:
                    data = self._sock.recv(4096)
                except TimeoutError:
                    break
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
                if len(chunks) > 32:
                    break
        finally:
            self._sock.settimeout(2.0)
        # Single control-char boundary for everything the admin plane sends
        # back. Replies echo player names (listplayers) and entity names
        # (listents) chosen by remote LAN peers, and every caller logs slices
        # of them to the operator terminal; scrubbing here covers each of
        # those echoes at once. The programmatic consumers (id regexes,
        # keyword substring checks, cvar number extraction) only need
        # printable text, so stripping controls cannot change a verdict.
        return scrub(b"".join(chunks).decode("utf-8", errors="replace"))

    def connected(self) -> bool:
        """True while the session socket survived the last exchange.

        exec()/get_cvar() close the socket when it breaks and return "" or
        None, indistinguishable from silence. Callers that record a fire as
        serviced must check this, or a teleport/save/say that never reached
        the server is booked as done.
        """
        return self._sock is not None

    def close(self) -> None:
        if self._sock:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None


# Signals converted to SystemExit during normal operation and ignored during
# teardown (see install_signal_handlers / main's finally). SIGINT belongs with
# TERM/HUP: Ctrl+C raises KeyboardInterrupt from wherever the main thread is,
# and one landing inside main()'s cleanup would skip stop_proc / release and
# strand a live runtime under a published claim, exactly like an unconverted
# TERM. Converting it routes abort through the same graceful path.
_TERMINATION_SIGNAL_NAMES = ("SIGTERM", "SIGHUP", "SIGINT")


def _ignore_termination_signals() -> None:
    """Set SIG_IGN for every termination signal; safe to call anywhere.

    Re-registering can fail once the interpreter is tearing down, hence the
    suppression.
    """
    for name in _TERMINATION_SIGNAL_NAMES:
        s = getattr(signal, name, None)
        if s is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(s, signal.SIG_IGN)


def _block_termination_signals() -> None:
    """Block TERM/HUP/INT delivery on this thread; safe to call anywhere.

    Closes the last interrupt window in main()'s finally: disarming through
    signal.signal only takes effect after those calls complete, so a
    first-ever TERM/HUP/INT landing between entering the cleanup and finishing
    the disarm still raises SystemExit from inside it, skipping stop_proc /
    release and stranding a live runtime under a published claim. Blocking
    keeps such a delivery pending instead, and the process exits right after
    teardown, so the pending signal is simply discarded.
    """
    sigs = {
        getattr(signal, name)
        for name in _TERMINATION_SIGNAL_NAMES
        if hasattr(signal, name)
    }
    if not sigs:
        return
    with contextlib.suppress(AttributeError, OSError, ValueError):
        # POSIX-only; per-thread mask. Other threads are covered by the
        # process-wide SIG_IGN that _ignore_termination_signals sets right
        # after this (the heartbeat daemon may still be alive here).
        signal.pthread_sigmask(signal.SIG_BLOCK, sigs)


def install_signal_handlers() -> None:
    """Convert SIGTERM/SIGHUP/SIGINT into SystemExit so the finally-based
    cleanup runs.

    Default signal action kills the process without unwinding: the detached
    client/server survive (start_new_session) and the lock file goes stale
    while a live runtime blocks takeover (stale_but_live wedge). Raising
    SystemExit routes termination through main()'s finally, which stops the
    runtime processes and releases the exclusivity lock. SIGINT's default is
    KeyboardInterrupt instead of death, but an interrupt mid-cleanup strands
    the runtime all the same, so it takes the same conversion.

    Once that finally is running, all three signals are ignored instead (see
    the disarm at the top of the block): delivery there would raise
    SystemExit from inside the cleanup itself and strand a live runtime under
    a published claim.
    """
    def _exit_fast(signum: int, _frame: object) -> None:
        # Ignore repeats while we unwind so a second hit during cleanup
        # cannot raise inside the finally block and skip stop_proc/release.
        _ignore_termination_signals()
        raise SystemExit(128 + signum)

    for name in _TERMINATION_SIGNAL_NAMES:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _exit_fast)
        except (ValueError, OSError) as ex:
            # ValueError: main() driven from a non-main thread; keep running.
            warn(f"cannot install {name} handler: {ex}")


# Soft-delete window for destructive pre-run wipes (--fresh-save, zdtd world
# reset, prior client-log evidence): data moves under <logdir>/quarantine and
# is pruned to the newest QUARANTINE_KEEP entries instead of being destroyed.
# A mispointed --userdata/--game-name/--world therefore costs a copy-back, not
# an unrecoverable loss.
QUARANTINE_DIRNAME = "quarantine"
QUARANTINE_KEEP = 5

# Per-run evidence (report-<epoch>.json / junit-<epoch>.xml) lands in the
# cache logdir on every orchestrated run and nothing reads older generations
# back: without a bound, months of runs fill the disk one file pair at a
# time. Same newest-wins policy as quarantine evidence; per pattern so a kept
# report never loses its junit twin.
REPORT_KEEP = 50


def prune_run_artifacts(logdir: Path, keep: int = REPORT_KEEP) -> None:
    """Keep only the newest `keep` report/junit files per pattern."""
    if keep <= 0:
        return
    for pattern in ("report-*.json", "junit-*.xml"):
        try:
            entries = sorted(p for p in logdir.glob(pattern) if p.is_file())
        except OSError as ex:
            warn(f"artifact prune skipped ({ex}); old {pattern} will accumulate")
            continue
        for old in entries[:-keep]:
            with contextlib.suppress(OSError):
                old.unlink()


def prune_quarantine(qroot: Path, keep: int = QUARANTINE_KEEP) -> None:
    """Keep only the newest `keep` quarantine entries (dirs or files)."""
    try:
        entries = sorted(qroot.iterdir())
    except OSError as ex:
        warn(f"quarantine prune skipped ({ex}); old entries will accumulate")
        return
    for old in entries[:-keep]:
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                old.unlink()


def _quarantine_entry(qroot: Path, label: str) -> Path | None:
    """Create a timestamped quarantine entry dir; prune older ones.

    None means the quarantine itself is unusable (disk, permissions): callers
    must then leave the data in place rather than destroy it unrecoverably.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    entry = qroot / f"{stamp}-{label}"
    n = 0
    while entry.exists():
        n += 1
        entry = qroot / f"{stamp}-{label}.{n}"
    try:
        entry.mkdir(parents=True)
    except OSError as ex:
        warn(f"quarantine unavailable ({ex}); keeping data in place")
        return None
    prune_quarantine(qroot)
    return entry


def _quarantine_move(src: Path, entry: Path, rel: str) -> bool:
    """Move src to entry/<rel>/<name>; False leaves src untouched in place."""
    dest_root = entry / rel
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest_root / src.name))
        return True
    except OSError as ex:
        warn(f"quarantine: could not move {src} aside: {ex}")
        return False


class FreshSaveError(RuntimeError):
    """The required clean starting state could not be established."""


def fresh_save(userdata: Path, game_name: str, quarantine: Path) -> None:
    """Move stock save folders aside into `quarantine` for a clean world.

    Typical layout: UserData/Saves/<World>/<GameName>. Every world's copy of
    the named game goes; sibling saves and stray files stay. Removal means
    "recoverable until pruned", so a mispointed --userdata cannot destroy real
    playthroughs irrecoverably.
    """
    saves = userdata / "Saves"
    if not saves.is_dir():
        return
    try:
        world_dirs = [d for d in saves.iterdir() if d.is_dir()]
    except OSError as ex:
        raise FreshSaveError(
            f"fresh-save: could not scan {saves}; refusing to reuse an "
            f"unknown prior save: {ex}"
        ) from ex
    entry = _quarantine_entry(quarantine, "stock-save")
    removed = 0
    failed = 0
    for world_dir in world_dirs:
        target = world_dir / game_name
        if target.is_dir():
            moved = (
                entry is not None
                and _quarantine_move(target, entry, f"{world_dir.name}--{game_name}")
            )
            if not moved:
                # A surviving save would silently poison the run: dig/place
                # would then test the previous run's terrain, not the server.
                failed += 1
                warn(f"fresh-save: could not remove {target}")
                continue
            removed += 1
            log(f"fresh-save removed {target}")
    if removed == 0 and failed == 0:
        log(f"fresh-save: no existing save named {game_name}")
    if failed:
        raise FreshSaveError(
            f"fresh-save: {failed} existing save(s) named {game_name} could not "
            "be quarantined; refusing to run against stale state"
        )


def fresh_zdtd_world(world: Path, quarantine: Path) -> None:
    """Move zdtd persisted state aside (`--world`) for a clean starting bag.

    players.zsv / containers.zct / blockmeta.zbm plus c_*.zch* chunk overlays
    go to `quarantine` so dig/place start from the map baseline; a failed move
    leaves the file in place (stale state reused, never silent loss).
    """
    if not world.is_dir():
        return
    try:
        chunks_to_move = sorted(world.glob("c_*.zch*"))
    except OSError as ex:
        raise FreshSaveError(
            f"fresh-save: could not scan zdtd world {world}; refusing to reuse "
            f"unknown persisted state: {ex}"
        ) from ex
    state_files = [
        world / name
        for name in ("players.zsv", "containers.zct", "blockmeta.zbm")
        if (world / name).is_file()
    ]
    if not state_files and not chunks_to_move:
        log(f"fresh-save: no persisted zdtd state under {world}")
        return
    entry = _quarantine_entry(quarantine, f"zdtd-world--{world.name}")
    if entry is None:
        raise FreshSaveError(
            f"fresh-save: could not quarantine persisted state under {world}; "
            "refusing to run against stale state"
        )
    state = 0
    failed: list[Path] = []
    for p in state_files:
        if _quarantine_move(p, entry, "state"):
            state += 1
            log(f"fresh-save removed {p}")
        else:
            failed.append(p)
    chunks = 0
    for ch in chunks_to_move:
        if _quarantine_move(ch, entry, "chunks"):
            chunks += 1
        else:
            failed.append(ch)
    if failed:
        names = ", ".join(str(path) for path in failed)
        raise FreshSaveError(
            f"fresh-save: could not quarantine persisted zdtd state: {names}; "
            "refusing to run against stale state"
        )
    log(
        f"fresh-save zdtd world cleaned under {world} "
        f"(state={state}, chunks={chunks})"
    )


def snapshot_previous_log(path: Path | None, qroot: Path, kind: str) -> bool:
    """Copy the previous run's log into the quarantine before truncation.

    Returns True when truncation is safe (nothing to preserve, copy done) or
    False when the quarantine is unusable and the caller must leave the bytes
    in place per the no-destruction rule (README "State, backups, and
    recovery"). The truncation itself stays part of the normal path:
    incremental readers depend on starting from an empty file.
    """
    if path is None or not path.is_file():
        return True
    entry = _quarantine_entry(qroot, kind)
    if entry is None:
        return False
    try:
        shutil.copy2(path, entry / path.name)
    except OSError as ex:
        warn(f"could not preserve previous {kind}: {ex}")
        return False
    return True


# Suite ids whose live cases depend on host-serviced admin fixtures. The
# barrier handlers below (spawn_zombie, kill_fixture_zombie, spawn_trader,
# spawn_vehicle, kill_player, settime_*, bot_spawn, bot_player_near) only arm
# when the selection names one of these; every other suite must stay
# telnet-free. demo/full/all/live/demo_mode/benchmark/bench/mp/residual/
# residual_light are aliases or synonyms whose expansions include fixture
# suites; each ExpandSuites synonym must appear here too, or one spelling of
# the same selection arms fixtures while the other leaves its barriers
# unserviced.
FIXTURE_SUITE_IDS = frozenset(
    (
        # Aliases that expand into fixture-bearing suites.
        "demo",
        "demo_mode",
        "full",
        "all",
        "live",
        "benchmark",
        "bench",
        "mp",
        "residual",
        "residual_light",
        # Catalog suites with live cases that fire host-serviced barriers:
        # combat/economy (AI + traders), vehicle (host-owned spawns),
        # finale (player kill), bot (BotMod telnet commands).
        "combat",
        "economy",
        "vehicle",
        "finale",
        "bot",
    )
)


def suite_tokens(suite: str) -> tuple[str, ...]:
    """Split a PLAYTEST_SUITE list on comma, semicolon, or whitespace."""
    return tuple(token for token in re.split(r"[,;\s]+", suite.strip()) if token)


def mixed_visual_suites(suite: str) -> bool:
    """True when a suite list asks for both prefab-look and block-place.

    One instance of mixing unrelated concerns: a prefab in the player's face
    and a block on a voxel are different pictures. The naming is how this
    instance is gated. Not a mix: a particle system that is already part of
    the staged prefab; consecutive cases of one feature in one suite.
    """
    tokens = suite_tokens(suite)
    look = any(token.endswith("_look") for token in tokens)
    block = any("_block_" in token for token in tokens)
    return look and block


# Comma-lists the harness itself documents as one run (README: smoke,core).
# Every other 2+ token list is mixed unrelated concerns unless the caller
# declares the exact set via --concern-suites / PLAYTEST_CONCERN_SUITES.
HARNESS_CONCERN_GROUPS: tuple[frozenset[str], ...] = (frozenset({"smoke", "core"}),)


def mixed_unrelated_suites(suite: str, *, concern_suites: str = "") -> bool:
    """True when a PLAYTEST_SUITE list is more than one undeclared concern.

    One invocation proves one concern. A single suite id is always one
    concern. Catalog aliases (demo, full, …) are one token. Comma-listing
    two feature suites is mixing — run them as separate invocations (a
    matrix) — unless the caller declares *exactly* that list as one concern
    because the cases are consecutive steps of one feature.

    A child that is part of a built prefab is not a second suite. look+block
    is always mixed, even if declared.
    """
    tokens = suite_tokens(suite)
    if mixed_visual_suites(suite):
        return True
    if len(tokens) <= 1:
        return False
    token_set = frozenset(tokens)
    if token_set in HARNESS_CONCERN_GROUPS:
        return False
    declared = frozenset(suite_tokens(concern_suites)) if concern_suites.strip() else frozenset()
    return token_set != declared


def suite_wants_host_fixtures(suite: str) -> bool:
    """True when any selected suite id needs host telnet fixtures.

    Matches whole suite tokens (same , ; space delimiters as the client's
    Catalog.ExpandSuites), never substrings of the joined list: a bare
    "smoke" or "gate" run stays telnet-free, while every catalog suite whose
    live cases emit barrier lines opens the fixture path.
    """
    return any(
        token in FIXTURE_SUITE_IDS
        for token in re.split(r"[,;\s]+", suite.lower())
        if token
    )


def host_fixtures_enabled(suite: str, *, disabled: bool, requested: bool) -> bool:
    """Resolve built-in fixture discovery and the provider-suite opt-in."""
    return not disabled and (requested or suite_wants_host_fixtures(suite))


# Every barrier the orchestrator counts in the client log and services over
# telnet/admin. Single source for both the fired-count and seen-count tables
# in main(), so a new barrier cannot be added to one and missed by the other.
BARRIER_NAMES: tuple[str, ...] = (
    "spawn_zombie",
    "spawn_trader",
    "kill_fixture_zombie",
    "kill_player",
    "settime_bloodmoon",
    "settime_day",
    "spawn_vehicle",
    "spawn_loadgen_peer",
    "spawn_loadgen_bots",
    "bot_spawn",
    "bot_player_near",
    "persist_setup_done",
    "rejoin_setup_done",
    "teleport_persist_pad",
    "apm_dump",
    "chat_echo",
)


# Cap on settime_bloodmoon fires per run: re-barrier spam was flipping the
# world back to 22:00 after settime_day and killing the player in economy cases.
SETTIME_BLOODMOON_MAX_FIRES = 2


def new_barrier_tables() -> tuple[dict[str, int], dict[str, int]]:
    """Fresh (fired, seen) counter pair for one client-log generation.

    The pair must be created together because handlers fire while
    ``fired[name] < seen[name]`` within a generation. Resetting only one side
    at the rejoin boundary keeps stale fired counts from the setup phase, so
    a verify-phase emission of an already-serviced name (teleport_persist_pad,
    rejoin_setup_done, or any provider-named barrier) stays swallowed until
    new lines exceed the leftover count instead of firing once, as intended.
    """
    return (
        dict.fromkeys(BARRIER_NAMES, 0),
        dict.fromkeys(BARRIER_NAMES, 0),
    )


# Parameterised barriers (`chat_echo:<token>`, `spawn_vehicle:<class>`) lift
# their parameter from client-log lines and forward it into telnet console
# commands (`say`, `spawnentityat`). Log bytes are attacker-reachable through
# remote chat text, so a parameter must be a plain identifier before it may
# cross into the admin plane: no whitespace or CR (the telnet session is one
# command per line), no quotes (one handler form wraps it in double quotes),
# no console metacharacters. Entity class names (`zombieBoe`,
# `vehicleMotorcycle`) and chat tokens (`ptchat12345`) all match.
BARRIER_PARAM_RE = re.compile(r"[A-Za-z0-9_]{1,64}")


def safe_barrier_param(value: str) -> bool:
    """True when a log-derived barrier parameter may reach a telnet command."""
    return bool(BARRIER_PARAM_RE.fullmatch(value))


# Barrier service actions: one telnet session per fire, passed as the ``act``
# callback of service_barrier. Module-level (they depend only on module
# constants) so the poll loop does not rebuild closures every iteration and
# each handler is named for the barrier it services.


def barrier_tele_pad_and_save(tn: TelnetAdmin) -> bool:
    if tn.teleport_players_to(*PERSIST_PAD_XYZ) == 0:
        warn("teleport_persist_pad: no player ids; retry next poll")
        return False
    # Let server commit player position before later setup/save.
    time.sleep(2.0)
    tn.exec("saveworld")
    return True


def barrier_spawn_zombie(tn: TelnetAdmin) -> None:
    n = tn.spawn_near_players("zombieBoe")
    if n == 0:
        time.sleep(1.0)
        tn.spawn_near_players("zombieBoe")


def barrier_ensure_bots(tn: TelnetAdmin) -> None:
    # BotMod auto-spawns TargetBotCount; ensure at least 6 via telnet if
    # needed (lines with "Bot " in bot list).
    out = tn.exec("bot list")
    if len(re.findall(r"Bot ", out)) < 6:
        r = tn.exec("bot count 6")
        log(f"telnet bot count 6 -> {r[:120]!r}")


def barrier_bot_near_player(tn: TelnetAdmin) -> None:
    pids = tn.list_player_ids()
    if pids:
        ident = str(pids[0])
        r = tn.exec(f"bot player {ident} 1")
        log(f"telnet bot player {ident} 1 -> {r[:120]!r}")
    else:
        r = tn.exec("bot spawn 1")
        log(f"telnet bot spawn 1 -> {r[:120]!r}")


def barrier_kill_fixtures(tn: TelnetAdmin) -> None:
    tn.kill_non_player_ai()


def barrier_kill_first_player(tn: TelnetAdmin) -> None:
    # Kill the human player entity for the death-screen case (not AI). Shared
    # parser: dedupes ids and also understands zdtd's "(entity N)" reply style.
    pids = tn.list_player_ids()
    for pid in pids[:1]:
        r = tn.exec(f"kill {pid}")
        log(f"telnet kill_player {pid} → {r[:80]!r}")


def barrier_set_night(tn: TelnetAdmin) -> None:
    # Day1 22:00 only (not day-7 BM horde).
    r = tn.exec("settime 22000")
    log(f"telnet settime 22000 → {r[:120]!r}")


def barrier_set_morning(tn: TelnetAdmin) -> None:
    # Morning restore; always last after any night set in this poll.
    r = tn.exec("settime 8000")
    log(f"telnet settime 8000 (day) → {r[:120]!r}")
    # Clear AI again after night so leftovers do not down the player.
    tn.clear_ai()


def barrier_spawn_bicycle(tn: TelnetAdmin) -> None:
    # Same path as zombies: spawnentity <playerId> <class>
    n = tn.spawn_near_players("vehicleBicycle")
    if n == 0:
        for cmd in (
            f"spawnentityat vehicleBicycle {PERSIST_PAD_COORDS}",
            "se vehicleBicycle",
        ):
            r = tn.exec(cmd)
            log(f"telnet vehicle {cmd} → {r[:80]!r}")
    else:
        log(f"telnet spawn vehicle near players units~={n}")


def barrier_spawn_trader(tn: TelnetAdmin) -> None:
    n = tn.spawn_near_players("npcTraderJoel")
    if n == 0:
        n = tn.spawn_near_players("npcTraderBob")
    log(f"telnet spawn trader near players units~={n}")


def barrier_teleport_to_pad(tn: TelnetAdmin) -> bool:
    moved = tn.teleport_players_to(*PERSIST_PAD_XYZ)
    if moved == 0:
        warn("teleport_persist_pad: no player ids yet; retry")
        return False
    return True


def pump_log_tail(tail: TailSource, scan: ClientLogScan) -> str:
    """Drain newly appended complete lines through the shared line parser.

    Returns the new text so per-line greps in the caller see exactly what was
    parsed. Both sides share ClientLogScan's parser so incremental results
    cannot drift from parse_client_log over the same bytes; feed_lines keeps
    that one parser to a single pass over each chunk.
    """
    chunk = tail.poll()
    if not chunk:
        return ""
    scan.feed_lines(chunk.splitlines())
    return chunk


def result_echo_line(row: dict[str, str], *, peer: bool = False) -> str:
    """Terminal line for one parsed result row, control characters stripped.

    status / case / detail are parsed back out of client log bytes, which
    carry remote chat text verbatim (chat-echo cases put the last received
    chat line into detail), so the same scrub as every other interactive
    echo applies before this reaches the operator terminal.
    """
    indent = "  peer " if peer else "  "
    return indent + scrub(f"{row['status']} {row['case']} {row.get('detail', '')}")


def latest_playtest_crumb(chunk: str) -> str:
    """Last orchestrator/connect line of one chunk, scrubbed for the terminal.

    Shared by every throttled progress echo so the line filter cannot drift
    between the rejoin-setup loop and the main poll loop.
    """
    crumbs = [
        ln
        for ln in chunk.splitlines()
        if "[7dtd-playtest]" in ln or "[7dtd-fastconnect]" in ln
    ]
    return scrub(crumbs[-1][-160:]) if crumbs else ""


def resolve_telnet_password(operator_value: str | None, *, no_server: bool) -> str:
    """Single credential source for the generated server config and every
    TelnetAdmin session:

      operator-provided   -> used verbatim (config + client agree);
      --no-server attach  -> explicit operator-provided credential required;
      own stock server    -> ephemeral per-run secret written into the 0600
                             generated config and never logged.
    """
    if operator_value:
        return operator_value
    if no_server:
        raise ValueError(
            "--no-server requires --telnet-password or PLAYTEST_TELNET_PASSWORD"
        )
    return secrets.token_urlsafe(15)


def acquire_exclusive_lock(
    session: str,
    path: Path,
    *,
    mark_held: Callable[[], None] | None = None,
) -> None:
    """Acquire the exclusivity lock, undoing a published claim on interrupt.

    A signal (SIGTERM/SIGHUP via the SystemExit conversion) or Ctrl+C landing
    after ``playtest_lock.acquire`` has written our claim but before main()
    records ``lock_held`` would otherwise exit without releasing: the orphan
    claim then sits unheartbeated until the stale window passes, blocking
    every other agent behind a run that is already dead. ``mark_held``
    therefore runs inside this guarded region, so an interrupt landing after
    publication but before main's bookkeeping unwinds through the release
    below instead of skipping it. Release refuses to write unless the file
    names us, so this undo is a no-op whenever the acquire was refused or
    never wrote.
    """
    try:
        playtest_lock.acquire(session, path=path)
        if mark_held is not None:
            mark_held()
    except BaseException:
        with contextlib.suppress(playtest_lock.PlaytestLockError, OSError):
            playtest_lock.release(session, path=path)
        raise


def heartbeat_claim_lost(heartbeat: playtest_lock.HeartbeatThread | None) -> bool:
    """True once our heartbeat saw the lock file stop naming our session.

    ``HeartbeatLoop.lost_claim`` is set when a refresh is refused because
    another session holds (or the shared file was reset): from that moment
    exclusivity is gone and another agent may take over the stale claim and
    start its own client/server. Continuing would interleave two runs on one
    machine (double-bound ports, one run's clean_processes killing the
    other's client), so every long-lived poll loop treats this as an
    immediate harness failure instead of finishing the suite against a
    runtime we may no longer own.
    """
    return heartbeat is not None and heartbeat.loop.lost_claim


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="stock-client playtest orchestrator",
        epilog=(
            "examples:\n"
            "  playtest_run.py --suite smoke\n"
            "  playtest_run.py --server zdtd --port 27025\n"
            "  playtest_run.py --no-server --skip-clean\n"
            "exit codes: 0 all cases pass, 1 case failures, 2 harness error\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--version",
        action="version",
        version=f"playtest_run.py {mod_version()}",
    )
    ap.add_argument(
        "--server",
        choices=("stock", "zdtd"),
        default=os.environ.get("PLAYTEST_SERVER", "stock"),
        help="server backend (env PLAYTEST_SERVER; default stock dedicated)",
    )
    ap.add_argument(
        "--suite",
        default="demo",
        help=(
            "one suite id or alias (demo, gate, smoke, core, …). "
            "A 2+ id list needs --concern-suites unless it is smoke,core"
        ),
    )
    ap.add_argument(
        "--world-name",
        default="Navezgane",
        help="stock GameWorld (Navezgane, Pregen06k01, …)",
    )
    ap.add_argument(
        "--game-name",
        default="PlaytestNav",
        help="stock save GameName under userdata",
    )
    ap.add_argument(
        "--world",
        type=Path,
        default=WORKSPACE / "zdtd-server" / "worlds" / "playtest_auto",
        help="zdtd save dir only (ignored for stock)",
    )
    ap.add_argument(
        "--port",
        type=tcp_port,
        default=None,
        help="ServerPort / connect-to-IP port (stock default 26900, zdtd 27025)",
    )
    ap.add_argument(
        "--admin-port", type=tcp_port, default=8081, help="telnet/admin port"
    )
    ap.add_argument(
        "--zdtd",
        type=Path,
        default=Path(os.environ.get("ZDTD", str(DEFAULT_ZDTD))),
        help="zdtd server binary (env ZDTD)",
    )
    ap.add_argument(
        "--game-srv",
        type=Path,
        default=DEFAULT_GAME_SRV,
        help="stock dedicated server install dir",
    )
    ap.add_argument(
        "--userdata",
        type=Path,
        default=Path(os.environ.get("RE_DEDICATED_USERDATA", str(DEFAULT_USERDATA))),
        help="stock dedicated userdata dir (env RE_DEDICATED_USERDATA)",
    )
    ap.add_argument(
        "--timeout",
        type=positive_seconds,
        default=None,
        help="harness wall-clock timeout in seconds > 0 (env PLAYTEST_TIMEOUT_SEC)",
    )
    ap.add_argument(
        "--rejoin-setup-suite",
        default="",
        help="external provider setup suite before save, server restart, and rejoin",
    )
    ap.add_argument(
        "--rejoin-setup-barrier",
        default="",
        help="Report.Barrier name that makes an external provider setup durable",
    )
    ap.add_argument(
        "--rejoin-teleport",
        type=float,
        nargs=3,
        metavar=("X", "Y", "Z"),
        default=None,
        help="after a provider rejoin, teleport the joined player to these world coordinates",
    )
    ap.add_argument(
        "--logdir",
        type=Path,
        default=Path(
            os.environ.get("LOGDIR", str(Path.home() / ".cache" / "7dtd-playtest"))
        ),
        help="report/server log dir (env LOGDIR)",
    )
    ap.add_argument(
        "--skip-clean",
        action="store_true",
        help="do not pkill leftover game processes before start",
    )
    ap.add_argument(
        "--kill-wine", action="store_true", help="also kill wineserver (disrupts Steam)"
    )
    ap.add_argument(
        "--no-server", action="store_true", help="use already-running server"
    )
    ap.add_argument(
        "--client-log",
        type=Path,
        default=None,
        help="client output log to parse for results "
        "(default: the log under the discovered client install's Proton prefix)",
    )
    ap.add_argument(
        "--attach-reviews",
        type=Path,
        default=None,
        help="directory of deadeye review evidence files (review-*.json); their "
        "paths are attached to the report keyed by suite/case, and no verdict "
        "from them ever reaches the report",
    )
    ap.add_argument(
        "--peer-client-name",
        default=os.environ.get("PLAYTEST_PEER_CLIENT_NAME", ""),
        help="optional distinct Local-platform name for one passive stock peer"
        " (env PLAYTEST_PEER_CLIENT_NAME)",
    )
    ap.add_argument(
        "--peer-client-compat",
        type=Path,
        default=(
            Path(os.environ["PLAYTEST_PEER_CLIENT_COMPAT"])
            if os.environ.get("PLAYTEST_PEER_CLIENT_COMPAT")
            else None
        ),
        help="separate initialized Proton compat profile for --peer-client-name"
        " (env PLAYTEST_PEER_CLIENT_COMPAT)",
    )
    ap.add_argument(
        "--peer-client-log",
        type=Path,
        default=None,
        help="optional stock game log for the peer compat profile",
    )
    ap.add_argument(
        "--peer-client-suite",
        default=os.environ.get("PLAYTEST_PEER_CLIENT_SUITE", ""),
        help="optional suite for the stock peer; empty leaves it passive"
        " (env PLAYTEST_PEER_CLIENT_SUITE)",
    )
    ap.add_argument(
        "--peer-client-teleport",
        type=float,
        nargs=3,
        metavar=("X", "Y", "Z"),
        default=None,
        help="after both scenario clients are ready, teleport all players to these coordinates",
    )
    ap.add_argument(
        "--telnet-password",
        default=os.environ.get("PLAYTEST_TELNET_PASSWORD", ""),
        help=(
            "stock dedicated telnet password (env PLAYTEST_TELNET_PASSWORD); "
            "when unset the orchestrator generates an ephemeral per-run "
            "secret for servers it starts itself; --no-server requires an "
            "explicit credential"
        ),
    )
    ap.add_argument(
        "--no-fixtures",
        action="store_true",
        help="do not telnet-spawn zombies / host fixtures",
    )
    ap.add_argument(
        "--host-fixtures",
        action="store_true",
        help="service host barriers emitted by an external provider suite",
    )
    ap.add_argument("--loadgen-observe-cvar", action="append", default=[], metavar="NAME")
    ap.add_argument("--loadgen-observe-buff", action="append", default=[], metavar="NAME")
    ap.add_argument("--loadgen-expect-cvar", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--loadgen-expect-cvar-positive", action="append", default=[], metavar="NAME")
    ap.add_argument("--loadgen-expect-cvar-equal", action="append", default=[], metavar="NAME=NAME")
    ap.add_argument("--loadgen-expect-buff", action="append", default=[], metavar="NAME=BOOL")
    ap.add_argument(
        "--loadgen-server-cvar-oracle",
        action="store_true",
        help="compare every observed CVar with cvar get on the joined server entity",
    )
    ap.add_argument(
        "--loadgen-server-cvar-tolerance",
        type=float,
        default=0.0001,
        metavar="VALUE",
        help="absolute tolerance for server-oracle versus decoded peer CVars",
    )
    ap.add_argument(
        "--loadgen-teleport",
        type=float,
        nargs=3,
        metavar=("X", "Y", "Z"),
        default=None,
        help="teleport the exact entity from loadgen's structured joined event",
    )
    ap.add_argument(
        "--fresh-save",
        action="store_true",
        help=(
            "accepted for back-compat; a no-op. Fresh save is the hard-rule default and "
            "cannot be turned off: every suite starts on a clean world so it measures fresh "
            "terrain and no leftover blocks, not the previous run's state."
        ),
    )
    ap.add_argument(
        "--junit",
        type=Path,
        default=None,
        help="optional JUnit XML output path",
    )
    ap.add_argument(
        "--session",
        default=os.environ.get("PLAYTEST_SESSION_ID", ""),
        help="playtest lock session id (or PLAYTEST_SESSION_ID); auto-generated if empty",
    )
    ap.add_argument(
        "--concern-suites",
        default=os.environ.get("PLAYTEST_CONCERN_SUITES", ""),
        help=(
            "exact PLAYTEST_SUITE token list that is one declared concern "
            "(consecutive steps of one feature). Required when --suite lists "
            "more than one id. PLAYTEST_CONCERN_SUITES is the env form."
        ),
    )
    args = ap.parse_args(argv)
    if mixed_visual_suites(args.suite):
        ap.error(
            "PLAYTEST_SUITE mixes a prefab-look suite (*_look) and a "
            "block-placement suite (*_block_*); they are different pictures. "
            "Run them as separate playtest invocations."
        )
    if mixed_unrelated_suites(args.suite, concern_suites=args.concern_suites):
        ap.error(
            "PLAYTEST_SUITE lists more than one suite; that is more than one "
            "concern. Run them as separate invocations (a matrix), or pass "
            "--concern-suites with exactly this list when they are consecutive "
            "steps of one feature. A child that is part of a built prefab is "
            "not a second suite."
        )
    # Backend default resolves before the validation below: require_litenet_room
    # compares an int, and every default-port invocation (make playtest with
    # PORT= empty) reaches here without --port.
    if args.port is None:
        args.port = 27025 if args.server == "zdtd" else 26900
    if args.server == "stock" and args.no_server and not args.telnet_password:
        ap.error("--no-server requires --telnet-password or PLAYTEST_TELNET_PASSWORD")
    if (
        not math.isfinite(args.loadgen_server_cvar_tolerance)
        or args.loadgen_server_cvar_tolerance < 0
    ):
        ap.error("--loadgen-server-cvar-tolerance must be a finite non-negative number")
    try:
        require_litenet_room(args.port)
    except ValueError as ex:
        ap.error(f"--port invalid: {ex}")
    loadgen_observer_requested = bool(
        args.loadgen_observe_cvar
        or args.loadgen_observe_buff
        or args.loadgen_expect_cvar
        or args.loadgen_expect_cvar_positive
        or args.loadgen_expect_cvar_equal
        or args.loadgen_expect_buff
        or args.loadgen_server_cvar_oracle
        or args.loadgen_teleport
    )
    if (
        args.loadgen_expect_cvar
        or args.loadgen_expect_cvar_positive
        or args.loadgen_expect_cvar_equal
        or args.loadgen_expect_buff
        or args.loadgen_server_cvar_oracle
    ) and not (
        args.loadgen_observe_cvar or args.loadgen_observe_buff
    ):
        ap.error("loadgen expectations require matching observe options")
    peer_client_name = args.peer_client_name.strip()
    peer_client_suite = args.peer_client_suite.strip()
    if bool(peer_client_name) != bool(args.peer_client_compat):
        ap.error("--peer-client-name and --peer-client-compat must be provided together")
    if peer_client_suite and not peer_client_name:
        ap.error("--peer-client-suite requires --peer-client-name and --peer-client-compat")
    if args.peer_client_teleport is not None and not peer_client_suite:
        ap.error("--peer-client-teleport requires --peer-client-suite")
    has_rejoin_setup_suite = bool(args.rejoin_setup_suite.strip())
    has_rejoin_setup_barrier = bool(args.rejoin_setup_barrier.strip())
    if has_rejoin_setup_suite != has_rejoin_setup_barrier:
        ap.error(
            "--rejoin-setup-suite and --rejoin-setup-barrier must be provided together"
        )
    if args.suite.strip() == "persist" and has_rejoin_setup_suite:
        ap.error("persist already has a built-in rejoin setup; omit provider rejoin options")
    provider_rejoin = has_rejoin_setup_suite
    if args.rejoin_teleport is not None and not provider_rejoin:
        ap.error("--rejoin-teleport requires the paired provider rejoin options")
    rejoin_flow = args.suite.strip() == "persist" or provider_rejoin
    if rejoin_flow and peer_client_name:
        ap.error("stock peer clients are not supported with a rejoin flow")
    rejoin_setup_suite = (
        args.rejoin_setup_suite.strip() if provider_rejoin else "persist_setup"
    )
    rejoin_setup_barrier = (
        args.rejoin_setup_barrier.strip() if provider_rejoin else "persist_setup_done"
    )
    rejoin_label = "provider rejoin" if provider_rejoin else "persist multi-phase"

    if args.timeout is None:
        args.timeout = seconds_from_env("PLAYTEST_TIMEOUT_SEC", 900.0)

    # One effective-config line at startup (password redacted) so a misread
    # env var or stale shell default is visible in every log without --help.
    log("config: " + config_summary(args))

    report_path = args.logdir / f"report-{int(time.time())}.json"
    server_log = args.logdir / "server-orch.log"
    client_launch_log = args.logdir / "client-launch.log"
    peer_client_launch_log = args.logdir / "peer-client-launch.log"
    peer_client_log = (
        args.peer_client_log
        or (client_log_for_compat(args.peer_client_compat) if peer_client_name else None)
    )

    if args.server == "zdtd" and not args.no_server and not args.zdtd.is_file():
        err(f"missing zdtd binary: {args.zdtd}")
        return 2
    if (
        args.server == "stock"
        and not args.no_server
        and not (args.game_srv / "7DaysToDieServer.x86_64").is_file()
    ):
        err(f"missing stock dedicated under {args.game_srv}")
        return 2
    if not (CONNECT / "scripts" / "launch_client.sh").is_file():
        err(f"missing connect launcher under {CONNECT}")
        return 2
    # The client install is preflighted for the same reason the dedicated
    # server above is. launch_client.sh resolves it from GAME and exits with
    # "Game not found" into its own launch log, which nothing here reads until
    # the run is over, so a caller who forgot to export GAME saw a client that
    # simply never appeared and a harness that spent its whole timeout budget
    # waiting for a log the launcher never opened.
    game_dir = client_game_dir()
    if game_dir is None:
        err(
            f"no client install found: no Steam library holds a {CLIENT_EXECUTABLE}. "
            "launch_client.sh reads GAME (not SEVEN_DAYS_TO_DIE_DIR), so export "
            "GAME=<client install> before running, and COMPAT too when the Proton "
            "prefix is not beside it."
        )
        return 2
    if not game_dir.is_dir():
        err(
            f"missing client install at {game_dir} (from GAME). launch_client.sh reads "
            "GAME, not SEVEN_DAYS_TO_DIE_DIR; point it at the client install."
        )
        return 2
    if not (game_dir / CLIENT_EXECUTABLE).is_file():
        err(f"{game_dir} holds no {CLIENT_EXECUTABLE}; GAME must name the client install")
        return 2
    # The log to parse belongs to the prefix of *that* install. Defaulting it
    # from a fixed library instead means a caller on any other layout parses a
    # file the launcher never writes, and reads an empty run as a failed one.
    if args.client_log is None:
        args.client_log = client_log_for_compat(client_compat_for_game(game_dir))
    log(f"client install {game_dir}")
    log(f"client log {args.client_log}")
    if peer_client_name and not args.peer_client_compat.is_dir():
        err(f"peer compat profile is missing or not a directory: {args.peer_client_compat}")
        return 2

    server_proc = None
    client_proc = None
    peer_client_proc = None
    loadgen_proc = None
    loadgen_events_path = args.logdir / "loadgen_events.jsonl"
    # Incremental reader created before any loadgen start so every appended
    # event is parsed exactly once (same discipline as the client-log tail).
    loadgen_event_reader = LoadgenEventReader(loadgen_events_path)
    loadgen_teleported_entity: int | None = None
    exit_code = 2
    parsed: ParsedClientLog = empty_client_log()
    summary: dict | None = None
    unity_log: Path | None = None
    # One-shot flag for the mid-run backend-exit announcement below; reset by
    # start_server() so each new server process gets exactly one verdict.
    server_exit_announced = False
    lock_session = (args.session or "").strip() or playtest_lock.new_session_id("playtest")
    lock_path = playtest_lock.default_lock_path()
    lock_held = False
    lock_heartbeat: playtest_lock.HeartbeatThread | None = None

    install_signal_handlers()

    try:
        # Duration measurement on the monotonic clock; wall clock only for
        # naming and recorded instants.
        t0 = time.monotonic()
        # Exclusive live-client lock BEFORE clean_processes / launch so a second
        # orchestrator cannot wipe another agent's client. See AGENTS.md.
        try:
            def _mark_held() -> None:
                nonlocal lock_held
                lock_held = True

            # The flag flip runs inside the wrapper's guarded region: a signal
            # landing between the claim write and this assignment would
            # otherwise escape both except arms below (SystemExit is neither)
            # and leave finally with lock_held=False, stranding the fresh
            # claim unheartbeated.
            acquire_exclusive_lock(lock_session, lock_path, mark_held=_mark_held)
        except playtest_lock.PlaytestLockError as ex:
            holder = ex.held_by or "unknown"
            err(
                f"refusing start: {ex} "
                f"(held_by={holder} reason={ex.reason} file={lock_path})"
            )
            err(
                "see AGENTS.md (Playtest / live-client exclusivity); "
                "set PLAYTEST_LOCK_FILE / PLAYTEST_SESSION_ID to coordinate"
            )
            return 2
        except OSError as ex:
            # Unwritable lock dir/sidecar is an environment failure, not a
            # holder: name it like a refusal instead of a traceback.
            err(f"refusing start: lock storage unavailable at {lock_path}: {ex}")
            return 2
        log(
            f"playtest lock acquired session={lock_session} file={lock_path} "
            f"(exclusive client+server runtime)"
        )
        lock_heartbeat = playtest_lock.HeartbeatThread(
            lock_session,
            path=lock_path,
            on_error=lambda ex: warn(f"lock heartbeat: {ex}"),
        )
        lock_heartbeat.start()

        def write_run_ended(reason: str) -> None:
            """Record why the orchestrator's poll loop ended (log contract)."""
            try:
                (args.logdir / "run-ended").write_text(
                    reason + "\n", encoding="utf-8"
                )
            except OSError as ex:
                warn(f"could not write run-ended marker in {args.logdir}: {ex}")

        def abort_if_lock_lost() -> bool:
            """True when the caller must return immediately: the exclusivity
            heartbeat saw the file stop naming our session, so another agent
            may now drive this machine and finishing the suite would race
            its run."""
            if not heartbeat_claim_lost(lock_heartbeat):
                return False
            err(
                "playtest lock claim lost (heartbeat saw a foreign holder); "
                "stopping instead of sharing this machine with another run"
            )
            write_run_ended("lock_lost")
            return True

        if not args.skip_clean:
            clean_processes(kill_wine=args.kill_wine)

        # After clean, refuse to double-bind if something else still owns ports
        # (orphan outside our pkill patterns, or race with another host).
        if not args.no_server:
            busy = [
                p
                for p in (args.port, args.admin_port)
                if playtest_lock.tcp_port_in_use(p)
            ]
            if busy:
                err(
                    f"refusing start: TCP port(s) still in use after clean: {busy} "
                    f"(another dedicated/zdtd or leftover process). "
                    f"Stop it or pick different --port/--admin-port."
                )
                return 2

        qroot = args.logdir / QUARANTINE_DIRNAME
        # Fresh save is a hard-rule default and cannot be turned off: every suite
        # starts on a clean world, so a place/dig suite measures fresh terrain and
        # no leftover blocks rather than the previous run's state.
        if args.server == "stock":
            fresh_save(args.userdata, args.game_name, qroot)
        else:
            # args.world always carries a Path default; only zdtd reads it.
            fresh_zdtd_world(args.world, qroot)

        preserved_client = snapshot_previous_log(
            args.client_log, qroot, "client-log"
        )
        preserved_peer = snapshot_previous_log(
            peer_client_log, qroot, "peer-client-log"
        ) if peer_client_log is not None else True
        if preserved_client:
            truncate_file(args.client_log, "client log")
        else:
            # Quarantine unusable: the previous generation's bytes stay in
            # place; the tail starts past them so stale events cannot be
            # re-parsed as this run's.
            warn("previous client log kept untruncated; run events are read "
                 "from the end of the existing bytes")
        if peer_client_log is not None:
            peer_client_log.parent.mkdir(parents=True, exist_ok=True)
            if preserved_peer:
                truncate_file(peer_client_log, "peer client log")

        # Incremental readers created right after the truncation above so every
        # later append is seen exactly once. Later truncations (rejoin phases)
        # are detected by LogTail as a shrink and restart from zero. A log left
        # untruncated (quarantine unavailable) starts past its existing bytes.
        client_tail = LogTail(args.client_log, from_end=not preserved_client)
        client_scan = ClientLogScan()
        peer_tail = (
            LogTail(peer_client_log, from_end=not preserved_peer)
            if peer_client_log is not None
            else None
        )
        peer_scan = ClientLogScan()

        # Telnet/admin surface, needed by start_stock_dedicated (config password)
        # and every barrier handler below. Assigned once, before first use.
        telnet_host = "127.0.0.1"
        telnet_port = args.admin_port
        telnet_password = resolve_telnet_password(
            args.telnet_password, no_server=args.no_server
        )

        def service_barrier(
            name: str,
            *,
            counts: dict[str, int],
            seen: dict[str, int],
            act: Callable[[TelnetAdmin], bool | None],
            cap: int | None = None,
        ) -> None:
            """Service each unseen fire of one barrier over a fresh session.

            One connect failure leaves every remaining fire pending so the
            next poll retries instead of losing events. An ``act`` returning
            False means "not serviceable yet" and leaves that fire pending
            too; any other result counts it as serviced. ``cap`` bounds the
            total services per log generation (re-barrier spam guard).
            """
            while counts.get(name, 0) < seen.get(name, 0) and (
                cap is None or counts.get(name, 0) < cap
            ):
                tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                if not tn.connect():
                    warn(f"{name}: telnet connect fail; retry next poll")
                    break
                try:
                    serviced = act(tn)
                finally:
                    tn.close()
                if serviced is False:
                    break
                counts[name] = counts.get(name, 0) + 1

        def teleport_all_players_via_telnet(
            coords: tuple[float, float, float],
        ) -> tuple[int, bool]:
            """Teleport every listed player over one fresh telnet session.

            Returns (moved count, connected) so callers can separate the "no
            player ids listed yet" retry from the connect-failure retry.
            """
            tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
            try:
                connected = tn.connect()
                moved = tn.teleport_players_to(*coords) if connected else 0
            finally:
                tn.close()
            return moved, connected

        def start_server() -> bool:
            """Start the selected backend (unless --no-server) and wait ready.

            One path for the initial start and the rejoin restart so they
            cannot drift (same reason the ready-wait budgets are shared).
            """
            nonlocal server_proc, unity_log, server_exit_announced
            if args.no_server:
                return True
            server_exit_announced = False
            if args.server == "stock":
                server_proc, unity_log = start_stock_dedicated(
                    args.game_srv,
                    args.userdata,
                    server_log,
                    world_name=args.world_name,
                    game_name=args.game_name,
                    port=args.port,
                    telnet_port=args.admin_port,
                    telnet_password=telnet_password,
                )
                return wait_stock_dedicated_ready(server_proc, unity_log)
            server_proc = start_zdtd(
                args.zdtd,
                args.world,
                args.port,
                args.admin_port,
                args.game_srv,
                server_log,
            )
            return wait_zdtd_ready(server_proc, server_log)

        def note_backend_exit() -> None:
            """Announce once when the started dedicated/zdtd dies mid-run.

            After readiness nothing polls the backend: a crash shows up only
            as scattered case failures, telnet connect misses, or a full
            timeout, none of which name the cause. Naming the exit code and
            log here makes the root cause visible in the run transcript; the
            run itself still ends by its own rules (DONE / no-DONE verdict).
            """
            nonlocal server_exit_announced
            if args.no_server or server_proc is None or server_exit_announced:
                return
            if server_proc.poll() is None:
                return
            server_exit_announced = True
            err(
                f"{args.server} backend exited mid-run code={server_proc.returncode} "
                f"(log {server_log})"
            )

        if not start_server():
            return 2

        # zdtd admin TCP speaks the same command surface the orch uses for stock
        # telnet (listplayers/listents/kill/spawnentity/settime). Enable fixtures
        # for both backends so kill/spawn barriers actually fire on playtest-zdtd.
        want_fixtures = host_fixtures_enabled(
            args.suite,
            disabled=args.no_fixtures,
            requested=args.host_fixtures,
        )

        # Poll budget on the monotonic clock so a wall-clock step (NTP or
        # manual) during a long soak cannot hang or truncate the run.
        deadline = time.monotonic() + args.timeout
        # soak_long needs ≥15 min wall + setup; bump default timeout. Whole-
        # token match (same , ; space delimiters as Catalog.ExpandSuites, see
        # suite_wants_host_fixtures): a provider suite whose name merely
        # contains "soak_long" must not silently inflate the run budget.
        if "soak_long" in re.split(r"[,;\s]+", args.suite.lower()):
            deadline = time.monotonic() + max(args.timeout, 1100.0)
            log(f"soak_long timeout deadline wall_s>={int(deadline - time.monotonic())}")
        last_progress = float("-inf")
        # Fired and seen counts per barrier (fired may trail seen: combat +
        # sleeper + economy re-spawn/kill). Created as one pair per log
        # generation so a handler can never compare across generations.
        barrier_counts, barrier_seen = new_barrier_tables()
        ready_seen = False
        peer_ready_seen = not bool(peer_client_suite)
        peer_teleport_done = args.peer_client_teleport is None
        rejoin_teleport_done = args.rejoin_teleport is None
        cleaned_ai = False
        # One-shot barrier state for parameterized barriers (per run, not per call).
        chat_tokens_fired: set[str] = set()
        vehicle_spawns_fired: dict[str, int] = {}
        # Cumulative spawn_vehicle:<class> barrier lines seen in the log.
        vehicle_seen: dict[str, int] = {}
        apm_dump_path = args.logdir / "zdtd_apm_dump.txt"
        apm_run_id = f"apm-{int(time.time())}-{os.getpid()}"
        client_extra_env: dict[str, str] = {}
        # Same , ; space delimiters as the client's Catalog.ExpandSuites (see
        # suite_wants_host_fixtures): "smoke apm" must arm the dump env exactly
        # like "smoke,apm", or the in-client apm case waits on a path this
        # host never hands it and can only fail.
        if "apm" in re.split(r"[,;\s]+", args.suite.lower()):
            client_extra_env["ZDTD_APM_DUMP"] = str(apm_dump_path)
            client_extra_env["ZDTD_APM_RUN_ID"] = apm_run_id
            # Preseed is explicitly NOT a valid live dump (client rejects APM_PRESEED).
            try:
                apm_dump_path.parent.mkdir(parents=True, exist_ok=True)
                apm_dump_path.write_text(
                    "APM_PRESEED placeholder; waiting for barrier dump\n",
                    encoding="utf-8",
                )
                log(f"apm preseed placeholder → {apm_dump_path} run_id={apm_run_id}")
            except OSError as ex:
                # The client's apm case will fail on the stale/missing dump;
                # losing the whole run to this write would hide every other
                # result behind a traceback.
                warn(f"could not write apm preseed {apm_dump_path}: {ex}")

        # Rejoin flows start their setup client below; other suites start now.
        if rejoin_flow:
            client_proc = None
        else:
            client_proc = start_client(
                args.port,
                args.suite,
                client_launch_log,
                extra_env=client_extra_env or None,
            )
            if peer_client_name:
                # V3.1 LiteNetLibAuthWrapperServer rejects same-IP connection
                # attempts less than 500 ms apart. Both local stock clients
                # use 127.0.0.1, so launching them back-to-back deterministically
                # strands the peer at ConnectionRejected/RateLimit. Keep a
                # full-second margin over the installed engine's limit.
                time.sleep(1.0)
                peer_env = {
                    "COMPAT": str(args.peer_client_compat),
                    "7DTD_PLAYER_NAME": peer_client_name,
                }
                peer_client_proc = start_client(
                    args.port,
                    peer_client_suite,
                    peer_client_launch_log,
                    extra_env=peer_env,
                    run_suite=bool(peer_client_suite),
                )

        # Rejoin flow: setup → saveworld → restart server → rejoin verify.
        # Built-in persist keeps its authoritative pad handling. Providers only
        # need to report their declared durable setup barrier.
        if rejoin_flow:
            log(
                f"{rejoin_label}: setup={rejoin_setup_suite} → saveworld → "
                "restart server → rejoin verify"
            )
            stop_proc(client_proc)
            truncate_file(args.client_log, "client log")
            # Fresh readers for the new log generation: the scan must hold only
            # setup-phase events, not bytes from before the truncation.
            client_tail = LogTail(args.client_log)
            client_scan = ClientLogScan()
            client_proc = start_client(
                args.port,
                rejoin_setup_suite,
                client_launch_log,
                extra_env=client_extra_env,
            )
            # Phase budgets stay inside the documented harness wall clock
            # (--timeout bounds the whole run): each rejoin phase is bounded
            # by its own per-phase cap AND what remains of the run deadline.
            remaining_sec = max(0.0, deadline - time.monotonic())
            setup_deadline = time.monotonic() + min(
                min(args.timeout, 300), remaining_sec
            )
            last_setup_progress = float("-inf")
            rejoin_setup_seen = 0

            while time.monotonic() < setup_deadline:
                if abort_if_lock_lost():
                    return 2
                reap_finished_helpers()
                note_backend_exit()
                chunk = pump_log_tail(client_tail, client_scan)
                if chunk:
                    now = time.monotonic()
                    if now - last_setup_progress > 8:
                        last_setup_progress = now
                        crumb = latest_playtest_crumb(chunk)
                        if crumb:
                            log(f"setup progress: {crumb}")
                    add_barrier_hits(barrier_seen, chunk)
                    # The provider barrier name is arbitrary, so it cannot live
                    # in the fixed barrier_seen table; count it separately.
                    rejoin_setup_seen += barrier_line_hits(chunk, rejoin_setup_barrier)
                    if not provider_rejoin:
                        # Server-authoritative pad tele so pos_survives_rejoin is real.
                        service_barrier(
                            "teleport_persist_pad",
                            counts=barrier_counts,
                            seen=barrier_seen,
                            act=barrier_tele_pad_and_save,
                        )
                    if rejoin_setup_seen > barrier_counts["rejoin_setup_done"]:
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if not tn.connect():
                            warn(f"{rejoin_setup_barrier}: telnet connect fail; retry")
                        else:
                            try:
                                n = 1
                                if not provider_rejoin:
                                    # Re-tele pad once more so the last write
                                    # before disconnect is the pad position.
                                    n = tn.teleport_players_to(*PERSIST_PAD_XYZ)
                                if n == 0:
                                    warn(
                                        f"{rejoin_setup_barrier}: no player ids; "
                                        "retry next poll"
                                    )
                                else:
                                    time.sleep(1.5)
                                    for cmd in ("saveworld", "sa"):
                                        r = tn.exec(cmd)
                                        log(f"telnet {cmd} → {r[:100]!r}")
                                    time.sleep(2.0)
                                    r = tn.exec("saveworld")
                                    log(f"telnet saveworld (settle) → {r[:100]!r}")
                                    # The fire is marked once either way (retrying
                                    # the save every poll would spam), but a
                                    # session that died mid-save must say so: the
                                    # rejoin verify would otherwise load pre-setup
                                    # state with nothing in the transcript naming
                                    # why.
                                    if not tn.connected():
                                        warn(
                                            f"{rejoin_setup_barrier}: telnet session "
                                            "died during save; setup state may not "
                                            "be durable"
                                        )
                                    barrier_counts["rejoin_setup_done"] = rejoin_setup_seen
                            finally:
                                tn.close()
                    setup_parsed = client_scan.result()
                    if setup_parsed.get("done") is not None:
                        log(
                            f"{rejoin_label} setup DONE "
                            f"pass={(setup_parsed.get('summary') or {}).get('pass')} "
                            f"fail={(setup_parsed.get('summary') or {}).get('fail')}"
                        )
                        break
                if client_proc.poll() is not None:
                    time.sleep(1)
                    if client_scan.result().get("done") is not None:
                        log(f"{rejoin_label} setup DONE (client exited)")
                        break
                    # Same fail-fast as the main poll loop: a setup client that
                    # died without DONE should abort the rejoin, not sit out
                    # the loop's whole budget.
                    log(
                        f"{rejoin_label} setup client exited before DONE; "
                        "aborting rejoin instead of waiting out the timeout"
                    )
                    break
                time.sleep(0.5)
            # Require setup DONE before rejoin verify; otherwise fixtures never existed.
            setup_parsed = client_scan.result()
            setup_done = setup_parsed.get("done") is not None
            setup_fail = int((setup_parsed.get("summary") or {}).get("fail") or 0)
            setup_pass = int((setup_parsed.get("summary") or {}).get("pass") or 0)
            setup_barrier_seen = barrier_counts["rejoin_setup_done"] > 0
            if not setup_done or setup_fail > 0 or not setup_barrier_seen:
                log(
                    f"{rejoin_label} setup incomplete done={setup_done} pass={setup_pass} "
                    f"fail={setup_fail} barrier={setup_barrier_seen}; "
                    "aborting rejoin verify"
                )
                stop_proc(client_proc)
                client_proc = None
                stop_proc(server_proc)
                server_proc = None
                pkill_patterns(GAME_PROC_PATTERNS, sig="-9")
                summary = {
                    "pass": setup_pass,
                    "fail": max(setup_fail, 1),
                    "skip": int((setup_parsed.get("summary") or {}).get("skip") or 0),
                }
                results = setup_parsed.get("results") or []
                junit_path = args.logdir / f"junit-{int(time.time())}.xml"
                write_report(
                    report_path,
                    {
                        "suite": args.suite,
                        "summary": summary,
                        "results": results,
                        "error": f"{rejoin_label} setup incomplete",
                        "server_exited_mid_run": server_exit_announced,
                    },
                )
                write_junit(junit_path, args.suite, results)
                prune_run_artifacts(args.logdir)
                if setup_done and setup_fail > 0:
                    # A completed setup phase that reported FAIL rows is "one
                    # or more case failures" (exit 1 per the README table),
                    # not a harness error; only an aborted phase is exit 2.
                    err(
                        f"FAIL: {rejoin_label} setup reported {setup_fail} case "
                        f"failure(s); rejoin verify not run"
                    )
                    return 1
                err(f"FAIL harness: {rejoin_label} setup incomplete")
                return 2

            tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
            if not tn.connect():
                # Silent would look like a clean save when nothing was saved;
                # say why the setup state may not be durable before teardown.
                warn(
                    f"{rejoin_label}: post-setup saveworld/kickall skipped "
                    "(telnet connect fail)"
                )
            else:
                try:
                    # Persist needs the pad as the last player state; providers retain
                    # the position their setup case actually established.
                    if not provider_rejoin:
                        tn.teleport_players_to(*PERSIST_PAD_XYZ)
                    time.sleep(1.5)
                    r = tn.exec("saveworld")
                    log(f"telnet saveworld (post-setup) → {r[:100]!r}")
                    time.sleep(2.0)
                    tn.exec("sa")
                    time.sleep(1.0)
                    tn.exec("kickall")
                finally:
                    tn.close()
            time.sleep(4)
            stop_proc(client_proc)
            client_proc = None
            stop_proc(server_proc)
            server_proc = None
            # Prefer graceful exit first; escalate only if still alive.
            pkill_patterns(
                [
                    r"7DaysToDieServer\.x86_64",
                ],
                sig="-15",
            )
            time.sleep(3)
            pkill_patterns(GAME_PROC_PATTERNS, sig="-9")
            time.sleep(5)
            # Restart dedicated on same save (no fresh_save).
            if not start_server():
                return 2
            truncate_file(args.client_log, "client log")
            # Fresh readers + fresh counter pair for the verify generation: the
            # old log's barrier lines were already serviced (or deliberately
            # dropped with the setup client) and must neither re-fire nor leak
            # into the final parsed report, nor leave fired counts that would
            # swallow the first verify-generation emission of the same name.
            client_tail = LogTail(args.client_log)
            client_scan = ClientLogScan()
            barrier_counts, barrier_seen = new_barrier_tables()
            client_proc = start_client(
                args.port, args.suite, client_launch_log, extra_env=client_extra_env
            )
            ready_seen = False
            rejoin_teleport_done = args.rejoin_teleport is None
            # Same run-budget discipline as the setup phase above: the verify
            # phase cannot push the total past --timeout.
            deadline = time.monotonic() + min(
                min(args.timeout, 400),
                max(0.0, deadline - time.monotonic()),
            )

        # Always defined so timeout / missing client logs cannot UnboundLocalError.
        peer_parsed: ParsedClientLog = empty_client_log()
        primary_done_logged = False

        def read_peer_results() -> ParsedClientLog:
            if peer_tail is None:
                return empty_client_log()
            # Drain first so callers between poll ticks still see fresh bytes.
            pump_log_tail(peer_tail, peer_scan)
            return peer_scan.result()

        def peer_suite_done() -> bool:
            return not peer_client_suite or peer_parsed.get("done") is not None

        run_end_reason = "timeout"
        while time.monotonic() < deadline:
            if abort_if_lock_lost():
                return 2
            reap_finished_helpers()
            note_backend_exit()
            chunk = pump_log_tail(client_tail, client_scan)
            peer_chunk = (
                pump_log_tail(peer_tail, peer_scan) if peer_tail is not None else ""
            )
            if chunk:
                # Progress crumbs for long joins
                now = time.monotonic()
                if now - last_progress > 8:
                    last_progress = now
                    crumb = latest_playtest_crumb(chunk)
                    if crumb:
                        log(f"progress: {crumb}")
                add_barrier_hits(barrier_seen, chunk)

                if not ready_seen and "ready player=" in chunk:
                    ready_seen = True
                    log("client playtest ready")
                    # Soft-clear leftover AI (never killall: that kills the player).
                    if want_fixtures and not cleaned_ai:
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if tn.connect():
                            try:
                                tn.clear_ai()
                            finally:
                                # Do NOT enable dm/god here: finale player_death_screen needs
                                # a real kill, and god mode blocked telnet kill entirely.
                                tn.close()
                        else:
                            warn("post-ready clear_ai: telnet connect fail")
                        cleaned_ai = True

                if ready_seen and not rejoin_teleport_done:
                    moved, connected = teleport_all_players_via_telnet(
                        args.rejoin_teleport
                    )
                    if moved > 0:
                        rejoin_teleport_done = True
                        x, y, z = args.rejoin_teleport
                        log(f"provider rejoin teleport complete → {x:g} {y:g} {z:g}")
                    elif connected:
                        warn(
                            "provider rejoin teleport: no player ids from "
                            "listplayers; retry next poll"
                        )
                    else:
                        warn("provider rejoin teleport: telnet connect fail; retry")

                if want_fixtures:
                    # spawn_zombie may fire more than once: combat + sleeper_wake.
                    service_barrier(
                        "spawn_zombie",
                        counts=barrier_counts,
                        seen=barrier_seen,
                        act=barrier_spawn_zombie,
                    )

                    service_barrier(
                        "bot_spawn",
                        counts=barrier_counts,
                        seen=barrier_seen,
                        act=barrier_ensure_bots,
                    )

                    service_barrier(
                        "bot_player_near",
                        counts=barrier_counts,
                        seen=barrier_seen,
                        act=barrier_bot_near_player,
                    )

                    service_barrier(
                        "kill_fixture_zombie",
                        counts=barrier_counts,
                        seen=barrier_seen,
                        act=barrier_kill_fixtures,
                    )

                    service_barrier(
                        "kill_player",
                        counts=barrier_counts,
                        seen=barrier_seen,
                        act=barrier_kill_first_player,
                    )

                    service_barrier(
                        "settime_bloodmoon",
                        counts=barrier_counts,
                        seen=barrier_seen,
                        cap=SETTIME_BLOODMOON_MAX_FIRES,
                        act=barrier_set_night,
                    )
                    if (
                        barrier_counts["settime_bloodmoon"] >= SETTIME_BLOODMOON_MAX_FIRES
                        and barrier_seen["settime_bloodmoon"]
                        > barrier_counts["settime_bloodmoon"]
                    ):
                        # Cap reached: extras could never fire, so swallow them
                        # here. A connect failure must keep retrying instead.
                        barrier_counts["settime_bloodmoon"] = barrier_seen[
                            "settime_bloodmoon"
                        ]

                    service_barrier(
                        "settime_day",
                        counts=barrier_counts,
                        seen=barrier_seen,
                        act=barrier_set_morning,
                    )

                    service_barrier(
                        "spawn_vehicle",
                        counts=barrier_counts,
                        seen=barrier_seen,
                        act=barrier_spawn_bicycle,
                    )

                    service_barrier(
                        "spawn_trader",
                        counts=barrier_counts,
                        seen=barrier_seen,
                        act=barrier_spawn_trader,
                    )

                # Multi-peer / chat / APM barriers (stock or zdtd).
                while (
                    barrier_counts["spawn_loadgen_peer"]
                    < barrier_seen["spawn_loadgen_peer"]
                ):
                    if loadgen_proc is not None and loadgen_proc.poll() is None:
                        # Already running; consume this edge without restart.
                        barrier_counts["spawn_loadgen_peer"] += 1
                        continue
                    if loadgen_proc is not None:
                        # Prior instance already exited (poll above): stop_proc
                        # reaps it here so it cannot linger as a zombie for the
                        # rest of the run (one per spawn_loadgen_peer fire), and
                        # closes its log handle before the rebind.
                        stop_proc(loadgen_proc)
                        loadgen_proc = None
                    loadgen_proc = start_loadgen(
                        game_port=args.port,
                        count=1,
                        timeout_ms=120_000,
                        log_path=args.logdir / "loadgen_peer.log",
                        events_path=(loadgen_events_path if loadgen_observer_requested else None),
                        observe_cvars=args.loadgen_observe_cvar,
                        observe_buffs=args.loadgen_observe_buff,
                    )
                    if loadgen_proc is None:
                        warn("loadgen peer start failed; will retry next poll")
                        break
                    barrier_counts["spawn_loadgen_peer"] += 1

                if (
                    loadgen_proc is not None
                    and args.loadgen_teleport is not None
                    and loadgen_teleported_entity is None
                ):
                    joined_entity = loadgen_joined_entity(loadgen_event_reader.drain())
                    if joined_entity is not None:
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if tn.connect():
                            try:
                                x, y, z = args.loadgen_teleport
                                response = tn.exec(
                                    f"teleportplayer {joined_entity} {x:g} {y:g} {z:g}"
                                )
                                # A broken session returns "" exactly like a silent
                                # success; trust the teleport only when the socket
                                # survived the exchange (exec closes it on failure),
                                # so it retries next poll instead of being recorded
                                # as done for an entity that never moved.
                                survived = tn.connected()
                            finally:
                                tn.close()
                            if survived:
                                loadgen_teleported_entity = joined_entity
                                log(
                                    f"loadgen teleport entity={joined_entity} -> "
                                    f"({x:g},{y:g},{z:g}) {response[:80]!r}"
                                )
                            else:
                                warn(
                                    "loadgen teleport: telnet session died "
                                    "mid-exchange; retrying next poll"
                                )

                while (
                    barrier_counts["spawn_loadgen_bots"]
                    < barrier_seen["spawn_loadgen_bots"]
                ):
                    stop_proc(loadgen_proc)
                    loadgen_proc = start_loadgen(
                        game_port=args.port,
                        count=3,
                        timeout_ms=180_000,
                        log_path=args.logdir / "loadgen_bots.log",
                    )
                    if loadgen_proc is None:
                        warn("loadgen bots start failed; will retry next poll")
                        break
                    barrier_counts["spawn_loadgen_bots"] += 1

                for full in barrier_hits_prefix(chunk, "chat_echo:"):
                    # Fire once per unique token name (only after successful telnet say).
                    raw = full.split(":", 1)[-1].strip()
                    if not raw or raw in chat_tokens_fired:
                        continue
                    token = raw if safe_barrier_param(raw) else ""
                    if not token:
                        # A log line is attacker-reachable via remote chat;
                        # only identifier-shaped tokens may cross into the
                        # console. Mark fired so a bad token is dropped once,
                        # not retried every poll.
                        warn(f"chat_echo:{raw!r}: unsafe token, dropped")
                        chat_tokens_fired.add(raw)
                        continue
                    tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                    if not tn.connect():
                        warn(f"chat_echo:{token} telnet connect fail; retry")
                        continue
                    try:
                        for cmd in (f"say {token}", f'say "{token}"'):
                            r = tn.exec(cmd)
                            log(f"telnet {cmd} → {r[:100]!r}")
                        # Same trust rule as spawn_near_players: a session that
                        # died mid-exchange returns "" exactly like silence. Not
                        # counting the fire leaves it visibly unserviced in the
                        # report instead of a green count over an unsent say.
                        survived = tn.connected()
                    finally:
                        tn.close()
                    if not survived:
                        warn(f"chat_echo:{token}: telnet session died during say")
                        continue
                    chat_tokens_fired.add(token)
                    barrier_counts["chat_echo"] += 1

                # spawn_vehicle:<entityClass>: one host-owned vehicle of that
                # class per barrier line (a provider case that needs, say, a
                # gyrocopter rather than the bare barrier's bicycle). A vehicle
                # the client creates itself is never known to the dedicated
                # server: every position update for it is rejected as an
                # invalid entityId and it cannot fly, so providers must ask the
                # host for one the same way the stock vehicle cases do.
                for full in barrier_hits_prefix(chunk, "spawn_vehicle:"):
                    cls = full.split(":", 1)[-1].strip()
                    if not cls:
                        continue
                    if not safe_barrier_param(cls):
                        # Same trust boundary as chat_echo: the class string
                        # is interpolated into spawnentityat/spawnentity.
                        warn(f"spawn_vehicle:{cls!r}: unsafe entity class, dropped")
                        continue
                    # Key by the full barrier name so service_barrier's
                    # counts/seen lookup sees it (it compares on name).
                    key = f"spawn_vehicle:{cls}"
                    vehicle_seen[key] = vehicle_seen.get(key, 0) + 1
                for key in vehicle_seen:
                    def spawn_class_vehicle(
                        tn: TelnetAdmin, key: str = key
                    ) -> None:
                        cls = key.split(":", 1)[-1]
                        n = tn.spawn_near_players(cls)
                        if n == 0:
                            r = tn.exec(f"spawnentityat {cls} {PERSIST_PAD_COORDS}")
                            log(f"telnet vehicle spawnentityat {cls} → {r[:80]!r}")
                        else:
                            log(f"telnet spawn vehicle {cls} near players units~={n}")

                    service_barrier(
                        key,
                        counts=vehicle_spawns_fired,
                        seen=vehicle_seen,
                        act=spawn_class_vehicle,
                    )

                service_barrier(
                    "teleport_persist_pad",
                    counts=barrier_counts,
                    seen=barrier_seen,
                    act=barrier_teleport_to_pad,
                )

                while barrier_counts["apm_dump"] < barrier_seen["apm_dump"]:
                    ok = write_zdtd_apm_dump(
                        args.zdtd,
                        args.world,
                        args.game_srv,
                        apm_dump_path,
                        ticks=80,
                        run_id=apm_run_id,
                    )
                    if not ok:
                        warn("apm dump write failed; retry next poll")
                        break
                    barrier_counts["apm_dump"] += 1

                parsed = client_scan.result()
                if parsed.get("done") is not None:
                    if not primary_done_logged:
                        primary_done_logged = True
                        log("saw DONE in primary client log")
                    peer_parsed = read_peer_results()
                    if peer_suite_done():
                        log("saw DONE in every scenario client log")
                        run_end_reason = "done"
                        break

            if not peer_ready_seen and peer_chunk and "ready player=" in peer_chunk:
                peer_ready_seen = True
                log("peer client playtest ready")
            if peer_client_suite:
                peer_parsed = read_peer_results()

            if (
                ready_seen
                and peer_ready_seen
                and not peer_teleport_done
                and args.peer_client_teleport is not None
            ):
                moved, connected = teleport_all_players_via_telnet(
                    args.peer_client_teleport
                )
                if moved >= 2:
                    peer_teleport_done = True
                    x, y, z = args.peer_client_teleport
                    log(f"stock peer teleport complete players={moved} → {x:g} {y:g} {z:g}")
                elif connected:
                    warn(
                        f"stock peer teleport: only {moved} player(s) listed, "
                        "need both joined; retry next poll"
                    )
                else:
                    warn("stock peer teleport: telnet connect fail; retry next poll")

            if client_proc is not None and client_proc.poll() is not None:
                time.sleep(2)
                pump_log_tail(client_tail, client_scan)
                parsed = client_scan.result()
                peer_parsed = read_peer_results()
                if parsed.get("done") is not None and peer_suite_done():
                    run_end_reason = "done"
                    break
                # The client exited without the suite's DONE. Waiting out the
                # timeout hides a mid-suite crash behind a 15-minute stall;
                # the 2s drain above already gave the client its last chance
                # to write DONE before the process vanished.
                log("client exited before DONE; failing instead of waiting out the timeout")
                run_end_reason = "client_exit"
                break
            time.sleep(0.5)
        else:
            log(f"timeout after {time.monotonic() - t0:.0f}s waiting for DONE")

        # The run is decided: DONE parsed, the client exited, or the budget
        # ran out. Record it for any consumer watching this run (the capture
        # loop ends when this file appears) instead of leaving them to wait
        # out their own timeout.
        write_run_ended(run_end_reason)

        # Final parse from everything drained so far, plus any bytes appended
        # between the last poll and here.
        pump_log_tail(client_tail, client_scan)
        parsed = client_scan.result()
        if peer_client_suite:
            peer_parsed = read_peer_results()

        summary = parsed.get("summary")
        done = parsed.get("done")
        results = parsed.get("results") or []
        nre = parsed.get("nre_like") or []
        peer_summary = peer_parsed.get("summary")
        peer_done = peer_parsed.get("done")
        peer_results = peer_parsed.get("results") or []
        peer_nre = peer_parsed.get("nre_like") or []
        combined_results = results + peer_results
        wall_s = time.monotonic() - t0

        # Slowest cases from results if ms present in JSON events. Event lines
        # come from the client log, so ms can be garbage or non-finite; either
        # would raise here (losing the whole report) or poison sort/JSON.
        slowest = []
        for ev in parsed.get("json_events") or []:
            if ev.get("t") == "result" and ev.get("status") in ("pass", "fail"):
                # json.loads values only: numbers and numeric strings convert,
                # anything else skips (the old try/except TypeError path).
                ms_raw = ev.get("ms") or 0
                if not isinstance(ms_raw, (int, float, str)):
                    continue
                try:
                    ms = float(ms_raw)
                except ValueError:
                    continue
                if not math.isfinite(ms):
                    continue
                slowest.append(
                    (
                        f"{ev.get('suite')}/{ev.get('case')}",
                        ms,
                    )
                )
        slowest.sort(key=lambda x: -x[1])

        payload = {
            "server": args.server,
            "suite": args.suite,
            "world_name": args.world_name
            if args.server == "stock"
            else str(args.world),
            "port": args.port,
            "summary": summary,
            "done": done,
            "results": results,
            "peer_summary": peer_summary,
            "peer_done": peer_done,
            "peer_results": peer_results,
            "slowest": [{"case": c, "ms": ms} for c, ms in slowest[:8]],
            "nre_like_count": len(nre),
            "nre_like_sample": nre[:10],
            "peer_nre_like_count": len(peer_nre),
            "peer_nre_like_sample": peer_nre[:10],
            "malformed_client_events": parsed.get("malformed_events", 0),
            "peer_malformed_client_events": peer_parsed.get("malformed_events", 0),
            "client_log": str(args.client_log),
            "peer_client_log": str(peer_client_log) if peer_client_log else None,
            "peer_client_suite": peer_client_suite or None,
            "server_log": str(server_log),
            "unity_log": str(unity_log) if unity_log else None,
            "timeout_sec": args.timeout,
            "wall_sec": round(wall_s, 1),
            "ran_epoch": int(time.time()),
            # Structured echo of note_backend_exit(): a report whose cases
            # failed against an already-dead server must say so, not just the
            # terminal transcript.
            "server_exited_mid_run": server_exit_announced,
            "fixtures": {
                "zombie_spawn_attempted": barrier_counts.get("spawn_zombie", 0) > 0,
                "kill_fixture_attempted": barrier_counts.get("kill_fixture_zombie", 0) > 0,
                "barrier_counts": dict(barrier_counts),
                # Fresh save is a hard-rule default and cannot be turned off.
                "fresh_save": True,
            },
            # Additive, optional, paths only: a review's verdict never reaches
            # the report, so a review can never change a case's result.
            "visual_reviews": collect_visual_reviews(args.attach_reviews),
        }
        write_report(report_path, payload)
        junit_path = args.junit or (args.logdir / f"junit-{int(time.time())}.xml")
        write_junit(junit_path, args.suite, combined_results)
        # A user-provided --junit path outside logdir is deliberate evidence
        # placement and is never pruned; only the timestamped logdir defaults
        # are bounded.
        prune_run_artifacts(args.logdir)

        if done is None or (peer_client_suite and peer_done is None):
            missing = "primary" if done is None else "peer"
            err(f"FAIL harness: no DONE from {missing} playtest mod")
            if summary:
                log(f"partial summary={summary}")
            for row in results:
                log(result_echo_line(row))
            for row in peer_results:
                log(result_echo_line(row, peer=True))
            if args.client_log.is_file():
                # One split shared by every key grep: a failed run's client log
                # can reach tens of MB, and re-splitting per key multiplies it.
                try:
                    cl_lines = args.client_log.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                except OSError as ex:
                    # The report/junit above are already written; a log that
                    # vanished (rotation, EIO) must not turn the structured
                    # verdict echo into a raw traceback.
                    warn(f"could not re-read client log for greps: {ex}")
                else:
                    for key in (
                        "7dtd-playtest",
                        "7dtd-fastconnect",
                        "InitMod",
                        "Connect",
                        "ERROR",
                        "Exception",
                    ):
                        hits = [ln for ln in cl_lines if key in ln]
                        if hits:
                            shown = [scrub(ln)[-160:] for ln in hits[-3:]]
                            err(f"client log '{key}' ({len(hits)}): {shown}")
            exit_code = 2
        else:
            if summary:
                log(
                    f"SUMMARY pass={summary['pass']} fail={summary['fail']} "
                    f"skip={summary.get('skip', 0)} wall_s={wall_s:.1f}"
                )
            for row in results:
                log(result_echo_line(row))
            if peer_client_suite and peer_summary:
                log(
                    f"PEER SUMMARY pass={peer_summary['pass']} fail={peer_summary['fail']} "
                    f"skip={peer_summary.get('skip', 0)}"
                )
            for row in peer_results:
                log(result_echo_line(row, peer=True))
            if slowest:
                # Case names come from JSON event fields parsed out of the
                # client log; same control-char boundary as the rows above.
                log(
                    "slowest: "
                    + scrub(", ".join(f"{c}={ms:.0f}ms" for c, ms in slowest[:5]))
                )
            if nre or peer_nre:
                warn(
                    f"primary={len(nre)} peer={len(peer_nre)} "
                    "NRE/underrun-like client lines (see report)"
                )
            malformed = int(parsed.get("malformed_events") or 0) + int(
                peer_parsed.get("malformed_events") or 0
            )
            if malformed:
                warn(
                    f"{malformed} client event line(s) looked like JSON but did "
                    "not parse; skipped (count in report)"
                )
            fails = int(summary["fail"]) if summary else None
            exit_hint = done.get("exit_hint")
            if fails is None and exit_hint is not None:
                fails = exit_hint
            if fails is None:
                fails = 1
            if peer_summary:
                fails += int(peer_summary["fail"])
            if loadgen_observer_requested:
                # One snapshot feeds every observer check: the events file is
                # still growing while loadgen runs, so two reads can straddle
                # an append and make the expectation verdict and the oracle
                # state disagree with each other.
                observer_entity, observer_latest = read_loadgen_latest_state(
                    loadgen_events_path
                )
                observer_failures = loadgen_expectation_failures_from_latest(
                    observer_entity,
                    observer_latest,
                    args.loadgen_expect_cvar,
                    args.loadgen_expect_buff,
                    args.loadgen_expect_cvar_positive,
                    args.loadgen_expect_cvar_equal,
                )
                if args.loadgen_server_cvar_oracle and observer_entity is not None:
                    tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                    if tn.connect():
                        try:
                            observer_failures.extend(
                                server_cvar_oracle_failures(
                                    tn,
                                    observer_entity,
                                    args.loadgen_observe_cvar,
                                    observer_latest,
                                    args.loadgen_server_cvar_tolerance,
                                )
                            )
                        finally:
                            tn.close()
                    else:
                        observer_failures.append(
                            "server CVar oracle telnet connect failed"
                        )
                if args.loadgen_teleport is not None and loadgen_teleported_entity is None:
                    observer_failures.append("joined loadgen entity was never teleported")
                if loadgen_proc is None or loadgen_proc.poll() is not None:
                    observer_failures.append(
                        "loadgen observer process exited before suite completion"
                    )
                if observer_failures:
                    for failure in observer_failures:
                        err(f"loadgen observer: {failure}")
                    fails += 1
                else:
                    log("loadgen observer expectations PASS")
            exit_code = 1 if fails > 0 else 0

        log(f"exit={exit_code}")
        return exit_code
    finally:
        # Disarm first. A TERM/HUP delivered while these cleanup steps run
        # must be ignored, not converted into a SystemExit that aborts the
        # remaining steps: skipping stop_proc/release strands a live runtime
        # under a published claim (the stale_but_live wedge this handler set
        # exists to prevent). The mask goes up before the disarm so a signal
        # landing in the gap between finally entry and the disarm completing
        # is pended, not raised mid-cleanup.
        _block_termination_signals()
        _ignore_termination_signals()
        # Only stop/kill processes when we held the exclusivity lock. A refused
        # acquire must not pkill another agent's client or dedicated server.
        if lock_heartbeat is not None:
            lock_heartbeat.stop()
            lock_heartbeat = None
        if lock_held:
            # client/server/peer/loadgen are pre-initialized to None before the
            # try block, so the finally can always reference them directly.
            stop_proc(client_proc)
            stop_proc(peer_client_proc)
            stop_proc(loadgen_proc)
            stop_proc(server_proc)
            # Poll loops reap mute helpers each iteration, but teardown paths
            # (rejoin abort, exception unwind, post-DONE) skip them: without
            # this reap an exited helper lingers as a zombie until interpreter
            # exit. Helpers still alive here are detached and self-exit within
            # their poll window, reparented to init once this process ends.
            reap_finished_helpers()
            # Soft clean after: leave Steam alone
            pkill_patterns(
                [
                    *GAME_PROC_PATTERNS,
                    r"zig-out/bin/zdtd",
                    r"7dtd-loadgen",
                ],
                sig="-9",
            )
            try:
                playtest_lock.release(lock_session, path=lock_path)
                log(f"playtest lock released session={lock_session}")
            except playtest_lock.PlaytestLockError as ex:
                warn(f"playtest lock release refused: {ex}")
            except OSError as ex:
                warn(f"playtest lock release failed: {ex}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        # main() documents 2 = harness error. An unhandled crash must not fall
        # through to Python's default exit code 1, which means playtest
        # assertion failures. The finally inside main() has already stopped
        # runtimes and released the lock by the time we get here.
        err("harness crashed: unhandled exception (exit 2)")
        traceback.print_exc(file=sys.stderr)
        sys.exit(2)
