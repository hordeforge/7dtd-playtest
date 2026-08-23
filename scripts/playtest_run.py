#!/usr/bin/env python3
"""Host orchestrator: stock dedicated (default) or zdtd + stock client playtest.

Exit codes:
  0  all cases pass (DONE with fail=0)
  1  playtest assertion failures
  2  harness error (no DONE, server/client fail, timeout, lock refused,
     ports still bound after clean)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_lock
from playtest_telnet import TelnetAdmin

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
def client_log_for_compat(compat: Path) -> Path:
    """Stock launch_client.sh's game log location for one Proton profile."""
    return (
        compat
        / "pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/logs"
        / "output_log_client_zdtd_connect.txt"
    )


CLIENT_LOG = client_log_for_compat(
    Path.home() / f".local/share/Steam/steamapps/compatdata/{STEAM_APPID}"
)

RESULT_RE = re.compile(r"\[7dtd-playtest\]\s+(PASS|FAIL|SKIP)\s+(\S+)\s*(.*)$")
SUMMARY_RE = re.compile(
    r"\[7dtd-playtest\]\s+SUMMARY\s+pass=(\d+)\s+fail=(\d+)(?:\s+skip=(\d+))?"
)
DONE_RE = re.compile(r"\[7dtd-playtest\]\s+DONE(?:\s+exit_hint=(\d+))?")
JSON_RE = re.compile(r"\[7dtd-playtest\]\s+(\{.*\})\s*$")
NRE_RE = re.compile(r"NullReferenceException|NCSimple|underrun|IndexOutOfRange", re.IGNORECASE)

# pkill -f patterns for clean/teardown of one exclusive run. The [/] in the
# client exe pattern keeps pkill from matching this orchestrator's own cmdline.
CLIENT_KILL_PATTERNS = [
    r"[/]7DaysToDie\.exe",
    r"wine64-preloader.*7DaysToDie",
    r"proton.*7DaysToDie",
]
DEDI_KILL_PATTERN = r"7DaysToDieServer\.x86_64"
ZDTD_KILL_PATTERN = r"zig-out/bin/zdtd"
LOADGEN_KILL_PATTERN = r"7dtd-loadgen"

# World coords of the persist dig pad; rejoin/persist flows teleport players
# here so saved position is deterministic (pos_survives_rejoin asserts on it).
PERSIST_PAD_POS = (520.0, 62.0, 950.0)


def log(msg: str) -> None:
    print(f"[playtest-orch] {msg}", flush=True)


def barrier_hits(blob: str, name: str) -> int:
    # Count only the human log line. Report.Barrier also emits JSON with
    # the same name; summing both double-fires handlers (e.g. kills bots).
    return blob.count(f"barrier {name}")


def barrier_hits_prefix(blob: str, prefix: str) -> list[str]:
    """Return full barrier names that start with prefix (e.g. chat_echo:token)."""
    found: list[str] = []
    # Human line only (same double-count reason as barrier_hits).
    for m in re.finditer(
        rf"\[7dtd-playtest\]\s+barrier\s+({re.escape(prefix)}[^\s\"]*)",
        blob,
    ):
        found.append(m.group(1))
    # Dedup while preserving order (same token can appear many times).
    return list(dict.fromkeys(found))


class BarrierLedger:
    """Barrier emissions seen vs host actions fired, accumulated across polls.

    The client emits ``barrier <name>`` lines; each emission must trigger its
    host action exactly once even when emissions land in different poll
    chunks. Comparing a cumulative action counter against one chunk's hit
    count drops every edge after the first chunk's quota, so emissions are
    folded into a run-total here and actions fire until they catch up.
    A failed telnet attempt leaves fired behind seen and retries on the
    next poll instead of silently consuming the edge.
    """

    def __init__(self) -> None:
        self.seen: dict[str, int] = {}
        self.fired: dict[str, int] = {}

    def observe(self, blob: str, name: str) -> int:
        """Fold this chunk's hits for name into the run total; returns total."""
        self.seen[name] = self.seen.get(name, 0) + barrier_hits(blob, name)
        return self.seen[name]

    def pending(self, name: str, cap: int | None = None) -> bool:
        """True while fired lags seen for name (optionally capped)."""
        target = self.seen.get(name, 0)
        if cap is not None:
            target = min(target, cap)
        return self.fired.get(name, 0) < target

    def mark_fired(self, name: str) -> None:
        """Record one completed host action for name."""
        self.fired[name] = self.fired.get(name, 0) + 1


def fire_barrier_edges(
    led: BarrierLedger,
    chunk: str,
    name: str,
    open_admin: Callable[[], TelnetAdmin | None],
    act: Callable[[TelnetAdmin], bool | None],
    cap: int | None = None,
) -> None:
    """Fold this chunk's emissions of barrier ``name`` into the ledger, then
    run one connected-telnet action per pending edge until none remain.

    ``act(telnet)`` may return False to leave its edge pending for the next
    poll (e.g. no player ids yet); any other result consumes the edge. A
    failed telnet connect also leaves the edge pending.
    """
    led.observe(chunk, name)
    while led.pending(name, cap=cap):
        tn = open_admin()
        if tn is None:
            log(f"warn: {name}: telnet connect fail; retry next poll")
            return
        applied = act(tn)
        tn.close()
        if applied is False:
            log(f"warn: {name}: not applied yet; retry next poll")
            return
        led.mark_fired(name)


def pkill_patterns(patterns: list[str], sig: str = "-9") -> None:
    for pat in patterns:
        subprocess.run(
            ["pkill", sig, "-f", pat],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def spawn_logged(
    cmd: list[str],
    log_path: Path,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start a detached process with stdout+stderr captured into log_path.

    The log handle rides on the Popen as _log_fh so stop_proc can close it.
    On Popen failure the handle is closed before raising (no fd leak).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — handle rides the Popen as _log_fh; stop_proc closes it
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            cwd=str(cwd) if cwd is not None else None,
        )
    except BaseException:
        fh.close()
        raise
    proc._log_fh = fh  # type: ignore[attr-defined]
    return proc


def clean_processes(*, kill_wine: bool = False, include_server: bool = True) -> None:
    """Stop prior servers/clients. Avoid killing whole wineserver by default
    (that drops Steam); only kill game + dedicated + optional zdtd.
    include_server=False (--no-server) spares the already-running dedicated/zdtd
    the run was told to attach to."""
    log(
        "cleaning prior dedicated / client / zdtd"
        if include_server
        else "cleaning prior client only (sparing server: --no-server)"
    )
    patterns = list(CLIENT_KILL_PATTERNS)
    if include_server:
        patterns = [
            DEDI_KILL_PATTERN,
            r"7DaysToDieServe",  # truncated comm
            ZDTD_KILL_PATTERN,
        ] + patterns
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


def wait_file_contains(path: Path, needle: str, timeout: float) -> bool:
    """Poll an appending log for needle without re-reading the whole file.

    Reads only bytes appended since the last poll (plus a small overlap so a
    match split across polls is still seen). Truncation restarts at offset 0.
    """
    deadline = time.time() + timeout
    offset = 0
    overlap = ""
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            size = -1
        if 0 <= size < offset:  # truncated / rotated
            offset = 0
            overlap = ""
        if size > offset:
            try:
                with path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                offset += len(chunk)
                text = overlap + chunk.decode("utf-8", errors="replace")
                if needle in text:
                    return True
                overlap = text[-len(needle):] if needle else ""
            except OSError:
                pass
        time.sleep(0.5)
    return False


class LogTail:
    """Incremental reader for a growing log file.

    read_new() returns only complete lines appended since the previous call,
    so poll loops stop re-reading and re-scanning the whole file each tick.
    A partial trailing line is held back until its newline arrives, so no
    barrier/result pattern can be missed or split. Truncation (fresh run
    truncating the same path) restarts from zero.
    """

    def __init__(self, path: Path):
        self.path = path
        self._offset = 0
        self._pending = b""

    def read_new(self) -> str:
        try:
            size = self.path.stat().st_size
        except OSError:
            return ""
        if size < self._offset:  # truncated / rotated
            self._offset = 0
            self._pending = b""
        if size == self._offset:
            return ""
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(size - self._offset)
        except OSError:
            return ""
        self._offset += len(chunk)
        data = self._pending + chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            # No complete line yet; keep buffering until one lands.
            self._pending = data
            return ""
        self._pending = data[cut + 1:]
        return data[: cut + 1].decode("utf-8", errors="replace")

    def read_all(self) -> str:
        try:
            return self.path.read_text(errors="replace")
        except OSError:
            return ""


def merge_parsed(acc: dict | None, inc: dict) -> dict:
    """Fold an incremental parse_client_log result into a running accumulator.

    Stream preference follows parse_client_log's whole-blob rule: once any
    valid JSON result event is seen, JSON becomes the authoritative result
    stream for the run and human PASS/FAIL/SKIP lines (the mod emits both
    shapes back to back) are echoes, so they are dropped even if an earlier
    chunk recorded them.
    """
    if acc is None:
        acc = {"stream": "human", "results": []}
    if inc.get("summary") is not None:
        acc["summary"] = inc["summary"]
    if inc.get("done") is not None:
        acc["done"] = inc["done"]
    if inc.get("json_events"):
        acc["json_events"] = (acc.get("json_events") or []) + inc["json_events"]
    if inc.get("nre_like"):
        merged = (acc.get("nre_like") or []) + inc["nre_like"]
        acc["nre_like"] = merged[:50]
    stream = inc.get("stream", "human")
    results = inc.get("results") or []
    if stream == "json":
        if acc.get("stream") != "json":
            # First JSON event wins retroactively, matching one-shot parsing.
            acc["stream"] = "json"
            acc["results"] = []
        acc["results"] = (acc.get("results") or []) + results
    elif acc.get("stream") != "json":
        acc["results"] = (acc.get("results") or []) + results
    return acc


def write_stock_config(
    src_cfg: Path,
    out_cfg: Path,
    userdata: Path,
    *,
    world_name: str,
    game_name: str,
    port: int,
    telnet_port: int,
    max_players: int = 8,
    telnet_password: str = "retest",
) -> None:
    text = src_cfg.read_text(encoding="utf-8")
    # Operator-supplied strings land inside double-quoted XML attributes;
    # escape them so quotes/&/< cannot break out or corrupt the serverconfig.
    ud = xml_attr_escape(str(userdata.resolve()))
    if 'name="UserDataFolder"' not in text:
        text = text.replace(
            "<ServerSettings>",
            f'<ServerSettings>\n\t<property name="UserDataFolder" value="{ud}"/>',
        )
    else:
        text = re.sub(
            r'name="UserDataFolder"\s*value="[^"]*"',
            f'name="UserDataFolder" value="{ud}"',
            text,
        )
    repls = {
        "GameWorld": xml_attr_escape(world_name),
        "GameName": xml_attr_escape(game_name),
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
        "TelnetPassword": xml_attr_escape(telnet_password),
        "BuildCreate": "true",
        "ServerPassword": "",
        "PlayerKillingMode": "0",
    }
    for k, v in repls.items():
        text = re.sub(
            rf'name="{k}"\s*value="[^"]*"',
            f'name="{k}" value="{v}"',
            text,
        )
    out_cfg.parent.mkdir(parents=True, exist_ok=True)
    out_cfg.write_text(text, encoding="utf-8")


def start_stock_dedicated(
    game_srv: Path,
    userdata: Path,
    server_log: Path,
    *,
    world_name: str,
    game_name: str,
    port: int,
    telnet_port: int,
    telnet_password: str = "retest",
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
            subprocess.run(["rm", "-rf", str(dest)], check=False)
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
    proc = spawn_logged(cmd, server_log, cwd=game_srv)
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
    return spawn_logged(cmd, server_log)


# Backend readiness marker and wait budget (stock world load is slow cold).
SERVER_READY_SPECS = {
    "stock": ("StartGame done", 600.0),
    "zdtd": ("tick=20Hz", 60.0),
}


def start_configured_server(
    args: argparse.Namespace, server_log: Path
) -> tuple[subprocess.Popen, Path | None]:
    """Start the selected backend. Returns (proc, unity_log or None)."""
    if args.server == "stock":
        return start_stock_dedicated(
            args.game_srv,
            args.userdata,
            server_log,
            world_name=args.world_name,
            game_name=args.game_name,
            port=args.port,
            telnet_port=args.admin_port,
            telnet_password=args.telnet_password,
        )
    return (
        start_zdtd(
            args.zdtd,
            args.world,
            args.port,
            args.admin_port,
            args.game_srv,
            server_log,
        ),
        None,
    )


def wait_server_ready(
    args: argparse.Namespace,
    proc: subprocess.Popen,
    server_log: Path,
    unity_log: Path | None,
    *,
    context: str = "",
) -> tuple[bool, bool]:
    """Wait for the backend readiness marker.

    Stock logs its marker to the Unity log; zdtd to the orchestrator-captured
    stdout. Returns (marker_seen, exited_early). Logs the tail when the
    process died.
    """
    ready_log = unity_log if args.server == "stock" else server_log
    marker, timeout = SERVER_READY_SPECS[args.server]
    if wait_file_contains(ready_log, marker, timeout=timeout):
        log(f"{args.server} ready{context} ({marker})")
        return True, False
    if proc.poll() is not None:
        log(f"{args.server} exited early code={proc.returncode}{context}")
        if ready_log.is_file():
            log(
                "tail server log:\n"
                + "\n".join(
                    ready_log.read_text(errors="replace").splitlines()[-40:]
                )
            )
        return False, True
    log(f"warn: no {marker}{context}; server still running, proceeding")
    return False, False


def client_mute_enabled() -> bool:
    """Default on: mute client audio for automated runs (opt-out CLIENT_MUTE=0)."""
    raw = (
        os.environ.get("CLIENT_MUTE")
        or os.environ.get("PLAYTEST_MUTE")
        or os.environ.get("SEVEN_DAYS_TO_DIE_CLIENT_MUTE")
        or "1"
    )
    return raw.strip().lower() not in ("0", "false", "no", "off")


def mute_client_audio_async() -> subprocess.Popen | None:
    """Poll PipeWire/Pulse for 7DaysToDie sink-input and mute it (default on).

    Prefer 7dtd-connect's mute_client_audio.sh (same helper launch_client uses).
    Best-effort: missing pactl/jq only logs a warning inside the helper.
    Returns the helper process so stop_proc can reap/kill it at teardown;
    without that wait path each launch leaves an unreaped child behind.
    """
    if not client_mute_enabled():
        log("client mute: off (CLIENT_MUTE=0)")
        return None
    helper = CONNECT / "scripts" / "mute_client_audio.sh"
    wait_s = os.environ.get(
        "CLIENT_MUTE_TIMEOUT",
        os.environ.get("SEVEN_DAYS_TO_DIE_CLIENT_MUTE_TIMEOUT", "60"),
    )
    if not helper.is_file():
        log(f"client mute: helper missing ({helper}); skip")
        return None
    log(f"client mute: on (opt-out CLIENT_MUTE=0); polling up to {wait_s}s")
    try:
        return subprocess.Popen(
            ["bash", str(helper), str(wait_s)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as ex:
        log(f"client mute: failed to start helper: {ex}")
        return None


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
    env["ZDTD_CONNECT"] = f"127.0.0.1:{port}"
    if run_suite:
        env["PLAYTEST_SUITE"] = suite
        env["PLAYTEST"] = "1"
        if "PLAYTEST_LAPS" in os.environ:
            env["PLAYTEST_LAPS"] = os.environ["PLAYTEST_LAPS"]
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
    role = "scenario" if run_suite else "stock-peer"
    log(f"start client role={role} suite={suite or '(none)'} connect={env['ZDTD_CONNECT']}")
    proc = spawn_logged(["bash", str(launch)], client_launch_log, cwd=CONNECT, env=env)
    # Belt-and-suspenders: connect mutes itself; also start poll from orch.
    # The helper rides the client proc so teardown reaps it (no stray child
    # outliving the run outside the pkill patterns).
    proc._mute_helper = mute_client_audio_async()  # type: ignore[attr-defined]
    return proc


# Bound for the synchronous loadgen build below. It runs while this
# orchestrator holds the exclusivity lock with a live heartbeat thread:
# an unbounded build would wedge one poll iteration forever, keeping the
# run past its own deadline and the lock fresh past stale takeover.
LOADGEN_BUILD_TIMEOUT_SEC = 600.0


def ensure_loadgen_built() -> Path | None:
    exe = LOADGEN / "src" / "LoadGen" / "bin" / "Release" / "net8.0" / "7dtd-loadgen"
    if exe.is_file():
        return exe
    proj = LOADGEN / "src" / "LoadGen" / "LoadGen.csproj"
    if not proj.is_file():
        log(f"loadgen project missing: {proj}")
        return None
    log("building loadgen…")
    try:
        r = subprocess.run(
            ["dotnet", "build", str(proj), "-c", "Release", "-v", "q"],
            cwd=str(LOADGEN),
            capture_output=True,
            text=True,
            check=False,
            timeout=LOADGEN_BUILD_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as ex:
        # subprocess.run killed + reaped the timed-out child.
        tail = ex.stderr if isinstance(ex.stderr, str) else ""
        log(f"loadgen build timeout after {LOADGEN_BUILD_TIMEOUT_SEC:.0f}s {tail[-400:]}")
        return None
    except OSError as ex:
        log(f"loadgen build failed to start: {ex}")
        return None
    if r.returncode != 0:
        log(f"loadgen build failed: {r.stderr[-400:]}")
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
    return spawn_logged(cmd, log_path, cwd=LOADGEN)


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
        log(f"apm dump: missing zdtd {zdtd}")
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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as ex:
        log(f"apm dump fail: {ex}")
        return False
    out = (r.stdout or "") + (r.stderr or "")
    # Fail closed: do not invent markers. Client would soft-pass a synthetic dump.
    has_marker = "zdtd-apm" in out or "wall_ns" in out or "tick_total" in out
    if not has_marker or not out.strip():
        log(f"apm dump: no live markers in output (len={len(out)} rc={r.returncode})")
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


def stop_proc(proc: subprocess.Popen | None, *, term_timeout: float = 8.0) -> None:
    """Stop one run-owned process group, reap it, and close its log handle.

    Also stops any helper riding the proc (client mute poller) so no child
    of this orchestrator is left unreaped or running past teardown.
    """
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001, S110 — best-effort SIGTERM fallback
            pass
    try:
        proc.wait(timeout=term_timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001 — fall through to per-process kill
            try:
                proc.kill()
            except Exception:  # noqa: BLE001, S110 — already SIGKILLed the group
                pass
        # Reap after SIGKILL: an unreaped child stays a zombie until orchestrator
        # exit, and the rejoin flow must see its predecessor fully dead before
        # starting a new server on the same ports.
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001, S110 — reap is best-effort after kill
            pass
    fh = getattr(proc, "_log_fh", None)
    if fh:
        try:
            fh.close()
        except Exception:  # noqa: BLE001, S110 — log fd close is best-effort
            pass
    helper = getattr(proc, "_mute_helper", None)
    if helper is not None:
        # Already-exited helpers reap here; wedged ones are killed. Either way
        # the orchestrator leaves no zombie and no live stray.
        stop_proc(helper, term_timeout=term_timeout)


def parse_client_log(text: str) -> dict:
    """Parse playtest log. Prefer JSON events when present (avoid double human+JSON)."""
    human_results: list[dict] = []
    json_results: list[dict] = []
    summary = None
    done = None
    json_events: list[dict] = []
    json_summary = None
    json_done = None
    human_summary = None
    human_done = None
    # Single pass: NRE-like lines ride the same iteration as result parsing.
    nre_hits: list[str] = []

    for line in text.splitlines():
        if len(nre_hits) < 50 and NRE_RE.search(line):
            nre_hits.append(line)
        m = JSON_RE.search(line)
        if m:
            try:
                ev = json.loads(m.group(1))
                json_events.append(ev)
                if ev.get("t") == "result":
                    json_results.append(
                        {
                            "status": ev.get("status", "").upper(),
                            "case": f"{ev.get('suite', '')}/{ev.get('case', '')}",
                            "detail": ev.get("detail", ""),
                        }
                    )
                elif ev.get("t") == "summary":
                    json_summary = {
                        "pass": int(ev.get("pass", 0)),
                        "fail": int(ev.get("fail", 0)),
                        "skip": int(ev.get("skip", 0)),
                    }
                elif ev.get("t") == "done":
                    json_done = {"exit_hint": int(ev.get("exit_hint", 1))}
            except json.JSONDecodeError:
                pass
            continue

        m = RESULT_RE.search(line)
        if m:
            human_results.append(
                {
                    "status": m.group(1),
                    "case": m.group(2),
                    "detail": (m.group(3) or "").strip(),
                }
            )
            continue
        m = SUMMARY_RE.search(line)
        if m:
            human_summary = {
                "pass": int(m.group(1)),
                "fail": int(m.group(2)),
                "skip": int(m.group(3) or 0),
            }
            continue
        m = DONE_RE.search(line)
        if m:
            hint = int(m.group(1)) if m.group(1) is not None else None
            human_done = {"exit_hint": hint}

    if json_results:
        results = json_results
        summary = json_summary or human_summary
        done = json_done or human_done
        stream = "json"
    else:
        results = human_results
        summary = human_summary
        done = human_done
        stream = "human"

    if summary is None and results:
        summary = {
            "pass": sum(1 for r in results if r["status"] == "PASS"),
            "fail": sum(1 for r in results if r["status"] == "FAIL"),
            "skip": sum(1 for r in results if r["status"] == "SKIP"),
        }

    return {
        "results": results,
        "summary": summary,
        "done": done,
        "json_events": json_events,
        "nre_like": nre_hits,
        "stream": stream,
    }


def write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log(f"report → {path}")


def xml_attr_escape(value: str) -> str:
    """Escape a string for use inside a double-quoted XML attribute."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_junit(
    path: Path, suite: str, results: list[dict], summary: dict | None
) -> None:
    """Minimal JUnit XML for CI UIs."""
    tests = len(results)
    failures = sum(1 for r in results if r.get("status") == "FAIL")
    skipped = sum(1 for r in results if r.get("status") == "SKIP")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<testsuite name="7dtd-playtest.{xml_attr_escape(suite)}" tests="{tests}" '
            f'failures="{failures}" skipped="{skipped}">'
        ),
    ]
    for r in results:
        case = xml_attr_escape(r.get("case", "unknown"))
        status = r.get("status", "FAIL")
        detail = xml_attr_escape(r.get("detail") or "")
        lines.append(f'  <testcase classname="playtest" name="{case}">')
        if status == "FAIL":
            lines.append(f'    <failure message="{detail}"/>')
        elif status == "SKIP":
            lines.append(f'    <skipped message="{detail}"/>')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"junit → {path}")


def fresh_save(userdata: Path, game_name: str) -> None:
    """Remove stock save folder for a clean, reproducible world state."""
    # Typical layout: UserData/Saves/<World>/<GameName>
    saves = userdata / "Saves"
    if not saves.is_dir():
        return
    removed = 0
    for world_dir in saves.iterdir():
        if not world_dir.is_dir():
            continue
        target = world_dir / game_name
        if target.is_dir():
            subprocess.run(["rm", "-rf", str(target)], check=False)
            removed += 1
            log(f"fresh-save removed {target}")
    if removed == 0:
        log(f"fresh-save: no existing save named {game_name}")


# Suite ids / aliases whose built-in cases emit host-fixture barriers in
# Catalog.cs (spawn_zombie, kill_fixture_zombie, kill_player, settime_*,
# spawn_trader, spawn_vehicle). Must stay a superset of every suite id whose
# cases emit those barriers: a missing entry silently disables the admin
# fixture plane when that suite runs standalone (client-side spawn fallbacks
# are not server-authoritative on a dedicated server). The structural drift
# test derives the expected set from Catalog.cs (test_catalog_surface.py).
FIXTURE_SUITE_KEYS = (
    "demo",
    "combat",
    "economy",
    "vehicle",
    "finale",
    "full",
    "all",
    "live",
    "benchmark",
    "bench",
    "mp",
    "residual",
)


def suite_tokens(raw: str) -> set[str]:
    """Split a PLAYTEST-suite argument into its lowercase suite ids/aliases.

    The client accepts ',', ';', and whitespace separators (Catalog.ExpandSuites),
    so the host must tokenize the same way. Exact tokens, not substrings: a
    provider suite named "temp_check" must not trip the "mp" key, and
    "gateway" must not inherit "gate"'s exclusions.
    """
    return {p.lower() for p in re.split(r"[,;\s]+", raw or "") if p}


def suite_wants_zombie_fixture(suite: str) -> bool:
    tokens = suite_tokens(suite)
    wants = bool(tokens & set(FIXTURE_SUITE_KEYS))
    excluded = bool(tokens & {"demo_min", "gate"})
    smoke_only = tokens == {"smoke"}
    return wants and not excluded and not smoke_only


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="stock-client playtest orchestrator")
    ap.add_argument(
        "--server",
        choices=("stock", "zdtd"),
        default=os.environ.get("PLAYTEST_SERVER", "stock"),
        help="server backend (default: stock dedicated)",
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
        type=int,
        default=None,
        help="ServerPort / connect-to-IP port (stock default 26900, zdtd 27025)",
    )
    ap.add_argument("--admin-port", type=int, default=8081, help="telnet/admin port")
    ap.add_argument(
        "--zdtd", type=Path, default=Path(os.environ.get("ZDTD", str(DEFAULT_ZDTD)))
    )
    ap.add_argument("--game-srv", type=Path, default=DEFAULT_GAME_SRV)
    ap.add_argument(
        "--userdata",
        type=Path,
        default=Path(os.environ.get("RE_DEDICATED_USERDATA", str(DEFAULT_USERDATA))),
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("PLAYTEST_TIMEOUT_SEC", "900")),
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
    )
    ap.add_argument("--skip-clean", action="store_true")
    ap.add_argument(
        "--kill-wine", action="store_true", help="also kill wineserver (disrupts Steam)"
    )
    ap.add_argument(
        "--no-server", action="store_true", help="use already-running server"
    )
    ap.add_argument("--client-log", type=Path, default=CLIENT_LOG)
    ap.add_argument(
        "--peer-client-name",
        default=os.environ.get("PLAYTEST_PEER_CLIENT_NAME", ""),
        help="optional distinct Local-platform name for one passive stock peer",
    )
    ap.add_argument(
        "--peer-client-compat",
        type=Path,
        default=(
            Path(os.environ["PLAYTEST_PEER_CLIENT_COMPAT"])
            if os.environ.get("PLAYTEST_PEER_CLIENT_COMPAT")
            else None
        ),
        help="separate initialized Proton compat profile for --peer-client-name",
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
        help="optional suite for the stock peer; empty leaves it passive",
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
        help="stock dedicated telnet password",
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

    report_path = args.logdir / f"report-{int(time.time())}.json"
    server_log = args.logdir / "server-orch.log"
    client_launch_log = args.logdir / "client-launch.log"
    peer_client_launch_log = args.logdir / "peer-client-launch.log"
    peer_client_log = (
        args.peer_client_log
        or (client_log_for_compat(args.peer_client_compat) if peer_client_name else None)
    )

    if args.server == "zdtd" and not args.no_server and not args.zdtd.is_file():
        log(f"missing zdtd binary: {args.zdtd}")
        return 2
    if (
        args.server == "stock"
        and not args.no_server
        and not (args.game_srv / "7DaysToDieServer.x86_64").is_file()
    ):
        log(f"missing stock dedicated under {args.game_srv}")
        return 2
    if not (CONNECT / "scripts" / "launch_client.sh").is_file():
        log(f"missing connect launcher under {CONNECT}")
        return 2
    if peer_client_name and not args.peer_client_compat.is_dir():
        log(f"peer compat profile is missing or not a directory: {args.peer_client_compat}")
        return 2

    # Pre-bound so the finally block can stop whatever this run launched even
    # on early exceptions (lock refused paths return before the try).
    server_proc: subprocess.Popen | None = None
    client_proc: subprocess.Popen | None = None
    peer_client_proc: subprocess.Popen | None = None
    loadgen_proc: subprocess.Popen | None = None
    unity_log: Path | None = None
    lock_session = (args.session or "").strip() or playtest_lock.new_session_id("playtest")
    lock_path = playtest_lock.default_lock_path()
    lock_held = False
    lock_heartbeat: playtest_lock.HeartbeatThread | None = None

    try:
        t0 = time.time()
        # Exclusive live-client lock BEFORE clean_processes / launch so a second
        # orchestrator cannot wipe another agent's client. See AGENTS.md.
        try:
            playtest_lock.acquire(lock_session, path=lock_path)
        except playtest_lock.PlaytestLockError as ex:
            holder = ex.held_by or "unknown"
            log(
                f"refusing start: {ex} "
                f"(held_by={holder} reason={ex.reason} file={lock_path})"
            )
            log(
                "see AGENTS.md — Playtest / live-client exclusivity; "
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
            on_error=lambda ex: log(f"warn: lock heartbeat: {ex}"),
        )
        lock_heartbeat.start()

        if not args.skip_clean:
            clean_processes(kill_wine=args.kill_wine, include_server=not args.no_server)

        # After clean, refuse to double-bind if something else still owns ports
        # (orphan outside our pkill patterns, or race with another host).
        if not args.no_server:
            busy = [
                p
                for p in (args.port, args.admin_port)
                if playtest_lock.tcp_port_in_use(p)
            ]
            if busy:
                log(
                    f"refusing start: TCP port(s) still in use after clean: {busy} "
                    f"(another dedicated/zdtd or leftover process). "
                    f"Stop it or pick different --port/--admin-port."
                )
                return 2

        if args.fresh_save:
            if args.server == "stock":
                fresh_save(args.userdata, args.game_name)
            elif args.server == "zdtd" and args.world is not None:
                # zdtd persists players.zsv / chunks under --world; wipe for clean bag.
                wpath = Path(args.world)
                if wpath.is_dir():
                    for name in ("players.zsv", "containers.zct", "blockmeta.zbm"):
                        p = wpath / name
                        if p.is_file():
                            try:
                                p.unlink()
                                log(f"fresh-save removed {p}")
                            except OSError as ex:
                                log(f"fresh-save warn {p}: {ex}")
                    # Drop chunk overlays so dig/place start from map baseline.
                    for ch in wpath.glob("c_*.zch*"):
                        try:
                            ch.unlink()
                        except OSError:
                            pass
                    log(f"fresh-save zdtd world cleaned under {wpath}")

        if args.client_log.is_file():
            try:
                args.client_log.write_text("", encoding="utf-8")
            except OSError as ex:
                log(f"warn: could not truncate client log: {ex}")
        if peer_client_log is not None:
            try:
                peer_client_log.parent.mkdir(parents=True, exist_ok=True)
                peer_client_log.write_text("", encoding="utf-8")
            except OSError as ex:
                log(f"warn: could not truncate peer client log: {ex}")

        if not args.no_server:
            server_proc, unity_log = start_configured_server(args, server_log)
            _, exited_early = wait_server_ready(args, server_proc, server_log, unity_log)
            if exited_early:
                return 2

        # zdtd admin TCP speaks the same command surface the orch uses for stock
        # telnet (listplayers/listents/kill/spawnentity/settime). Enable fixtures
        # for both backends so kill/spawn barriers actually fire on playtest-zdtd.
        want_fixtures = (
            args.server in ("stock", "zdtd")
            and not args.no_fixtures
            and suite_wants_zombie_fixture(args.suite)
        )
        telnet_host = "127.0.0.1"
        telnet_port = args.admin_port
        telnet_password = args.telnet_password

        deadline = time.time() + args.timeout
        # soak_long needs ≥15 min wall + setup; bump default timeout.
        if "soak_long" in suite_tokens(args.suite):
            deadline = time.time() + max(args.timeout, 1100.0)
            log(f"soak_long timeout deadline wall_s>={int(deadline - time.time())}")
        last_progress = 0.0
        # Barrier ledger: emissions (seen) vs completed host actions (fired),
        # accumulated across polls so edges split across chunks still fire
        # exactly once each. Multi-fire is expected: combat + sleeper +
        # economy may re-spawn/kill.
        led = BarrierLedger()

        def open_admin() -> TelnetAdmin | None:
            tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
            return tn if tn.connect() else None

        def run_barrier(
            name: str,
            act: Callable[[TelnetAdmin], bool | None],
            *,
            chunk: str,
            cap: int | None = None,
        ) -> None:
            fire_barrier_edges(led, chunk, name, open_admin, act, cap=cap)

        ready_seen = False
        peer_ready_seen = not bool(peer_client_suite)
        peer_teleport_done = args.peer_client_teleport is None
        rejoin_teleport_done = args.rejoin_teleport is None
        cleaned_ai = False
        chat_tokens_fired: set[str] = set()
        apm_dump_path = args.logdir / "zdtd_apm_dump.txt"
        apm_run_id = f"apm-{int(time.time())}-{os.getpid()}"
        client_extra_env: dict[str, str] = {}
        if "apm" in suite_tokens(args.suite):
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
                peer_env = {
                    "COMPAT": str(args.peer_client_compat),
                    "ZDTD_PLAYER_NAME": peer_client_name,
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
            if args.client_log.is_file():
                try:
                    args.client_log.write_text("", encoding="utf-8")
                except OSError:
                    pass
            client_proc = start_client(
                args.port,
                rejoin_setup_suite,
                client_launch_log,
                extra_env=client_extra_env,
            )
            setup_deadline = time.time() + min(args.timeout, 300)
            last_setup_progress = 0.0
            setup_tail = LogTail(args.client_log)
            setup_acc: dict | None = None
            while time.time() < setup_deadline:
                chunk = setup_tail.read_new()
                if chunk:
                    now = time.time()
                    if now - last_setup_progress > 8:
                        last_setup_progress = now
                        crumbs = [
                            ln
                            for ln in chunk.splitlines()
                            if "[7dtd-playtest]" in ln or "[zdtd-connect]" in ln
                        ]
                        if crumbs:
                            log(f"setup progress: {crumbs[-1][-160:]}")
                    if not provider_rejoin:
                        # Server-authoritative pad tele so pos_survives_rejoin is real.
                        led.observe(chunk, "teleport_persist_pad")
                        while led.pending("teleport_persist_pad"):
                            tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                            n = 0
                            if tn.connect():
                                n = tn.teleport_players_to(*PERSIST_PAD_POS)
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
                            led.mark_fired("teleport_persist_pad")
                    led.observe(chunk, rejoin_setup_barrier)
                    if led.pending(rejoin_setup_barrier):
                        tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                        if tn.connect():
                            n = 1
                            if not provider_rejoin:
                                # Re-tele pad once more so last write before disconnect is pad pos.
                                n = tn.teleport_players_to(*PERSIST_PAD_POS)
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
                                while led.pending(rejoin_setup_barrier):
                                    led.mark_fired(rejoin_setup_barrier)
                        else:
                            log(f"warn: {rejoin_setup_barrier}: telnet connect fail; retry")
                    setup_acc = merge_parsed(setup_acc, parse_client_log(chunk))
                    if setup_acc.get("done") is not None:
                        setup_parsed = setup_acc
                        log(
                            f"{rejoin_label} setup DONE "
                            f"pass={setup_parsed.get('summary', {}).get('pass')} "
                            f"fail={setup_parsed.get('summary', {}).get('fail')}"
                        )
                        break
                if client_proc is not None and client_proc.poll() is not None:
                    time.sleep(1)
                    chunk = setup_tail.read_new()
                    if chunk:
                        setup_acc = merge_parsed(setup_acc, parse_client_log(chunk))
                    if (setup_acc or {}).get("done") is not None:
                        log(f"{rejoin_label} setup DONE (client exited)")
                        break
                time.sleep(0.5)
            # Require setup DONE before rejoin verify; otherwise fixtures never existed.
            setup_text = ""
            if args.client_log.is_file():
                try:
                    setup_text = args.client_log.read_text(errors="replace")
                except OSError:
                    setup_text = ""
            setup_parsed = parse_client_log(setup_text) if setup_text else {}
            setup_done = setup_parsed.get("done") is not None
            setup_fail = int((setup_parsed.get("summary") or {}).get("fail") or 0)
            setup_pass = int((setup_parsed.get("summary") or {}).get("pass") or 0)
            setup_barrier_seen = led.fired.get(rejoin_setup_barrier, 0) > 0
            if not setup_done or setup_fail > 0 or not setup_barrier_seen:
                log(
                    f"{rejoin_label} setup incomplete done={setup_done} pass={setup_pass} "
                    f"fail={setup_fail} barrier={setup_barrier_seen}; "
                    "aborting rejoin verify"
                )
                stop_proc(client_proc)
                client_proc = None
                if not args.no_server:
                    stop_proc(server_proc)
                    server_proc = None
                abort_patterns = list(CLIENT_KILL_PATTERNS)
                if not args.no_server:
                    # Only the run that owns/started a dedicated may kill it.
                    abort_patterns.insert(0, DEDI_KILL_PATTERN)
                pkill_patterns(
                    abort_patterns,
                    sig="-9",
                )
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
                write_junit(junit_path, args.suite, results, summary)
                log(f"FAIL harness: {rejoin_label} setup incomplete")
                return 2

            tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
            if tn.connect():
                # Persist needs the pad as the last player state; providers retain
                # the position their setup case actually established.
                if not provider_rejoin:
                    tn.teleport_players_to(*PERSIST_PAD_POS)
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
            if args.no_server:
                # External dedicated/zdtd stays up across the rejoin; only the
                # scenario client cycles (kickall above already dropped it).
                log("rejoin: keeping external server alive (--no-server)")
                time.sleep(2)
            else:
                stop_proc(server_proc)
                server_proc = None
                # Prefer graceful exit first; escalate only if still alive.
                pkill_patterns(
                    [DEDI_KILL_PATTERN],
                    sig="-15",
                )
                time.sleep(3)
                pkill_patterns(
                    [
                        DEDI_KILL_PATTERN,
                        ZDTD_KILL_PATTERN,
                    ]
                    + CLIENT_KILL_PATTERNS,
                    sig="-9",
                )
                time.sleep(5)
            if not args.no_server:
                # Restart on same save (no fresh_save).
                server_proc, unity_log = start_configured_server(args, server_log)
                wait_server_ready(
                    args, server_proc, server_log, unity_log,
                    context=" after rejoin restart",
                )
            if args.client_log.is_file():
                try:
                    args.client_log.write_text("", encoding="utf-8")
                except OSError:
                    pass
            client_proc = start_client(
                args.port, args.suite, client_launch_log, extra_env=client_extra_env
            )
            ready_seen = False
            rejoin_teleport_done = args.rejoin_teleport is None
            deadline = time.time() + min(args.timeout, 400)

        # Always defined so timeout / missing client logs cannot UnboundLocalError.
        parsed: dict = {}
        peer_parsed: dict = {}
        primary_done_logged = False

        def read_peer_results() -> dict:
            if peer_client_log is None or not peer_client_log.is_file():
                return {}
            try:
                return parse_client_log(peer_client_log.read_text(errors="replace"))
            except OSError:
                return {}

        def peer_suite_done() -> bool:
            return not peer_client_suite or peer_parsed.get("done") is not None

        # Incremental tails: process only appended complete lines each poll so
        # scan cost tracks new data, not total log size (logs grow to tens of
        # MB over soak runs and this host shares CPU with the game under test).
        client_tail = LogTail(args.client_log)
        peer_tail = LogTail(peer_client_log) if peer_client_log is not None else None

        # Host fixture actions; each runs on one connected telnet per barrier
        # edge (see run_barrier).
        def spawn_zombie(tn: TelnetAdmin) -> None:
            n = tn.spawn_near_players("zombieBoe", per=1)
            if n == 0:
                time.sleep(1.0)
                tn.spawn_near_players("zombieBoe", per=1)

        def kill_player(tn: TelnetAdmin) -> None:
            # Kill the human player entity for death-screen case (not AI).
            out = tn.exec("listplayers")
            pids = [
                int(x)
                for x in re.findall(r"id\s*=\s*(\d+)", out, flags=re.IGNORECASE)
            ]
            for pid in pids[:1]:
                r = tn.exec(f"kill {pid}")
                log(f"telnet kill_player {pid} → {r[:80]!r}")

        def set_bloodmoon_night(tn: TelnetAdmin) -> None:
            # Day1 22:00 only (not day-7 BM horde).
            r = tn.exec("settime 22000")
            log(f"telnet settime 22000 → {r[:120]!r}")

        def restore_day(tn: TelnetAdmin) -> None:
            # Morning restore; always last after any night set in this poll.
            r = tn.exec("settime 8000")
            log(f"telnet settime 8000 (day) → {r[:120]!r}")
            # Clear AI again after night so leftovers do not down the player.
            tn.clear_ai()

        def spawn_vehicle(tn: TelnetAdmin) -> None:
            # Same path as zombies: spawnentity <playerId> <class>
            n = tn.spawn_near_players("vehicleBicycle", per=1)
            if n == 0:
                for cmd in (
                    "spawnentityat vehicleBicycle 520 62 950",
                    "se vehicleBicycle",
                ):
                    r = tn.exec(cmd)
                    log(f"telnet vehicle {cmd} → {r[:80]!r}")
            else:
                log(f"telnet spawn vehicle near players units~={n}")

        def spawn_trader(tn: TelnetAdmin) -> None:
            n = tn.spawn_near_players("npcTraderJoel", per=1)
            if n == 0:
                n = tn.spawn_near_players("npcTraderBob", per=1)
            log(f"telnet spawn trader near players units~={n}")

        def teleport_to_pad(tn: TelnetAdmin) -> bool:
            return tn.teleport_players_to(*PERSIST_PAD_POS) > 0

        while time.time() < deadline:
            chunk = client_tail.read_new()
            if chunk:
                # Progress crumbs for long joins
                now = time.time()
                if now - last_progress > 8:
                    last_progress = now
                    crumbs = [
                        ln
                        for ln in chunk.splitlines()
                        if "[7dtd-playtest]" in ln or "[zdtd-connect]" in ln
                    ]
                    if crumbs:
                        log(f"progress: {crumbs[-1][-160:]}")

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
                        log("warn: provider rejoin teleport: no joined player yet; retry")

                if want_fixtures:
                    # spawn_zombie (may fire more than once: combat + sleeper_wake)
                    run_barrier("spawn_zombie", spawn_zombie, chunk=chunk)

                    run_barrier(
                        "kill_fixture_zombie",
                        lambda tn: tn.kill_non_player_ai(),
                        chunk=chunk,
                    )

                    run_barrier("kill_player", kill_player, chunk=chunk)

                    # Cap night sets: re-barrier spam was flipping the world back
                    # to 22:00 after settime_day and killing the player in economy
                    # cases. Later emissions beyond the cap stay pending forever,
                    # which is the intended swallow.
                    run_barrier(
                        "settime_bloodmoon", set_bloodmoon_night, chunk=chunk, cap=2
                    )

                    run_barrier("settime_day", restore_day, chunk=chunk)
                    run_barrier("spawn_vehicle", spawn_vehicle, chunk=chunk)
                    run_barrier("spawn_trader", spawn_trader, chunk=chunk)

                # Multi-peer / chat / APM barriers (stock or zdtd).
                led.observe(chunk, "spawn_loadgen_peer")
                while led.pending("spawn_loadgen_peer"):
                    if loadgen_proc is not None and loadgen_proc.poll() is None:
                        # Already running; consume this edge without restart.
                        led.mark_fired("spawn_loadgen_peer")
                        continue
                    loadgen_proc = start_loadgen(
                        game_port=args.port,
                        count=1,
                        timeout_ms=120_000,
                        log_path=args.logdir / "loadgen_peer.log",
                    )
                    if loadgen_proc is None:
                        log("warn: loadgen peer start failed; will retry next poll")
                        break
                    led.mark_fired("spawn_loadgen_peer")

                led.observe(chunk, "spawn_loadgen_bots")
                while led.pending("spawn_loadgen_bots"):
                    stop_proc(loadgen_proc)
                    loadgen_proc = start_loadgen(
                        game_port=args.port,
                        count=3,
                        timeout_ms=180_000,
                        log_path=args.logdir / "loadgen_bots.log",
                    )
                    if loadgen_proc is None:
                        log("warn: loadgen bots start failed; will retry next poll")
                        break
                    led.mark_fired("spawn_loadgen_bots")

                for full in barrier_hits_prefix(chunk, "chat_echo:"):
                    # Fire once per unique token name (only after successful telnet say).
                    token = full.split(":", 1)[-1].strip()
                    if not token:
                        continue
                    if token in chat_tokens_fired:
                        continue
                    tn = TelnetAdmin(telnet_host, telnet_port, telnet_password)
                    if not tn.connect():
                        log(f"warn: chat_echo:{token} telnet connect fail; retry")
                        continue
                    for cmd in (f"say {token}", f'say "{token}"'):
                        r = tn.exec(cmd)
                        log(f"telnet {cmd} → {r[:100]!r}")
                    tn.close()
                    chat_tokens_fired.add(token)
                    led.mark_fired("chat_echo")

                run_barrier("teleport_persist_pad", teleport_to_pad, chunk=chunk)

                led.observe(chunk, "apm_dump")
                while led.pending("apm_dump"):
                    ok = write_zdtd_apm_dump(
                        args.zdtd,
                        args.world,
                        args.game_srv,
                        apm_dump_path,
                        ticks=80,
                        run_id=apm_run_id,
                    )
                    if not ok:
                        log("warn: apm dump write failed; retry next poll")
                        break
                    led.mark_fired("apm_dump")

                if "DONE" in chunk and "[7dtd-playtest]" in chunk:
                    parsed = parse_client_log(client_tail.read_all())
                    if parsed.get("done") is not None:
                        if not primary_done_logged:
                            primary_done_logged = True
                            log("saw DONE in primary client log")
                        peer_parsed = read_peer_results()
                        if peer_suite_done():
                            log("saw DONE in every scenario client log")
                            break

            # Fold only newly appended peer lines into the accumulator; a
            # whole-file re-parse per poll is quadratic over soak logs. A final
            # full parse below refreshes results/summary for the report.
            peer_stream_needed = not peer_ready_seen or (
                peer_client_suite and peer_parsed.get("done") is None
            )
            if peer_tail is not None and peer_stream_needed:
                pchunk = peer_tail.read_new()
                if pchunk:
                    if "ready player=" in pchunk:
                        peer_ready_seen = True
                        log("peer client playtest ready")
                    if peer_client_suite and peer_parsed.get("done") is None:
                        peer_parsed = merge_parsed(
                            peer_parsed, parse_client_log(pchunk)
                        )
                # Late peer completion after primary DONE must still exit the loop.
                if (
                    peer_client_suite
                    and parsed.get("done") is not None
                    and peer_suite_done()
                ):
                    log("saw DONE in every scenario client log")
                    break

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
                    log("warn: stock peer teleport: waiting for both joined players")

            if client_proc is not None and client_proc.poll() is not None:
                time.sleep(2)
                parsed = parse_client_log(client_tail.read_all())
                peer_parsed = read_peer_results()
                if parsed.get("done") is not None and peer_suite_done():
                    break
            time.sleep(0.5)
        else:
            log(f"timeout after {args.timeout}s waiting for DONE")
            parsed = parse_client_log(client_tail.read_all())

        if not parsed:
            parsed = parse_client_log(client_tail.read_all())
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
        wall_s = time.time() - t0

        # Slowest cases from results if ms present in JSON events
        slowest = []
        for ev in parsed.get("json_events") or []:
            if ev.get("t") == "result" and ev.get("status") in ("pass", "fail"):
                slowest.append(
                    (
                        f"{ev.get('suite')}/{ev.get('case')}",
                        float(ev.get("ms") or 0),
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
            "client_log": str(args.client_log),
            "peer_client_log": str(peer_client_log) if peer_client_log else None,
            "peer_client_suite": peer_client_suite or None,
            "server_log": str(server_log),
            "unity_log": str(unity_log) if unity_log else None,
            "timeout_sec": args.timeout,
            "wall_sec": round(wall_s, 1),
            "fixtures": {
                "zombie_spawn_attempted": led.fired.get("spawn_zombie", 0) > 0,
                "kill_fixture_zombie_attempted": led.fired.get("kill_fixture_zombie", 0) > 0,
                # fired = host actions completed; seen = barrier emissions parsed.
                # A gap (seen > fired) means telnet actions kept failing.
                "barrier_counts": dict(led.fired),
                "barrier_seen": dict(led.seen),
                "fresh_save": bool(args.fresh_save),
            },
        }
        write_report(report_path, payload)
        junit_path = args.junit or (args.logdir / f"junit-{int(time.time())}.xml")
        write_junit(junit_path, args.suite, combined_results, summary)

        if done is None or (peer_client_suite and peer_done is None):
            missing = "primary" if done is None else "peer"
            log(f"FAIL harness: no DONE from {missing} playtest mod")
            if summary:
                log(f"partial summary={summary}")
            for r in results:
                log(f"  {r['status']} {r['case']} {r.get('detail', '')}")
            for r in peer_results:
                log(f"  peer {r['status']} {r['case']} {r.get('detail', '')}")
            if args.client_log.is_file():
                cl = args.client_log.read_text(errors="replace")
                for key in (
                    "7dtd-playtest",
                    "zdtd-connect",
                    "InitMod",
                    "Connect",
                    "ERROR",
                    "Exception",
                ):
                    hits = [ln for ln in cl.splitlines() if key in ln]
                    if hits:
                        log(f"client log '{key}' ({len(hits)}): {hits[-3:]}")
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
            stop_proc(client_proc)
            stop_proc(peer_client_proc)
            stop_proc(loadgen_proc)
            stop_proc(server_proc)
            # Soft clean after: leave Steam alone. --no-server runs must not
            # kill the external dedicated/zdtd they were told to attach to.
            teardown = CLIENT_KILL_PATTERNS + [LOADGEN_KILL_PATTERN]
            if not args.no_server:
                teardown = [
                    DEDI_KILL_PATTERN,
                    ZDTD_KILL_PATTERN,
                ] + teardown
            pkill_patterns(
                teardown,
                sig="-9",
            )
            try:
                playtest_lock.release(lock_session, path=lock_path)
                log(f"playtest lock released session={lock_session}")
            except playtest_lock.PlaytestLockError as ex:
                log(f"warn: playtest lock release refused: {ex}")
            except OSError as ex:
                log(f"warn: playtest lock release failed: {ex}")


if __name__ == "__main__":
    sys.exit(main())
