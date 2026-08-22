# Deterministic simulation testing

`make dst`

The host side of this repo has exactly one component that is stateful,
concurrent, crash-prone, and impossible to test honestly against the real
thing: the **playtest exclusivity lock** (`scripts/playtest_lock.py`). Several
agents and orchestrators on one machine coordinate access to a single live
client plus dedicated server through one file, a heartbeat, and a documented
stale-takeover rule. The bugs that matter there are timing bugs - "the holder
crashed 200 ms into a write while its game processes were still up" - and they
surface once a month on somebody's machine, never in a unit test.

So it is simulated instead. One seed determines an entire run: every clock
read, every random choice, every injected fault, and the order in which the
agents act. A failure prints the seed and the command to replay it; the replay
is byte-identical.

```
make dst                       # regression seeds + 200 fresh seeds
make dst-soak DST_SOAK_SEC=900 # tail-bug hunt, records failing seeds
python3 scripts/dst_run.py --seed 18            # replay one run exactly
python3 scripts/dst_run.py --regressions        # only the captured seeds
python3 scripts/dst_run.py --agents 5 --no-faults
```

200 seeds is about 200 simulated hours and takes ~2 seconds, because no wall
time is ever spent waiting.

## Pieces

| File | Role |
|---|---|
| `scripts/dst.py` | Simulation core: seeded `Rng`, `VirtualClock`, cooperative `Simulation` scheduler, `Trace` with a digest |
| `scripts/dst_sim.py` | The model: simulated filesystem, faults, agent lifecycle, invariants |
| `scripts/dst_run.py` | Seeded runner, replay, soak, regression seeds, trace dump |
| `scripts/dst_seeds.txt` | Seeds that failed once; replayed forever |
| `scripts/test_dst.py` | Offline gate (part of `make test`) |

## The seams

Nothing in the lock reaches the outside world directly any more. Everything it
cannot reproduce on its own goes through one injected `playtest_lock.LockEnv`:

| Nondeterminism | Seam | Production | Simulation |
|---|---|---|---|
| Wall clock | `LockEnv.now()` | `time.time()` | virtual clock, optional per-agent skew |
| Entropy (session ids, tmp names) | `LockEnv.token_hex()` | `secrets` | seeded substream |
| PID | `LockEnv.pid()` | `os.getpid()` | seeded substream |
| Disk | `LockStorage` | `Path` + `os.replace` | in-memory dict with faults |
| Cross-process mutex | `LockStorage.exclusive()` | `fcntl.flock` | deterministic critical section |
| Heartbeat timing | `HeartbeatLoop.tick()` | `HeartbeatThread` | stepped by the scheduler |

The production code path and the simulated one are the same code path; only
the env differs. `HeartbeatThread` still exists and still owns the real thread,
but the refresh *policy* lives in `HeartbeatLoop`, which the simulator drives
directly. Live-process detection is already injectable (`live_probe=`), which
is why it needed no change.

## Faults

All seed-driven, so a scenario is replayed rather than re-rolled
(`dst_sim.Faults`):

- `crash_during_write` - death after the tmp write, before the rename
- `torn_write` - short write then death; the rename never runs
- `write_error` - ENOSPC / EIO on the tmp write
- `external_corruption` - a foreign writer truncates the shared file (the path
  is documented as shared with the Atomic / 7dtd-mods helpers, which do not
  necessarily write it atomically)
- `agent_crash` - holder dies, taking its game processes with it
- `agent_hang` - holder alive with processes up but no longer heartbeating;
  the case `stale_but_live` exists for
- `stray_process` - a client started by hand outside any orchestrator
- `stale_release` - a late exit handler releasing with a session it no longer owns
- `clock_skew_sec` - agents disagreeing about the time

Process death is modelled as world state, not only as an exception. Production
code legitimately catches broad exceptions (`HeartbeatLoop` swallows anything
so a transient failure does not stop the refresh loop), and a dead process
cannot be swallowed - so a crash also sets a flag that agents check after every
call.

## Invariants

Checked after **every** scheduler step, not just at the end. These are beliefs
about the design, written down first; the simulator's job is to find the holes
in them.

| | Property |
|---|---|
| I1 | Two agents never have live runtime processes at once. **The** safety property: two runtimes means duplicate dedicated servers, a port fight, and one run's `clean_processes` killing the other's client |
| I2 | Two agents never both believe they hold the lock |
| I3 | A crash mid-write is never visible: readers see the old record or the new one, never half of one |
| I4 | The record only ever names a session that actually asked for it |
| I5 | A holder's heartbeat never moves backwards |
| I6 | A stale claim with no live runtime is always reclaimable - corruption must be survivable, not fatal |
| I7 | While an agent holds, the durable record still agrees with it |

I1/I2 and I7 are deliberately paired across the boundary: the acquiring agent
asserts before it starts processes, and the world asserts after every step.

## What it found

The simulation found a real defect on its first serious sweep. `release()`
wrote `running=no` whenever the record failed to name *someone else*. After a
foreign writer mangled the shared file - so it read as free, or as a claim
naming nobody - a late exit handler releasing with a stale session id erased a
live holder's claim, and exclusivity then rested entirely on the live-process
probe. Release now writes only when the record names the caller (seeds 18, 40,
50 in `dst_seeds.txt`, plus a unit test in `test_dst.py`).

`HeartbeatLoop.lost_claim` was added at the same time: the refresh loop used to
bury a `foreign_holder` error in the same catch-all as a transient I/O failure.
Losing the claim means exclusivity is no longer guaranteed, and the orchestrator
should be able to see that. What a run *does* about it is a policy decision and
is deliberately left to the orchestrator.

## The gate on the gate

A simulation that cannot fail is worse than none. `test_dst.py` plants three
known defect classes in the lock and requires the simulator to catch each one:
ignoring the live-process probe, publishing without the tmp + `os.replace` hop,
and releasing a lock the caller does not own. It also asserts that the faults
actually fire and that the interesting refusals (`foreign_holder`,
`stale_but_live`, `live_runtime`), hangs, and crashes are really reached - a
seed count means nothing if the scenarios were never entered.

## Not simulated yet

`scripts/playtest_run.py` is the other stateful host component, but it is a
different problem: it is mostly a driver for real subprocesses (game client,
dedicated server, telnet admin, loadgen) whose behaviour is the thing under
test. Simulating it is only worth doing for the parts that are pure decisions
about time and log content, and it would need its own seams first:

1. A clock seam for the roughly forty `time.time()` / `time.sleep()` call sites
   that drive timeouts, barrier waits, and phase deadlines.
2. A process port (start / poll / stop / kill) with a simulated implementation,
   so crash-and-restart and slow-boot cases become reachable.
3. A telnet port, so `TelnetAdmin` retries and partial reads can be modelled.

Order matters: the clock alone would already make the barrier and deadline
logic testable without waiting fifteen minutes for a soak. Until then the
orchestrator's deterministic parts (`parse_client_log`, `barrier_hits_prefix`,
the compare diff) stay covered by the ordinary offline gates.
