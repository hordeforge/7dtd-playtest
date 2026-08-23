#!/usr/bin/env python3
"""Offline gates for the stock-vs-zdtd playtest comparison (playtest_compare.py).

Synthetic client logs on both sides are parsed and diffed: status mismatches
and one-sided cases become findings; matching cases do not.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_run  # noqa: E402

TOOL = _SCRIPTS / "playtest_compare.py"

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
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
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
    assert payload["compared"] is True
    assert payload["findings"] == []
    # Identical sides must still be diffed case by case, not collapsed away.
    assert {c["case"] for c in payload["cases"]} == {"smoke/join", "smoke/enter"}
    assert payload["stock"]["summary"] == {"pass": 1, "fail": 1, "skip": 0}


def test_report_json_wall_axis(tmp_path):
    """Report JSONs carry wall_sec; the diff surfaces it as a cost axis and
    labels the sides, never as a per-case finding."""
    def report(server: str, wall: float, passn: int) -> dict:
        return {
            "server": server,
            "wall_sec": wall,
            "summary": {"pass": passn, "fail": 0, "skip": 0},
            "results": [{"case": "bench/x", "status": "PASS", "detail": "ok"}],
        }

    s = tmp_path / "stock.json"
    z = tmp_path / "zdtd.json"
    s.write_text(json.dumps(report("stock", 157.1, 1)), encoding="utf-8")
    z.write_text(json.dumps(report("zdtd", 128.0, 1)), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(TOOL), "--stock", str(s), "--zdtd", str(z), "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
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


def _run_bad_input(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    out = tmp_path / "out"
    return subprocess.run(
        [sys.executable, str(TOOL), *extra, "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )


def test_nonexistent_input_refuses_diff(tmp_path):
    """A bad --stock/--zdtd path must fail like every other unusable input:
    exit 2 with the offending flag named on stderr, no traceback, and no
    comparison outputs."""
    r = _run_bad_input(
        tmp_path,
        "--stock", str(tmp_path / "nope.json"),
        "--zdtd", str(tmp_path / "also-nope.json"),
    )
    assert r.returncode == 2, r.stderr
    assert "--stock" in r.stderr and "not a readable file" in r.stderr
    assert "nope.json" in r.stderr
    assert "Traceback" not in r.stderr
    assert not (tmp_path / "out" / "playtest-compare.json").exists()


def test_directory_input_refuses_diff(tmp_path):
    """--stock pointing at a directory is not silently globbed like
    --stock-dir would be: refuse with the flag named instead of crashing on
    IsADirectoryError."""
    d = tmp_path / "adir"
    d.mkdir()
    other = tmp_path / "side.log"
    other.write_text(STOCK_LOG, encoding="utf-8")
    r = _run_bad_input(tmp_path, "--stock", str(d), "--zdtd", str(other))
    assert r.returncode == 2, r.stderr
    assert "--stock" in r.stderr and "not a readable file" in r.stderr
    assert "Traceback" not in r.stderr
    assert not (tmp_path / "out" / "playtest-compare.json").exists()


def test_exit_codes_documented_in_help():
    """The 0/1/2/3 contract is part of the CLI surface; --help must show it."""
    r = subprocess.run(
        [sys.executable, str(TOOL), "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert r.returncode == 0, r.stderr
    assert "Exit codes:" in r.stdout
    for line in ("0  comparison written", "1  no playtest result lines",
                 "2  a side has no input", "3  inputs older than"):
        assert line in r.stdout, line


def test_missing_side_refuses_diff(tmp_path):
    """A side dir without any report must fail loudly, naming the side, and
    must NOT write comparison outputs (no phantom 'compared' result)."""
    import time
    now = int(time.time())
    s = tmp_path / "stock" / f"report-{now}.json"
    s.parent.mkdir(parents=True)
    s.write_text(json.dumps({"server": "stock", "ran_epoch": now,
                             "summary": {"pass": 1, "fail": 0, "skip": 0},
                             "results": [{"case": "smoke/join", "status": "PASS"}]}),
                 encoding="utf-8")
    z = tmp_path / "zdtd"   # empty dir: side never ran
    z.mkdir()
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(TOOL), "--stock-dir", str(s.parent), "--zdtd-dir", str(z),
         "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert r.returncode == 2, r.stderr
    assert "no report found on the zdtd side" in r.stderr
    assert not (out / "playtest-compare.json").exists()


def test_stale_report_refuses_diff(tmp_path):
    """Old reports (e.g. a previous session) must fail the freshness guard
    instead of being diffed as if fresh, and must not write outputs."""
    import time
    old = int(time.time()) - 6 * 86400
    def report(server: str) -> dict:
        return {"server": server, "ran_epoch": old,
                "summary": {"pass": 1, "fail": 0, "skip": 0},
                "results": [{"case": "smoke/join", "status": "PASS"}]}
    s = tmp_path / "stock" / f"report-{old}.json"
    z = tmp_path / "zdtd" / f"report-{old}.json"
    s.parent.mkdir()
    z.parent.mkdir()
    s.write_text(json.dumps(report("stock")), encoding="utf-8")
    z.write_text(json.dumps(report("zdtd")), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(TOOL), "--stock-dir", str(s.parent), "--zdtd-dir", str(z.parent),
         "--out", str(out), "--require-fresh-minutes", "60"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert r.returncode == 3, r.stderr
    assert "comparison inputs are stale" in r.stderr
    assert not (out / "playtest-compare.json").exists()


def test_ran_at_surfaces_in_report(tmp_path):
    """Fresh report JSONs carry ranAtUtc; the md shows a ran (UTC) row so a
    reader can tell when each side actually ran."""
    import time
    now = int(time.time())
    def report(server: str) -> dict:
        return {"server": server, "ran_epoch": now,
                "summary": {"pass": 1, "fail": 0, "skip": 0},
                "results": [{"case": "smoke/join", "status": "PASS"}]}
    s = tmp_path / "stock.json"
    z = tmp_path / "zdtd.json"
    s.write_text(json.dumps(report("stock")), encoding="utf-8")
    z.write_text(json.dumps(report("zdtd")), encoding="utf-8")
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(TOOL), "--stock", str(s), "--zdtd", str(z), "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads((out / "playtest-compare.json").read_text(encoding="utf-8"))
    assert payload["stock"]["ranAtUtc"] and payload["zdtd"]["ranAtUtc"]
    report_md = (out / "playtest-compare.md").read_text(encoding="utf-8")
    assert "| ran (UTC) | " in report_md


def test_newest_report_picks_greatest_name_on_mtime_tie(tmp_path):
    """Equal mtimes must not hand the choice of diffed evidence to readdir
    order: the lexicographically greatest report name wins."""
    import json as _json
    import os
    import time
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("playtest_compare", TOOL)
    assert spec is not None and spec.loader is not None, f"cannot load tool: {TOOL}"
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = tmp_path / "stock"
    d.mkdir()

    def write(name: str, passn: int) -> None:
        p = d / name
        p.write_text(_json.dumps({
            "server": "stock", "ran_epoch": None,
            "summary": {"pass": passn, "fail": 0, "skip": 0},
            "results": [{"case": "smoke/join", "status": "PASS"}],
        }), encoding="utf-8")

    write("report-100.json", 1)
    write("report-200.json", 2)
    stamp = time.time() - 60
    for name in ("report-100.json", "report-200.json"):
        os.utime(d / name, (stamp, stamp))
    picked = mod.newest_report(d)
    assert picked is not None and picked.name == "report-200.json"


def test_orchestrator_report_diffs_through_dir_mode(tmp_path):
    """Producer→consumer contract, end to end: a report written by the
    orchestrator's real write_report must be found by newest_report's
    report-*.json glob, pass --require-fresh-minutes, and diff per case.

    Every other test here hand-builds its fixture JSONs; without this test a
    rename of a payload field or the report filename in playtest_run would
    keep all gates green and only surface as exit 2 on a live compare run.
    """
    now = int(time.time())

    def write(side: Path, name_epoch: int, server: str, ran_epoch: int | None) -> None:
        side.mkdir(parents=True)
        payload = {
            "server": server,
            "suite": "smoke",
            "summary": {"pass": 1, "fail": 1, "skip": 0},
            "done": {"exit_hint": 0},
            "results": [
                {"status": "PASS", "case": "smoke/join", "detail": "ok"},
                {"status": "FAIL", "case": "smoke/enter", "detail": "denied"},
            ],
            "wall_sec": 12.5,
            "ran_epoch": ran_epoch,
        }
        playtest_run.write_report(side / f"report-{name_epoch}.json", payload)

    stock = tmp_path / "stock"
    zdtd = tmp_path / "zdtd"
    write(stock, now, "stock", now)
    # zdtd side omits ran_epoch on purpose: freshness must fall back to the
    # report-<epoch>.json filename (ran_epoch_of), as with older reports.
    write(zdtd, now + 1, "zdtd", None)

    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, str(TOOL), "--stock-dir", str(stock), "--zdtd-dir", str(zdtd),
         "--out", str(out), "--require-fresh-minutes", "60"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads((out / "playtest-compare.json").read_text(encoding="utf-8"))
    assert payload["compared"] is True
    assert payload["findings"] == [], payload["findings"]
    assert payload["stock"]["wall"] == 12.5 and payload["zdtd"]["wall"] == 12.5
    assert payload["stock"]["summary"] == {"pass": 1, "fail": 1, "skip": 0}
    # Filename-epoch fallback kept both sides fresh (not "unknown").
    assert payload["stock"]["ranAtUtc"] != "unknown"
    assert payload["zdtd"]["ranAtUtc"] != "unknown"


def test_no_results_on_either_side_refuses(tmp_path):
    """Two live logs that contain no result lines at all are not an empty
    diff: refuse loudly instead of writing a zero-case comparison."""
    noise = "[game] boot noise, no playtest events\n"
    r = _run(tmp_path, noise, noise)
    assert r.returncode == 1, r.stderr
    assert "no playtest result lines" in r.stderr
    assert not (tmp_path / "out" / "playtest-compare.json").exists()


def test_orchestrator_payload_keys_match_consumer_contract():
    """Structural drift guard for the report JSON boundary.

    playtest_compare.load_results reads results/summary/wall_sec/server/
    ran_epoch out of payloads that main() builds inline in playtest_run.py,
    and newest_report globs report-<epoch>.json. The behavioral round-trip is
    covered by test_orchestrator_report_diffs_through_dir_mode; this pin
    catches a plain key/filename rename on the producer side, which no
    hand-built fixture can see.
    """
    src = (_SCRIPTS / "playtest_run.py").read_text(encoding="utf-8")
    for key in ('"results"', '"summary"', '"server"', '"wall_sec"', '"ran_epoch"'):
        assert key in src, f"producer payload lost consumer key {key}"
    assert 'report-{int(time.time())}.json' in src, (
        "producer report filename no longer matches newest_report's glob"
    )


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
