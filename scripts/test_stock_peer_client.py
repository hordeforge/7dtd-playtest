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
        "peer clears inherited scenario environment",
        "run_suite: bool = True" in RUNNER
        and 'env.pop(key, None)' in RUNNER
        and "run_suite=False" in RUNNER,
    ),
    require(
        "peer lifecycle is cleaned with the primary client",
        'stop_proc(locals().get("peer_client_proc"))' in RUNNER,
    ),
    require(
        "README distinguishes stock peer from loadgen",
        "passive **stock** peer" in README and "not a loadgen bot" in README,
    ),
]

raise SystemExit(0 if all(checks) else 1)
