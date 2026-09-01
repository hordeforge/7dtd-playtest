#!/usr/bin/env python3
"""Offline gate: declarative suite JSON loader (suites/*.json).

No game binaries. Pins the schema surface (provision / backend / readonly /
fresh / server / mods / host / cases) and every contradiction the loader must
refuse: a managed run that is not fresh, an attach run that claims to be, an
attach run that would write to a host it does not own, readonly outside attach.
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

MANAGED = {
    "id": "x",
    "cases": [{"id": "c", "kind": "live", "ref": "catalog.x.c"}],
}


def write(tmp: Path, doc: dict, name: str = "s.json") -> Path:
    path = tmp / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def expect_error(doc: dict, needle: str) -> None:
    with tempfile.TemporaryDirectory(prefix="suite-loader-") as td:
        path = write(Path(td), doc)
        try:
            sl.load_suite_file(path)
        except sl.SuiteLoadError as ex:
            assert needle in str(ex), f"want {needle!r} in {ex}"
        else:
            raise AssertionError(f"expected SuiteLoadError mentioning {needle!r}")


def test_discover_builtin_suites() -> None:
    found = sl.discover_suites(SUITES)
    assert "smoke" in found
    assert "core" in found
    smoke = found["smoke"]
    assert smoke.provision == "managed"
    assert smoke.backend == "stock"
    assert smoke.readonly is False
    assert smoke.fresh is True
    assert smoke.host.fixtures is False
    assert smoke.server_config["GameWorld"] == "Navezgane"
    assert len(smoke.cases) >= 1
    assert all(c.ref for c in smoke.cases)
    assert all(c.kind in ("live", "staged", "defer") for c in smoke.cases)


def test_load_suite_by_id_missing_is_none() -> None:
    assert sl.load_suite_by_id("demo", SUITES) is None
    assert sl.load_suite_by_id("smoke", SUITES) is not None


def test_defaults_are_managed_stock_fresh() -> None:
    doc = sl.parse_suite_dict(dict(MANAGED))
    assert doc.provision == "managed"
    assert doc.backend == "stock"
    assert doc.fresh is True
    assert doc.readonly is False
    assert doc.mods == sl.DEFAULT_MODS
    assert doc.server_mods == (), "the server gets no client mods by default"


def test_attach_is_never_fresh_and_writes_nothing() -> None:
    """The live-server contradiction: an attach run does not own the save."""
    doc = sl.parse_suite_dict({**MANAGED, "provision": "attach", "readonly": True})
    assert doc.fresh is False
    assert doc.readonly is True
    assert doc.mods == ()
    assert doc.server_config == {}

    expect_error({**MANAGED, "provision": "attach", "fresh": True}, "cannot claim fresh")
    expect_error(
        {**MANAGED, "provision": "attach", "server": {"GameWorld": "Navezgane"}},
        "does not own",
    )
    expect_error({**MANAGED, "provision": "attach", "mods": ["playtest"]}, "does not own")
    expect_error({**MANAGED, "provision": "attach", "server_mods": ["x"]}, "does not own")


def test_managed_must_be_fresh() -> None:
    expect_error({**MANAGED, "fresh": False}, "fresh must be true")


def test_readonly_requires_attach() -> None:
    expect_error({**MANAGED, "readonly": True}, "requires provision 'attach'")


def test_reject_unknown_axes() -> None:
    expect_error({**MANAGED, "provision": "prod"}, "provision 'prod' not in")
    expect_error({**MANAGED, "backend": "bedrock"}, "backend 'bedrock' not in")


def test_reject_empty_and_duplicate_cases() -> None:
    expect_error({**MANAGED, "cases": []}, "cases must be a non-empty list")
    expect_error(
        {
            **MANAGED,
            "cases": [
                {"id": "c", "kind": "live", "ref": "a"},
                {"id": "c", "kind": "live", "ref": "b"},
            ],
        },
        "duplicate case id",
    )
    expect_error(
        {**MANAGED, "cases": [{"id": "c", "kind": "sideways", "ref": "a"}]},
        "kind 'sideways' not in",
    )


def test_reject_invalid_json() -> None:
    with tempfile.TemporaryDirectory(prefix="suite-loader-") as td:
        path = Path(td) / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        try:
            sl.load_suite_file(path)
        except sl.SuiteLoadError as ex:
            assert "invalid JSON" in str(ex)
        else:
            raise AssertionError("expected SuiteLoadError for malformed JSON")


def test_server_values_are_stringified_for_the_game() -> None:
    """Stock ParseBool takes true/false, never Python's True/False."""
    doc = sl.parse_suite_dict(
        {**MANAGED, "server": {"EACEnabled": False, "WorldGenSize": 4096, "GameWorld": "Nav"}}
    )
    assert doc.server_config == {
        "EACEnabled": "false",
        "WorldGenSize": "4096",
        "GameWorld": "Nav",
    }


def test_external_suite_cannot_shadow_a_builtin() -> None:
    with tempfile.TemporaryDirectory(prefix="suite-loader-") as td:
        path = write(Path(td), {**MANAGED, "id": "smoke"}, "smoke.json")
        try:
            sl.load_external_suite(path)
        except sl.SuiteLoadError as ex:
            assert "built-in stock-fidelity suite" in str(ex)
        else:
            raise AssertionError("expected SuiteLoadError for a shadowed built-in id")
        # A distinct id loads fine.
        other = write(Path(td), {**MANAGED, "id": "mod_acceptance"}, "mod.json")
        assert sl.load_external_suite(other).id == "mod_acceptance"


def test_resolve_mods_short_names_and_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="suite-loader-") as td:
        tmp = Path(td)
        path = write(tmp, {**MANAGED, "mods": ["playtest", "fastconnect", "../dist/MyMod"]})
        doc = sl.load_suite_file(path)
        mods = sl.resolve_mods(doc, workspace=Path("/ws"), repo=Path("/repo"))
        assert mods[0] == Path("/repo/dist/7dtd-playtest")
        assert mods[1] == Path("/ws/7dtd-fastconnect/dist/7dtd-fastconnect")
        assert mods[2] == (tmp.parent / "dist" / "MyMod").resolve()
        # The two sides resolve independently.
        server = write(tmp, {**MANAGED, "mods": [], "server_mods": ["playtest"]}, "s2.json")
        sdoc = sl.load_suite_file(server)
        assert sl.resolve_mods(sdoc, workspace=Path("/ws"), repo=Path("/repo")) == []
        assert sl.resolve_mods(
            sdoc, workspace=Path("/ws"), repo=Path("/repo"), side="server"
        ) == [Path("/repo/dist/7dtd-playtest")]


def test_suite_to_report_shape() -> None:
    doc = sl.load_suite_by_id("smoke", SUITES)
    assert doc is not None
    report = sl.suite_to_report(doc)
    for key in (
        "id", "provision", "backend", "readonly", "fresh", "mods", "server_mods",
        "server", "cases",
    ):
        assert key in report, key
    server = report["server"]
    cases = report["cases"]
    assert isinstance(server, dict)
    assert isinstance(cases, list)
    assert str(cases[0]["ref"]).startswith("catalog.")


TESTS = (
    ("discover_builtin_suites", test_discover_builtin_suites),
    ("load_suite_by_id_missing_is_none", test_load_suite_by_id_missing_is_none),
    ("defaults_are_managed_stock_fresh", test_defaults_are_managed_stock_fresh),
    ("attach_is_never_fresh_and_writes_nothing", test_attach_is_never_fresh_and_writes_nothing),
    ("managed_must_be_fresh", test_managed_must_be_fresh),
    ("readonly_requires_attach", test_readonly_requires_attach),
    ("reject_unknown_axes", test_reject_unknown_axes),
    ("reject_empty_and_duplicate_cases", test_reject_empty_and_duplicate_cases),
    ("reject_invalid_json", test_reject_invalid_json),
    ("server_values_are_stringified_for_the_game", test_server_values_are_stringified_for_the_game),
    ("external_suite_cannot_shadow_a_builtin", test_external_suite_cannot_shadow_a_builtin),
    ("resolve_mods_short_names_and_paths", test_resolve_mods_short_names_and_paths),
    ("suite_to_report_shape", test_suite_to_report_shape),
)


def main() -> int:
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as ex:
            fails += 1
            print(f"FAIL {name}: {ex}")
    if fails:
        print(f"FAILED {fails}/{len(TESTS)}")
        return 1
    print(f"OK {len(TESTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
