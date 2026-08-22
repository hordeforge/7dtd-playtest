#!/usr/bin/env python3
"""Offline gate: orchestrator report/log surface stays injection- and crash-safe.

write_junit renders client-log-derived case ids and details into JUnit XML
consumed by CI UIs, write_stock_config renders operator values into the
generated serverconfig.xml, parse_client_log eats arbitrary game log lines,
and barrier_hits_prefix greps fixture barriers out of those logs (repeats are
events, never duplicates to collapse). A hostile or corrupt log line must not
break out of an XML attribute or crash either parser.

Beyond fixed regression cases, two seeded grammar fuzzers drive those same
surfaces with hostile blobs (inf/nan counts, wrong JSON types, NUL bytes,
control soup, attribute-breakout markup) and assert structural invariants:
the parser never raises and stays deterministic under input doubling, and
every JUnit render reparses well-formed with values round-tripped.
"""
from __future__ import annotations

import contextlib
import io
import random
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_run


def test_write_junit_escapes_log_derived_attributes() -> None:
    results = [
        {
            "case": 'smoke/break"out<script>alert(1)</script>',
            "status": "FAIL",
            "detail": 'hp=0 & "quoted" <tag> detail',
        }
    ]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "junit.xml"
        playtest_run.write_junit(path, 'suite"&x', results, {"pass": 0, "fail": 1})
        # Parses as well-formed XML and every value round-trips intact: the
        # injected markup stayed text, never became elements or attributes.
        root = ElementTree.parse(path).getroot()
        cases = root.findall("testcase")
        assert len(cases) == 1, f"expected 1 testcase, got {len(cases)}"
        assert cases[0].get("name") == results[0]["case"]
        failure = cases[0].find("failure")
        assert failure is not None, "FAIL case must keep its <failure>"
        assert failure.get("message") == results[0]["detail"]
        assert root.get("name") == '7dtd-playtest.suite"&x'
        print("PASS junit_escape attribute breakout rendered inert")


def test_parse_client_log_survives_null_numbers() -> None:
    text = (
        '[7dtd-playtest] {"v":1,"t":"summary","pass":null,"fail":1,"skip":0}\n'
        '[7dtd-playtest] {"v":1,"t":"result","suite":"s","case":"c",'
        '"status":"pass","ms":12,"detail":"ok"}\n'
        '[7dtd-playtest] {"v":1,"t":"done","exit_hint":[1,2]}\n'
        '[7dtd-playtest] PASS s/c2 ok\n'
    )
    parsed = playtest_run.parse_client_log(text)
    # The malformed summary/done events are skipped, the valid result event
    # survives, and summary falls back to recounting the surviving results.
    assert parsed["done"] is None, f"bad done event must be dropped: {parsed['done']}"
    assert parsed["summary"] == {"pass": 1, "fail": 0, "skip": 0}, (
        f"summary must be recounted from surviving results: {parsed['summary']}"
    )
    assert parsed["results"] == [
        {"status": "PASS", "case": "s/c", "detail": "ok"}
    ], f"valid sibling event lost or mangled: {parsed['results']}"
    print("PASS log_parse_bad_json no TypeError on null/array counts")


def test_parse_client_log_survives_inf_and_type_garbage() -> None:
    """Regression pins for crashes the log fuzzer found: int(inf) raised
    OverflowError past the (TypeError, ValueError) net, and a non-string
    status/detail crashed .upper() or downstream string consumers."""
    text = (
        '[7dtd-playtest] {"v":1,"t":"summary","pass":1e999,"fail":0,"skip":0}\n'
        '[7dtd-playtest] {"v":1,"t":"done","exit_hint":-Infinity}\n'
        '[7dtd-playtest] {"v":1,"t":"result","suite":"s","case":"c",'
        '"status":["pass"],"detail":12}\n'
        '[7dtd-playtest] PASS s/c2 ok\n'
    )
    parsed = playtest_run.parse_client_log(text)
    # Both inf events are dropped, so the summary falls back to recounting
    # the surviving results; the coerced "['PASS']" status matches none of
    # the three countable statuses.
    assert parsed["summary"] == {"pass": 0, "fail": 0, "skip": 0}, (
        f"inf summary must be dropped and recounted: {parsed['summary']}"
    )
    assert parsed["done"] is None, f"inf exit_hint must be dropped: {parsed['done']}"
    # JSON events take precedence over human lines, so the trailing human
    # PASS is not part of results; the garbage-typed event is coerced to
    # strings, not lost, so write_junit/compare cannot raise on it.
    assert parsed["results"] == [
        {"status": "['PASS']", "case": "s/c", "detail": "12"}
    ], f"coerced results wrong: {parsed['results']}"
    print("PASS log_parse_inf_garbage OverflowError and non-string fields contained")


def test_barrier_hits_prefix_keeps_repeats_and_scope() -> None:
    """barrier_hits_prefix feeds spawn_vehicle:/chat_echo: consumers that keep
    their own fired counts, so repeated barrier lines are events and must not
    collapse; non-barrier lines and other prefixes must not match."""
    blob = (
        "[7dtd-playtest] barrier spawn_vehicle:bicycle\n"
        "[7dtd-playtest] PASS smoke/ok detail=ok\n"
        "[7dtd-playtest] barrier spawn_vehicle:jeep\n"
        "[7dtd-playtest] barrier spawn_vehicle:bicycle\n"
        "[game] barrier spawn_vehicle:not_ours\n"
        "[7dtd-playtest] barrier chat_echo:hello\n"
    )
    hits = playtest_run.barrier_hits_prefix(blob, "spawn_vehicle:")
    assert hits == [
        "spawn_vehicle:bicycle",
        "spawn_vehicle:jeep",
        "spawn_vehicle:bicycle",
    ], f"repeated barriers collapsed or misparsed: {hits}"
    assert playtest_run.barrier_hits_prefix(blob, "chat_echo:") == ["chat_echo:hello"]
    print("PASS barrier_prefix repeats preserved, foreign lines excluded")


def test_write_stock_config_escapes_values() -> None:
    src = (
        "<ServerSettings>\n"
        '  <property name="GameWorld" value="Navezgane"/>\n'
        '  <property name="GameName" value="PlaytestNav"/>\n'
        '  <property name="TelnetPassword" value="old"/>\n'
        "</ServerSettings>\n"
    )
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src_cfg = tdp / "serverconfig.xml"
        out_cfg = tdp / "out" / "serverconfig_playtest.xml"
        src_cfg.write_text(src, encoding="utf-8")
        playtest_run.write_stock_config(
            src_cfg,
            out_cfg,
            tdp / "userdata",
            world_name='Nav"x',
            game_name='Play&<t>',
            port=26900,
            telnet_port=8081,
            telnet_password='sec"ret&<pw>',
        )
        root = ElementTree.parse(out_cfg).getroot()
        props = {p.get("name"): p.get("value") for p in root.iter("property")}
        assert props["GameWorld"] == 'Nav"x', props
        assert props["GameName"] == "Play&<t>", props
        assert props["TelnetPassword"] == 'sec"ret&<pw>', props
        assert 'value="Nav"' not in out_cfg.read_text(encoding="utf-8"), (
            "raw quote survived into the generated config"
        )
    print("PASS stock_config_escape values stay inside their XML attributes")


# Fuzz grammars for the two log-derived surfaces below. Deterministic seeds:
# a failure prints its seed and the blob so the exact input can be pasted as
# the next fixed regression case. The pools deliberately carry every crash and
# injection class found so far (int(inf) OverflowError, null/array counts,
# NUL and other XML-illegal controls, attribute breakout markup) plus random
# junk, because a fuzzer proves presence of bugs; these assertions are what
# turn a future parser regression into a red gate instead of a dead run.

_EVENT_FRAGMENTS = [
    # Well-formed events the client actually emits.
    '[7dtd-playtest] {"v":1,"t":"result","suite":"s","case":"c","status":"pass",'
    '"ms":12,"detail":"ok"}',
    '[7dtd-playtest] {"v":1,"t":"summary","pass":1,"fail":2,"skip":3}',
    '[7dtd-playtest] {"v":1,"t":"done","exit_hint":0}',
    "[7dtd-playtest] barrier spawn_vehicle:bicycle",
    "[7dtd-playtest] PASS smoke/dig detail=hp=100",
    "[7dtd-playtest] SUMMARY pass=1 fail=0 skip=0",
    "[7dtd-playtest] DONE exit_hint=0",
    # Counts that int() cannot take: inf via exponent overflow or bare
    # Infinity/NaN tokens, negative exponents.
    '[7dtd-playtest] {"t":"summary","pass":1e999,"fail":0,"skip":0}',
    '[7dtd-playtest] {"t":"done","exit_hint":-1e999}',
    '[7dtd-playtest] {"t":"summary","pass":Infinity,"fail":-Infinity,"skip":NaN}',
    '[7dtd-playtest] {"t":"done","exit_hint":1e309}',
    # Wrong JSON types where scalars belong.
    '[7dtd-playtest] {"t":"summary","pass":null,"fail":[1],"skip":{"a":1}}',
    '[7dtd-playtest] {"t":"result","status":["pass"],"case":null,"detail":12}',
    '[7dtd-playtest] {"t":"done","exit_hint":"zero"}',
    # Not objects at all, truncated or fake JSON-looking text.
    "[7dtd-playtest] [1,2,3]",
    '[7dtd-playtest] "just a string"',
    "[7dtd-playtest] 42",
    '[7dtd-playtest] {"t":"summary","pass":',
    "[7dtd-playtest] {}",
    '[game] {"t":"summary","pass":1} not our prefix',
]

_JUNK_LINES = [
    "",
    "\x00\x00binary-ish\x00",
    "NullReferenceException: Object reference not set",
    "chat: player said <script>alert('&\"')</script>",
    "\x01\x02\x1f[7dtd-playtest]\x7fcontrol soup",
    "ünïcödé　全角 combining é́ emoji 🧟 BOM ﻿ NBSP x",
    "[7dtd-playtest] " + "{" + "}" * 400,
    "[" + "9" * 300 + "] PASS x/y " + "=" * 500,
]


def _log_fuzz_blob(rng: random.Random) -> str:
    lines = [
        rng.choice(_EVENT_FRAGMENTS + _JUNK_LINES)
        for _ in range(rng.randrange(0, 18))
    ]
    if rng.random() < 0.3 and lines:
        idx = rng.randrange(len(lines))
        lines[idx] = lines[idx].replace("[7dtd-playtest]", "\t[7dtd-playtest] ", 1)
    sep = "\r\n" if rng.random() < 0.2 else "\n"
    blob = sep.join(lines)
    if rng.random() < 0.8:
        blob += sep
    return blob


def _assert_parsed_shape(parsed: dict, seed: int) -> None:
    assert set(parsed) == {
        "results",
        "summary",
        "done",
        "json_events",
        "nre_like",
    }, f"seed {seed}: unexpected parse keys {sorted(parsed)}"
    for r in parsed["results"]:
        assert set(r) == {"status", "case", "detail"}, f"seed {seed}: result keys {r}"
        assert all(isinstance(v, str) for v in r.values()), f"seed {seed}: result {r}"
    assert all(isinstance(e, dict) for e in parsed["json_events"]), (
        f"seed {seed}: json_events leaked a non-dict"
    )
    done = parsed["done"]
    assert done is None or set(done) == {"exit_hint"}, f"seed {seed}: done shape {done}"
    assert done is None or isinstance(done["exit_hint"], (int, type(None))), (
        f"seed {seed}: exit_hint type {done}"
    )
    summary = parsed["summary"]
    if summary is not None:
        assert set(summary) == {"pass", "fail", "skip"}, f"seed {seed}: summary {summary}"
        assert all(
            isinstance(v, int) and not isinstance(v, bool) for v in summary.values()
        ), f"seed {seed}: summary types {summary}"
    assert len(parsed["nre_like"]) <= 50, f"seed {seed}: nre cap broken"


def test_fuzz_parse_client_log_survives_hostile_logs() -> None:
    """Seeded grammar fuzzer over arbitrary client-log text.

    Invariants per generated blob: never raises, result/event shapes hold,
    parsing is deterministic, and duplicating the input exactly doubles the
    matched results (no line may collapse or be deduplicated away)."""
    for seed in range(60):
        rng = random.Random(seed)
        # Doubling compares line sequences, so the blob must end at a line
        # boundary or concatenation would merge its last line with its own
        # first copy and change what matches.
        blob = _log_fuzz_blob(rng)
        if not blob.endswith("\n"):
            blob += "\n"
        parsed = playtest_run.parse_client_log(blob)
        _assert_parsed_shape(parsed, seed)
        again = playtest_run.parse_client_log(blob)
        assert again == parsed, f"seed {seed}: parse is nondeterministic"
        doubled = playtest_run.parse_client_log(blob * 2)
        _assert_parsed_shape(doubled, seed)
        n_single = len(parsed["results"])
        assert len(doubled["results"]) == 2 * n_single, (
            f"seed {seed}: doubling changed result count "
            f"{n_single} -> {len(doubled['results'])}"
        )
    print("PASS log_fuzz 60 hostile blobs parsed without crash or drift")


_JUNIT_CHARS = [
    '"',
    "'",
    "<",
    ">",
    "&",
    "\x00",
    "\x01",
    "\x0b",
    "\x1f",
    "\x7f",
    "\x9f",
    "\t",
    "\n",
    "\r",
    "﻿",
    " ",
    "é́",
    "‮rtl‭",
    "🧟",
    "</testsuite><!--",
    '="',
    "%s%s%s",
]
_JUNIT_STATUSES = ["FAIL", "SKIP", "PASS", "weird", "", "fail"]


def _xml_attr_expected(value: str) -> str:
    """Mirror the serializer contract, restated from the XML 1.0 Char
    production: characters illegal in any XML document are dropped by the
    writer, and literal tab/LF/CR inside attribute values become spaces
    when the document is reparsed (attribute-value normalization)."""
    kept = []
    for ch in value:
        o = ord(ch)
        if o <= 0x1F and o not in (0x09, 0x0A, 0x0D):
            continue
        if 0x7F <= o <= 0x9F or 0xD800 <= o <= 0xDFFF or o in (0xFFFE, 0xFFFF):
            continue
        kept.append(ch)
    return (
        "".join(kept).replace("\t", " ").replace("\n", " ").replace("\r", " ")
    )


def test_fuzz_write_junit_roundtrips_hostile_strings() -> None:
    """Seeded fuzzer over log-derived suite/case/detail strings.

    Invariants per iteration: the emitted JUnit always reparses as
    well-formed XML (no NUL or breakout survives), and every attribute value
    round-trips through that reparse under the serializer contract."""
    with tempfile.TemporaryDirectory() as td:
        for seed in range(50):
            rng = random.Random(1000 + seed)

            def nasty() -> str:
                return "".join(rng.choice(_JUNIT_CHARS) for _ in range(rng.randrange(0, 8)))

            case = f"s/{nasty()}c{nasty()}"
            detail = nasty()
            suite = nasty()
            status = rng.choice(_JUNIT_STATUSES)
            path = Path(td) / f"junit-{seed}.xml"
            # write_junit logs every render; keep the 50 iterations quiet.
            with contextlib.redirect_stdout(io.StringIO()):
                playtest_run.write_junit(
                    path,
                    suite,
                    [{"case": case, "status": status, "detail": detail}],
                    None,
                )
            root = ElementTree.parse(path).getroot()
            cases = root.findall("testcase")
            assert len(cases) == 1, f"seed {seed}: testcase lost"
            expect_case = _xml_attr_expected(case)
            assert cases[0].get("name") == expect_case, (
                f"seed {seed}: case {case!r} rendered as {cases[0].get('name')!r}, "
                f"expected {expect_case!r}"
            )
            expect_suite = _xml_attr_expected(suite)
            assert root.get("name") == f"7dtd-playtest.{expect_suite}", (
                f"seed {seed}: suite name round-trip broke"
            )
            child = cases[0].find("failure") if status == "FAIL" else None
            if status == "SKIP":
                child = cases[0].find("skipped")
            if child is not None:
                expect_detail = _xml_attr_expected(detail)
                assert child.get("message") == expect_detail, (
                    f"seed {seed}: detail {detail!r} rendered as "
                    f"{child.get('message')!r}, expected {expect_detail!r}"
                )
    print("PASS junit_fuzz 50 hostile renders well-formed and round-tripped")


def test_write_junit_drops_xml_illegal_characters() -> None:
    """Regression pin for the NUL crash the junit fuzzer found: control
    bytes are legal UTF-8 and survive log decoding with errors=replace, but
    they cannot be escaped into XML 1.0, so the writer must drop them or
    the whole JUnit report is unparseable by CI."""
    results = [
        {"case": "s/c\x00x", "status": "FAIL", "detail": "hp=0\x0b\x7f"},
    ]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "junit.xml"
        playtest_run.write_junit(path, "smoke", results, None)
        root = ElementTree.parse(path).getroot()
        case = root.find("testcase")
        assert case.get("name") == "s/cx", f"NUL must be dropped: {case.get('name')!r}"
        assert case.find("failure").get("message") == "hp=0", (
            "illegal controls must be dropped from detail"
        )
    print("PASS junit_illegal_chars NUL/control bytes dropped, document stays valid")


def main() -> int:
    test_write_junit_escapes_log_derived_attributes()
    test_parse_client_log_survives_null_numbers()
    test_parse_client_log_survives_inf_and_type_garbage()
    test_barrier_hits_prefix_keeps_repeats_and_scope()
    test_write_stock_config_escapes_values()
    test_write_junit_drops_xml_illegal_characters()
    test_fuzz_parse_client_log_survives_hostile_logs()
    test_fuzz_write_junit_roundtrips_hostile_strings()
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
