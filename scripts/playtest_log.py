#!/usr/bin/env python3
"""Parser for the [7dtd-playtest] client log contract.

Single home of the stable log tokens documented in AGENTS.md: result,
SUMMARY, DONE, JSON event lines, NRE-like hits, and barrier greps. Shared by
the orchestrator (playtest_run.py), the comparison tool
(playtest_compare.py), and the offline gates; importing this module must not
pull in process management or any other orchestrator machinery.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

RESULT_RE = re.compile(r"\[7dtd-playtest\]\s+(PASS|FAIL|SKIP)\s+(\S+)\s*(.*)$")
SUMMARY_RE = re.compile(
    r"\[7dtd-playtest\]\s+SUMMARY\s+pass=(\d+)\s+fail=(\d+)(?:\s+skip=(\d+))?"
)
DONE_RE = re.compile(r"\[7dtd-playtest\]\s+DONE(?:\s+exit_hint=(\d+))?")
JSON_RE = re.compile(r"\[7dtd-playtest\]\s+(\{.*\})\s*$")
NRE_RE = re.compile(r"NullReferenceException|NCSimple|underrun|IndexOutOfRange", re.IGNORECASE)

NRE_SAMPLE_CAP = 50


def barrier_hits_prefix(blob: str, prefix: str) -> list[str]:
    """Return every full barrier name that starts with ``prefix``.

    Repeated lines are significant: providers may request several fixtures of
    the same class during one composed run. Consumers keep their own fired
    counts or token sets, so collapsing identical names here loses events.
    """
    return [
        match.group(1)
        for match in re.finditer(
            rf"\[7dtd-playtest\]\s+barrier\s+({re.escape(prefix)}[^\s\"]*)",
            blob,
        )
    ]


def barrier_line_hits(blob: str, name: str) -> int:
    """Count human `barrier <name>` lines in ``blob`` (whole-name match).

    Report.Barrier also emits JSON with the same name; summing both
    double-fires handlers (e.g. kills bots). The whole-name match keeps
    "spawn_vehicle" from also counting parameterised "spawn_vehicle:<class>"
    lines, which are collected separately via barrier_hits_prefix.
    """
    return len(re.findall(rf"barrier {re.escape(name)}(?![\w:])", blob))


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
        self.human_results: list[dict] = []
        self.json_results: list[dict] = []
        self.json_events: list[dict] = []
        self.json_summary: dict | None = None
        self.json_done: dict | None = None
        self.human_summary: dict | None = None
        self.human_done: dict | None = None
        self.nre_hits: list[str] = []
        self.nre_total = 0
        # Lines that looked like events but failed to parse. Skipped on
        # purpose; the count is the only trace they leave.
        self.malformed_events = 0

    def feed_line(self, line: str) -> None:
        m = JSON_RE.search(line)
        if m:
            try:
                ev = json.loads(m.group(1))
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

        m = RESULT_RE.search(line)
        if m:
            self.human_results.append(
                {
                    "status": m.group(1),
                    "case": m.group(2),
                    "detail": (m.group(3) or "").strip(),
                }
            )
            return
        m = SUMMARY_RE.search(line)
        if m:
            self.human_summary = {
                "pass": int(m.group(1)),
                "fail": int(m.group(2)),
                "skip": int(m.group(3) or 0),
            }
            return
        m = DONE_RE.search(line)
        if m:
            hint = int(m.group(1)) if m.group(1) is not None else None
            self.human_done = {"exit_hint": hint}

    def feed_chunk(self, chunk: str) -> None:
        """Feed text made of complete newline-terminated lines (see LogTail)."""
        for line in chunk.splitlines():
            if NRE_RE.search(line):
                self.nre_total += 1
                if len(self.nre_hits) < NRE_SAMPLE_CAP:
                    self.nre_hits.append(line)

    def result(self) -> dict:
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


def parse_client_log(text: str) -> dict:
    """Parse a whole playtest log at once. Prefer JSON events when present
    (avoid double human+JSON). Incremental consumers should use
    :class:`ClientLogScan` instead of re-running this over the full text."""
    scan = ClientLogScan()
    for line in text.splitlines():
        scan.feed_line(line)
        scan.feed_chunk(line)
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
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        self._pending = b""

    def poll(self) -> str:
        """Return newly appended complete-line text since the previous call."""
        try:
            size = self._path.stat().st_size
        except OSError:
            return ""
        if size < self._offset:
            self._offset = 0
            self._pending = b""
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
