#!/usr/bin/env python3
"""Deterministic simulation core (FoundationDB / TigerBeetle style).

One seed determines an entire run: every clock read, every random choice,
every fault, and the interleaving of every actor. A failing run prints its
seed, and re-running that seed reproduces the same event trace byte for byte.

Three pieces:

* :class:`Rng` - the single seeded randomness source. Consumers take a *named
  substream* so that adding a new consumer does not shift the draws of the
  existing ones (a run stays reproducible across code changes that only add
  call sites).
* :class:`VirtualClock` - simulated time. Nothing sleeps; time advances only
  when the scheduler decides it does, so a 30 minute soak runs in
  milliseconds and timeouts are exact rather than flaky.
* :class:`Simulation` - a single-threaded cooperative scheduler. Actors are
  generators that yield the delay they want to wait. Ties are broken by
  (time, sequence), never by OS scheduling, so the interleaving is a pure
  function of the seed.

This module knows nothing about the playtest lock; see ``dst_sim.py`` for the
system model, and ``dst_run.py`` for the seeded runner.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

MAX_SEED = 2**64 - 1


def derive_seed(seed: int, label: str) -> int:
    """Stable child seed. Uses sha256, not hash(), which is PYTHONHASHSEED
    dependent and would make runs irreproducible across processes."""
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


class Rng:
    """The one seeded randomness source. No module-level ``random`` anywhere."""

    def __init__(self, seed: int, label: str = "root") -> None:
        self.seed = int(seed) & MAX_SEED
        self.label = label
        self._r = random.Random(self.seed)
        self._streams: dict[str, Rng] = {}

    def stream(self, name: str) -> Rng:
        """Named substream, created lazily and cached.

        Independent of draw order in other streams: two runs of the same seed
        give the same values even if an unrelated stream is consumed more.
        """
        got = self._streams.get(name)
        if got is None:
            got = Rng(derive_seed(self.seed, f"{self.label}/{name}"), name)
            self._streams[name] = got
        return got

    def uniform(self, lo: float, hi: float) -> float:
        return self._r.uniform(lo, hi)

    def randint(self, lo: int, hi: int) -> int:
        return self._r.randint(lo, hi)

    def chance(self, probability: float) -> bool:
        """True with the given probability. The only fault-injection coin."""
        if probability <= 0.0:
            return False
        if probability >= 1.0:
            return True
        return self._r.random() < probability

    def hex(self, nbytes: int) -> str:
        return "".join(f"{self._r.randrange(256):02x}" for _ in range(max(0, nbytes)))


class VirtualClock:
    """Simulated epoch clock. Only the scheduler moves it forward."""

    def __init__(self, start_epoch: float = 1_800_000_000.0) -> None:
        self._now = float(start_epoch)
        self.start_epoch = float(start_epoch)

    def now(self) -> float:
        return self._now

    def advance_to(self, epoch: float) -> None:
        if epoch < self._now:
            raise InvariantViolation(
                f"virtual clock moved backwards: {self._now} -> {epoch}"
            )
        self._now = float(epoch)


class InvariantViolation(AssertionError):
    """An always-true property of the model did not hold."""


@dataclass
class TraceEvent:
    t: float
    actor: str
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> str:
        return json.dumps(
            {
                "t": round(self.t, 6),
                "actor": self.actor,
                "kind": self.kind,
                "detail": self.detail,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )


class Trace:
    """Recorded action history. Two runs of one seed must produce one digest;
    a divergent replay is diffed line by line against this."""

    def __init__(self, limit: int = 200_000) -> None:
        self.events: list[TraceEvent] = []
        self.limit = limit

    def record(self, t: float, actor: str, kind: str, **detail: Any) -> TraceEvent:
        ev = TraceEvent(t=t, actor=actor, kind=kind, detail=detail)
        if len(self.events) < self.limit:
            self.events.append(ev)
        return ev

    def digest(self) -> str:
        h = hashlib.sha256()
        for ev in self.events:
            h.update(ev.canonical().encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()

    def lines(self) -> list[str]:
        return [ev.canonical() for ev in self.events]


@dataclass(order=True)
class _Scheduled:
    at: float
    seq: int
    actor_name: str = field(compare=False)
    gen: Iterator[Any] = field(compare=False)


class Simulation:
    """Deterministic single-threaded scheduler.

    Actors are generators. ``yield <float>`` waits that many simulated
    seconds; ``yield 0`` yields to any actor due at the same instant. All
    ordering comes from (time, spawn/resume sequence), never from the OS.
    """

    def __init__(
        self,
        seed: int,
        *,
        clock: VirtualClock | None = None,
        trace: Trace | None = None,
    ) -> None:
        self.rng = Rng(seed)
        self.clock = clock or VirtualClock()
        self.trace = trace or Trace()
        self._queue: list[_Scheduled] = []
        self._seq = 0
        self._invariants: list[tuple[str, Callable[[Simulation], None]]] = []
        self.steps = 0
        self.actors_finished = 0
        # Which modeled situations this run actually reached. A seed count is
        # meaningless without knowing the scenarios were hit at all.
        self.coverage: set[str] = set()

    # -- wiring ---------------------------------------------------------
    def record(self, actor: str, kind: str, **detail: Any) -> None:
        self.coverage.add(kind)
        self.trace.record(self.clock.now(), actor, kind, **detail)

    def spawn(self, name: str, gen: Iterator[Any], *, delay: float = 0.0) -> None:
        self._push(self.clock.now() + max(0.0, delay), name, gen)
        self.record("sim", "spawn", spawned=name, delay=delay)

    def add_invariant(self, name: str, fn: Callable[[Simulation], None]) -> None:
        """Checked after every scheduler step, not only at the end."""
        self._invariants.append((name, fn))

    def _push(self, at: float, name: str, gen: Iterator[Any]) -> None:
        heapq.heappush(self._queue, _Scheduled(at, self._seq, name, gen))
        self._seq += 1

    # -- driving --------------------------------------------------------
    def check_invariants(self) -> None:
        for name, fn in self._invariants:
            try:
                fn(self)
            except InvariantViolation as ex:
                self.record("sim", "invariant_violated", invariant=name, detail=str(ex))
                raise InvariantViolation(f"[{name}] {ex}") from ex

    def run(self, *, until: float | None = None, max_steps: int = 1_000_000) -> None:
        """Run until the queue drains, ``until`` simulated seconds elapse, or
        ``max_steps`` resumes happen (a runaway actor must not hang CI)."""
        deadline = None if until is None else self.clock.start_epoch + until
        self.check_invariants()
        while self._queue:
            if self.steps >= max_steps:
                self.record("sim", "max_steps", steps=self.steps)
                return
            item = heapq.heappop(self._queue)
            if deadline is not None and item.at > deadline:
                self.clock.advance_to(deadline)
                self.record("sim", "deadline", at=deadline)
                return
            self.clock.advance_to(item.at)
            self.steps += 1
            try:
                delay = next(item.gen)
            except InvariantViolation as ex:
                # An actor-side assertion. Record it so the dumped trace ends
                # on the failure the same way a checker violation does.
                self.record(item.actor_name, "invariant_violated", detail=str(ex))
                raise
            except StopIteration:
                self.actors_finished += 1
                self.record(item.actor_name, "actor_done")
                self.check_invariants()
                continue
            self.check_invariants()
            wait = 0.0 if delay is None else float(delay)
            if wait < 0.0:
                raise InvariantViolation(
                    f"actor {item.actor_name} yielded negative delay {wait}"
                )
            self._push(self.clock.now() + wait, item.actor_name, item.gen)
