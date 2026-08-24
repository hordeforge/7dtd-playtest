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


def percentage() -> int:
    out = Path(".coverage.json")
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-q", "-o", str(out)],
        check=True,
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    totals = data["totals"]
    return round(float(totals["percent_covered"]))


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
    if len(argv) != 2:
        print("usage: coverage_badge.py OUTPUT.svg", file=sys.stderr)
        return 2
    pct = percentage()
    Path(argv[1]).write_text(badge(pct, colour(pct)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
