#!/usr/bin/env python3
"""Seeded runner for the deterministic simulation.

    python3 scripts/dst_run.py                 # 50 seeds from a random start
    python3 scripts/dst_run.py --seed 12345    # exactly that run, again
    python3 scripts/dst_run.py --soak 300      # keep going for 5 minutes
    python3 scripts/dst_run.py --regressions   # replay every captured seed

Every run is a pure function of its seed. A failure prints the seed, the
trace digest, and the exact command to reproduce it, dumps the trace without
overwriting the dump a previous run of that seed left (the baseline a replay
is diffed against), and (with --record) appends the seed to the regression
list so it is replayed forever after. ``--json`` carries ``run_digest`` over
every seed it ran, so two runs of the same seed list are comparable without
keeping the traces.
"""

from __future__ import annotations

import argparse
import hashlib
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
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as ex:
        # A regression list this run cannot read is a silently shorter replay
        # set, so a seed that once broke the lock can stop being replayed and
        # the failure returns as "new". Say so instead of replaying nothing.
        print(
            f"[dst] error: cannot read the regression seed list {path}: {ex}",
            file=sys.stderr,
        )
        raise
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            seeds.append(int(line))
        except ValueError:
            # A typo here silently removes a regression from replay forever;
            # say so instead of skipping the line quietly.
            print(
                f"[dst] warn: {path.name}: ignoring non-integer seed line: "
                f"{raw.strip()!r}",
                file=sys.stderr,
            )
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
    try:
        with path.open("a", encoding="utf-8") as fh:
            if header:
                fh.write(header)
            fh.write(f"{seed}  # {note}\n")
    except OSError as ex:
        # A failed append must not abort the run before the verdict and repro
        # command are printed; the operator records the seed by hand instead.
        print(f"[dst] warn: could not record seed {seed} in {path}: {ex}",
              file=sys.stderr)
        return False
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
        max_steps=args.max_steps,
        faults=faults,
    )


def replay_flags(cfg: SimConfig) -> str:
    """Flags that pin every knob ``config_from_args`` can move.

    The seed alone does not determine a run: stale/heartbeat seconds and the
    fault mode change scheduling and injected faults, so a replay command
    that omits them can silently reproduce a different run than the one
    that failed.
    """
    flags = [
        f"--agents {cfg.agents}",
        f"--sim-seconds {cfg.run_seconds:g}",
        f"--stale-sec {cfg.stale_sec:g}",
        f"--heartbeat-sec {cfg.heartbeat_sec:g}",
        f"--max-steps {cfg.max_steps}",
    ]
    faults = cfg.faults
    if faults != Faults():
        happy_with_skew = Faults.none()
        happy_with_skew.clock_skew_sec = faults.clock_skew_sec
        if faults == happy_with_skew:
            # --no-faults, optionally with --clock-skew, rebuilds this shape
            # through config_from_args exactly (including skew-only moves).
            flags.append("--no-faults")
            if faults.clock_skew_sec > 0.0:
                flags.append("--clock-skew")
        elif faults.clock_skew_sec > 0.0 and faults == Faults(
            clock_skew_sec=faults.clock_skew_sec
        ):
            # Only the skew knob moved off the default; --clock-skew rebuilds it.
            flags.append("--clock-skew")
        # Any other shape is not expressible on the CLI, so emit nothing
        # rather than a command that lies about the fault set.
    return " ".join(flags)


def replay_command(seed: int, cfg: SimConfig, argv0: str) -> str:
    """Command line that reruns this exact simulation."""
    return f"python3 {argv0} --seed {seed} {replay_flags(cfg)}"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "a failure prints the seed, the replay command, and the tail of the\n"
            "event history on stderr; the run verdict is the exit code.\n"
            "exit codes: 0 every seed held its invariants, 1 a seed violated one,\n"
            "2 bad usage"
        ),
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
    ap.add_argument("--max-steps", type=int, default=SimConfig.max_steps,
                    help="scheduler resumes before the run is called a runaway "
                         "(reported as a failure, never as a clean stop)")
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
    return ap


def trace_dump_path(trace_dir: Path, seed: int) -> tuple[Path, Path | None]:
    """Dump path for ``seed``, and the earlier dump it must not overwrite.

    Replaying a failing seed is how you find out whether the divergence is
    yours, so the first trace is the baseline the replay is diffed against.
    Writing over it destroys the only record of what the run actually did,
    and a second dump that lands on the same name is indistinguishable from a
    replay that reproduced it exactly. So a name already taken gets a
    numbered sibling, and the caller is told which file that was.
    """
    base = trace_dir / f"dst-trace-{seed}.jsonl"
    if not base.exists():
        return base, None
    n = 2
    while (trace_dir / f"dst-trace-{seed}-{n}.jsonl").exists():
        n += 1
    return trace_dir / f"dst-trace-{seed}-{n}.jsonl", base


def report_failure(
    result: SimResult, cfg: SimConfig, trace_dir: Path, argv0: str
) -> Path | None:
    """Print the seed, the trace digest, the repro command, and the tail of
    the event history.

    The digest is what says whether a replay reproduced the run: two dumps of
    one seed with the same digest are the same run, and a differing one names
    the divergence without anyone reading a few thousand events.

    Returns the trace path, or None when the dump could not be written. The
    dump failing (full disk, unwritable dir) must not prevent the verdict,
    seed, and replay command from reaching the operator: the trace file is a
    convenience, those prints are the evidence.
    """
    path: Path | None = None
    baseline: Path | None = None
    lines = result.trace_lines
    try:
        trace_dir.mkdir(parents=True, exist_ok=True)
        dump, baseline = trace_dump_path(trace_dir, result.seed)
        dump.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path = dump
    except OSError as ex:
        print(f"[dst] warn: could not write trace dump: {ex}", file=sys.stderr)
    print("", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(f"[dst] FAIL seed={result.seed}", file=sys.stderr)
    print(f"[dst] invariant: {result.violation}", file=sys.stderr)
    print(f"[dst] digest:    {result.digest}", file=sys.stderr)
    print(f"[dst] replay:    {replay_command(result.seed, cfg, argv0)}",
          file=sys.stderr)
    if path is not None:
        print(f"[dst] trace:     {path} ({len(lines)} events)", file=sys.stderr)
        if baseline is not None:
            print(f"[dst] baseline:  {baseline} (kept; diff the two to see where "
                  "the replay diverged)", file=sys.stderr)
    else:
        print("[dst] trace:     <unavailable; see warning above>", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    for line in lines[-25:]:
        print(f"  {line}", file=sys.stderr)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    cfg = config_from_args(args)

    if args.regressions:
        seeds = load_regression_seeds()
        if not seeds:
            if not args.quiet:
                print("[dst] no regression seeds recorded yet")
            return 0
        if not args.quiet:
            print(f"[dst] replaying {len(seeds)} regression seed(s)")
    elif args.seed is not None:
        seeds = [args.seed]
    else:
        start = secrets.randbelow(2**48)
        seeds = [start + i for i in range(max(1, args.iterations))]
        if not args.quiet:
            print(f"[dst] start_seed={start} iterations={len(seeds)}")

    # Elapsed-time budget and wall measurement on the monotonic clock so a
    # wall-clock step mid-soak cannot extend or truncate the soak window.
    started = time.monotonic()
    ran = 0
    coverage: set[str] = set()
    failures: list[SimResult] = []
    # One digest over every run's own trace digest, in order: the evidence a
    # later replay of the same seed list is compared against. Without it a
    # green CI run leaves nothing behind that says what it actually did.
    run_digest = hashlib.sha256()
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
        run_digest.update(f"{seed}:{result.digest}\n".encode())
        if result.violation:
            failures.append(result)
            report_failure(result, cfg, args.trace_dir, sys.argv[0])
            if args.record and record_seed(seed, result.violation.split("]")[0].strip("[")):
                print(f"[dst] recorded seed {seed} in {SEEDS_FILE}")
            break
        if not args.quiet and ran % 25 == 0:
            print(f"[dst] {ran} seeds ok (last={seed})")

    wall = time.monotonic() - started
    run_digest_hex = run_digest.hexdigest()
    summary = {
        "seeds_run": ran,
        "failures": len(failures),
        "wall_sec": round(wall, 3),
        "simulated_sec": round(ran * cfg.run_seconds, 1),
        "agents": cfg.agents,
        "faults": not args.no_faults,
        "coverage": sorted(coverage),
        "failing_seed": failures[0].seed if failures else None,
        "failing_trace_digest": failures[0].digest if failures else None,
        "run_digest": run_digest_hex,
    }
    if args.json:
        try:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(summary, indent=2) + "\n",
                                 encoding="utf-8")
        except OSError as ex:
            # The summary JSON is derived evidence; losing it must not lose
            # the PASS/FAIL verdict printed below.
            print(f"[dst] warn: could not write {args.json}: {ex}",
                  file=sys.stderr)
    if failures:
        print(f"[dst] FAIL after {ran} seeds in {wall:.1f}s")
        return 1
    print(
        f"[dst] PASS {ran} seeds, {summary['simulated_sec']:.0f} simulated seconds "
        f"in {wall:.1f}s wall, {len(coverage)} scenario kinds covered, "
        f"run_digest={run_digest_hex[:16]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
