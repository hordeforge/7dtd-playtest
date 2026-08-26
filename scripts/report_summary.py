#!/usr/bin/env python3
"""Print "pass fail skip" from one orchestrator report JSON.

The lap aggregator in playtest_repeat.sh reads these three counts. It lives in
its own file rather than inline in the shell so there is one language per file
and the parsing is lintable and typed.

A report whose summary is missing, malformed, or non-integral exits non-zero
with nothing on stdout: the caller counts an unreadable lap as failed, so a
silently-zeroed count would read as a clean lap.
Usage: report_summary.py REPORT.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIELDS = ("pass", "fail", "skip")


def counts(report: Path) -> tuple[int, ...]:
    summary = json.loads(report.read_text(encoding="utf-8"))["summary"]
    if not isinstance(summary, dict):
        raise TypeError(f"summary is {type(summary).__name__}, expected object")
    # bool is an int subclass, and a float count means the writer lost precision:
    # take neither silently.
    values = []
    for field in FIELDS:
        value = summary.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"summary.{field} is {value!r}, expected an integer")
        if value < 0:
            raise ValueError(f"summary.{field} is {value}, expected >= 0")
        values.append(value)
    return tuple(values)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: report_summary.py REPORT.json", file=sys.stderr)
        return 2
    try:
        values = counts(Path(argv[1]))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"report_summary: unreadable summary in {argv[1]}: {exc}", file=sys.stderr)
        return 1
    print(*values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
