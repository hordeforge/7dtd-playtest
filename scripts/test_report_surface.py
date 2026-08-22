#!/usr/bin/env python3
"""Offline gate: orchestrator report/log surface stays injection- and crash-safe.

write_junit renders client-log-derived case ids and details into JUnit XML
consumed by CI UIs, write_stock_config renders operator values into the
generated serverconfig.xml, parse_client_log eats arbitrary game log lines,
and barrier_hits_prefix greps fixture barriers out of those logs (repeats are
events, never duplicates to collapse). A hostile or corrupt log line must not
break out of an XML attribute or crash either parser.
"""
from __future__ import annotations

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


def main() -> int:
    test_write_junit_escapes_log_derived_attributes()
    test_parse_client_log_survives_null_numbers()
    test_barrier_hits_prefix_keeps_repeats_and_scope()
    test_write_stock_config_escapes_values()
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
