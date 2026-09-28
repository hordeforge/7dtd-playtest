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
from catalog_surface import REF_PREFIX  # noqa: E402

ROOT = _SCRIPTS.parent
SUITES = ROOT / "suites"

MANAGED = {
    "id": "x",
    "cases": [{"id": "c", "kind": "live", "ref": "catalog.x.c"}],
}


def write(tmp: Path, doc: dict[str, object], name: str = "s.json") -> Path:
    path = tmp / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def expect_error(doc: dict[str, object], needle: str) -> None:
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
    assert doc.server_mods == sl.DEFAULT_MODS, (
        "both sides run the same mods by default; a C# mod on one side only "
        "desynchronises the connection"
    )


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


def test_reject_unknown_fields() -> None:
    """A misspelled key reads as "unset", which is the dangerous direction:
    `provison: attach` on a production suite left the run managed, so it
    wiped the world it was meant to attach read-only to."""
    expect_error({**MANAGED, "provison": "attach"}, "unknown field(s) 'provison'")
    expect_error({**MANAGED, "read_only": True}, "unknown field(s) 'read_only'")
    expect_error(
        {**MANAGED, "host": {"fixture": True}}, "unknown field(s) 'fixture'"
    )
    expect_error(
        {
            **MANAGED,
            "cases": [{"id": "c", "kind": "live", "ref": "a", "tag": "x"}],
        },
        "unknown field(s) 'tag'",
    )


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


def test_non_utf8_suite_file_fails_closed() -> None:
    """A suite doc is UTF-8 by contract; a cp1252 byte is a load error, not a traceback.

    The orchestrator catches SuiteLoadError, so an undecodable file that
    escaped as UnicodeDecodeError would abort the run with a stack trace
    instead of naming the file.
    """
    with tempfile.TemporaryDirectory(prefix="suite-loader-") as td:
        path = Path(td) / "cp1252.json"
        path.write_bytes(json.dumps(MANAGED).encode("utf-8").replace(b'"x"', b'"caf\xe9"'))
        try:
            sl.load_suite_file(path)
        except sl.SuiteLoadError as ex:
            assert "not valid UTF-8" in str(ex)
        else:
            raise AssertionError("expected SuiteLoadError for a non-UTF-8 suite file")


def test_non_ascii_suite_fields_load_verbatim() -> None:
    """UTF-8 is what the loader decodes, so a non-ASCII id is carried, not mangled."""
    with tempfile.TemporaryDirectory(prefix="suite-loader-") as td:
        path = write(Path(td), {**MANAGED, "id": "café"}, "u.json")
        assert sl.load_suite_file(path).id == "café"


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
    assert str(cases[0]["ref"]).startswith(REF_PREFIX)


def test_published_schema_matches_the_loader() -> None:
    """schema/suite.schema.json is what an external suite author reads.

    A default or enum documented there and contradicted by the loader is the
    worst failure this surface has: the document is well formed, so nothing
    fails closed and the suite runs with mods on the wrong side.
    """
    schema = json.loads((ROOT / "schema" / "suite.schema.json").read_text(encoding="utf-8"))
    props = schema["properties"]
    assert props["provision"]["enum"] == list(sl.ALLOWED_PROVISIONS)
    assert props["backend"]["enum"] == list(sl.ALLOWED_BACKENDS)
    case_props = props["cases"]["items"]["properties"]
    assert case_props["kind"]["enum"] == list(sl.ALLOWED_KINDS)
    assert props["mods"]["default"] == list(sl.DEFAULT_MODS)
    assert props["server_mods"]["default"] == list(sl.DEFAULT_SERVER_MODS)
    # The defaults the schema states must be the ones an omitted field gets.
    managed = sl.parse_suite_dict(MANAGED)
    assert managed.mods == tuple(props["mods"]["default"])
    assert managed.server_mods == tuple(props["server_mods"]["default"])


def test_resolve_mods_refuses_an_unknown_side() -> None:
    doc = sl.parse_suite_dict(MANAGED)
    try:
        sl.resolve_mods(doc, workspace=Path("/ws"), repo=Path("/repo"), side="clientt")
    except sl.SuiteLoadError as ex:
        assert "side must be client or server" in str(ex)
    else:
        raise AssertionError("expected SuiteLoadError for an unknown side")


TESTS = (
    ("discover_builtin_suites", test_discover_builtin_suites),
    ("load_suite_by_id_missing_is_none", test_load_suite_by_id_missing_is_none),
    ("defaults_are_managed_stock_fresh", test_defaults_are_managed_stock_fresh),
    ("attach_is_never_fresh_and_writes_nothing", test_attach_is_never_fresh_and_writes_nothing),
    ("managed_must_be_fresh", test_managed_must_be_fresh),
    ("readonly_requires_attach", test_readonly_requires_attach),
    ("reject_unknown_axes", test_reject_unknown_axes),
    ("reject_unknown_fields", test_reject_unknown_fields),
    ("reject_empty_and_duplicate_cases", test_reject_empty_and_duplicate_cases),
    ("reject_invalid_json", test_reject_invalid_json),
    ("non_utf8_suite_file_fails_closed", test_non_utf8_suite_file_fails_closed),
    ("non_ascii_suite_fields_load_verbatim", test_non_ascii_suite_fields_load_verbatim),
    ("server_values_are_stringified_for_the_game", test_server_values_are_stringified_for_the_game),
    ("external_suite_cannot_shadow_a_builtin", test_external_suite_cannot_shadow_a_builtin),
    ("resolve_mods_short_names_and_paths", test_resolve_mods_short_names_and_paths),
    ("suite_to_report_shape", test_suite_to_report_shape),
    ("published_schema_matches_the_loader", test_published_schema_matches_the_loader),
    ("resolve_mods_refuses_an_unknown_side", test_resolve_mods_refuses_an_unknown_side),
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
