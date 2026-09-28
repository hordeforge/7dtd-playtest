#!/usr/bin/env python3
"""Render the line-coverage badge SVG from the local .coverage file.

Must be run by an interpreter that has coverage importable: the locked dev
dependency group provides it (`uv run --locked`, as `make coverage` does).
Usage: coverage_badge.py OUTPUT.svg
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

USAGE = "usage: coverage_badge.py OUTPUT.svg"

# `coverage json` re-reads and re-parses the whole measured tree, so it is
# bounded like every other external call: an unbounded one hangs `make
# coverage` forever on a large or network-mounted tree.
COVERAGE_JSON_TIMEOUT_SEC = 300.0


class CoverageDataError(ValueError):
    """The `.coverage.json` report is not the shape this reader needs."""


def percentage() -> int:
    out = Path(".coverage.json")
    # The scratch report is unlinked in a finally: a json.loads, read or
    # `coverage json` failure would otherwise leave .coverage.json in the
    # project root, where the next run would pick up a stale one.
    try:
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-q", "-o", str(out)],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=COVERAGE_JSON_TIMEOUT_SEC,
        )
        data = json.loads(out.read_text(encoding="utf-8"))
    finally:
        out.unlink(missing_ok=True)
    try:
        totals = data["totals"]
        return round(float(totals["percent_covered"]))
    except (KeyError, TypeError, ValueError) as exc:
        # A report written by a different coverage version parses as JSON and
        # then has no `totals`; name the file and the key rather than leaving
        # the operator with a bare KeyError.
        raise CoverageDataError(
            f"{out.name} has no usable 'totals.percent_covered' ({exc}); it was "
            "written by a different coverage version or truncated"
        ) from exc


def colour(pct: int) -> str:
    if pct >= 90:
        return "#4c1"
    if pct >= 75:
        return "#97ca00"
    if pct >= 60:
        return "#dfb317"
    if pct >= 40:
        return "#fe7d37"
    return "#e05d44"


def badge(pct: int, fill: str) -> str:
    lw, vw = 64, 36
    w = lw + vw
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20"'
        f' role="img" aria-label="coverage: {pct}%">\n'
        f"<title>coverage: {pct}%</title>\n"
        '<linearGradient id="s" x2="0" y2="100%">'
        '<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        '<stop offset="1" stop-opacity=".1"/></linearGradient>\n'
        f'<clipPath id="r"><rect width="{w}" height="20" rx="3" fill="#fff"/></clipPath>\n'
        f'<g clip-path="url(#r)"><rect width="{lw}" height="20" fill="#555"/>'
        f'<rect x="{lw}" width="{vw}" height="20" fill="{fill}"/>'
        f'<rect width="{w}" height="20" fill="url(#s)"/></g>\n'
        "<g fill=\"#fff\" text-anchor=\"middle\""
        ' font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f"<text x={lw / 2!r} y=\"14\">coverage</text>"
        f"<text x={(lw + vw / 2)!r} y=\"14\">{pct}%</text></g>\n"
        "</svg>\n"
    )


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        print(
            "\nWrites the shields.io line-coverage badge SVG for the local"
            " .coverage file."
        )
        print("Exit codes: 0 badge written, 1 no coverage data, 2 bad usage.")
        return 0
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        pct = percentage()
    except subprocess.TimeoutExpired:
        print(
            f"coverage_badge: `coverage json` did not finish within "
            f"{COVERAGE_JSON_TIMEOUT_SEC:g}s",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            "coverage_badge: `coverage json` failed; run it under the project's "
            f"interpreter after a measured test run (`uv run --locked make coverage`): {exc}",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as exc:
        print(f"coverage_badge: cannot compute coverage: {exc}", file=sys.stderr)
        return 1
    try:
        Path(args[0]).write_text(badge(pct, colour(pct)), encoding="utf-8")
    except OSError as exc:
        print(f"coverage_badge: cannot write {args[0]}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
