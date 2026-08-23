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

RESULT_RE = re.compile(r"\[7dtd-playtest\]\s+(PASS|FAIL|SKIP)\s+(\S+)\s*(.*)$")
SUMMARY_RE = re.compile(
    r"\[7dtd-playtest\]\s+SUMMARY\s+pass=(\d+)\s+fail=(\d+)(?:\s+skip=(\d+))?"
)
DONE_RE = re.compile(r"\[7dtd-playtest\]\s+DONE(?:\s+exit_hint=(\d+))?")
JSON_RE = re.compile(r"\[7dtd-playtest\]\s+(\{.*\})\s*$")
NRE_RE = re.compile(r"NullReferenceException|NCSimple|underrun|IndexOutOfRange", re.I)


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


def parse_client_log(text: str) -> dict:
    """Parse playtest log. Prefer JSON events when present (avoid double human+JSON)."""
    human_results: list[dict] = []
    json_results: list[dict] = []
    summary = None
    done = None
    json_events: list[dict] = []
    json_summary = None
    json_done = None
    human_summary = None
    human_done = None

    for line in text.splitlines():
        m = JSON_RE.search(line)
        if m:
            try:
                ev = json.loads(m.group(1))
                # The client log carries arbitrary game/chat lines; a line that
                # merely looks like an event must not crash the parser.
                if not isinstance(ev, dict):
                    raise ValueError("event is not a JSON object")
                json_events.append(ev)
                if ev.get("t") == "result":
                    # Crafted lines may put any JSON value where a scalar
                    # belongs. Coerce status/detail to str so .upper() and
                    # downstream string consumers cannot raise.
                    status = ev.get("status", "")
                    detail = ev.get("detail", "")
                    json_results.append(
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
                    json_summary = {
                        "pass": int(ev.get("pass", 0)),
                        "fail": int(ev.get("fail", 0)),
                        "skip": int(ev.get("skip", 0)),
                    }
                elif ev.get("t") == "done":
                    json_done = {"exit_hint": int(ev.get("exit_hint", 1))}
            except (TypeError, ValueError, OverflowError):
                # JSONDecodeError subclasses ValueError; a non-numeric count
                # or exit_hint raises it too. A JSON null (or list) where a
                # number belongs raises TypeError, and int(inf) from a 1e999
                # or bare Infinity token raises OverflowError. Skip the bad
                # event, keep the rest.
                pass
            continue

        m = RESULT_RE.search(line)
        if m:
            human_results.append(
                {
                    "status": m.group(1),
                    "case": m.group(2),
                    "detail": (m.group(3) or "").strip(),
                }
            )
            continue
        m = SUMMARY_RE.search(line)
        if m:
            human_summary = {
                "pass": int(m.group(1)),
                "fail": int(m.group(2)),
                "skip": int(m.group(3) or 0),
            }
            continue
        m = DONE_RE.search(line)
        if m:
            hint = int(m.group(1)) if m.group(1) is not None else None
            human_done = {"exit_hint": hint}

    if json_results:
        results = json_results
        summary = json_summary or human_summary
        done = json_done or human_done
    else:
        results = human_results
        summary = human_summary
        done = human_done

    if summary is None and results:
        summary = {
            "pass": sum(1 for r in results if r["status"] == "PASS"),
            "fail": sum(1 for r in results if r["status"] == "FAIL"),
            "skip": sum(1 for r in results if r["status"] == "SKIP"),
        }

    nre_hits = [ln for ln in text.splitlines() if NRE_RE.search(ln)]
    return {
        "results": results,
        "summary": summary,
        "done": done,
        "json_events": json_events,
        "nre_like": nre_hits[:50],
    }
