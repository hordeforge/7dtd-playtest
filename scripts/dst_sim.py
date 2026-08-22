#!/usr/bin/env python3
"""Deterministic simulation of the playtest exclusivity lock.

The lock in ``playtest_lock.py`` is the one stateful, concurrent, crash-prone
component the host owns: several agents/orchestrators on one machine race for
a single live client plus dedicated server, coordinating only through a file
with a heartbeat and a documented stale-takeover rule. Real-world coverage of
that is hopeless - the interesting cases are "holder crashed 200ms into a
write while its game processes were still up", and they show up once a month
on somebody's machine.

So we simulate it: virtual clock, in-memory filesystem, seeded faults, and a
cooperative scheduler that interleaves the agents. Everything the lock cannot
reproduce on its own is injected through ``playtest_lock.LockEnv``, so the
production code path under test here is the same code path a real run takes.

The model we assert (and the reason these are the assertions):

  I1  Live-runtime exclusivity. **The** safety property. Two agents must never
      have game processes up at the same time: that is what double-binds
      ports and makes one run's ``clean_processes`` kill the other's client.
  I2  A takeover never happens while the previous holder's runtime is live.
      Stale heartbeat alone is not enough - hence ``stale_but_live``.
  I3  Durable state is never torn: a reader either sees the old payload or
      the new one, never a half-written record that reads as a valid claim by
      the wrong session. Crash-during-write is injected on purpose.
  I4  The lock file only ever names a session that actually asked for it.
  I5  While one agent holds, its heartbeat never moves backwards.

I1/I2 are paired across the boundary: the acquiring side asserts before it
starts processes, and the world asserts on every scheduler step after.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import playtest_lock as pl
from dst import InvariantViolation, Rng, Simulation

LOCK_PATH = Path("/sim/.cache/7dtd-playtest/playtest_running")


class SimCrash(BaseException):
    """Process death, unwinding the stack the way a real death would.

    The exception alone is not the model: production code legitimately
    catches broad exceptions (``HeartbeatLoop`` swallows anything so a
    transient failure does not stop the refresh loop), and a dead process
    cannot be swallowed. So a crash also sets a flag on the storage, and
    agents check that flag after every call. The flag is the truth; the
    exception is only the unwind.
    """


class SimIOError(OSError):
    """Injected disk failure (ENOSPC / EIO)."""


@dataclass
class Faults:
    """Fault probabilities. Every draw comes from the run seed, so a failing
    scenario is replayed exactly, not re-rolled."""

    crash_during_write: float = 0.05
    write_error: float = 0.03
    torn_write: float = 0.04
    external_corruption: float = 0.02
    stale_release: float = 0.04
    agent_crash: float = 0.06
    agent_hang: float = 0.05
    stray_process: float = 0.02
    stray_max_sec: float = 600.0
    clock_skew_sec: float = 0.0
    max_clock_skew_sec: float = 45.0

    @classmethod
    def none(cls) -> Faults:
        return cls(
            crash_during_write=0.0,
            write_error=0.0,
            torn_write=0.0,
            external_corruption=0.0,
            stale_release=0.0,
            agent_crash=0.0,
            agent_hang=0.0,
            stray_process=0.0,
            stray_max_sec=0.0,
            clock_skew_sec=0.0,
            max_clock_skew_sec=0.0,
        )


@dataclass
class SimConfig:
    agents: int = 3
    stale_sec: float = 120.0
    heartbeat_sec: float = 30.0
    run_seconds: float = 3600.0
    session_min_hold: float = 60.0
    session_max_hold: float = 900.0
    faults: Faults = field(default_factory=Faults)


# ---------------------------------------------------------------------------
# Simulated filesystem: the storage port with faults
# ---------------------------------------------------------------------------


class SimStorage(pl.LockStorage):
    """In-memory filesystem behind ``playtest_lock``'s storage port.

    Models exactly what the real one guarantees and nothing more: writes can
    fail, writes can tear, the process can die between the tmp write and the
    rename, and ``exclusive`` is a mutex that a dying holder releases (an
    flock dies with the process).
    """

    def __init__(self, sim: Simulation, faults: Faults) -> None:
        self.sim = sim
        self.faults = faults
        self.files: dict[str, str] = {}
        self.rng: Rng = sim.rng.stream("storage")
        self.locked_by: str | None = None
        self.actor: str = "sim"
        self.writes = 0
        self.torn = 0
        self.corruptions = 0
        self.externally_corrupted = False
        self.crashes = 0
        self.io_errors = 0
        self.dead: set[str] = set()

    # -- port ----------------------------------------------------------
    def kill(self, actor: str) -> None:
        self.dead.add(actor)

    def take_death(self, actor: str) -> bool:
        """True once per injected death of ``actor``."""
        if actor in self.dead:
            self.dead.discard(actor)
            return True
        return False

    def is_file(self, path: Path) -> bool:
        return str(path) in self.files

    def read_text(self, path: Path) -> str | None:
        return self.files.get(str(path))

    def write_text(self, path: Path, text: str) -> None:
        self.writes += 1
        if self.rng.chance(self.faults.write_error):
            self.io_errors += 1
            self.sim.record(self.actor, "fault_write_error", path=str(path))
            raise SimIOError(f"injected write failure on {path}")
        if self.rng.chance(self.faults.torn_write):
            # Short write, then death. The tmp file is half a record - but it
            # is only ever the *tmp* file, and the rename that would publish
            # it never runs. This is exactly what os.replace buys us, and I3
            # is the assertion that it holds.
            cut = self.rng.randint(0, max(0, len(text) - 1))
            self.files[str(path)] = text[:cut]
            self.torn += 1
            self.sim.record(self.actor, "fault_torn_write", kept=cut, of=len(text))
            self.kill(self.actor)
            raise SimCrash(f"crash mid-write: {path}")
        self.files[str(path)] = text
        if self.rng.chance(self.faults.crash_during_write):
            self.crashes += 1
            self.sim.record(self.actor, "fault_crash_during_write", path=str(path))
            self.kill(self.actor)
            raise SimCrash(f"crash after write, before replace: {path}")

    def corrupt_in_place(self, path: Path) -> bool:
        """A foreign writer mangles the shared lock file.

        Not hypothetical: the same path is documented as shared with the
        Atomic / 7dtd-mods helpers, and a shell script writing it with a
        plain redirect is not atomic. We must survive reading whatever that
        leaves behind, and must still be able to reclaim it.
        """
        body = self.files.get(str(path))
        if not body:
            return False
        cut = self.rng.randint(0, max(0, len(body) - 1))
        self.files[str(path)] = body[:cut]
        self.corruptions += 1
        self.externally_corrupted = True
        self.sim.record("external", "fault_external_corruption", kept=cut)
        return True

    def replace(self, src: Path, dst: Path) -> None:
        body = self.files.pop(str(src), None)
        if body is None:
            raise SimIOError(f"rename source missing: {src}")
        # Atomic by construction: one dict assignment, no intermediate state.
        self.files[str(dst)] = body
        if str(dst) == str(LOCK_PATH):
            # Any publish by our own writer produces a well-formed record, so
            # from here on the file means what it says and the invariants are
            # back in force. That is deliberate: a mangled file is excusable,
            # but confidently publishing a *wrong* well-formed record over it
            # is the bug we want the simulator to catch.
            self.externally_corrupted = False

    def exists(self, path: Path) -> bool:
        return str(path) in self.files

    def unlink(self, path: Path) -> None:
        self.files.pop(str(path), None)

    def mkdir_parents(self, path: Path) -> None:
        return None

    def exclusive(self, path: Path, fn) -> None:
        if self.locked_by is not None:
            # The scheduler never resumes another actor inside a critical
            # section, so this can only mean the model itself is broken.
            raise InvariantViolation(
                f"reentrant critical section: held by {self.locked_by}, "
                f"entered by {self.actor}"
            )
        self.locked_by = self.actor
        try:
            fn()
        finally:
            # An flock dies with its process: a crash must not wedge the lock.
            self.locked_by = None


class SimEnv(pl.LockEnv):
    """Virtual clock + seeded entropy + simulated disk."""

    def __init__(self, sim: Simulation, cfg: SimConfig, storage: SimStorage) -> None:
        super().__init__(storage=storage)
        self.sim = sim
        self.cfg = cfg
        self.storage: SimStorage = storage
        self._entropy = sim.rng.stream("entropy")
        self._pids = sim.rng.stream("pid")
        self.skew: dict[str, float] = {}
        self.actor: str = "sim"

    def now(self) -> float:
        # Per-agent clock skew: agents do not share a perfect clock, and the
        # stale rule compares one agent's stamp against another's read.
        return self.sim.clock.now() + self.skew.get(self.actor, 0.0)

    def token_hex(self, nbytes: int) -> str:
        return self._entropy.hex(nbytes)

    def pid(self) -> int:
        return self._pids.randint(2, 65535)

    def stale_sec(self) -> float:
        return self.cfg.stale_sec

    def heartbeat_interval_sec(self) -> float:
        return self.cfg.heartbeat_sec

    def bind(self, actor: str) -> None:
        """Point the clock/entropy at whichever agent is currently running."""
        self.actor = actor
        self.storage.actor = actor


# ---------------------------------------------------------------------------
# World: the shared runtime the lock is protecting
# ---------------------------------------------------------------------------


class World:
    """Ground truth about game processes and who believes they hold the lock."""

    def __init__(self, sim: Simulation) -> None:
        self.sim = sim
        self.runtime_up: set[str] = set()  # owners with client/server processes
        self.holders: set[str] = set()  # agents that think they hold the lock
        self.holder_sessions: dict[str, str] = {}
        # A client started by hand outside any orchestrator. It has a
        # lifetime: a permanent stray would wedge every agent forever and
        # collapse the state space we are trying to explore.
        self.stray_until = 0.0
        self.max_concurrent_runtime = 0
        self.takeovers = 0
        self.acquires = 0
        self.refusals: dict[str, int] = {}

    @property
    def stray(self) -> bool:
        return self.sim.clock.now() < self.stray_until

    def live_probe(self) -> bool:
        return self.stray or bool(self.runtime_up)

    def start_runtime(self, agent: str) -> None:
        self.runtime_up.add(agent)
        self.max_concurrent_runtime = max(
            self.max_concurrent_runtime, len(self.runtime_up)
        )
        self.sim.record(agent, "runtime_up", count=len(self.runtime_up))

    def stop_runtime(self, agent: str) -> None:
        if agent in self.runtime_up:
            self.runtime_up.discard(agent)
            self.sim.record(agent, "runtime_down", count=len(self.runtime_up))

    def note_refusal(self, reason: str) -> None:
        self.sim.coverage.add(f"refusal/{reason}")
        self.refusals[reason] = self.refusals.get(reason, 0) + 1


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class Agent:
    """One orchestrator run: acquire, hold with heartbeats, release.

    Failure modes modeled: refusal and backoff, crash before/after launching
    the runtime, and hang (alive, processes up, heartbeat stopped) - the case
    the stale rule exists for.
    """

    def __init__(
        self,
        name: str,
        sim: Simulation,
        env: SimEnv,
        world: World,
        cfg: SimConfig,
    ) -> None:
        self.name = name
        self.sim = sim
        self.env = env
        self.world = world
        self.cfg = cfg
        self.rng = sim.rng.stream(f"agent/{name}")
        self.session = pl.new_session_id("sim", env=self._bound_env())
        self.previous_session: str | None = None
        self.attempts = 0
        self.completed = 0
        self.crashed = 0

    def _bound_env(self) -> SimEnv:
        self.env.bind(self.name)
        return self.env

    def _died(self) -> bool:
        """Did a fault just kill this process? Never inferable from a caught
        exception: the code under test is allowed to swallow those."""
        return self.env.storage.take_death(self.name)

    # -- lifecycle -----------------------------------------------------
    def run(self) -> Iterator[float]:
        f = self.cfg.faults
        while True:
            yield self.rng.uniform(0.0, 30.0)
            self._bound_env()
            if self.rng.chance(f.stray_process) and not self.world.stray:
                # Someone started a client by hand outside any orchestrator.
                span = self.rng.uniform(30.0, max(31.0, f.stray_max_sec))
                self.world.stray_until = self.sim.clock.now() + span
                self.sim.record(self.name, "stray_process", seconds=round(span, 3))
            if self.rng.chance(f.stale_release):
                self._stale_release()
            if self.rng.chance(f.external_corruption):
                self.env.storage.corrupt_in_place(LOCK_PATH)
            self.attempts += 1
            try:
                state = pl.acquire(
                    self.session,
                    path=LOCK_PATH,
                    live_probe=self.world.live_probe,
                    env=self.env,
                )
            except pl.PlaytestLockError as ex:
                self.world.note_refusal(ex.reason)
                self.sim.record(
                    self.name, "acquire_refused", reason=ex.reason, held_by=ex.held_by
                )
                yield self.rng.uniform(5.0, 60.0)
                continue
            except SimCrash:
                # Died mid-write. No runtime was up yet, so nothing to clean.
                self._died()
                self.crashed += 1
                self.sim.record(self.name, "crash_in_acquire")
                yield self.rng.uniform(30.0, 120.0)
                self._rotate_session()
                continue
            except SimIOError:
                self.sim.record(self.name, "acquire_io_error")
                yield self.rng.uniform(5.0, 60.0)
                continue

            # Paired assertion, acquire side: the claim we just published must
            # name us before we are allowed to touch the shared runtime.
            if not (state.running and state.session == self.session):
                raise InvariantViolation(
                    f"{self.name}: acquire returned running={state.running} "
                    f"session={state.session!r}, expected own session"
                )
            if self.world.runtime_up:
                raise InvariantViolation(
                    f"{self.name}: acquired while runtime owned by "
                    f"{sorted(self.world.runtime_up)} was still up"
                )
            self.world.acquires += 1
            self.world.holders.add(self.name)
            self.world.holder_sessions[self.name] = self.session
            yield from self._hold()

    def _hold(self) -> Iterator[float]:
        f = self.cfg.faults
        loop = pl.HeartbeatLoop(
            self.session,
            path=LOCK_PATH,
            interval_sec=self.cfg.heartbeat_sec,
            env=self.env,
        )
        self.world.start_runtime(self.name)
        hold_for = self.rng.uniform(self.cfg.session_min_hold, self.cfg.session_max_hold)
        end_at = self.sim.clock.now() + hold_for
        self.sim.record(self.name, "hold_start", seconds=round(hold_for, 3))

        while self.sim.clock.now() < end_at:
            step = min(self.cfg.heartbeat_sec, max(1.0, end_at - self.sim.clock.now()))
            yield step
            self._bound_env()
            if self.rng.chance(f.agent_hang):
                # Alive, processes still up, heartbeat stopped: the lock will
                # go stale but takeover must stay blocked (stale_but_live).
                self.sim.record(self.name, "hang_start")
                yield self.rng.uniform(self.cfg.stale_sec, self.cfg.stale_sec * 3)
                self._bound_env()
                self.sim.record(self.name, "hang_end")
            if self.rng.chance(f.agent_crash):
                self._die("crash_holding")
                return
            try:
                loop.tick(self.sim.clock.now())
            except SimCrash:
                pass
            if self._died():
                self._die("crash_in_heartbeat")
                return
        yield from self._finish()

    def _finish(self) -> Iterator[float]:
        self._bound_env()
        self.world.stop_runtime(self.name)
        self.world.holders.discard(self.name)
        self.world.holder_sessions.pop(self.name, None)
        for _ in range(3):
            try:
                pl.release(self.session, path=LOCK_PATH, env=self.env)
                break
            except SimCrash:
                # Crashed while releasing: processes are already down, so the
                # lock is reclaimable once it goes stale. Nothing to retry.
                self._died()
                self.crashed += 1
                self.sim.record(self.name, "crash_in_release")
                break
            except (SimIOError, pl.PlaytestLockError) as ex:
                self.sim.record(self.name, "release_retry", error=type(ex).__name__)
                yield 5.0
                self._bound_env()
        self.completed += 1
        self.sim.record(self.name, "released")
        self._rotate_session()
        yield self.rng.uniform(10.0, 120.0)

    def _stale_release(self) -> None:
        """A late exit handler releasing with a session we no longer own.

        Real orchestrators do this: the shutdown path runs after a takeover,
        or a helper script is invoked with a stale id from a log. Release
        must refuse it. If it does not, a live holder loses its claim.
        """
        old = self.previous_session
        if not old or old == self.session:
            return
        self._bound_env()
        try:
            pl.release(old, path=LOCK_PATH, env=self.env)
            self.sim.record(self.name, "stale_release_accepted", session=old)
        except pl.PlaytestLockError as ex:
            self.sim.record(self.name, "stale_release_refused", reason=ex.reason)
        except (SimCrash, SimIOError):
            self._died()
            self.sim.record(self.name, "stale_release_error")

    def _rotate_session(self) -> None:
        self.previous_session = self.session
        self.session = pl.new_session_id("sim", env=self._bound_env())

    def _die(self, kind: str) -> None:
        """Process death: runtime processes die with it, lock file stays."""
        self.crashed += 1
        self.world.stop_runtime(self.name)
        self.world.holders.discard(self.name)
        self.world.holder_sessions.pop(self.name, None)
        self.sim.record(self.name, kind)
        self._rotate_session()


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def install_invariants(sim: Simulation, world: World, storage: SimStorage) -> None:
    """Install the properties checked after **every** scheduler step.

    Each one encodes a belief about the design, written down before the
    simulator was pointed at it. The simulator's job is to find holes in
    these beliefs, not to substitute for having them.
    """

    known_sessions: set[str] = set()
    last_heartbeat: dict[str, tuple[str | None, float | None]] = {}
    sim.known_sessions = known_sessions  # type: ignore[attr-defined]

    def _state():
        """Read the lock on a canonical clock, not a skewed agent's."""
        env = pl.current_env()
        if isinstance(env, SimEnv):
            env.bind("sim")
        return pl.read_lock(LOCK_PATH, env=env)

    def i1_runtime_exclusivity(_s: Simulation) -> None:
        # The safety property the whole lock exists for. Two live runtimes
        # means duplicate dedicated servers, a port fight, and one run's
        # clean_processes killing the other's client.
        if len(world.runtime_up) > 1:
            raise InvariantViolation(
                f"two agents hold live runtime at once: {sorted(world.runtime_up)}"
            )

    def i2_single_believed_holder(_s: Simulation) -> None:
        # Pairs with the acquire-side check in Agent.run: the acquiring agent
        # asserts before it starts processes, the world asserts after every
        # step. A second believer means the takeover rule let someone in.
        if len(world.holders) > 1:
            raise InvariantViolation(
                f"two agents believe they hold the lock: {sorted(world.holders)}"
            )

    def i3_no_torn_claim(_s: Simulation) -> None:
        # Pairs with the torn-write injection in SimStorage.write_text. Our
        # own writer publishes through os.replace, so a crash mid-write must
        # never be visible: readers see the old payload or the new one.
        if storage.externally_corrupted:
            return
        state = _state()
        if state.running and not state.session:
            raise InvariantViolation("lock claims running=yes with no session")
        if state.running and not state.heartbeat:
            raise InvariantViolation(
                f"lock claims running=yes for {state.session} with no heartbeat"
            )

    def i4_known_session(_s: Simulation) -> None:
        # Only a session that actually asked for the lock may be named in it.
        # Skipped while a foreign writer has mangled the file: that case is
        # covered by i6 (we must recover from it), not by this one.
        if storage.externally_corrupted:
            return
        state = _state()
        if state.running and state.session and state.session not in known_sessions:
            raise InvariantViolation(f"lock names unknown session {state.session!r}")

    def i5_heartbeat_monotonic(_s: Simulation) -> None:
        if storage.externally_corrupted:
            return
        state = _state()
        if not (state.running and state.session):
            return
        ep = state.heartbeat_epoch
        if ep is None:
            return
        prev_session, prev_ep = last_heartbeat.get("v", (None, None))
        if prev_session == state.session and prev_ep is not None and ep < prev_ep:
            raise InvariantViolation(
                f"heartbeat moved backwards for {state.session}: {prev_ep} -> {ep}"
            )
        last_heartbeat["v"] = (state.session, ep)

    def i6_stale_lock_is_reclaimable(_s: Simulation) -> None:
        # Liveness written as a step-checkable safety property: a claim whose
        # holder is gone (stale heartbeat, no live runtime) must not wedge the
        # machine. This is what makes corruption survivable rather than fatal,
        # so it deliberately still runs while externally_corrupted is set.
        env = pl.current_env()
        if isinstance(env, SimEnv):
            env.bind("sim")
        state = _state()
        if not state.running or world.live_probe():
            return
        if not pl.is_stale(state, env=env):
            return
        if not pl.can_start(
            "simprobe-00000000-000000-0000",
            path=LOCK_PATH,
            live_probe=lambda: False,
            env=env,
        ):
            raise InvariantViolation(
                f"stale claim by {state.session!r} (heartbeat={state.heartbeat!r}) "
                "is not reclaimable with no live runtime"
            )

    def i7_holder_claim_persists(_s: Simulation) -> None:
        # Paired with I2 on the other side of the boundary: I2 says nobody
        # else believes they hold it, I7 says the durable record still agrees
        # with the one who does. A release accepted from a foreign session,
        # or a lost write, shows up here and nowhere else.
        if storage.externally_corrupted or not world.holders:
            return
        state = _state()
        for agent, session in sorted(world.holder_sessions.items()):
            if not state.running or state.session != session:
                raise InvariantViolation(
                    f"{agent} holds session={session} but lock reads "
                    f"running={state.running} session={state.session!r}"
                )

    sim.add_invariant("I1_runtime_exclusivity", i1_runtime_exclusivity)
    sim.add_invariant("I2_single_believed_holder", i2_single_believed_holder)
    sim.add_invariant("I3_no_torn_claim", i3_no_torn_claim)
    sim.add_invariant("I4_known_session", i4_known_session)
    sim.add_invariant("I5_heartbeat_monotonic", i5_heartbeat_monotonic)
    sim.add_invariant("I6_stale_lock_reclaimable", i6_stale_lock_is_reclaimable)
    sim.add_invariant("I7_holder_claim_persists", i7_holder_claim_persists)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@dataclass
class SimResult:
    seed: int
    digest: str
    steps: int
    elapsed: float
    acquires: int
    refusals: dict[str, int]
    writes: int
    torn: int
    crashes: int
    io_errors: int
    max_concurrent_runtime: int
    violation: str | None = None
    coverage: set[str] = field(default_factory=set)
    # Full event history, kept only for failing runs: this is what a
    # divergent replay gets diffed against.
    trace_lines: list[str] = field(default_factory=list)


def run_simulation(seed: int, cfg: SimConfig | None = None) -> SimResult:
    """Run one seeded simulation. Pure function of (seed, cfg)."""
    cfg = cfg or SimConfig()
    sim = Simulation(seed)
    storage = SimStorage(sim, cfg.faults)
    env = SimEnv(sim, cfg, storage)
    world = World(sim)
    install_invariants(sim, world, storage)

    previous = pl.set_env(env)
    try:
        skew_rng = sim.rng.stream("skew")
        agents = []
        for i in range(cfg.agents):
            name = f"agent{i}"
            if cfg.faults.max_clock_skew_sec > 0 and skew_rng.chance(
                cfg.faults.clock_skew_sec
            ):
                env.skew[name] = skew_rng.uniform(
                    -cfg.faults.max_clock_skew_sec, cfg.faults.max_clock_skew_sec
                )
            agents.append(Agent(name, sim, env, world, cfg))
        for a in agents:
            sim.known_sessions.add(a.session)  # type: ignore[attr-defined]

        # Sessions rotate after each crash/release; keep I4's set current.
        original_new_session = pl.new_session_id

        def tracking_new_session(prefix: str = "playtest", *, env=None) -> str:
            sid = original_new_session(prefix, env=env)
            sim.known_sessions.add(sid)  # type: ignore[attr-defined]
            return sid

        pl.new_session_id = tracking_new_session  # type: ignore[assignment]
        try:
            for i, a in enumerate(agents):
                sim.spawn(a.name, a.run(), delay=i * 0.5)
            violation: str | None = None
            try:
                sim.run(until=cfg.run_seconds)
            except InvariantViolation as ex:
                violation = str(ex)
        finally:
            pl.new_session_id = original_new_session  # type: ignore[assignment]
    finally:
        pl.set_env(previous)

    return SimResult(
        seed=seed,
        digest=sim.trace.digest(),
        steps=sim.steps,
        elapsed=sim.clock.elapsed,
        acquires=world.acquires,
        refusals=dict(sorted(world.refusals.items())),
        writes=storage.writes,
        torn=storage.torn,
        crashes=storage.crashes,
        io_errors=storage.io_errors,
        max_concurrent_runtime=world.max_concurrent_runtime,
        violation=violation,
        coverage=set(sim.coverage),
        trace_lines=sim.trace.lines() if violation else [],
    )
