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


USAGE = "usage: report_summary.py REPORT.json"


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        print("\nPrints 'pass fail skip' from one orchestrator report JSON.")
        print("Exit codes: 0 counts printed, 1 unreadable report, 2 bad usage.")
        return 0
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        values = counts(Path(args[0]))
    except OSError as exc:
        print(f"report_summary: cannot read {args[0]}: {exc.strerror}", file=sys.stderr)
        return 1
    except (ValueError, TypeError, KeyError) as exc:
        print(f"report_summary: unreadable summary in {args[0]}: {exc}", file=sys.stderr)
        return 1
    print(*values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
