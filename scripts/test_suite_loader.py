#!/usr/bin/env python3
"""Offline gate: declarative suite JSON loader (suites/*.json).

No game binaries. Pins schema surface: target/fresh/host/cases/ref,
discover_suites, suite_to_report, and reject paths for bad JSON / bad target.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import suite_loader as sl  # noqa: E402

ROOT = _SCRIPTS.parent
SUITES = ROOT / "suites"


def test_discover_builtin_suites() -> None:
    found = sl.discover_suites(SUITES)
    assert "smoke" in found
    assert "core" in found
    smoke = found["smoke"]
    assert smoke.target == "sandbox"
    assert smoke.fresh is True
    assert smoke.host.fixtures is False
    assert len(smoke.cases) >= 1
    assert all(c.ref for c in smoke.cases)
    assert all(c.kind in ("live", "staged", "defer") for c in smoke.cases)


def test_load_suite_by_id_missing_is_none() -> None:
    assert sl.load_suite_by_id("demo", SUITES) is None
    assert sl.load_suite_by_id("smoke", SUITES) is not None


def test_suite_to_report_shape() -> None:
    doc = sl.load_suite_by_id("smoke", SUITES)
    assert doc is not None
    report = sl.suite_to_report(doc)
    assert report["id"] == "smoke"
    assert report["target"] == "sandbox"
    assert report["fresh"] is True
    assert "fixtures" in report["host"]  # type: ignore[operator]
    assert isinstance(report["cases"], list)
    assert report["cases"][0]["ref"]


def test_reject_unknown_target() -> None:
    with tempfile.TemporaryDirectory(prefix="suite-bad-target-") as td:
        path = Path(td) / "bad.json"
        path.write_text(
            json.dumps(
                {
                    "id": "bad",
                    "target": "prod",
                    "fresh": True,
                    "cases": [{"id": "c1", "kind": "live", "ref": "X.Y"}],
                }
            ),
            encoding="utf-8",
        )
        try:
            sl.load_suite_file(path)
        except sl.SuiteLoadError as ex:
            assert "prod" in str(ex) or "target" in str(ex).lower()
        else:
            raise AssertionError("expected SuiteLoadError for unknown target")


def test_reject_empty_cases() -> None:
    with tempfile.TemporaryDirectory(prefix="suite-empty-") as td:
        path = Path(td) / "empty.json"
        path.write_text(
            json.dumps(
                {
                    "id": "empty",
                    "target": "stock",
                    "fresh": True,
                    "cases": [],
                }
            ),
            encoding="utf-8",
        )
        try:
            sl.load_suite_file(path)
        except sl.SuiteLoadError as ex:
            assert "cases" in str(ex).lower()
        else:
            raise AssertionError("expected SuiteLoadError for empty cases")


def test_reject_invalid_json() -> None:
    with tempfile.TemporaryDirectory(prefix="suite-json-") as td:
        path = Path(td) / "broken.json"
        path.write_text("{not-json", encoding="utf-8")
        try:
            sl.load_suite_file(path)
        except sl.SuiteLoadError:
            pass
        else:
            raise AssertionError("expected SuiteLoadError for invalid JSON")


def test_fresh_defaults_true() -> None:
    with tempfile.TemporaryDirectory(prefix="suite-fresh-") as td:
        path = Path(td) / "nofresh.json"
        path.write_text(
            json.dumps(
                {
                    "id": "nofresh",
                    "target": "attach",
                    "cases": [{"id": "c1", "kind": "live", "ref": "A.B"}],
                }
            ),
            encoding="utf-8",
        )
        doc = sl.load_suite_file(path)
        # fresh is a hard rule; loader must treat missing as True or reject False.
        assert doc.fresh is True


def test_reject_fresh_false() -> None:
    with tempfile.TemporaryDirectory(prefix="suite-reuse-") as td:
        path = Path(td) / "reuse.json"
        path.write_text(
            json.dumps(
                {
                    "id": "reuse",
                    "target": "stock",
                    "fresh": False,
                    "cases": [{"id": "c1", "kind": "live", "ref": "A.B"}],
                }
            ),
            encoding="utf-8",
        )
        try:
            sl.load_suite_file(path)
        except sl.SuiteLoadError as ex:
            assert "fresh" in str(ex).lower()
        else:
            # If loader coerces to True instead of rejecting, still fail closed
            # on reuse-save: fresh must remain True.
            doc = sl.load_suite_file(path)
            assert doc.fresh is True


def main() -> int:
    fails = 0
    cases = [
        ("discover_builtin_suites", test_discover_builtin_suites),
        ("load_suite_by_id_missing_is_none", test_load_suite_by_id_missing_is_none),
        ("suite_to_report_shape", test_suite_to_report_shape),
        ("reject_unknown_target", test_reject_unknown_target),
        ("reject_empty_cases", test_reject_empty_cases),
        ("reject_invalid_json", test_reject_invalid_json),
        ("fresh_defaults_true", test_fresh_defaults_true),
        ("reject_fresh_false", test_reject_fresh_false),
    ]
    for name, fn in cases:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as ex:
            fails += 1
            print(f"FAIL {name}: {ex}")
    if fails:
        print(f"FAILED {fails}/{len(cases)}")
        return 1
    print(f"OK {len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
