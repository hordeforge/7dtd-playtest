#!/usr/bin/env python3
"""Seeded runner for the deterministic simulation.

    python3 scripts/dst_run.py                 # 50 seeds from a random start
    python3 scripts/dst_run.py --seed 12345    # exactly that run, again
    python3 scripts/dst_run.py --soak 300      # keep going for 5 minutes
    python3 scripts/dst_run.py --regressions   # replay every captured seed

Every run is a pure function of its seed. A failure prints the seed and the
exact command to reproduce it, dumps the trace, and (with --record) appends
the seed to the regression list so it is replayed forever after.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dst_sim import Faults, SimConfig, SimResult, run_simulation  # noqa: E402

SEEDS_FILE = SCRIPTS / "dst_seeds.txt"
DEFAULT_TRACE_DIR = Path.home() / ".cache" / "7dtd-playtest" / "dst"


def load_regression_seeds(path: Path = SEEDS_FILE) -> list[int]:
    """Seeds that failed once. They are replayed on every run, forever."""
    if not path.is_file():
        return []
    seeds: list[int] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            try:
                seeds.append(int(line))
            except ValueError:
                continue
    return seeds


def record_seed(seed: int, note: str, path: Path = SEEDS_FILE) -> bool:
    if seed in load_regression_seeds(path):
        return False
    header = "" if path.is_file() else (
        "# Seeds that once failed the deterministic simulation.\n"
        "# Replayed by `make dst` on every run. Never delete a line here\n"
        "# without understanding why the scenario can no longer occur.\n"
    )
    with path.open("a", encoding="utf-8") as fh:
        if header:
            fh.write(header)
        fh.write(f"{seed}  # {note}\n")
    return True


def config_from_args(args: argparse.Namespace) -> SimConfig:
    faults = Faults.none() if args.no_faults else Faults()
    if args.clock_skew:
        faults.clock_skew_sec = 0.5
    return SimConfig(
        agents=args.agents,
        stale_sec=args.stale_sec,
        heartbeat_sec=args.heartbeat_sec,
        run_seconds=args.sim_seconds,
        faults=faults,
    )


def report_failure(
    result: SimResult, cfg: SimConfig, trace_dir: Path, argv0: str
) -> Path:
    """Print the seed, the repro command, and the tail of the event history."""
    trace_dir.mkdir(parents=True, exist_ok=True)
    path = trace_dir / f"dst-trace-{result.seed}.jsonl"
    lines = result.trace_lines
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(f"[dst] FAIL seed={result.seed}", file=sys.stderr)
    print(f"[dst] invariant: {result.violation}", file=sys.stderr)
    print(
        f"[dst] replay:    python3 {argv0} --seed {result.seed}"
        f" --agents {cfg.agents} --sim-seconds {int(cfg.run_seconds)}",
        file=sys.stderr,
    )
    print(f"[dst] trace:     {path} ({len(lines)} events)", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    for line in lines[-25:]:
        print(f"  {line}", file=sys.stderr)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--seed", type=int, default=None,
                    help="run exactly this seed (default: random start seed)")
    ap.add_argument("--iterations", type=int, default=50,
                    help="how many consecutive seeds to run (default 50)")
    ap.add_argument("--soak", type=float, default=0.0,
                    help="keep running new seeds for this many wall seconds")
    ap.add_argument("--agents", type=int, default=3,
                    help="simulated lock contenders per run (default 3)")
    ap.add_argument("--sim-seconds", type=float, default=3600.0,
                    help="simulated seconds per run (virtual: costs no wall time)")
    ap.add_argument("--stale-sec", type=float, default=120.0,
                    help="simulated heartbeat age after which a lock is stale")
    ap.add_argument("--heartbeat-sec", type=float, default=30.0,
                    help="simulated heartbeat interval")
    ap.add_argument("--no-faults", action="store_true",
                    help="disable fault injection (happy path only)")
    ap.add_argument("--clock-skew", action="store_true",
                    help="give agents skewed clocks")
    ap.add_argument("--regressions", action="store_true",
                    help="replay the captured regression seeds and stop")
    ap.add_argument("--record", action="store_true",
                    help="append a failing seed to the regression list")
    ap.add_argument("--trace-dir", type=Path,
                    default=Path(os.environ.get("DST_TRACE_DIR", str(DEFAULT_TRACE_DIR))),
                    help="where failure traces are dumped (env DST_TRACE_DIR)")
    ap.add_argument("--json", type=Path, default=None,
                    help="write a machine-readable summary here")
    ap.add_argument("--quiet", action="store_true",
                    help="only print failures and the final verdict")
    args = ap.parse_args(argv)

    cfg = config_from_args(args)

    if args.regressions:
        seeds = load_regression_seeds()
        if not seeds:
            print("[dst] no regression seeds recorded yet")
            return 0
        print(f"[dst] replaying {len(seeds)} regression seed(s)")
    elif args.seed is not None:
        seeds = [args.seed]
    else:
        start = secrets.randbelow(2**48)
        seeds = [start + i for i in range(max(1, args.iterations))]
        print(f"[dst] start_seed={start} iterations={len(seeds)}")

    # Elapsed-time budget and wall measurement on the monotonic clock so a
    # wall-clock step mid-soak cannot extend or truncate the soak window.
    started = time.monotonic()
    ran = 0
    coverage: set[str] = set()
    failures: list[SimResult] = []
    index = 0
    while True:
        if index >= len(seeds):
            if args.soak > 0 and (time.monotonic() - started) < args.soak:
                seeds.append(seeds[-1] + 1)
            else:
                break
        seed = seeds[index]
        index += 1
        result = run_simulation(seed, cfg)
        ran += 1
        coverage |= result.coverage
        if result.violation:
            failures.append(result)
            report_failure(result, cfg, args.trace_dir, sys.argv[0])
            if args.record and record_seed(seed, result.violation.split("]")[0].strip("[")):
                print(f"[dst] recorded seed {seed} in {SEEDS_FILE}")
            break
        if not args.quiet and ran % 25 == 0:
            print(f"[dst] {ran} seeds ok (last={seed})")

    wall = time.monotonic() - started
    summary = {
        "seeds_run": ran,
        "failures": len(failures),
        "wall_sec": round(wall, 3),
        "simulated_sec": round(ran * cfg.run_seconds, 1),
        "agents": cfg.agents,
        "faults": not args.no_faults,
        "coverage": sorted(coverage),
        "failing_seed": failures[0].seed if failures else None,
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if failures:
        print(f"[dst] FAIL after {ran} seeds in {wall:.1f}s")
        return 1
    print(
        f"[dst] PASS {ran} seeds, {summary['simulated_sec']:.0f} simulated seconds "
        f"in {wall:.1f}s wall, {len(coverage)} scenario kinds covered"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
