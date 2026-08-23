#!/usr/bin/env python3
"""Structural regression checks for passive stock-peer orchestration."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "scripts" / "playtest_run.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def require(name: str, ok: bool) -> bool:
    print(("PASS" if ok else "FAIL") + " " + name)
    return ok


checks = [
    require(
        "paired peer arguments require an isolated profile",
        '"--peer-client-name"' in RUNNER
        and '"--peer-client-compat"' in RUNNER
        and "must be provided together" in RUNNER,
    ),
    require(
        "peer gets a distinct stock Local identity", 
        '"ZDTD_PLAYER_NAME": peer_client_name' in RUNNER,
    ),
    require(
        "peer can run an explicit provider setup suite",
        '"--peer-client-suite"' in RUNNER
        and "run_suite=bool(peer_client_suite)" in RUNNER,
    ),
    require(
        "peer ready state gates a shared fixture teleport",
        '"--peer-client-teleport"' in RUNNER
        and "peer_ready_seen" in RUNNER
        and "teleport_players_to(*args.peer_client_teleport)" in RUNNER,
    ),
    require(
        "peer clears inherited scenario environment",
        "run_suite: bool = True" in RUNNER
        and 'env.pop(key, None)' in RUNNER
        and "run_suite=bool(peer_client_suite)" in RUNNER,
    ),
    require(
        "peer lifecycle is cleaned with the primary client",
        "stop_proc(peer_client_proc)" in RUNNER,
    ),
    require(
        "a peer scenario suite must finish before the run is green",
        "peer_suite_done()" in RUNNER
        and "saw DONE in every scenario client log" in RUNNER
        and '"peer_done": peer_done' in RUNNER,
    ),
    require(
        "peer scenario failures affect the orchestrator exit status",
        "peer_summary" in RUNNER
        and "peer_results" in RUNNER
        and 'int(peer_summary["fail"])' in RUNNER,
    ),
    require(
        "README distinguishes stock peer from loadgen",
        "passive **stock** peer" in README
        and "not a loadgen bot" in README
        and "--peer-client-suite" in README,
    ),
]

raise SystemExit(0 if all(checks) else 1)
