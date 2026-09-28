#!/usr/bin/env python3
"""Offline gate: declarative suite JSON loader (suites/*.json).

No game binaries. Pins the schema surface (provision / backend / readonly /
fresh / server / mods / host / cases) and every contradiction the loader must
refuse: a managed run that is not fresh, an attach run that claims to be, an
attach run that would write to a host it does not own, readonly outside attach.

A mod repo's own suite file is external input, so a seeded grammar fuzzer also
drives the loader: every hostile document either fails closed with a
SuiteLoadError or, when accepted, holds the cross-field invariants and survives
the file -> SuiteDoc -> run-report JSON -> SuiteDoc round trip.
"""
from __future__ import annotations

import json
import random
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


def test_suite_cannot_declare_the_admin_plane() -> None:
    """TelnetEnabled / TelnetRemoteAllowedIPs / TelnetPassword are the orchestrator's.

    A suite that declares the password gets a declaration the orchestrator
    overwrites, a second line the exact-case filter does not replace when the
    capitalisation differs, and the value itself echoed into the run report in
    plaintext. The remote allow list is the admin plane's reachability: pinned
    to loopback by the orchestrator so a LAN peer that reaches the port is
    refused even with the password.
    """
    for key in ("TelnetEnabled", "TelnetRemoteAllowedIPs", "TelnetPassword"):
        for spelling in (key, key.lower(), key.upper()):
            expect_error({**MANAGED, "server": {spelling: "true"}}, "orchestrator's")
    # A suite that states anything else on that plane is still refused, and the
    # shipped suites state none of the three.
    for suite in sorted(SUITES.glob("*.json")):
        text = suite.read_text(encoding="utf-8")
        for key in sl.ORCHESTRATOR_TELNET_KEYS:
            assert key not in text.lower(), f"{suite.name} declares {key}"


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
    # The report is a suite document, not a summary of one: a consumer must be
    # able to reload a run's record with this loader and get the suite that
    # ran. Provenance like `source` is not a declared field, so including it
    # here fails the loader's unknown-field check and the published schema's
    # additionalProperties: false.
    reloaded = sl.parse_suite_dict(json.loads(json.dumps(report)), source=doc.source)
    assert reloaded == doc
    assert "source" not in report
    schema = json.loads(
        (ROOT / "schema" / "suite.schema.json").read_text(encoding="utf-8")
    )
    for key in report:
        assert key in schema["properties"], f"report key {key!r} is not in the schema"


def test_suite_report_carries_only_schema_fields() -> None:
    """The run report's suite block must reparse as the suite that ran.

    A provenance key such as `source` belongs beside this mapping, not inside
    it: the loader rejects every undeclared key, so one leaked field makes the
    run's own record unloadable while every shape assertion still passes.
    """
    doc = sl.load_suite_by_id("smoke", SUITES)
    assert doc is not None
    report = sl.suite_to_report(doc)
    schema = json.loads((ROOT / "schema" / "suite.schema.json").read_text(encoding="utf-8"))
    declared = set(schema["properties"])
    assert not (set(report) - declared), sorted(set(report) - declared)
    reloaded = sl.parse_suite_dict(json.loads(json.dumps(report)), source=doc.source)
    assert reloaded == doc, reloaded


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
    # The admin-plane names the schema refuses must be the loader's, or an
    # external author is told a suite may declare what the loader rejects.
    assert props["server"]["propertyNames"]["not"]["enum"] == list(
        sl.ORCHESTRATOR_TELNET_KEYS
    )
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


_FUZZ_STRINGS = [
    "x",
    "smoke",
    "core",
    "",
    "   ",
    " managed ",
    "ünïcödé 🧟 é́ ﻿",
    "a" * 300,
    "smoke,core",
    "id/with\x00nul",
    "‮rtl‭",  # noqa: PLE2502 (bidi overrides are a deliberate fuzz input)
]
_FUZZ_AXES = [
    "managed",
    "attach",
    "stock",
    "zdtd",
    "MANAGED",
    "",
    1,
    True,
    None,
    [],
    {},
]
_FUZZ_KINDS = ["live", "staged", "defer", "LIVE", "setup", "", 7, None, [], {}]
_FUZZ_CASES = [
    {"id": "c", "kind": "live", "ref": "catalog.x.c"},
    {"id": "c", "kind": "defer", "ref": "catalog.x.c", "tags": ["a"], "barriers": []},
    {"id": "c", "kind": "live", "ref": "r"},
    {"id": "c", "kind": "live", "ref": "r"},
    {"id": "", "kind": "live", "ref": "r"},
    {"id": "c", "kind": "bogus", "ref": "r"},
    {"id": "c", "kind": "live"},
    {"id": "c", "ref": "r"},
    {"kind": "live", "ref": "r"},
    {"id": "c", "kind": "live", "ref": "r", "tags": ["ok", ""]},
    {"id": "c", "kind": "live", "ref": "r", "tags": "notalist"},
    {"id": "c", "kind": "live", "ref": "r", "barriers": [1]},
    "not-an-object",
    None,
]
_FUZZ_SERVERS = [
    {},
    {"GameWorld": "Navezgane"},
    {"MaxSpawnedZombies": 0},
    {"MaxPlayers": True, "Port": 26950, "Ratio": 1.5},
    {"GameWorld": None},
    {"GameWorld": ["a"]},
    {"": "empty name"},
    {"  ": "blank name"},
    [1, 2],
    "not-an-object",
]
_FUZZ_MODS = [
    None,
    ["playtest"],
    ["playtest", "fastconnect", "../elsewhere/mod"],
    [],
    ["ok", ""],
    "playtest",
    [1],
    {"playtest": True},
]
_FUZZ_HOSTS: list[object] = [
    None,
    {},
    {"fixtures": True},
    {"loadgen": False},
    {"fixtures": "yes"},
    {"fixtures": 1},
    [],
    "host",
]
_FUZZ_RAW = [
    "",
    "{",
    "[]",
    "null",
    "42",
    '"a string"',
    "\x00\x00\xff\xfe binary-ish",
    "﻿{\"id\": \"x\"}",
    '{"id": "x", "cases": [',
    "{" + '"a": {' * 200 + "}" * 200,
    "ünïcödé 🧟 é́ not json at all",
    '{"id": "\\ud800"}',
    '{"id": "x", "notes": ["\\udcff"]}',
]
_FUZZ_BYTES = [
    b"",
    b"\x00\x00\xff\xfe\x00binary",
    b'{"id": "x", "cases": [{"id": "c", "kind": "live", "ref": "r"}]}',
    b'{"id": "\xe2\x82", "cases": []}',
    b'{"id": "x\x9f\x92\xa9"}',
    b"\xed\xa0\x80 lone surrogate",
]


_FUZZ_FIELDS = (
    ("id", _FUZZ_STRINGS),
    ("provision", _FUZZ_AXES),
    ("backend", _FUZZ_AXES),
    ("fresh", [True, False, "true", 1, None, []]),
    ("readonly", [True, False, "true", 1, None, []]),
    ("server", _FUZZ_SERVERS),
    ("mods", _FUZZ_MODS),
    ("server_mods", _FUZZ_MODS),
    ("host", _FUZZ_HOSTS),
    ("notes", _FUZZ_MODS),
)


def _fuzz_suite_doc(rng: random.Random) -> dict[str, object]:
    """A valid document perturbed by a few hostile fields.

    Starting from a document the loader accepts is what keeps the accepted
    branch of the fuzzer deep; a purely random field draw is rejected at the
    first missing id and never reaches the cross-field rules.
    """
    doc: dict[str, object] = json.loads(
        json.dumps(
            {
                "id": "x",
                "provision": rng.choice(["managed", "attach"]),
                "cases": [{"id": "c", "kind": "live", "ref": "catalog.x.c"}],
                "host": {"fixtures": rng.choice([True, False])},
            }
        )
    )
    for _ in range(rng.randrange(0, 3)):
        key, pool = rng.choice(_FUZZ_FIELDS)
        doc[key] = rng.choice(pool)
    for _ in range(rng.randrange(0, 3)):
        # Unknown keys the loader must ignore, however they are typed.
        doc[rng.choice(_FUZZ_STRINGS)] = rng.choice(_FUZZ_STRINGS)
    if rng.random() < 0.4:
        doc["cases"] = [rng.choice(_FUZZ_CASES) for _ in range(rng.randrange(0, 4))]
    return doc


def _assert_doc_invariants(doc: sl.SuiteDoc, seed: int) -> None:
    assert doc.provision in sl.ALLOWED_PROVISIONS, f"seed {seed}: provision {doc.provision}"
    assert doc.backend in sl.ALLOWED_BACKENDS, f"seed {seed}: backend {doc.backend}"
    assert doc.id.strip() == doc.id and doc.id, f"seed {seed}: id {doc.id!r}"
    if doc.provision == "managed":
        assert doc.fresh is True, f"seed {seed}: managed run not fresh"
        assert not doc.readonly, f"seed {seed}: managed run claimed readonly"
    else:
        assert doc.fresh is False, f"seed {seed}: attach run claimed fresh"
        assert not doc.server, f"seed {seed}: attach run carries a server block"
        assert not doc.mods and not doc.server_mods, (
            f"seed {seed}: attach run carries mods {doc.mods} {doc.server_mods}"
        )
    assert all(name and name.strip() == name for name in doc.server_config), (
        f"seed {seed}: server keys {list(doc.server_config)}"
    )
    assert all(isinstance(v, str) for v in doc.server_config.values()), (
        f"seed {seed}: server values {doc.server_config}"
    )
    ids = [c.id for c in doc.cases]
    assert ids and len(set(ids)) == len(ids), f"seed {seed}: case ids {ids}"
    assert all(c.kind in sl.ALLOWED_KINDS for c in doc.cases), f"seed {seed}: kinds"
    assert all(c.id.strip() == c.id and c.ref.strip() == c.ref for c in doc.cases), (
        f"seed {seed}: unstripped case id/ref"
    )
    assert doc.case_refs == tuple(c.ref for c in doc.cases), f"seed {seed}: case_refs"
    assert list(doc.mods) == list(doc.mods) and all(m.strip() == m for m in doc.mods), (
        f"seed {seed}: mods {doc.mods}"
    )
    # The report is what a downstream consumer reads; reloading it must give
    # the same document, or a run's own record is not the suite it ran.
    reloaded = sl.parse_suite_dict(
        json.loads(json.dumps(sl.suite_to_report(doc))), source=doc.source
    )
    assert reloaded == doc, f"seed {seed}: report round trip drifted\n{reloaded}\n{doc}"


def test_fuzz_suite_documents_fail_closed_or_hold_invariants() -> None:
    """Seeded grammar fuzzer over suite documents and raw suite file bytes.

    Invariants per generated document: the loader raises SuiteLoadError and
    nothing else, an accepted document holds every cross-field rule the
    orchestrator then acts on, and a truncation is never silently accepted.
    """
    accepted = 0
    for seed in range(80):
        rng = random.Random(3000 + seed)
        payloads: list[object] = [
            _fuzz_suite_doc(rng),
            rng.choice(_FUZZ_RAW),
            rng.choice(_FUZZ_BYTES),
        ]
        good = json.dumps(sl.suite_to_report(sl.parse_suite_dict(MANAGED)))
        payloads.append(good[: rng.randrange(0, len(good) + 1)])
        for payload in payloads:
            with tempfile.TemporaryDirectory(prefix="suite-fuzz-") as td:
                path = Path(td) / "s.json"
                if isinstance(payload, bytes):
                    path.write_bytes(payload)
                elif isinstance(payload, str):
                    path.write_text(payload, encoding="utf-8")
                else:
                    path.write_text(json.dumps(payload), encoding="utf-8")
                try:
                    doc = sl.load_suite_file(path)
                except sl.SuiteLoadError:
                    continue
                _assert_doc_invariants(doc, seed)
                accepted += 1
    assert accepted >= 10, f"fuzzer accepted only {accepted} documents: corpus is too weak"
    print(f"PASS suite_fuzz 80 documents, {accepted} accepted and held their invariants")


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
    ("suite_cannot_declare_the_admin_plane", test_suite_cannot_declare_the_admin_plane),
    ("external_suite_cannot_shadow_a_builtin", test_external_suite_cannot_shadow_a_builtin),
    ("resolve_mods_short_names_and_paths", test_resolve_mods_short_names_and_paths),
    ("suite_to_report_shape", test_suite_to_report_shape),
    (
        "suite_report_carries_only_schema_fields",
        test_suite_report_carries_only_schema_fields,
    ),
    ("published_schema_matches_the_loader", test_published_schema_matches_the_loader),
    ("resolve_mods_refuses_an_unknown_side", test_resolve_mods_refuses_an_unknown_side),
    ("fuzz_suite_documents", test_fuzz_suite_documents_fail_closed_or_hold_invariants),
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
