#!/usr/bin/env python3
"""Offline gate for the deterministic simulation (no game install).

Guards the three things that make a simulator worth having:

1. It is deterministic - one seed, one trace, byte for byte, in this process
   and in a fresh one.
2. It is honest - a known regression planted in the lock is caught. A green
   simulation that cannot fail is worse than no simulation.
3. It stays green - the regression seeds recorded in ``dst_seeds.txt`` are
   replayed on every run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dst_run  # noqa: E402
import dst_sim  # noqa: E402
import playtest_lock as pl  # noqa: E402
from dst_sim import Faults, SimConfig, run_simulation  # noqa: E402

GATE_SEEDS = 60


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_replay_is_byte_identical() -> None:
    """Same seed, same trace. This is the whole contract."""
    a = run_simulation(4242)
    b = run_simulation(4242)
    _assert(a.digest == b.digest, f"digest differs: {a.digest} vs {b.digest}")
    _assert(a.steps == b.steps, "step count differs across replay")
    c = run_simulation(4243)
    _assert(c.digest != a.digest, "different seeds must produce different runs")


def test_replay_survives_a_fresh_process() -> None:
    """Catches entropy that leaks in through PYTHONHASHSEED or the OS."""
    code = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r});"
        "from dst_sim import run_simulation; print(run_simulation(99).digest)"
    )
    digests = set()
    for hash_seed in ("0", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": hash_seed},
        )
        digests.add(out.stdout.strip())
    _assert(len(digests) == 1, f"digest depends on the process: {digests}")
    _assert(
        digests.pop() == run_simulation(99).digest,
        "subprocess digest differs from in-process digest",
    )


def test_clean_run_holds_every_invariant() -> None:
    for seed in range(1, GATE_SEEDS + 1):
        r = run_simulation(seed)
        _assert(r.violation is None, f"seed {seed}: {r.violation}")
        _assert(r.max_concurrent_runtime <= 1, f"seed {seed}: two live runtimes")


def test_faults_actually_fire() -> None:
    """A fault-free simulation proves nothing. Assert the faults land."""
    totals = {"torn": 0, "crashes": 0, "io_errors": 0}
    reasons: set[str] = set()
    coverage: set[str] = set()
    for seed in range(1, GATE_SEEDS + 1):
        r = run_simulation(seed)
        totals["torn"] += r.torn
        totals["crashes"] += r.crashes
        totals["io_errors"] += r.io_errors
        reasons |= set(r.refusals)
        coverage |= r.coverage
    for name, count in totals.items():
        _assert(count > 0, f"fault {name} never fired across {GATE_SEEDS} seeds")
    for reason in ("foreign_holder", "stale_but_live", "live_runtime"):
        _assert(f"refusal/{reason}" in coverage, f"never exercised refusal {reason}")
    for kind in (
        "fault_external_corruption",
        "stale_release_refused",
        "hang_start",
        "crash_holding",
    ):
        _assert(kind in coverage, f"scenario {kind} never reached")


def test_happy_path_is_quiet() -> None:
    """With faults off, agents take turns and nothing is ever refused for a
    reason that only a fault can cause."""
    cfg = SimConfig(faults=Faults.none())
    for seed in range(1, 20):
        r = run_simulation(seed, cfg)
        _assert(r.violation is None, f"seed {seed} clean run: {r.violation}")
        _assert(r.torn == 0 and r.crashes == 0, "faults fired with faults disabled")
        _assert(
            "stale_but_live" not in r.refusals,
            "stale takeover attempted without any crash",
        )


def _sweep(seeds: int = 30) -> list[tuple[int, str]]:
    hits = []
    for seed in range(1, seeds + 1):
        r = run_simulation(seed)
        if r.violation:
            hits.append((seed, r.violation))
    return hits


def test_simulator_catches_planted_regressions() -> None:
    """The gate on the gate. Each planted bug is a real defect class:

    * ignoring the live-process probe (the original reason for the lock)
    * publishing without the tmp + os.replace hop (torn record visible)
    * releasing a lock we do not own (claim wipe)
    """
    planted = []

    real_acquire = pl.acquire

    def blind_acquire(session, *, path=None, live_probe=None, max_age_sec=None, env=None):
        return real_acquire(
            session, path=path, live_probe=lambda: False,
            max_age_sec=max_age_sec, env=env,
        )

    real_write = pl.write_lock

    def nonatomic_write(path, *, running, session=None, acquired=None,
                        heartbeat=None, env=None):
        e = pl._env(env)
        e.storage.mkdir_parents(path)
        if running:
            now = pl.utc_now_iso(e)
            body = (
                f"running=yes\nsession={session}\n"
                f"acquired={acquired or now}\nheartbeat={heartbeat or now}\n"
            )
        else:
            body = "running=no\n"
        e.storage.write_text(path, body)

    real_release = pl.release

    def unchecked_release(session, *, path=None, env=None):
        e = pl._env(env)
        pl._with_flock(
            path, lambda: pl.write_lock(path, running=False, session=None, env=e), env=e
        )
        return pl.read_lock(path, env=e)

    for name, attr, bug in (
        ("ignores live-runtime probe", "acquire", blind_acquire),
        ("non-atomic publish", "write_lock", nonatomic_write),
        ("release without ownership", "release", unchecked_release),
    ):
        original = getattr(pl, attr)
        setattr(pl, attr, bug)
        try:
            hits = _sweep()
        finally:
            setattr(pl, attr, original)
        planted.append((name, len(hits)))
        _assert(hits, f"simulator missed planted bug: {name}")

    _assert(pl.acquire is real_acquire, "acquire not restored")
    _assert(pl.write_lock is real_write, "write_lock not restored")
    _assert(pl.release is real_release, "release not restored")
    for name, count in planted:
        print(f"  planted '{name}' caught on {count}/30 seeds")


def test_regression_seeds_replay() -> None:
    seeds = dst_run.load_regression_seeds()
    for seed in seeds:
        r = run_simulation(seed)
        _assert(r.violation is None, f"regression seed {seed} failed again: {r.violation}")
    print(f"  {len(seeds)} regression seed(s) replayed")


def test_replay_command_pins_the_config() -> None:
    """The seed does not determine a run alone: stale/heartbeat seconds and
    the fault mode move scheduling and injected faults. Whatever config a
    failure ran under, the printed replay command must rebuild it exactly."""
    for argv in (
        [],
        ["--clock-skew"],
        ["--no-faults"],
        ["--agents", "5", "--sim-seconds", "600",
         "--stale-sec", "60", "--heartbeat-sec", "10"],
        ["--no-faults", "--agents", "7", "--sim-seconds", "120"],
    ):
        want = dst_run.config_from_args(dst_run.build_parser().parse_args(argv))
        cmd = dst_run.replay_command(1234, want, "scripts/dst_run.py")
        reparsed = dst_run.build_parser().parse_args(cmd.split()[2:])
        _assert(
            dst_run.config_from_args(reparsed) == want,
            f"replay command does not pin the config for argv={argv}: {cmd}",
        )


def test_lock_release_is_no_op_when_not_owner() -> None:
    """The defect the simulator found, pinned as a unit test."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        lock = Path(td) / "playtest_running"
        owner = "grok-20260810-231500-a1b2c3d4e5f6"
        pl.acquire(owner, path=lock, live_probe=lambda: False)
        stale = "codex-20260810-120000-deadbeefcafe"
        # Two shapes a foreign writer can leave behind: one that reads free,
        # and one that still reads as a claim but names nobody. The second is
        # the one the simulator needed 18 seeds to reach.
        for mangled in ("running=y", "running=yes\nsess"):
            lock.write_text(mangled, encoding="utf-8")
            pl.release(stale, path=lock)
            _assert(
                lock.read_text(encoding="utf-8") == mangled,
                f"release wrote over a record that does not name it: {mangled!r}",
            )
        # Owner release still works and is idempotent.
        pl.acquire(owner, path=lock, live_probe=lambda: False)
        _assert(pl.release(owner, path=lock).running is False, "owner release")
        _assert(pl.release(owner, path=lock).running is False, "release idempotent")


def test_heartbeat_loop_flags_a_lost_claim() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        lock = Path(td) / "playtest_running"
        owner = "grok-20260810-231500-a1b2c3d4e5f6"
        other = "codex-20260810-120000-deadbeefcafe"
        pl.acquire(owner, path=lock, live_probe=lambda: False)
        loop = pl.HeartbeatLoop(owner, path=lock, interval_sec=1.0)
        loop.tick(force=True)
        _assert(loop.touches == 1 and not loop.lost_claim, "healthy refresh")
        pl.write_lock(lock, running=True, session=other)
        loop.tick(force=True)
        _assert(loop.lost_claim, "lost claim must be flagged, not swallowed")


def main() -> int:
    tests = [
        ("replay_is_byte_identical", test_replay_is_byte_identical),
        ("replay_survives_fresh_process", test_replay_survives_a_fresh_process),
        ("clean_run_holds_invariants", test_clean_run_holds_every_invariant),
        ("faults_actually_fire", test_faults_actually_fire),
        ("happy_path_is_quiet", test_happy_path_is_quiet),
        ("simulator_catches_planted_regressions",
         test_simulator_catches_planted_regressions),
        ("regression_seeds_replay", test_regression_seeds_replay),
        ("replay_command_pins_the_config", test_replay_command_pins_the_config),
        ("release_no_op_when_not_owner", test_lock_release_is_no_op_when_not_owner),
        ("heartbeat_flags_lost_claim", test_heartbeat_loop_flags_a_lost_claim),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as ex:
            failed += 1
            print(f"FAIL {name}: {ex}")
        else:
            print(f"PASS {name}")
    print("RESULT", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
