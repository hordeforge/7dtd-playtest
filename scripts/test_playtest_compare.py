#!/usr/bin/env python3
"""Offline gates for the stock-vs-zdtd playtest comparison (playtest_compare.py).

Synthetic client logs on both sides are parsed and diffed: status mismatches
and one-sided cases become findings; matching cases do not.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "playtest_compare.py"

STOCK_LOG = (
    "[7dtd-playtest] PASS smoke/join detail=ok\n"
    "[7dtd-playtest] FAIL smoke/enter detail=denied\n"
    "[7dtd-playtest] SUMMARY pass=1 fail=1\n"
    "[7dtd-playtest] DONE\n"
)
ZDTD_LOG = (
    "[7dtd-playtest] PASS smoke/join detail=ok\n"
    "[7dtd-playtest] SKIP smoke/enter detail=no-capability\n"
    "[7dtd-playtest] PASS smoke/extra detail=zdtd-only\n"
    "[7dtd-playtest] SUMMARY pass=2 fail=0 skip=1\n"
    "[7dtd-playtest] DONE\n"
)


def _run(tmp_path: Path, stock: str, zdtd: str) -> subprocess.CompletedProcess[str]:
    s = tmp_path / "stock.log"
    z = tmp_path / "zdtd.log"
    s.write_text(stock, encoding="utf-8")
    z.write_text(zdtd, encoding="utf-8")
    out = tmp_path / "out"
    return subprocess.run(
        [sys.executable, str(TOOL), "--stock", str(s), "--zdtd", str(z), "--out", str(out)],
        capture_output=True, text=True, timeout=30,
    )


def test_status_mismatch_becomes_finding(tmp_path):
    r = _run(tmp_path, STOCK_LOG, ZDTD_LOG)
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out" / "playtest-compare.json").read_text(encoding="utf-8"))
    assert payload["compared"] is True
    assert payload["stock"]["summary"] == {"pass": 1, "fail": 1, "skip": 0}
    assert payload["zdtd"]["summary"] == {"pass": 2, "fail": 0, "skip": 1}
    assert any("smoke/enter: status differs" in f for f in payload["findings"])
    assert any("smoke/extra: ran only on zdtd" in f for f in payload["findings"])
    by = {c["case"]: c for c in payload["cases"]}
    assert by["smoke/join"]["stock"]["status"] == "PASS"
    assert by["smoke/join"]["zdtd"]["status"] == "PASS"
    report = (tmp_path / "out" / "playtest-compare.md").read_text(encoding="utf-8")
    assert "| `smoke/join` | PASS | PASS |" in report


def test_identical_sides_have_no_findings(tmp_path):
    r = _run(tmp_path, STOCK_LOG, STOCK_LOG)
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out" / "playtest-compare.json").read_text(encoding="utf-8"))
    assert payload["findings"] == []


def test_report_json_wall_axis(tmp_path):
    """Report JSONs carry wall_sec; the diff surfaces it as a cost axis and
    labels the sides, never as a per-case finding."""
    def report(server: str, wall: float, passn: int) -> dict:
        return {"server": server, "wall_sec": wall, "summary": {"pass": passn, "fail": 0, "skip": 0},
                "results": [{"case": "bench/x", "status": "PASS", "detail": "ok"}]}

    s = tmp_path / "stock.json"
    z = tmp_path / "zdtd.json"
    s.write_text(json.dumps(report("stock", 157.1, 1)), encoding="utf-8")
    z.write_text(json.dumps(report("zdtd", 128.0, 1)), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(TOOL), "--stock", str(s), "--zdtd", str(z), "--out", str(out)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads((out / "playtest-compare.json").read_text(encoding="utf-8"))
    assert payload["findings"] == []          # wall is an axis, not a mismatch
    assert payload["stock"]["wall"] == 157.1
    assert payload["zdtd"]["wall"] == 128.0
    assert payload["stock"]["server"] == "stock"
    assert payload["zdtd"]["server"] == "zdtd"
    report_md = (out / "playtest-compare.md").read_text(encoding="utf-8")
    assert "| wall time (s) | 157.1 | 128.0 |" in report_md
