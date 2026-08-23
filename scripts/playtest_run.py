#!/usr/bin/env python3
"""Host orchestrator: stock dedicated (default) or zdtd + stock client playtest.

Exit codes:
  0  all cases pass (DONE with fail=0)
  1  playtest assertion failures
  2  harness error (no DONE, server/client fail, timeout, lock refused)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_lock
from playtest_log import (  # noqa: E402
    ClientLogScan,
    LogTail,
    barrier_hits_prefix,
)

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
CONNECT = WORKSPACE / "7dtd-connect"
LOADGEN = WORKSPACE / "7dtd-loadgen"
DEFAULT_ZDTD = WORKSPACE / "zdtd" / "zig-out" / "bin" / "zdtd"
DEFAULT_GAME_SRV = (
    Path.home() / ".local/share/Steam/steamapps/common/7 Days to Die Dedicated Server"
)
DEFAULT_USERDATA = Path.home() / ".cache" / "7dtd-playtest-dedicated"
STEAM_APPID = "251570"
DEFAULT_COMPAT = (
    Path.home() / f".local/share/Steam/steamapps/compatdata/{STEAM_APPID}"
)


def client_log_for_compat(compat: Path) -> Path:
    """Stock launch_client.sh's game log location for one Proton profile."""
    return (
        compat
        / "pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/logs"
        / "output_log_client_7dtd_connect.txt"
    )


CLIENT_LOG = client_log_for_compat(DEFAULT_COMPAT)

# Server-authoritative persist pad: every rejoin/persist flow teleports players
# here before saveworld so the saved position is known and walkable. Tuple for
# teleport_players_to, string form for raw spawnentityat commands.
PERSIST_PAD_XYZ = (520, 62, 950)
PERSIST_PAD_COORDS = " ".join(str(v) for v in PERSIST_PAD_XYZ)

# Client + dedicated process patterns shared by the rejoin teardown steps.
REJOIN_GAME_PROC_PATTERNS = [
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
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}")
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
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}")
    if not 1 <= val <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range 1..65535: {val}")
    return val


def config_summary(args: argparse.Namespace) -> str:
    """Effective top-level options as one startup log line.

    The telnet password appears only as set/unset so run logs stay shareable;
    everything here is already visible in --help or the generated paths.
    """
    parts = [
        f"server={args.server}",
        f"suite={args.suite.strip()}",
        f"port={args.port}",
        f"admin_port={args.admin_port}",
        f"timeout_sec={args.timeout:g}",
        f"world={args.world_name if args.server == 'stock' else args.world}",
        f"game_name={args.game_name}",
        f"logdir={args.logdir}",
        f"fresh_save={bool(args.fresh_save)}",
        f"no_server={bool(args.no_server)}",
        f"fixtures={not args.no_fixtures}",
        f"telnet_password={'set' if args.telnet_password else 'unset'}",
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


def pkill_patterns(patterns: list[str], sig: str = "-9") -> None:
    for pat in patterns:
        subprocess.run(
            ["pkill", sig, "-f", pat],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def clean_processes(*, kill_wine: bool = False) -> None:
    """Stop prior servers/clients. Avoid killing whole wineserver by default
    (that drops Steam); only kill game + dedicated + optional zdtd."""
    log("cleaning prior dedicated / client / zdtd")
    patterns = [
        r"7DaysToDieServer\.x86_64",
        r"7DaysToDieServe",  # truncated comm
        r"zig-out/bin/zdtd",
        r"[/]7DaysToDie\.exe",
        r"wine64-preloader.*7DaysToDie",
        r"proton.*7DaysToDie",
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


def _literal_replacement(replacement: str):
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
    max_players: int = 8,
) -> None:
    text = src_cfg.read_text(encoding="utf-8")
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
        "ServerMaxPlayerCount": str(max_players),
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
    out_cfg.parent.mkdir(parents=True, exist_ok=True)
    out_cfg.write_text(text, encoding="utf-8")
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
    fh = open(log_path, "w", encoding="utf-8")
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
        bak = game_srv / "platform.cfg.playtest-bak"
        if not bak.is_file():
            bak.write_bytes(pcfg.read_bytes())
        pcfg.write_text(
            "platform=Steam\ncrossplatform=None\nserverplatforms=Steam,LAN,Local,\n",
            encoding="utf-8",
        )

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
    proc._unity_log = unity_log  # type: ignore[attr-defined]
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

    Prefer 7dtd-connect's mute_client_audio.sh (same helper launch_client uses).
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
    if exe.is_file():
        return exe
    proj = LOADGEN / "src" / "LoadGen" / "LoadGen.csproj"
    if not proj.is_file():
        warn(f"loadgen project missing: {proj}")
        return None
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
    log(f"start loadgen count={count} litenet={litenet} timeout_ms={timeout_ms}")
    return _popen_to_logfile(cmd, log_path, cwd=str(LOADGEN))


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
    if not has_marker or not out.strip():
        warn(f"apm dump: no live markers in output (len={len(out)} rc={r.returncode})")
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(
            "APM_DUMP_FAILED no markers from zdtd --ticks\n",
            encoding="utf-8",
        )
        return False
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    body = out
    if run_id:
        # Prefix for correlation only; markers must already exist in body.
        body = f"run_id={run_id}\n" + body
    dump_path.write_text(body, encoding="utf-8")
    log(
        f"apm dump → {dump_path} bytes={dump_path.stat().st_size} run_id={run_id or '-'}"
    )
    return dump_path.is_file() and dump_path.stat().st_size > 0


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
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=_STOP_TERM_WAIT_SEC)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # SIGKILL needs its own reap: without wait() the killed child stays a
        # zombie until this orchestrator exits.
        try:
            proc.wait(timeout=_STOP_KILL_WAIT_SEC)
        except subprocess.TimeoutExpired:
            warn(f"stop_proc: pid {proc.pid} not reaped after SIGKILL")
    fh = getattr(proc, "_log_fh", None)
    if fh:
        try:
            fh.close()
        except Exception:
            pass


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

    def connect(self, timeout: float = 5.0) -> bool:
        try:
            self.close()
            s = socket.create_connection((self.host, self.port), timeout=timeout)
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

    def _ai_entity_ids(self, out: str) -> list[str]:
        """Entity ids from listents lines matching the shared AI keyword table."""
        ids: list[str] = []
        for line in out.splitlines():
            low = line.lower()
            if not any(k in low for k in self.AI_LINE_KEYWORDS):
                continue
            m = re.search(r"(?:id|ID)\s*=\s*(\d+)", line)
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
        spawned = 0 if "No spawn point" in r else 1
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
        assert self._sock
        self._sock.sendall((line + "\n").encode("utf-8", errors="replace"))

    def _recv(self, settle: float) -> str:
        assert self._sock
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
        return b"".join(chunks).decode("utf-8", errors="replace")

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


def install_signal_handlers() -> None:
    """Convert SIGTERM/SIGHUP into SystemExit so the finally-based cleanup runs.

    Default signal action kills the process without unwinding: the detached
    client/server survive (start_new_session) and the lock file goes stale
    while a live runtime blocks takeover (stale_but_live wedge). Raising
    SystemExit routes termination through main()'s finally, which stops the
    runtime processes and releases the exclusivity lock.
    """
    sig_names = ("SIGTERM", "SIGHUP")

    def _exit_fast(signum, _frame):
        # A second hit during cleanup must not raise inside the finally block
        # and skip stop_proc/release, so ignore repeats while we unwind.
        for name in sig_names:
            s = getattr(signal, name, None)
            if s is not None:
                try:
                    signal.signal(s, signal.SIG_IGN)
                except (ValueError, OSError):
                    pass
        raise SystemExit(128 + signum)

    for name in sig_names:
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
            try:
                old.unlink()
            except OSError:
                pass


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
            try:
                old.unlink()
            except OSError:
                pass


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
    entry = _quarantine_entry(quarantine, "stock-save")
    removed = 0
    failed = 0
    for world_dir in saves.iterdir():
        if not world_dir.is_dir():
            continue
        target = world_dir / game_name
        if target.is_dir():
            moved = (
                entry is not None
                and _quarantine_move(target, entry, f"{world_dir.name}--{game_name}")
            )
            if not moved:
                # A surviving save would silently poison the run: dig/place
                # would then fail on the previous run's terrain, not on the
                # server. Say so instead of logging a false "removed".
                failed += 1
                warn(
                    f"fresh-save: could not remove {target}; "
                    "stale save will be reused"
                )
                continue
            removed += 1
            log(f"fresh-save removed {target}")
    if removed == 0 and failed == 0:
        log(f"fresh-save: no existing save named {game_name}")


def fresh_zdtd_world(world: Path, quarantine: Path) -> None:
    """Move zdtd persisted state aside (`--world`) for a clean starting bag.

    players.zsv / containers.zct / blockmeta.zbm plus c_*.zch* chunk overlays
    go to `quarantine` so dig/place start from the map baseline; a failed move
    leaves the file in place (stale state reused, never silent loss).
    """
    if not world.is_dir():
        return
    entry = _quarantine_entry(quarantine, f"zdtd-world--{world.name}")
    if entry is None:
        warn(f"fresh-save: could not clean {world}; stale world will be reused")
        return
    state = 0
    for name in ("players.zsv", "containers.zct", "blockmeta.zbm"):
        p = world / name
        if p.is_file() and _quarantine_move(p, entry, "state"):
            state += 1
            log(f"fresh-save removed {p}")
    chunks = 0
    for ch in sorted(world.glob("c_*.zch*")):
        if _quarantine_move(ch, entry, "chunks"):
            chunks += 1
    if state == 0 and chunks == 0:
        log(f"fresh-save: no persisted zdtd state under {world}")
        return
    log(
        f"fresh-save zdtd world cleaned under {world} "
        f"(state={state}, chunks={chunks})"
    )


def snapshot_previous_log(path: Path | None, qroot: Path, kind: str) -> None:
    """Copy the previous run's log into the quarantine before truncation.

    The truncation itself stays: incremental readers depend on starting from
    an empty file. Only the evidence of the previous run is preserved.
    """
    if path is None or not path.is_file():
        return
    entry = _quarantine_entry(qroot, kind)
    if entry is None:
        return
    try:
        shutil.copy2(path, entry / path.name)
    except OSError as ex:
        warn(f"could not preserve previous {kind}: {ex}")


# Suite ids whose live cases depend on host-serviced admin fixtures. The
# barrier handlers below (spawn_zombie, kill_fixture_zombie, spawn_trader,
# spawn_vehicle, kill_player, settime_*, bot_spawn, bot_player_near) only arm
# when the selection names one of these; every other suite must stay
# telnet-free. demo/full/all/live/benchmark/bench/mp/residual are legacy
# aliases whose expansions include fixture suites.
FIXTURE_SUITE_IDS = frozenset(
    (
        # Aliases that expand into fixture-bearing suites.
        "demo",
        "full",
        "all",
        "live",
        "benchmark",
        "bench",
        "mp",
        "residual",
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


def barrier_line_hits(blob: str, name: str) -> int:
    """Count human `barrier <name>` lines in ``blob`` (whole-name match).

    Report.Barrier also emits JSON with the same name; summing both
    double-fires handlers (e.g. kills bots). The whole-name match keeps
    "spawn_vehicle" from also counting parameterised "spawn_vehicle:<class>"
    lines, which are collected separately via barrier_hits_prefix.
    """
    return len(re.findall(rf"barrier {re.escape(name)}(?![\w:])", blob))


def add_barrier_hits(totals: dict[str, int], blob: str) -> None:
    """Fold the barrier lines of one newly read chunk into cumulative totals.

    Poll loops feed only appended chunks through here instead of re-scanning
    the whole log each poll; totals only grow, matching how handlers compare
    their fired counts against everything seen so far.
    """
    for name in totals:
        hits = barrier_line_hits(blob, name)
        if hits:
            totals[name] += hits


def pump_log_tail(tail: LogTail, scan: ClientLogScan) -> str:
    """Drain newly appended complete lines through the shared line parser.

    Returns the new text so per-line greps in the caller see exactly what was
    parsed. Both sides share ClientLogScan's parser so incremental results
    cannot drift from parse_client_log over the same bytes.
    """
    chunk = tail.poll()
    if not chunk:
        return ""
    for line in chunk.splitlines():
        scan.feed_line(line)
    scan.feed_chunk(chunk)
    return chunk


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
        help="suite alias or list: demo, gate, benchmark, full, smoke, core, …",
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
        default=WORKSPACE / "zdtd" / "worlds" / "playtest_auto",
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
        default=CLIENT_LOG,
        help="client output log to parse for results",
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
        default=os.environ.get("PLAYTEST_TELNET_PASSWORD", "retest"),
        help="stock dedicated telnet password (env PLAYTEST_TELNET_PASSWORD)",
    )
    ap.add_argument(
        "--no-fixtures",
        action="store_true",
        help="do not telnet-spawn zombies / host fixtures",
    )
    ap.add_argument(
        "--fresh-save",
        action="store_true",
        help="wipe stock save GameName before start (reproducible dig pad)",
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
    args = ap.parse_args(argv)
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

    if args.port is None:
        args.port = 27025 if args.server == "zdtd" else 26900
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
    if peer_client_name and not args.peer_client_compat.is_dir():
        err(f"peer compat profile is missing or not a directory: {args.peer_client_compat}")
        return 2

    server_proc = None
    client_proc = None
    peer_client_proc = None
    loadgen_proc = None
    exit_code = 2
    parsed: dict = {}
    summary: dict | None = None
    unity_log: Path | None = None
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
            playtest_lock.acquire(lock_session, path=lock_path)
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
        lock_held = True
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
        if args.fresh_save:
            if args.server == "stock":
                fresh_save(args.userdata, args.game_name, qroot)
            elif args.server == "zdtd" and args.world is not None:
                fresh_zdtd_world(Path(args.world), qroot)

        snapshot_previous_log(args.client_log, qroot, "client-log")
        if peer_client_log is not None:
            snapshot_previous_log(peer_client_log, qroot, "peer-client-log")
        truncate_file(args.client_log, "client log")
        if peer_client_log is not None:
            peer_client_log.parent.mkdir(parents=True, exist_ok=True)
            truncate_file(peer_client_log, "peer client log")

        # Incremental readers created right after the truncation above so every
        # later append is seen exactly once. Later truncations (rejoin phases)
        # are detected by LogTail as a shrink and restart from zero.
        client_tail = LogTail(args.client_log)
        client_scan = ClientLogScan()
        peer_tail = (
            LogTail(peer_client_log) if peer_client_log is not None else None
        )
        peer_scan = ClientLogScan()

        # Telnet/admin surface, needed by start_stock_dedicated (config password)
        # and every barrier handler below. Assigned once, before first use.
        telnet_host = "127.0.0.1"
        telnet_port = args.admin_port
        telnet_password = args.telnet_password

        if not args.no_server:
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
                ready_log = unity_log
                # Navezgane load: up to ~10 min cold
                if not wait_file_contains(ready_log, "StartGame done", timeout=600):
                    if server_proc.poll() is not None:
                        err(
                            f"stock dedicated exited early code={server_proc.returncode}"
                        )
                        if ready_log.is_file():
                            err(
                                "tail server log:\n"
                                + "\n".join(
                                    ready_log.read_text(encoding="utf-8", errors="replace").splitlines()[
                                        -40:
                                    ]
                                )
                            )
                        return 2
                    warn("no StartGame done yet; server still running, proceeding")
                else:
                    log("stock dedicated ready (StartGame done)")
            else:
                server_proc = start_zdtd(
                    args.zdtd,
                    args.world,
                    args.port,
                    args.admin_port,
                    args.game_srv,
                    server_log,
                )
                if not wait_file_contains(server_log, "tick=20Hz", timeout=60):
                    if server_proc.poll() is not None:
                        log(f"zdtd exited early code={server_proc.returncode}")
                        return 2
                    warn("no tick=20Hz; proceeding")
                else:
                    log("zdtd ready (tick=20Hz)")

        # zdtd admin TCP speaks the same command surface the orch uses for stock
        # telnet (listplayers/listents/kill/spawnentity/settime). Enable fixtures
        # for both backends so kill/spawn barriers actually fire on playtest-zdtd.
        want_fixtures = (
            args.server in ("stock", "zdtd")
            and not args.no_fixtures
            and suite_wants_host_fixtures(args.suite)
        )

        # Poll budget on the monotonic clock so a wall-clock step (NTP or
        # manual) during a long soak cannot hang or truncate the run.
        deadline = time.monotonic() + args.timeout
        # soak_long needs ≥15 min wall + setup; bump default timeout.
        if "soak_long" in args.suite:
            deadline = time.monotonic() + max(args.timeout, 1100.0)
            log(f"soak_long timeout deadline wall_s>={int(deadline - time.monotonic())}")
        last_progress = float("-inf")
        # Fired counts per barrier (multi-fire: combat + sleeper + economy may
        # re-spawn/kill). Handlers fire while a fired count trails the
        # cumulative lines seen for that barrier (barrier_seen below).
        barrier_counts: dict[str, int] = dict.fromkeys(BARRIER_NAMES, 0)
        # Cumulative barrier lines seen in the log so far.
        barrier_seen: dict[str, int] = dict.fromkeys(BARRIER_NAMES, 0)
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
        if "apm" in args.suite.split(","):
            client_extra_env["ZDTD_APM_DUMP"] = str(apm_dump_path)
            client_extra_env["ZDTD_APM_RUN_ID"] = apm_run_id
            # Preseed is explicitly NOT a valid live dump (client rejects APM_PRESEED).
            apm_dump_path.parent.mkdir(parents=True, exist_ok=True)
            apm_dump_path.write_text(
                "APM_PRESEED placeholder; waiting for barrier dump\n",
                encoding="utf-8",
            )
            log(f"apm preseed placeholder → {apm_dump_path} run_id={apm_run_id}")

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
            setup_deadline = time.monotonic() + min(args.timeout, 300)
            last_setup_progress = float("-inf")
            rejoin_setup_seen = 0
            while time.monotonic() < setup_deadline:
                reap_finished_helpers()
                chunk = pump_log_tail(client_tail, client_scan)
                if chunk:
                    now = time.monotonic()
                    if now - last_setup_progress > 8:
                        last_setup_progress = now
                        crumbs = [
                            ln
                            for ln in chunk.splitlines()
                            if "[7dtd-playtest]" in ln or "[7dtd-connect]" in ln
                        ]
                        if crumbs:
                            log(f"setup progress: {crumbs[-1][-160:]}")
                    add_barrier_hits(barrier_seen, chunk)
                    # The provider barrier name is arbitrary, so it cannot live
                    # in the fixed barrier_seen table; count it separately.
                    rejoin_setup_seen += barrier_line_hits(chunk, rejoin_setup_barrier)
                    if not provider_rejoin:
                        # Server-authoritative pad tele so pos_survives_rejoin is real.
                        while (
                            barrier_counts["teleport_persist_pad"]
                            < barrier_seen["teleport_persist_pad"]
                        ):
                            tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                            n = 0
                            if tn.connect():
                                n = tn.teleport_players_to(*PERSIST_PAD_XYZ)
                                if n == 0:
                                    log(
                                        "warn: teleport_persist_pad: no player ids yet; retry next poll"
                                    )
                                    tn.close()
                                    break
                                # Let server commit player position before later setup/save.
                                time.sleep(2.0)
                                tn.exec("saveworld")
                                tn.close()
                            else:
                                log(
                                    "warn: teleport_persist_pad: telnet connect fail; retry"
                                )
                                break
                            barrier_counts["teleport_persist_pad"] += 1
                    if rejoin_setup_seen > barrier_counts["rejoin_setup_done"]:
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if tn.connect():
                            n = 1
                            if not provider_rejoin:
                                # Re-tele pad once more so last write before disconnect is pad pos.
                                n = tn.teleport_players_to(*PERSIST_PAD_XYZ)
                            if n == 0:
                                log(
                                    f"warn: {rejoin_setup_barrier}: no player ids yet; retry next poll"
                                )
                                tn.close()
                            else:
                                time.sleep(1.5)
                                for cmd in ("saveworld", "sa"):
                                    r = tn.exec(cmd)
                                    log(f"telnet {cmd} → {r[:100]!r}")
                                time.sleep(2.0)
                                r = tn.exec("saveworld")
                                log(f"telnet saveworld (settle) → {r[:100]!r}")
                                tn.close()
                                barrier_counts["rejoin_setup_done"] = rejoin_setup_seen
                        else:
                            warn(f"{rejoin_setup_barrier}: telnet connect fail; retry")
                    setup_parsed = client_scan.result()
                    if setup_parsed.get("done") is not None:
                        log(
                            f"{rejoin_label} setup DONE "
                            f"pass={setup_parsed.get('summary', {}).get('pass')} "
                            f"fail={setup_parsed.get('summary', {}).get('fail')}"
                        )
                        break
                if client_proc is not None and client_proc.poll() is not None:
                    time.sleep(1)
                    if client_scan.result().get("done") is not None:
                        log(f"{rejoin_label} setup DONE (client exited)")
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
                pkill_patterns(REJOIN_GAME_PROC_PATTERNS, sig="-9")
                summary = {
                    "pass": setup_pass,
                    "fail": max(setup_fail, 1),
                    "skip": int((setup_parsed.get("summary") or {}).get("skip") or 0),
                }
                results = setup_parsed.get("results") or []
                report_path = args.logdir / f"report-{int(time.time())}.json"
                junit_path = args.logdir / f"junit-{int(time.time())}.xml"
                write_report(
                    report_path,
                    {
                        "suite": args.suite,
                        "summary": summary,
                        "results": results,
                        "error": f"{rejoin_label} setup incomplete",
                    },
                )
                write_junit(junit_path, args.suite, results)
                prune_run_artifacts(args.logdir)
                err(f"FAIL harness: {rejoin_label} setup incomplete")
                return 2

            tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
            if tn.connect():
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
            pkill_patterns(REJOIN_GAME_PROC_PATTERNS, sig="-9")
            time.sleep(5)
            # Restart dedicated on same save (no fresh_save).
            if args.server == "stock" and not args.no_server:
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
                ready_log = unity_log
                if wait_file_contains(ready_log, "StartGame done", timeout=600):
                    log("stock dedicated ready after rejoin restart")
                else:
                    warn("no StartGame done after rejoin restart; proceeding")
            elif args.server == "zdtd" and not args.no_server:
                server_proc = start_zdtd(
                    args.zdtd,
                    args.world,
                    args.port,
                    args.admin_port,
                    args.game_srv,
                    server_log,
                )
                if wait_file_contains(server_log, "tick=20Hz", timeout=60):
                    log("zdtd ready after rejoin restart (tick=20Hz)")
                else:
                    warn("no tick=20Hz after rejoin restart; proceeding")
            truncate_file(args.client_log, "client log")
            # Fresh readers + seen counts for the verify generation: the old
            # log's barrier lines were already serviced and must neither
            # re-fire nor leak into the final parsed report.
            client_tail = LogTail(args.client_log)
            client_scan = ClientLogScan()
            barrier_seen = dict.fromkeys(BARRIER_NAMES, 0)
            client_proc = start_client(
                args.port, args.suite, client_launch_log, extra_env=client_extra_env
            )
            ready_seen = False
            rejoin_teleport_done = args.rejoin_teleport is None
            deadline = time.monotonic() + min(args.timeout, 400)

        # Always defined so timeout / missing client logs cannot UnboundLocalError.
        peer_parsed: dict = {}
        primary_done_logged = False

        def read_peer_results() -> dict:
            if peer_tail is None:
                return {}
            # Drain first so callers between poll ticks still see fresh bytes.
            pump_log_tail(peer_tail, peer_scan)
            return peer_scan.result()

        def peer_suite_done() -> bool:
            return not peer_client_suite or peer_parsed.get("done") is not None

        while time.monotonic() < deadline:
            reap_finished_helpers()
            chunk = pump_log_tail(client_tail, client_scan)
            peer_chunk = (
                pump_log_tail(peer_tail, peer_scan) if peer_tail is not None else ""
            )
            if chunk:
                # Progress crumbs for long joins
                now = time.monotonic()
                if now - last_progress > 8:
                    last_progress = now
                    crumbs = [
                        ln
                        for ln in chunk.splitlines()
                        if "[7dtd-playtest]" in ln or "[7dtd-connect]" in ln
                    ]
                    if crumbs:
                        log(f"progress: {crumbs[-1][-160:]}")
                add_barrier_hits(barrier_seen, chunk)

                if not ready_seen and "ready player=" in chunk:
                    ready_seen = True
                    log("client playtest ready")
                    # Soft-clear leftover AI (never killall: that kills the player).
                    if want_fixtures and not cleaned_ai:
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if tn.connect():
                            tn.clear_ai()
                            # Do NOT enable dm/god here: finale player_death_screen needs
                            # a real kill, and god mode blocked telnet kill entirely.
                            tn.close()
                        else:
                            warn("post-ready clear_ai: telnet connect fail")
                        cleaned_ai = True

                if ready_seen and not rejoin_teleport_done:
                    tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                    moved = 0
                    if tn.connect():
                        moved = tn.teleport_players_to(*args.rejoin_teleport)
                        tn.close()
                    if moved > 0:
                        rejoin_teleport_done = True
                        x, y, z = args.rejoin_teleport
                        log(f"provider rejoin teleport complete → {x:g} {y:g} {z:g}")
                    else:
                        warn("provider rejoin teleport: no joined player yet; retry")

                if want_fixtures:
                    # spawn_zombie (may fire more than once: combat + sleeper_wake)
                    while (
                        barrier_counts["spawn_zombie"]
                        < barrier_seen["spawn_zombie"]
                    ):
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if not tn.connect():
                            warn("spawn_zombie: telnet connect fail; retry next poll")
                            break
                        n = tn.spawn_near_players("zombieBoe")
                        if n == 0:
                            time.sleep(1.0)
                            tn.spawn_near_players("zombieBoe")
                        tn.close()
                        barrier_counts["spawn_zombie"] += 1

                    while barrier_counts["bot_spawn"] < barrier_seen["bot_spawn"]:
                        # BotMod auto-spawns TargetBotCount; ensure at least 6 via telnet if needed
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if not tn.connect():
                            warn("bot_spawn: telnet connect fail; retry next poll")
                            break
                        out = tn.exec("bot list")
                        # Count bots from bot list output (lines with "Bot ")
                        n = len(re.findall(r"Bot ", out))
                        if n < 4:
                            r = tn.exec("bot count 6")
                            log(f"telnet bot count 6 -> {r[:120]!r}")
                        tn.close()
                        barrier_counts["bot_spawn"] += 1

                    while (
                        barrier_counts["bot_player_near"]
                        < barrier_seen["bot_player_near"]
                    ):
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if not tn.connect():
                            warn("bot_player_near: telnet connect fail; retry next poll")
                            break
                        pids = tn.list_player_ids()
                        if pids:
                            ident = str(pids[0])
                            r = tn.exec(f"bot player {ident} 1")
                            log(f"telnet bot player {ident} 1 -> {r[:120]!r}")
                        else:
                            r = tn.exec("bot spawn 1")
                            log(f"telnet bot spawn 1 -> {r[:120]!r}")
                        tn.close()
                        barrier_counts["bot_player_near"] += 1

                    while (
                        barrier_counts["kill_fixture_zombie"]
                        < barrier_seen["kill_fixture_zombie"]
                    ):
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if not tn.connect():
                            warn("kill_fixture_zombie: telnet connect fail; retry next poll")
                            break
                        tn.kill_non_player_ai()
                        tn.close()
                        barrier_counts["kill_fixture_zombie"] += 1

                    while barrier_counts["kill_player"] < barrier_seen["kill_player"]:
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if not tn.connect():
                            warn("kill_player: telnet connect fail; retry next poll")
                            break
                        # Kill the human player entity for death-screen case (not AI).
                        out = tn.exec("listplayers")
                        pids = [
                            int(x)
                            for x in re.findall(r"id\s*=\s*(\d+)", out, flags=re.IGNORECASE)
                        ]
                        for pid in pids[:1]:
                            r = tn.exec(f"kill {pid}")
                            log(f"telnet kill_player {pid} → {r[:80]!r}")
                        tn.close()
                        barrier_counts["kill_player"] += 1

                    # Cap night sets: re-barrier spam was flipping the world back to 22:00
                    # after settime_day and killing the player in economy cases.
                    while (
                        barrier_counts["settime_bloodmoon"] < SETTIME_BLOODMOON_MAX_FIRES
                        and barrier_counts["settime_bloodmoon"]
                        < barrier_seen["settime_bloodmoon"]
                    ):
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if not tn.connect():
                            warn("settime_bloodmoon: telnet connect fail; retry next poll")
                            break
                        # Day1 22:00 only (not day-7 BM horde).
                        r = tn.exec("settime 22000")
                        log(f"telnet settime 22000 → {r[:120]!r}")
                        tn.close()
                        barrier_counts["settime_bloodmoon"] += 1
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

                    while barrier_counts["settime_day"] < barrier_seen["settime_day"]:
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if not tn.connect():
                            warn("settime_day: telnet connect fail; retry next poll")
                            break
                        # Morning restore; always last after any night set in this poll.
                        r = tn.exec("settime 8000")
                        log(f"telnet settime 8000 (day) → {r[:120]!r}")
                        # Clear AI again after night so leftovers do not down the player.
                        tn.clear_ai()
                        tn.close()
                        barrier_counts["settime_day"] += 1

                    while barrier_counts["spawn_vehicle"] < barrier_seen["spawn_vehicle"]:
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if not tn.connect():
                            warn("spawn_vehicle: telnet connect fail; retry next poll")
                            break
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
                        tn.close()
                        barrier_counts["spawn_vehicle"] += 1

                    while barrier_counts["spawn_trader"] < barrier_seen["spawn_trader"]:
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if not tn.connect():
                            warn("spawn_trader: telnet connect fail; retry next poll")
                            break
                        n = tn.spawn_near_players("npcTraderJoel")
                        if n == 0:
                            n = tn.spawn_near_players("npcTraderBob")
                        log(f"telnet spawn trader near players units~={n}")
                        tn.close()
                        barrier_counts["spawn_trader"] += 1

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
                        # Prior instance already exited: release its log handle
                        # before rebinding instead of waiting on GC.
                        fh = getattr(loadgen_proc, "_log_fh", None)
                        try:
                            if fh is not None:
                                fh.close()
                        except OSError:
                            pass
                        loadgen_proc = None
                    loadgen_proc = start_loadgen(
                        game_port=args.port,
                        count=1,
                        timeout_ms=120_000,
                        log_path=args.logdir / "loadgen_peer.log",
                    )
                    if loadgen_proc is None:
                        warn("loadgen peer start failed; will retry next poll")
                        break
                    barrier_counts["spawn_loadgen_peer"] += 1

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
                    token = full.split(":", 1)[-1].strip()
                    if not token:
                        continue
                    if token in chat_tokens_fired:
                        continue
                    tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                    if not tn.connect():
                        warn(f"chat_echo:{token} telnet connect fail; retry")
                        continue
                    for cmd in (f"say {token}", f'say "{token}"'):
                        r = tn.exec(cmd)
                        log(f"telnet {cmd} → {r[:100]!r}")
                    tn.close()
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
                    if cls:
                        vehicle_seen[cls] = vehicle_seen.get(cls, 0) + 1
                for cls, seen in vehicle_seen.items():
                    while vehicle_spawns_fired.get(cls, 0) < seen:
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if tn.connect():
                            n = tn.spawn_near_players(cls)
                            if n == 0:
                                r = tn.exec(f"spawnentityat {cls} {PERSIST_PAD_COORDS}")
                                log(f"telnet vehicle spawnentityat {cls} → {r[:80]!r}")
                            else:
                                log(f"telnet spawn vehicle {cls} near players units~={n}")
                            tn.close()
                        else:
                            warn(f"spawn_vehicle:{cls} telnet connect fail; retry")
                            break
                        vehicle_spawns_fired[cls] = vehicle_spawns_fired.get(cls, 0) + 1

                while (
                    barrier_counts["teleport_persist_pad"]
                    < barrier_seen["teleport_persist_pad"]
                ):
                    tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                    n = 0
                    if tn.connect():
                        n = tn.teleport_players_to(*PERSIST_PAD_XYZ)
                        if n == 0:
                            warn("teleport_persist_pad: no player ids yet; retry")
                            tn.close()
                            break
                        tn.close()
                    else:
                        warn("teleport_persist_pad: telnet connect fail; retry")
                        break
                    barrier_counts["teleport_persist_pad"] += 1

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
                tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                moved = 0
                if tn.connect():
                    moved = tn.teleport_players_to(*args.peer_client_teleport)
                    tn.close()
                if moved >= 2:
                    peer_teleport_done = True
                    x, y, z = args.peer_client_teleport
                    log(f"stock peer teleport complete players={moved} → {x:g} {y:g} {z:g}")
                else:
                    warn("stock peer teleport: waiting for both joined players")

            if client_proc is not None and client_proc.poll() is not None:
                time.sleep(2)
                pump_log_tail(client_tail, client_scan)
                parsed = client_scan.result()
                peer_parsed = read_peer_results()
                if parsed.get("done") is not None and peer_suite_done():
                    break
            time.sleep(0.5)
        else:
            log(f"timeout after {time.monotonic() - t0:.0f}s waiting for DONE")

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
                try:
                    ms = float(ev.get("ms") or 0)
                except (TypeError, ValueError):
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
            "fixtures": {
                "zombie_spawn_attempted": barrier_counts.get("spawn_zombie", 0) > 0,
                "kill_fixture_attempted": barrier_counts.get("kill_fixture_zombie", 0) > 0,
                "barrier_counts": dict(barrier_counts),
                "fresh_save": bool(args.fresh_save),
            },
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
            for r in results:
                log(f"  {r['status']} {r['case']} {r.get('detail', '')}")
            for r in peer_results:
                log(f"  peer {r['status']} {r['case']} {r.get('detail', '')}")
            if args.client_log.is_file():
                cl = args.client_log.read_text(encoding="utf-8", errors="replace")
                for key in (
                    "7dtd-playtest",
                    "7dtd-connect",
                    "InitMod",
                    "Connect",
                    "ERROR",
                    "Exception",
                ):
                    hits = [ln for ln in cl.splitlines() if key in ln]
                    if hits:
                        err(f"client log '{key}' ({len(hits)}): {hits[-3:]}")
            exit_code = 2
        else:
            if summary:
                log(
                    f"SUMMARY pass={summary['pass']} fail={summary['fail']} "
                    f"skip={summary.get('skip', 0)} wall_s={wall_s:.1f}"
                )
            for r in results:
                log(f"  {r['status']} {r['case']} {r.get('detail', '')}")
            if peer_client_suite and peer_summary:
                log(
                    f"PEER SUMMARY pass={peer_summary['pass']} fail={peer_summary['fail']} "
                    f"skip={peer_summary.get('skip', 0)}"
                )
            for r in peer_results:
                log(f"  peer {r['status']} {r['case']} {r.get('detail', '')}")
            if slowest:
                log("slowest: " + ", ".join(f"{c}={ms:.0f}ms" for c, ms in slowest[:5]))
            if nre or peer_nre:
                log(
                    "warn: "
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
            if fails is None and done.get("exit_hint") is not None:
                fails = int(done["exit_hint"])
            if fails is None:
                fails = 1
            if peer_summary:
                fails += int(peer_summary["fail"])
            exit_code = 1 if fails > 0 else 0

        log(f"exit={exit_code}")
        return exit_code
    finally:
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
            # Soft clean after: leave Steam alone
            pkill_patterns(
                [
                    r"7DaysToDieServer\.x86_64",
                    r"[/]7DaysToDie\.exe",
                    r"wine64-preloader.*7DaysToDie",
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
