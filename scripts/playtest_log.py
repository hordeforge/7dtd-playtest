#!/usr/bin/env python3
"""Parser for the [7dtd-playtest] client log contract.

Single home of the stable log tokens documented in AGENTS.md: result,
SUMMARY, DONE, JSON event lines, NRE-like hits, and barrier greps. Shared by
the orchestrator (playtest_run.py), the comparison tool
(playtest_compare.py), and the offline gates; importing this module must not
pull in process management or any other orchestrator machinery.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, TypedDict

# Contract lines are located structurally, not by matching their payload with
# regexes: a line is a contract line only when `[7dtd-playtest]` is its first
# bracketed token. The mod emits each contract line through the game's own
# logger, which prefixes every line with a timestamp, game-time and level
# before the tag ("2026-08-25T11:44:24 56.401 INF [7dtd-playtest] ..."), and
# chat/game lines carry their own tag first. Requiring the marker to be the
# first bracket accepts the game's prefix while keeping a chat message that
# merely contains the marker from forging results, SUMMARY/DONE verdicts, JSON
# events or barrier fires (client-log bytes are attacker-reachable through
# remote LAN chat). The payload after the marker is parsed structurally: JSON
# via json.loads, human lines by whitespace tokens.
MARKER = "[7dtd-playtest]"


def _contract_tail(line: str) -> str | None:
    """Text after the marker when it is the line's first bracketed token.

    ``None`` for every other line: game/chat lines with their own tag, and
    lines with no tag at all, cannot forge a contract line.
    """
    start = line.find("[")
    if start < 0 or not line.startswith(MARKER, start):
        return None
    return line[start + len(MARKER):].strip()


def _key_values(tokens: list[str]) -> dict[str, str]:
    """``key=value`` tokens of a human contract line, as a dict."""
    out: dict[str, str] = {}
    for tok in tokens:
        if "=" in tok:
            key, value = tok.split("=", 1)
            out[key] = value
    return out


NRE_RE = re.compile(r"NullReferenceException|NCSimple|underrun|IndexOutOfRange", re.IGNORECASE)

NRE_SAMPLE_CAP = 50


class ParsedClientLog(TypedDict):
    """Shape of :meth:`ClientLogScan.result` / :func:`parse_client_log`.

    Single home of the parsed-log contract shared by the orchestrator, the
    comparison tool, and the offline gates. ``json_events`` entries are
    whatever JSON objects the client emitted; every other field is coerced
    by the parser to the types shown here.
    """

    results: list[dict[str, str]]
    summary: dict[str, int] | None
    done: dict[str, int | None] | None
    json_events: list[dict[str, object]]
    nre_like: list[str]
    nre_like_total: int
    malformed_events: int


def empty_client_log() -> ParsedClientLog:
    """Placeholder before any log bytes exist; same shape as :meth:`ClientLogScan.result`."""
    return {
        "results": [],
        "summary": None,
        "done": None,
        "json_events": [],
        "nre_like": [],
        "nre_like_total": 0,
        "malformed_events": 0,
    }


def barrier_hits_prefix(blob: str, prefix: str) -> list[str]:
    """Return every full barrier name that starts with ``prefix``.

    Repeated lines are significant: providers may request several fixtures of
    the same class during one composed run. Consumers keep their own fired
    counts or token sets, so collapsing identical names here loses events.
    """
    hits: list[str] = []
    for line in blob.splitlines():
        tail = _contract_tail(line)
        if tail is None or not tail.startswith("barrier "):
            continue
        name = tail[len("barrier "):].split()[0] if tail[len("barrier "):] else ""
        if name.startswith(prefix):
            hits.append(name)
    return hits


def barrier_line_hits(blob: str, name: str) -> int:
    """Count human `barrier <name>` lines in ``blob`` (whole-name match).

    Only Report.Barrier emissions may count toward servicing an admin action,
    never a game/chat/mod line that merely contains the words: the marker must
    be the line's first bracketed token (see :func:`_contract_tail`).

    Report.Barrier also emits JSON with the same name; summing both
    double-fires handlers (e.g. kills bots). The whole-name match keeps
    "spawn_vehicle" from also counting parameterised "spawn_vehicle:<class>"
    lines, which are collected separately via barrier_hits_prefix.
    """
    count = 0
    for line in blob.splitlines():
        tail = _contract_tail(line)
        if tail is None or not tail.startswith("barrier "):
            continue
        tokens = tail.split()
        if len(tokens) >= 2 and tokens[1] == name:
            count += 1
    return count


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


class ClientLogScan:
    """Incremental equivalent of :func:`parse_client_log`.

    The orchestrator polls a growing client log at ~2 Hz for up to tens of
    minutes; feeding only newly appended lines through the same line parser
    keeps that loop O(new bytes) instead of re-parsing the whole file every
    poll (quadratic in run length, on the same CPU as the game under test).
    :meth:`result` returns exactly what :func:`parse_client_log` would return
    for the concatenation of everything fed so far; both share one line
    parser so they cannot drift.
    """

    def __init__(self) -> None:
        self.human_results: list[dict[str, str]] = []
        self.json_results: list[dict[str, str]] = []
        self.json_events: list[dict[str, object]] = []
        self.json_summary: dict[str, int] | None = None
        self.json_done: dict[str, int | None] | None = None
        self.human_summary: dict[str, int] | None = None
        self.human_done: dict[str, int | None] | None = None
        self.nre_hits: list[str] = []
        self.nre_total = 0
        # Lines that looked like events but failed to parse. Skipped on
        # purpose; the count is the only trace they leave.
        self.malformed_events = 0

    def feed_line(self, line: str) -> None:
        tail = _contract_tail(line)
        if tail is None:
            return
        if tail.startswith("{"):
            try:
                ev = json.loads(tail)
                # The client log carries arbitrary game/chat lines; a line that
                # merely looks like an event must not crash the parser.
                if not isinstance(ev, dict):
                    raise ValueError("event is not a JSON object")
                self.json_events.append(ev)
                if ev.get("t") == "result":
                    # Crafted lines may put any JSON value where a scalar
                    # belongs. Coerce status/detail to str so .upper() and
                    # downstream string consumers cannot raise.
                    status = ev.get("status", "")
                    detail = ev.get("detail", "")
                    self.json_results.append(
                        {
                            "status": str(status).upper(),
                            "case": f"{ev.get('suite', '')}/{ev.get('case', '')}",
                            "detail": (
                                detail
                                if isinstance(detail, str)
                                else "" if detail is None else str(detail)
                            ),
                        }
                    )
                elif ev.get("t") == "summary":
                    self.json_summary = {
                        "pass": int(ev.get("pass", 0)),
                        "fail": int(ev.get("fail", 0)),
                        "skip": int(ev.get("skip", 0)),
                    }
                elif ev.get("t") == "done":
                    self.json_done = {"exit_hint": int(ev.get("exit_hint", 1))}
            except (TypeError, ValueError, OverflowError):
                # JSONDecodeError subclasses ValueError; a non-numeric count
                # or exit_hint raises it too. A JSON null (or list) where a
                # number belongs raises TypeError, and int(inf) from a 1e999
                # or bare Infinity token raises OverflowError. Skip the bad
                # event, keep the rest; count it so discarded evidence is
                # visible in the report instead of vanishing.
                self.malformed_events += 1
            return

        tokens = tail.split()
        if not tokens:
            return
        head = tokens[0]
        if head in ("PASS", "FAIL", "SKIP"):
            self.human_results.append(
                {
                    "status": head,
                    "case": tokens[1] if len(tokens) > 1 else "",
                    "detail": " ".join(tokens[2:]),
                }
            )
            return
        if head == "SUMMARY":
            counts = _key_values(tokens[1:])
            if "pass" in counts and "fail" in counts:
                # Non-numeric counts are not a summary; keep the rest of the log.
                with contextlib.suppress(ValueError):
                    self.human_summary = {
                        "pass": int(counts["pass"]),
                        "fail": int(counts["fail"]),
                        "skip": int(counts.get("skip", 0)),
                    }
            return
        if head == "DONE":
            hint: int | None = None
            for tok in tokens[1:]:
                if tok.startswith("exit_hint="):
                    try:
                        hint = int(tok.split("=", 1)[1])
                    except ValueError:
                        hint = None
                    break
            self.human_done = {"exit_hint": hint}

    def _count_nre(self, line: str) -> None:
        if NRE_RE.search(line):
            self.nre_total += 1
            if len(self.nre_hits) < NRE_SAMPLE_CAP:
                self.nre_hits.append(line)

    def feed_lines(self, lines: Iterable[str]) -> None:
        """Parse already-split complete lines (see LogTail) in one pass.

        Per line this is :meth:`feed_line` plus the NRE scan, without
        splitting (and re-iterating) the same bytes twice on the
        orchestrator's ~2 Hz poll path.
        """
        for line in lines:
            self.feed_line(line)
            self._count_nre(line)

    def result(self) -> ParsedClientLog:
        if self.json_results:
            results = self.json_results
            summary = self.json_summary or self.human_summary
            done = self.json_done or self.human_done
        else:
            results = self.human_results
            summary = self.human_summary
            done = self.human_done

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
            "json_events": self.json_events,
            "nre_like": self.nre_hits[:NRE_SAMPLE_CAP],
            "nre_like_total": self.nre_total,
            "malformed_events": self.malformed_events,
        }


def parse_client_log(text: str) -> ParsedClientLog:
    """Parse a whole playtest log at once. Prefer JSON events when present
    (avoid double human+JSON). Incremental consumers should use
    :class:`ClientLogScan` instead of re-running this over the full text."""
    scan = ClientLogScan()
    scan.feed_lines(text.splitlines())
    return scan.result()


class TailSource(Protocol):
    """Structural consumer view of :class:`LogTail` (poll-only)."""

    def poll(self) -> str:
        """Return only bytes appended since the previous call."""
        ...


class LogTail:
    """Incremental reader for an append-only UTF-8 log.

    Returns only bytes appended since the previous :meth:`poll`, so polling
    loops stay O(new bytes) instead of re-reading a growing game log every
    interval. Only complete newline-terminated lines are returned; a trailing
    partial line stays buffered until its newline arrives, so pattern matches
    never see half a line and nothing is counted twice. If the file shrank
    (truncated before a restart), reading restarts from zero. Decoding happens
    per completed line, so a multi-byte character split across polls stays
    intact inside the byte buffer.

    ``from_end`` starts the tail at the file's current size instead of zero:
    for a log that must not be truncated (its previous generation could not
    be preserved), only bytes appended after construction are returned.

    ``generations`` counts detected shrinks (truncation before a restart).
    A consumer that accumulates parsed state across polls must reset it when
    this advances, or events from the previous generation would answer for
    the new one.
    """

    def __init__(self, path: Path, *, from_end: bool = False) -> None:
        self._path = path
        if from_end:
            try:
                self._offset = path.stat().st_size
            except OSError:
                self._offset = 0
        else:
            self._offset = 0
        self._pending = b""
        self.generations = 0

    def poll(self) -> str:
        """Return newly appended complete-line text since the previous call."""
        try:
            size = self._path.stat().st_size
        except OSError:
            return ""
        if size < self._offset:
            self._offset = 0
            self._pending = b""
            self.generations += 1
        if size <= self._offset:
            return ""
        try:
            with self._path.open("rb") as fh:
                fh.seek(self._offset)
                raw = fh.read(size - self._offset)
        except OSError:
            return ""
        if not raw:
            return ""
        self._offset += len(raw)
        buf = self._pending + raw
        cut = buf.rfind(b"\n")
        if cut < 0:
            self._pending = buf
            return ""
        self._pending = buf[cut + 1 :]
        return buf[: cut + 1].decode("utf-8", errors="replace")
