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
import json
import random
import sys
import tempfile
from itertools import pairwise
from pathlib import Path
from xml.etree import ElementTree

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import playtest_log  # noqa: E402
import playtest_run  # noqa: E402
import report_summary  # noqa: E402
from playtest_log import ParsedClientLog  # noqa: E402


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
        playtest_run.write_junit(path, 'suite"&x', results)
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
    parsed = playtest_log.parse_client_log(text)
    # The malformed summary/done events are skipped, the valid result event
    # survives, and summary falls back to recounting the surviving results.
    assert parsed["done"] is None, f"bad done event must be dropped: {parsed['done']}"
    assert parsed["summary"] == {"pass": 1, "fail": 0, "skip": 0}, (
        f"summary must be recounted from surviving results: {parsed['summary']}"
    )
    assert parsed["results"] == [
        {"status": "PASS", "case": "s/c", "detail": "ok"}
    ], f"valid sibling event lost or mangled: {parsed['results']}"
    assert parsed["malformed_events"] == 2, (
        f"dropped events must be counted, got {parsed['malformed_events']}"
    )
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
    parsed = playtest_log.parse_client_log(text)
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
    assert parsed["malformed_events"] == 2, (
        f"inf events must be counted as malformed, got {parsed['malformed_events']}"
    )
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
    hits = playtest_log.barrier_hits_prefix(blob, "spawn_vehicle:")
    assert hits == [
        "spawn_vehicle:bicycle",
        "spawn_vehicle:jeep",
        "spawn_vehicle:bicycle",
    ], f"repeated barriers collapsed or misparsed: {hits}"
    assert playtest_log.barrier_hits_prefix(blob, "chat_echo:") == ["chat_echo:hello"]
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
    "ünïcödé　全角 combining é́ emoji 🧟 BOM ﻿ NBSP x",
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


def _assert_parsed_shape(parsed: ParsedClientLog, seed: int) -> None:
    assert set(parsed) == {
        "results",
        "summary",
        "done",
        "json_events",
        "nre_like",
        "nre_like_total",
        "malformed_events",
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
    assert isinstance(parsed["nre_like_total"], int) and not isinstance(
        parsed["nre_like_total"], bool
    ), f"seed {seed}: nre_like_total type {parsed['nre_like_total']}"
    assert parsed["nre_like_total"] >= len(parsed["nre_like"]), (
        f"seed {seed}: nre total below sample count"
    )
    assert isinstance(parsed["malformed_events"], int) and not isinstance(
        parsed["malformed_events"], bool
    ), f"seed {seed}: malformed_events type {parsed['malformed_events']}"
    assert parsed["malformed_events"] >= 0, (
        f"seed {seed}: malformed_events negative"
    )


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
        parsed = playtest_log.parse_client_log(blob)
        _assert_parsed_shape(parsed, seed)
        again = playtest_log.parse_client_log(blob)
        assert again == parsed, f"seed {seed}: parse is nondeterministic"
        doubled = playtest_log.parse_client_log(blob * 2)
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
    " ",  # noqa: RUF001 (NBSP is a deliberate fuzz input)
    "é́",
    "‮rtl‭",  # noqa: PLE2502 (bidi overrides are a deliberate fuzz input)
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

            def nasty(r: random.Random) -> str:
                return "".join(r.choice(_JUNIT_CHARS) for _ in range(r.randrange(0, 8)))

            case = f"s/{nasty(rng)}c{nasty(rng)}"
            detail = nasty(rng)
            suite = nasty(rng)
            status = rng.choice(_JUNIT_STATUSES)
            path = Path(td) / f"junit-{seed}.xml"
            # write_junit logs every render; keep the 50 iterations quiet.
            with contextlib.redirect_stdout(io.StringIO()):
                playtest_run.write_junit(
                    path,
                    suite,
                    [{"case": case, "status": status, "detail": detail}],
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
        playtest_run.write_junit(path, "smoke", results)
        root = ElementTree.parse(path).getroot()
        case = root.find("testcase")
        assert case is not None, "rendered junit must contain a testcase"
        assert case.get("name") == "s/cx", f"NUL must be dropped: {case.get('name')!r}"
        failure = case.find("failure")
        assert failure is not None, "failed case must carry a failure element"
        assert failure.get("message") == "hp=0", (
            "illegal controls must be dropped from detail"
        )
    print("PASS junit_illegal_chars NUL/control bytes dropped, document stays valid")


class _FakeTail:
    """Stands in for LogTail in the incremental equivalence test below."""

    def __init__(self, chunk: str) -> None:
        self._chunk = chunk

    def poll(self) -> str:
        chunk, self._chunk = self._chunk, ""
        return chunk


def test_incremental_scan_matches_whole_parse() -> None:
    """The orchestrator polls the client log through LogTail + ClientLogScan
    (pump_log_tail) instead of re-parsing the whole file each poll. The
    incremental result must equal parse_client_log over the same bytes, and
    cumulative barrier totals must equal whole-text barrier_line_hits, or the
    poll loop double-fires or misses host fixtures."""
    text = (
        "[7dtd-playtest] ready player=171 pos=(520.0, 62.0, 950.0)\n"
        "[game] noise line mentioning NullReferenceException\n"
        "[7dtd-playtest] PASS smoke/dig detail=ok\n"
        '[7dtd-playtest] {"v":1,"t":"result","suite":"smoke","case":"dig",'
        '"status":"pass","ms":12}\n'
        "[7dtd-playtest] barrier spawn_zombie\n"
        "[7dtd-playtest] barrier spawn_vehicle:gyrocopter\n"
        "[7dtd-playtest] barrier chat_echo:token1\n"
        # Barrier counting is anchored to the stable prefix: a foreign line
        # (game echo, chat text, another mod's log) that merely contains the
        # words must never service an admin action.
        "[chat] player said: barrier kill_player\n"
        "barrier spawn_trader without any prefix\n"
        "[7dtd-playtest] SUMMARY pass=1 fail=0 skip=0\n"
        "[7dtd-playtest] DONE exit_hint=0\n"
    )
    # Cut at awkward boundaries, including mid-line pieces that only become
    # pumpable once a later piece completes the newline.
    cuts = (0, 13, 40, 41, 120, 121, 200, len(text))
    buf = ""
    scan = playtest_log.ClientLogScan()
    for lo, hi in pairwise(cuts):
        buf += text[lo:hi]
        cut = buf.rfind("\n")
        if cut < 0:
            continue
        complete, buf = buf[: cut + 1], buf[cut + 1 :]
        playtest_run.pump_log_tail(_FakeTail(complete), scan)
    assert buf == "", f"tail bytes were dropped: {buf!r}"

    got = scan.result()
    want = playtest_log.parse_client_log(text)
    assert got["results"] == want["results"], (got["results"], want["results"])
    assert got["summary"] == want["summary"], (got["summary"], want["summary"])
    assert got["done"] == want["done"], (got["done"], want["done"])
    assert got["nre_like_total"] == 1, got["nre_like_total"]

    totals = dict.fromkeys(playtest_run.BARRIER_NAMES, 0)
    playtest_log.add_barrier_hits(totals, text)
    for name, total in totals.items():
        assert total == playtest_log.barrier_line_hits(text, name), name
    assert totals["spawn_zombie"] == 1, totals
    # Parameterised lines must not count toward the bare name.
    assert totals["spawn_vehicle"] == 0, totals
    # Unprefixed look-alike lines must not count toward any barrier.
    assert totals["kill_player"] == 0 and totals["spawn_trader"] == 0, totals
    print("PASS incremental_scan chunked feed equals whole-log parse and counts")


def test_pump_log_tail_survives_truncation_between_phases() -> None:
    """The rejoin flow truncates the client log between setup and verify and
    the orchestrator recreates tail+scan there. A real LogTail must restart
    from zero on the shrink so no stale bytes are misread, and the fresh scan
    must report only the new generation's events."""
    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "client.log"
        log_path.write_text(
            "[7dtd-playtest] barrier spawn_zombie\n"
            "[7dtd-playtest] DONE exit_hint=0\n",
            encoding="utf-8",
        )
        tail = playtest_log.LogTail(log_path)
        scan = playtest_log.ClientLogScan()
        chunk = playtest_run.pump_log_tail(tail, scan)
        assert "DONE" in chunk, chunk
        # Truncate between phases (setup → saveworld → restart → verify).
        log_path.write_text("", encoding="utf-8")
        assert playtest_run.pump_log_tail(tail, scan) == ""
        log_path.write_text(
            "[7dtd-playtest] PASS persist/pos_survives_rejoin detail=ok\n"
            "[7dtd-playtest] DONE exit_hint=0\n",
            encoding="utf-8",
        )
        playtest_run.pump_log_tail(tail, scan)
    got = scan.result()
    assert [r["case"] for r in got["results"]] == ["persist/pos_survives_rejoin"], (
        got["results"]
    )
    assert got["done"] == {"exit_hint": 0}, got["done"]
    print("PASS incremental_truncate shrink restarts tail, fresh scan per phase")


def test_log_tail_keeps_multibyte_char_split_across_polls() -> None:
    """LogTail decodes only complete lines, so a UTF-8 multi-byte character
    whose bytes straddle two polls stays intact in the byte buffer. The
    client log carries arbitrary game/chat text (chat_roundtrip echoes player
    strings into case details); decoding per poll would replace the torn
    character with U+FFFD and permanently corrupt the parsed detail."""
    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "client.log"
        tail = playtest_log.LogTail(log_path)
        # Torn write boundary: first 2 of the 4 bytes of an astral char.
        torn = "[7dtd-playtest] PASS mp/chat_roundtrip detail=tOKEN \N{GRINNING FACE}".encode(
            "utf-8"
        )
        cut = len(torn) - 2
        with log_path.open("wb") as fh:
            fh.write(torn[:cut])
        assert tail.poll() == "", "partial line without newline must stay buffered"
        with log_path.open("ab") as fh:
            fh.write(torn[cut:] + b"\n")
        chunk = tail.poll()
    assert "\N{GRINNING FACE}" in chunk, f"split character corrupted: {chunk!r}"
    scan = playtest_log.ClientLogScan()
    for line in chunk.splitlines():
        scan.feed_line(line)
    got = scan.result()
    assert len(got["results"]) == 1 and "\N{GRINNING FACE}" in got["results"][0]["detail"], (
        got["results"]
    )
    print("PASS logtail_multibyte torn UTF-8 char survives across polls intact")


def test_log_tail_from_end_starts_at_current_size() -> None:
    """``from_end`` is what the orchestrator relies on when the previous
    generation's client log could not be preserved: only bytes appended
    after construction may be returned, so stale barriers/results from the
    old log cannot re-fire into the new run's verdicts. A missing file must
    degrade to reading from zero, not raise."""
    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "client.log"
        log_path.write_text("[7dtd-playtest] barrier spawn_zombie\n", encoding="utf-8")
        tail = playtest_log.LogTail(log_path, from_end=True)
        assert tail.poll() == "", "pre-existing bytes replayed into the new run"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("[7dtd-playtest] barrier kill_player\n")
        chunk = tail.poll()
        assert "kill_player" in chunk and "spawn_zombie" not in chunk, chunk

    with tempfile.TemporaryDirectory() as td:
        absent = Path(td) / "absent.log"
        tail = playtest_log.LogTail(absent, from_end=True)
        assert tail.poll() == "", "missing log must poll empty"
        absent.write_text("[7dtd-playtest] PASS s/c ok\n", encoding="utf-8")
        assert "PASS s/c ok" in tail.poll(), "append after missing-start was lost"
    print("PASS logtail_from_end pre-existing bytes skipped, appends still read")


def test_loadgen_event_reader_matches_whole_read_and_resets_on_truncate() -> None:
    """The poll loop drains loadgen events incrementally instead of re-reading
    the whole JSONL every iteration. The accumulated list must equal
    read_loadgen_events over the same bytes, skip malformed/partial lines,
    and reset when the file is truncated (a fresh loadgen generation), or an
    id from a finished generation would answer for the current one."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "loadgen_events.jsonl"
        joined = '{"schema":"7dtd.loadgen.event.v1","type":"joined","entityId":107}\n'
        state = (
            '{"schema":"7dtd.loadgen.event.v1","type":"state",'
            '"entityId":107,"kind":"cvar","name":"HoldingController","value":1}\n'
        )
        noise = "not json\n{\"other\":\"wrong schema\"}\n"
        path.write_text(joined + noise + state, encoding="utf-8")
        reader = playtest_run.LoadgenEventReader(path)
        got = reader.drain()
        want = playtest_run.read_loadgen_events(path)
        assert got == want, (got, want)
        assert [e["type"] for e in got] == ["joined", "state"], got

        # A trailing partial line stays buffered until its newline arrives,
        # exactly like the client-log tail; nothing half-written parses.
        piece_a = '{"schema":"7dtd.loadgen.event.v1","type":"sta'
        with path.open("a", encoding="utf-8") as fh:
            fh.write(piece_a)
        assert reader.drain() == want, "partial line parsed as an event"
        with path.open("a", encoding="utf-8") as fh:
            fh.write('te","entityId":107}\n')
        got = reader.drain()
        want = playtest_run.read_loadgen_events(path)
        assert got == want and len(got) == 3, (got, want)

        # Truncation between loadgen runs: accumulated events must drop so a
        # stale entityId is never teleported again.
        path.write_text("", encoding="utf-8")
        assert reader.drain() == [], "events from the truncated generation kept"
        path.write_text(
            '{"schema":"7dtd.loadgen.event.v1","type":"joined","entityId":108}\n',
            encoding="utf-8",
        )
        got = reader.drain()
        want = playtest_run.read_loadgen_events(path)
        assert got == want and [e["entityId"] for e in got] == [108], (got, want)
    print("PASS loadgen_event_reader incremental equals whole read, truncate resets")


# Fuzz grammar for the loadgen events surface. The JSONL file is produced by
# another process and consumed by the verdict oracles, so hostile bytes here
# decide mp-suite results: the pool carries every crash class found so far
# (unbounded JSON ints that overflow float(), bare Infinity/NaN tokens,
# boolean values where numbers belong) plus wrong schemas, truncated lines
# and junk. Deterministic seeds; a failure prints its seed and blob so the
# exact input can be pasted as the next fixed regression case.
_LOADGEN_LINE_FRAGMENTS = [
    '{"schema":"7dtd.loadgen.event.v1","type":"joined","botId":1,"entityId":171}',
    '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,'
    '"kind":"cvar","name":"protection","value":1}',
    '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,'
    '"kind":"buff","name":"protected","active":true}',
    # Values the oracles must reject, never crash on: exponent overflow to
    # inf, unbounded integers, non-finite tokens, booleans, wrong types.
    '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,'
    '"kind":"cvar","name":"x","value":1e999}',
    '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,'
    '"kind":"cvar","name":"huge","value":1' + "0" * 400 + "}",
    '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,'
    '"kind":"cvar","name":"neg_huge","value":-1' + "0" * 400 + "}",
    '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,'
    '"kind":"cvar","name":"inf","value":Infinity}',
    '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,'
    '"kind":"cvar","name":"nan","value":NaN}',
    '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":true,'
    '"kind":"cvar","name":"x","value":4}',
    '{"schema":"7dtd.loadgen.event.v1","type":"joined","entityId":true}',
    '{"schema":"7dtd.loadgen.event.v1","type":"joined","entityId":-5}',
    '{"schema":"7dtd.loadgen.event.v1","type":"state","entityId":171,'
    '"kind":["cvar"],"name":{"a":1},"value":[null]}',
    # Not events at all: wrong schema, other JSON shapes, truncated lines.
    '{"schema":"other.v9","type":"joined","entityId":172}',
    '{"type":"joined","entityId":173}',
    "[1,2,3]",
    '"just a string"',
    "42",
    "null",
    '{"schema":"7dtd.loadgen.event.v1","type":"state",',
    "{",
    "",
]

_LOADGEN_JUNK_LINES = [
    "\x00\x00binary-ish\x00",
    "not json at all",
    "﻿leading BOM {\"schema\":\"7dtd.loadgen.event.v1\"}",
    "ünïcödé　全角 combining é́ emoji 🧟",
    "{" + "}" * 300,
]


def _loadgen_fuzz_blob(rng: random.Random) -> str:
    lines = [
        rng.choice(_LOADGEN_LINE_FRAGMENTS + _LOADGEN_JUNK_LINES)
        for _ in range(rng.randrange(0, 14))
    ]
    return "".join(line + "\n" for line in lines)


def test_fuzz_loadgen_events_survive_hostile_jsonl() -> None:
    """Seeded grammar fuzzer over loadgen event files and their oracles.

    Invariants per generated file: reading never raises, kept events are
    exactly the dict-with-matching-schema lines (doubling doubles them),
    parsing is deterministic, incremental draining through
    LoadgenEventReader equals the whole-file read with truncation resetting
    it, and every oracle answers without raising: a joined id is a positive
    non-boolean int, expectation failures are plain strings."""
    for seed in range(40):
        rng = random.Random(2000 + seed)
        blob = _loadgen_fuzz_blob(rng)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            path.write_text(blob, encoding="utf-8")

            events = playtest_run.read_loadgen_events(path)
            assert all(
                isinstance(e, dict) and e.get("schema") == "7dtd.loadgen.event.v1"
                for e in events
            ), f"seed {seed}: filter leaked a non-event"
            assert playtest_run.read_loadgen_events(path) == events, (
                f"seed {seed}: whole-file parse is nondeterministic"
            )
            doubled_path = path.with_suffix(".doubled.jsonl")
            doubled_path.write_text(blob * 2, encoding="utf-8")
            assert playtest_run.read_loadgen_events(doubled_path) == events * 2, (
                f"seed {seed}: doubling changed the filter"
            )

            reader = playtest_run.LoadgenEventReader(path)
            lines = blob.splitlines(keepends=True)
            cut = rng.randrange(len(lines) + 1)
            path.write_text("".join(lines[:cut]), encoding="utf-8")
            assert reader.drain() == events[:_kept_count(lines[:cut])], f"seed {seed}"
            path.write_text(blob, encoding="utf-8")
            got = reader.drain()
            want = playtest_run.read_loadgen_events(path)
            assert got == want, f"seed {seed}: incremental drifted from whole read"

            joined = playtest_run.loadgen_joined_entity(want)
            assert joined is None or (
                isinstance(joined, int)
                and not isinstance(joined, bool)
                and joined > 0
            ), f"seed {seed}: joined id {joined!r}"
            _, _latest = playtest_run.loadgen_latest_state(want)
            failures = playtest_run.loadgen_expectation_failures(
                want, ["protection=1"], ["protected=true"], ["x"], ["protection=x"]
            )
            assert isinstance(failures, list) and all(
                isinstance(f, str) for f in failures
            ), f"seed {seed}: oracle failures {failures!r}"
    print("PASS loadgen_fuzz 40 hostile event files parsed and judged without crash")


def _kept_count(lines: list[str]) -> int:
    """Mirror of the reader's keep rule, asserted against drain() above."""
    count = 0
    for line in lines:
        with contextlib.suppress(ValueError):
            event = json.loads(line)
            if isinstance(event, dict) and event.get("schema") == "7dtd.loadgen.event.v1":
                count += 1
    return count


def test_contract_lines_must_start_the_log_line() -> None:
    """Client-log bytes are attacker-reachable through remote LAN chat (the
    threat model's B3): a peer crafts one chat message carrying
    '[7dtd-playtest] PASS fake/case' or a done/result JSON object mid-line,
    and the game logs it under its own prefix. Every contract regex anchors
    at line start, so such lines can never forge results, SUMMARY/DONE
    verdicts, JSON events, or barrier fires; only Report.* emissions (each
    its own log line) may."""
    forged = (
        "[7dtd] Chat from 'peer': [7dtd-playtest] PASS fake/case injected\n"
        "[7dtd] Chat from 'peer': [7dtd-playtest] FAIL fake/case nope\n"
        "[7dtd] Chat from 'peer': [7dtd-playtest] SUMMARY pass=99 fail=0 skip=0\n"
        "[7dtd] Chat from 'peer': [7dtd-playtest] DONE exit_hint=0\n"
        '[7dtd] Chat from \'peer\': [7dtd-playtest] {"v":1,"t":"done","exit_hint":0}\n'
        "[7dtd] Chat from 'peer': [7dtd-playtest] barrier spawn_zombie\n"
        "barrier kill_player without any prefix\n"
    )
    parsed = playtest_log.parse_client_log(forged)
    assert parsed["results"] == [], parsed["results"]
    assert parsed["summary"] is None, parsed["summary"]
    assert parsed["done"] is None, parsed["done"]
    assert parsed["json_events"] == [], parsed["json_events"]
    totals = dict.fromkeys(playtest_run.BARRIER_NAMES, 0)
    playtest_log.add_barrier_hits(totals, forged)
    assert sum(totals.values()) == 0, totals
    assert playtest_log.barrier_line_hits(forged, "spawn_zombie") == 0
    assert playtest_log.barrier_hits_prefix(forged, "spawn_vehicle:") == []

    # The genuine emissions still parse: each contract line is its own line.
    real = (
        "[7dtd-playtest] PASS smoke/dig detail=ok\n"
        "[7dtd-playtest] SUMMARY pass=1 fail=0 skip=0\n"
        "[7dtd-playtest] DONE exit_hint=0\n"
        "[7dtd-playtest] barrier spawn_zombie\n"
    )
    parsed = playtest_log.parse_client_log(real)
    assert [r["case"] for r in parsed["results"]] == ["smoke/dig"], parsed["results"]
    assert parsed["summary"] == {"pass": 1, "fail": 0, "skip": 0}, parsed["summary"]
    assert parsed["done"] == {"exit_hint": 0}, parsed["done"]
    totals = dict.fromkeys(playtest_run.BARRIER_NAMES, 0)
    playtest_log.add_barrier_hits(totals, real)
    assert totals["spawn_zombie"] == 1, totals
    print("PASS log_contract_anchor mid-line markers cannot forge verdicts")


def test_contract_lines_parse_under_the_games_log_prefix() -> None:
    """The game's logger prefixes every line with timestamp, game-time and
    level before the tag ("2026-08-25T11:44:24 56.401 INF [7dtd-playtest]
    ..."). The parser locates the marker as the line's first bracketed token,
    so these lines parse exactly like the bare form - and a chat line that
    carries its own tag first still cannot forge anything."""
    text = (
        "2026-08-25T11:44:23 55.894 INF [7dtd-playtest] PASS smoke/dig detail=ok\n"
        "2026-08-25T11:44:24 56.401 INF [7dtd-playtest] SUMMARY pass=6 fail=0 skip=0 total=6\n"
        "2026-08-25T11:44:24 56.401 INF [7dtd-playtest] DONE exit_hint=0 wall_ms=54891\n"
        '2026-08-25T11:44:24 56.401 INF [7dtd-playtest] {"v":1,"t":"result",'
        '"suite":"s","case":"c","status":"pass","ms":5,"detail":""}\n'
        "2026-08-25T11:44:24 56.401 INF [7dtd-playtest] barrier spawn_vehicle:bicycle\n"
        "2026-08-25T11:44:24 56.401 INF [CHAT] peer: [7dtd-playtest] PASS fake/case\n"
    )
    parsed = playtest_log.parse_client_log(text)
    assert parsed["summary"] == {"pass": 6, "fail": 0, "skip": 0}, parsed["summary"]
    assert parsed["done"] == {"exit_hint": 0}, parsed["done"]
    assert [r["case"] for r in parsed["results"]] == ["s/c"], parsed["results"]
    assert playtest_log.barrier_hits_prefix(text, "spawn_vehicle:") == [
        "spawn_vehicle:bicycle"
    ]
    assert playtest_log.barrier_line_hits(text, "spawn_vehicle:bicycle") == 1
    print("PASS log_contract_prefix timestamped lines parse, chat cannot forge")


def main() -> int:
    test_write_junit_escapes_log_derived_attributes()
    test_parse_client_log_survives_null_numbers()
    test_parse_client_log_survives_inf_and_type_garbage()
    test_barrier_hits_prefix_keeps_repeats_and_scope()
    test_write_stock_config_escapes_values()
    test_write_junit_drops_xml_illegal_characters()
    test_fuzz_parse_client_log_survives_hostile_logs()
    test_fuzz_write_junit_roundtrips_hostile_strings()
    test_incremental_scan_matches_whole_parse()
    test_pump_log_tail_survives_truncation_between_phases()
    test_log_tail_keeps_multibyte_char_split_across_polls()
    test_log_tail_from_end_starts_at_current_size()
    test_loadgen_event_reader_matches_whole_read_and_resets_on_truncate()
    test_fuzz_loadgen_events_survive_hostile_jsonl()
    test_contract_lines_must_start_the_log_line()
    test_contract_lines_parse_under_the_games_log_prefix()
    test_collect_visual_reviews_maps_paths_and_never_verdicts()
    test_collect_visual_reviews_is_empty_without_a_directory()
    test_report_summary_prints_counts_and_fails_closed()
    print("RESULT PASS")
    return 0


def test_collect_visual_reviews_maps_paths_and_never_verdicts() -> None:
    """--attach-reviews attaches evidence paths only, keyed by suite/case.

    A review must never be able to change a case's result by existing, so the
    report field is paths; any verdict-shaped field would fail here.
    """
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        evidence = root / "reviews"
        evidence.mkdir()
        review = {
            "kind": "deadeye-review",
            "intent": {
                "content": {"suite": "demo", "case": "motion_thing"},
            },
            "result": {"summary": "clips at the shoulder"},
        }
        (evidence / "review-gemini-20260825.json").write_text(
            json.dumps(review), encoding="utf-8"
        )
        (root / "not-a-review.json").write_text("{}", encoding="utf-8")

        reviews = playtest_run.collect_visual_reviews(evidence)
        assert reviews == {
            "demo/motion_thing": str(evidence / "review-gemini-20260825.json")
        }, "reviews must map suite/case to the evidence path only"


def test_collect_visual_reviews_is_empty_without_a_directory() -> None:
    assert playtest_run.collect_visual_reviews(None) == {}
    with tempfile.TemporaryDirectory() as temporary:
        assert playtest_run.collect_visual_reviews(Path(temporary) / "missing") == {}


def _run_report_summary(path: Path) -> tuple[int, str]:
    """Drive report_summary's real entry point, returning (exit code, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = report_summary.main(["report_summary.py", str(path)])
    return code, out.getvalue().strip()


def test_report_summary_prints_counts_and_fails_closed() -> None:
    """playtest_repeat.sh counts a lap from this stdout.

    A malformed or hostile summary must exit non-zero with no counts: the
    aggregator scores an unreadable lap as failed, so any printed zero would
    launder a broken report into a clean lap.
    """
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "report-1.json"

        path.write_text(
            json.dumps({"summary": {"pass": 7, "fail": 2, "skip": 1}}), encoding="utf-8"
        )
        assert _run_report_summary(path) == (0, "7 2 1")

        # Absent counts default to zero; that is a real empty run, not a fault.
        path.write_text(json.dumps({"summary": {}}), encoding="utf-8")
        assert _run_report_summary(path) == (0, "0 0 0")

        hostile = [
            {},                                        # no summary key
            {"summary": [1, 2, 3]},                    # summary not an object
            {"summary": {"pass": "7"}},                # count as string
            {"summary": {"pass": 1.5}},                # count as float
            {"summary": {"pass": True}},               # bool is an int subclass
            {"summary": {"fail": -1}},                 # negative count
            {"summary": {"pass": float("inf")}},       # inf survives json.loads
        ]
        for blob in hostile:
            path.write_text(json.dumps(blob), encoding="utf-8")
            code, stdout = _run_report_summary(path)
            assert code != 0, f"{blob!r} must not report success"
            assert stdout == "", f"{blob!r} printed counts: {stdout!r}"

        path.write_text("{not json", encoding="utf-8")
        assert _run_report_summary(path)[0] != 0, "malformed JSON must fail closed"

        missing = Path(temporary) / "absent.json"
        assert _run_report_summary(missing)[0] != 0, "a missing report must fail closed"

        assert report_summary.main(["report_summary.py"]) == 2, "usage error is exit 2"
    print("PASS report_summary counts print, hostile summaries fail closed")

if __name__ == "__main__":
    sys.exit(main())
