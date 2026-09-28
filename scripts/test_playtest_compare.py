#!/usr/bin/env python3
"""Offline gates for the stock-vs-zdtd playtest comparison (playtest_compare.py).

Synthetic client logs on both sides are parsed and diffed: status mismatches
and one-sided cases become findings; matching cases do not.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_compare  # noqa: E402
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


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )


def _run(tmp_path: Path, stock: str, zdtd: str) -> subprocess.CompletedProcess[str]:
    s = tmp_path / "stock.log"
    z = tmp_path / "zdtd.log"
    s.write_text(stock, encoding="utf-8")
    z.write_text(zdtd, encoding="utf-8")
    return _run_cli("--stock", str(s), "--zdtd", str(z), "--out", str(tmp_path / "out"))


def _report(server: str, ran_epoch: int) -> dict:
    """A minimal single-pass report; the freshness guards vary the epoch."""
    return {
        "server": server,
        "ran_epoch": ran_epoch,
        "summary": {"pass": 1, "fail": 0, "skip": 0},
        "results": [{"case": "smoke/join", "status": "PASS"}],
    }


def _report_pair(tmp_path: Path, ran_epoch: int) -> tuple[Path, Path]:
    """One matching stock/zdtd report pair, both stamped `ran_epoch`."""
    s = tmp_path / "stock.json"
    z = tmp_path / "zdtd.json"
    s.write_text(json.dumps(_report("stock", ran_epoch)), encoding="utf-8")
    z.write_text(json.dumps(_report("zdtd", ran_epoch)), encoding="utf-8")
    return s, z


def test_status_mismatch_becomes_finding(tmp_path: Path) -> None:
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


def test_identical_sides_have_no_findings(tmp_path: Path) -> None:
    r = _run(tmp_path, STOCK_LOG, STOCK_LOG)
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out" / "playtest-compare.json").read_text(encoding="utf-8"))
    assert payload["compared"] is True
    assert payload["findings"] == []
    # Identical sides must still be diffed case by case, not collapsed away.
    assert {c["case"] for c in payload["cases"]} == {"smoke/join", "smoke/enter"}
    assert payload["stock"]["summary"] == {"pass": 1, "fail": 1, "skip": 0}


def test_report_json_wall_axis(tmp_path: Path) -> None:
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
    r = _run_cli("--stock", str(s), "--zdtd", str(z), "--out", str(out))
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
    return _run_cli(*extra, "--out", str(tmp_path / "out"))


def test_nonexistent_input_refuses_diff(tmp_path: Path) -> None:
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


def test_directory_input_refuses_diff(tmp_path: Path) -> None:
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


def test_exit_codes_documented_in_help() -> None:
    """The 0/1/2/3 contract is part of the CLI surface; --help must show it."""
    r = _run_cli("--help")
    assert r.returncode == 0, r.stderr
    assert "Exit codes:" in r.stdout
    for line in ("0  comparison written", "1  neither side had a playtest result line",
                 "2  a side has no input", "3  inputs older than"):
        assert line in r.stdout, line


def test_missing_side_refuses_diff(tmp_path: Path) -> None:
    """A side dir without any report must fail loudly, naming the side, and
    must NOT write comparison outputs (no phantom 'compared' result)."""
    now = int(time.time())
    s = tmp_path / "stock" / f"report-{now}.json"
    s.parent.mkdir(parents=True)
    s.write_text(json.dumps(_report("stock", now)), encoding="utf-8")
    z = tmp_path / "zdtd"   # empty dir: side never ran
    z.mkdir()
    out = tmp_path / "out"
    r = _run_cli("--stock-dir", str(s.parent), "--zdtd-dir", str(z), "--out", str(out))
    assert r.returncode == 2, r.stderr
    assert "no report found on the zdtd side" in r.stderr
    assert not (out / "playtest-compare.json").exists()


def test_stale_report_refuses_diff(tmp_path: Path) -> None:
    """Old reports (e.g. a previous session) must fail the freshness guard
    instead of being diffed as if fresh, and must not write outputs."""
    old = int(time.time()) - 6 * 86400
    s = tmp_path / "stock" / f"report-{old}.json"
    z = tmp_path / "zdtd" / f"report-{old}.json"
    s.parent.mkdir()
    z.parent.mkdir()
    s.write_text(json.dumps(_report("stock", old)), encoding="utf-8")
    z.write_text(json.dumps(_report("zdtd", old)), encoding="utf-8")
    out = tmp_path / "out"
    r = _run_cli("--stock-dir", str(s.parent), "--zdtd-dir", str(z.parent),
                 "--out", str(out), "--require-fresh-minutes", "60")
    assert r.returncode == 3, r.stderr
    assert "comparison inputs are stale" in r.stderr
    assert not (out / "playtest-compare.json").exists()


def test_future_epoch_refuses_freshness_guard(tmp_path: Path) -> None:
    """A ran_epoch years ahead of now used to pass --require-fresh-minutes
    because now-epoch is negative. It must fail like any other unusable age."""
    future = int(time.time()) + 50 * 365 * 86400
    s, z = _report_pair(tmp_path, future)
    out = tmp_path / "out"
    r = _run_cli("--stock", str(s), "--zdtd", str(z), "--out", str(out),
                 "--require-fresh-minutes", "60")
    assert r.returncode == 3, r.stderr
    assert "comparison inputs are stale" in r.stderr
    assert not (out / "playtest-compare.json").exists()


def test_oversized_freshness_window_is_a_usage_error(tmp_path: Path) -> None:
    """--require-fresh-minutes is scaled into float seconds, and a Python int
    is unbounded: a value past the float range used to raise OverflowError
    instead of the usage error every other bad value gets."""
    s, z = _report_pair(tmp_path, int(time.time()))
    r = _run_cli("--stock", str(s), "--zdtd", str(z),
                 "--out", str(tmp_path / "out"),
                 "--require-fresh-minutes", "9" * 400)
    assert r.returncode == 2, r.stderr
    assert "at most" in r.stderr
    assert "Traceback" not in r.stderr


def test_unwritable_out_dir_is_exit_4_not_traceback(tmp_path: Path) -> None:
    """An unwritable --out must fail with its own exit code (4) naming the
    destination, never a traceback with Python's default exit 1 (documented
    as 'neither side had a playtest result line')."""
    s = tmp_path / "stock.log"
    z = tmp_path / "zdtd.log"
    s.write_text(STOCK_LOG, encoding="utf-8")
    z.write_text(ZDTD_LOG, encoding="utf-8")
    blocked = tmp_path / "occupied"  # a file where the out dir should be
    blocked.write_text("x", encoding="utf-8")
    r = _run_cli("--stock", str(s), "--zdtd", str(z), "--out", str(blocked))
    assert r.returncode == 4, r.stderr
    assert "cannot write comparison outputs" in r.stderr
    assert "Traceback" not in r.stderr


def test_unreadable_input_refuses_diff_instead_of_reading_as_empty(tmp_path: Path) -> None:
    """A side that cannot be read is a refused input, not an empty one.

    main() only checks is_file(), so a file that exists but cannot be read
    (permissions, a vanished file, EIO) reaches load_results. Degrading that
    to an empty side reported the run as "neither side had a playtest result
    line" (exit 1), which reads as evidence the playtest produced nothing
    rather than evidence that its log could not be read.
    """
    s = tmp_path / "stock.log"
    z = tmp_path / "zdtd.log"
    s.write_text(STOCK_LOG, encoding="utf-8")
    z.write_text(ZDTD_LOG, encoding="utf-8")
    s.chmod(0o000)
    try:
        r = _run_bad_input(tmp_path, "--stock", str(s), "--zdtd", str(z))
        # Root ignores the mode bits, so an unreadable-by-mode file is still
        # readable here; only assert the refusal when it actually is not.
        if r.returncode == 0:
            return
        assert r.returncode == 2, r.stderr
        assert "cannot read" in r.stderr and str(s) in r.stderr
        assert "Traceback" not in r.stderr
        assert not (tmp_path / "out" / "playtest-compare.json").exists()
    finally:
        s.chmod(0o644)


def test_load_results_raises_on_unreadable_path(tmp_path: Path) -> None:
    """The refusal is raised at the read, so any consumer of load_results
    cannot silently diff an input it never saw."""
    missing = tmp_path / "gone.json"
    with pytest.raises(playtest_compare.CompareError) as excinfo:
        playtest_compare.load_results(missing)
    assert str(missing) in str(excinfo.value)


def test_ran_at_surfaces_in_report(tmp_path: Path) -> None:
    """Fresh report JSONs carry ranAtUtc; the md shows a ran (UTC) row so a
    reader can tell when each side actually ran."""
    now = int(time.time())
    s, z = _report_pair(tmp_path, now)
    out = tmp_path / "out"
    r = _run_cli("--stock", str(s), "--zdtd", str(z), "--out", str(out))
    assert r.returncode == 0, r.stderr
    payload = json.loads((out / "playtest-compare.json").read_text(encoding="utf-8"))
    assert payload["stock"]["ranAtUtc"] and payload["zdtd"]["ranAtUtc"]
    report_md = (out / "playtest-compare.md").read_text(encoding="utf-8")
    assert "| ran (UTC) | " in report_md


def test_newest_report_picks_greatest_name_on_mtime_tie(tmp_path: Path) -> None:
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


def test_orchestrator_report_diffs_through_dir_mode(tmp_path: Path) -> None:
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
    r = _run_cli("--stock-dir", str(stock), "--zdtd-dir", str(zdtd),
                 "--out", str(out), "--require-fresh-minutes", "60")
    assert r.returncode == 0, r.stderr
    payload = json.loads((out / "playtest-compare.json").read_text(encoding="utf-8"))
    assert payload["compared"] is True
    assert payload["findings"] == [], payload["findings"]
    assert payload["stock"]["wall"] == 12.5 and payload["zdtd"]["wall"] == 12.5
    assert payload["stock"]["summary"] == {"pass": 1, "fail": 1, "skip": 0}
    # Filename-epoch fallback kept both sides fresh (not "unknown").
    assert payload["stock"]["ranAtUtc"] != "unknown"
    assert payload["zdtd"]["ranAtUtc"] != "unknown"


def test_no_results_on_either_side_refuses(tmp_path: Path) -> None:
    """Two live logs that contain no result lines at all are not an empty
    diff: refuse loudly instead of writing a zero-case comparison."""
    noise = "[game] boot noise, no playtest events\n"
    r = _run(tmp_path, noise, noise)
    assert r.returncode == 1, r.stderr
    assert "neither side had a playtest result line" in r.stderr
    assert not (tmp_path / "out" / "playtest-compare.json").exists()


def test_orchestrator_payload_keys_match_consumer_contract() -> None:
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
    # The stamp reads the orchestrator's clock seam, so a simulated run names
    # its report the same way a real one does. Pinned by name to keep the seam
    # from being bypassed, and by behaviour so what is actually checked is that
    # the consumer parses the filename the producer builds.
    assert 'report-{int(epoch_now())}.json' in src, (
        "producer report filename no longer matches newest_report's glob"
    )
    produced = f"report-{int(playtest_run.epoch_now())}.json"
    assert playtest_compare.ran_epoch_of(Path(produced), {}) is not None, (
        f"newest_report cannot read the producer's filename: {produced}"
    )


def test_hostile_case_cannot_author_markdown(tmp_path: Path) -> None:
    """Result rows are parsed back out of client-log bytes, and the JSON event
    path carries any string as the case id, including real newlines (\\n
    escapes materialize in json.loads), pipes, and backticks. playtest-compare.md
    renders for humans, so a crafted id must stay inside its table cell: the
    md row is single-line with structural characters neutralized, while the
    JSON payload keeps the raw value as data."""
    hostile = "a|b\nc`d"
    event = {
        "v": 1,
        "t": "result",
        "suite": "smoke",
        "case": hostile,
        "status": "pass",
        "ms": 1,
        "detail": "",
    }
    stock_log = (
        "[7dtd-playtest] " + json.dumps(event) + "\n"
        "[7dtd-playtest] SUMMARY pass=1 fail=0\n"
        "[7dtd-playtest] DONE\n"
    )
    r = _run(tmp_path, stock_log, ZDTD_LOG)
    assert r.returncode == 0, r.stderr
    payload = json.loads(
        (tmp_path / "out" / "playtest-compare.json").read_text(encoding="utf-8")
    )
    # Data fidelity survives in the machine-readable artifact.
    assert any(c["case"] == f"smoke/{hostile}" for c in payload["cases"])
    report_md = (tmp_path / "out" / "playtest-compare.md").read_text(encoding="utf-8")
    # Pipe escaped, backtick replaced, newline dropped: one intact table row
    # (the parser prefixes the suite, hence smoke/).
    assert "| `smoke/a\\|bc'd` | PASS | MISSING |" in report_md
    assert any("smoke/a\\|bc'd: ran only on stock" in f for f in payload["findings"])


def test_non_string_case_in_report_does_not_crash_diff(tmp_path: Path) -> None:
    """A report JSON can carry any JSON value where a case id belongs; the
    diff must coerce it like every other parser here instead of crashing
    sorted() on a str/int key mix."""
    def report(server: str, case: object) -> dict:
        return {
            "server": server,
            "summary": {"pass": 1, "fail": 0, "skip": 0},
            "results": [{"case": case, "status": "PASS"}],
        }

    s = tmp_path / "stock.json"
    z = tmp_path / "zdtd.json"
    s.write_text(json.dumps(report("stock", 5)), encoding="utf-8")
    z.write_text(json.dumps(report("zdtd", "")), encoding="utf-8")
    out = tmp_path / "out"
    r = _run_cli("--stock", str(s), "--zdtd", str(z), "--out", str(out))
    assert r.returncode == 0, r.stderr
    payload = json.loads((out / "playtest-compare.json").read_text(encoding="utf-8"))
    by = {c["case"]: c for c in payload["cases"]}
    assert by["?"]["stock"]["status"] == "PASS"


# Scalars a report JSON can carry where a case id, a status, a count, a
# duration or a wall clock belongs. The parser must take every one of them and
# still produce a comparison, or refuse the input with a documented exit code.
_HOSTILE_SCALARS: tuple[object, ...] = (
    None, True, False, 0, -1, 7, 1 << 70, 1.5, float("inf"), float("nan"),
    "", "x", "PASS", "3", "0x10", "[]", "{}", [1, 2], {"k": "v"},
    "a|b\nc`d", "\x00\x1f\x7f\x9f", "‮rtl‭", "\U0001f600", "-",  # noqa: PLE2502
)


def _hostile_payload(rng: random.Random) -> object:
    """One run report, built from the shapes a real file can hold.

    A report-*.json is not the orchestrator's private output by construction:
    it is a file on disk that a crashed or interrupted run truncates, another
    tool writes, or an operator hand-builds. The grammar puts a hostile value
    at every position main() reads, and drops whole keys, so the diff has to
    hold every cross-field rule on what is left.
    """
    payload: dict[str, object] = {}
    if rng.random() < 0.85:
        rows: list[object] = []
        for _ in range(rng.randrange(0, 4)):
            row = rng.choice(_HOSTILE_SCALARS) if rng.random() < 0.15 else {
                "case": rng.choice(_HOSTILE_SCALARS),
                "status": rng.choice(_HOSTILE_SCALARS),
                "detail": rng.choice(_HOSTILE_SCALARS),
            }
            rows.append(row)
        payload["results"] = rng.choice(_HOSTILE_SCALARS) if rng.random() < 0.15 else rows
    if rng.random() < 0.8:
        payload["summary"] = (
            {k: rng.choice(_HOSTILE_SCALARS) for k in ("pass", "fail", "skip")}
            if rng.random() < 0.7
            else rng.choice(_HOSTILE_SCALARS)
        )
    if rng.random() < 0.6:
        payload["wall_sec"] = rng.choice(_HOSTILE_SCALARS)
    if rng.random() < 0.6:
        payload["server"] = rng.choice(_HOSTILE_SCALARS)
    if rng.random() < 0.6:
        payload["ran_epoch"] = rng.choice(_HOSTILE_SCALARS)
    return payload


def _hostile_side(rng: random.Random, path: Path) -> None:
    """Write one side of the diff: a report JSON, a client log, or raw bytes."""
    roll = rng.random()
    if roll < 0.2:
        # A log, or a truncated/mangled one: the non-JSON fallback path.
        path.write_text(
            "".join(
                rng.choice([
                    "[7dtd-playtest] PASS smoke/join detail=ok\n",
                    "[7dtd-playtest] " + json.dumps(_hostile_payload(rng)) + "\n",
                    "[7dtd-playtest] SUMMARY pass=x fail=\n",
                    '{"results": [\n', "\x00\xff\xfe truncated \xe2\x82",
                ])
                for _ in range(rng.randrange(0, 5))
            ),
            encoding="utf-8", errors="replace",
        )
        return
    payload = _hostile_payload(rng)
    if roll < 0.28:
        # Raw bytes that are not valid UTF-8 at all.
        path.write_bytes(json.dumps(payload).encode("utf-8")[: rng.randrange(0, 40)] + b"\xff\xfe")
        return
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_fuzz_compare_survives_hostile_report_pairs(tmp_path: Path) -> None:
    """Seeded grammar fuzzer over both diff inputs, driving the real CLI.

    A report JSON is a file, so its JSON types are untrusted: a hand-built
    fixture, a truncated run, or another tool's output can put a string, a
    number, null or a nested list where a result row, a case id, a count or a
    wall time belongs. The diff must still answer, and answer it with a
    documented exit code: a traceback out of this CLI is exit 1, which the
    module documents as "neither side had a result line", so a crash reads to
    every caller as a verdict about the run rather than about the file.
    """
    rng = random.Random(20260928)
    compared = 0
    refused = 0
    for i in range(40):
        case_dir = tmp_path / f"run{i}"
        case_dir.mkdir()
        stock, zdtd = case_dir / "stock.json", case_dir / "zdtd.json"
        _hostile_side(rng, stock)
        _hostile_side(rng, zdtd)
        out = case_dir / "out"
        r = _run_cli("--stock", str(stock), "--zdtd", str(zdtd), "--out", str(out))
        assert "Traceback" not in r.stderr, (
            f"iteration {i} crashed the diff:\n{r.stderr}\n"
            f"stock: {stock.read_bytes()[:200]!r}\nzdtd: {zdtd.read_bytes()[:200]!r}"
        )
        # Both sides exist and are readable files, so the comparison is the
        # answer: 0 (written) or 1 (no result line on either side). Any other
        # code means a freshness or write failure the fuzzer did not ask for.
        assert r.returncode in (0, 1), f"iteration {i} exit {r.returncode}: {r.stderr}"
        if r.returncode == 1:
            refused += 1
            assert not (out / "playtest-compare.json").exists()
            continue
        compared += 1
        payload = json.loads(
            (out / "playtest-compare.json").read_text(encoding="utf-8")
        )
        # Every row the diff emitted is a usable row: a string case id, string
        # statuses, string details, and int counts. A coerced value that is
        # still the wrong type would break the next consumer of this artifact
        # exactly where the run log was trusted one step less.
        for row in payload["cases"]:
            assert isinstance(row["case"], str) and row["case"]
            for side in ("stock", "zdtd"):
                assert isinstance(row[side]["status"], str)
                assert isinstance(row[side]["detail"], str)
                assert len(row[side]["detail"]) <= 120
        for side in ("stock", "zdtd"):
            for count in payload[side]["summary"].values():
                assert isinstance(count, int) and not isinstance(count, bool)
        # The markdown renders for a human: one table row per case, and no
        # crafted value authoring a line of its own below the table.
        md = (out / "playtest-compare.md").read_text(encoding="utf-8")
        body = md.split("## Per-case", 1)[1].split("## Findings", 1)[0]
        table = [ln for ln in body.splitlines() if ln.strip()]
        assert len(table) == len(payload["cases"]) + 2, (
            f"iteration {i}: {len(table)} table lines for {len(payload['cases'])} cases"
        )
    assert compared >= 15, f"fuzzer only compared {compared}/40 pairs: corpus is too weak"
    assert refused >= 1, "fuzzer never reached the no-result-line refusal"
    print(f"PASS compare_fuzz 40 hostile sides, {compared} compared and {refused} refused")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
