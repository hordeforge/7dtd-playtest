#!/usr/bin/env python3
"""Offline gate: orchestrator report surface stays injection- and crash-safe.

write_junit renders client-log-derived case ids and details into JUnit XML
consumed by CI UIs, write_stock_config renders operator values into the
generated serverconfig.xml, and parse_client_log eats arbitrary game log
lines. A hostile or corrupt log line must not break out of an XML attribute
or crash the parser.
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
    # The malformed summary/done events are skipped; the rest still parse.
    assert parsed["done"] is None or isinstance(parsed["done"], dict), (
        f"done event must not crash the parser: {parsed['done']}"
    )
    statuses = [r["status"] for r in parsed["results"]]
    assert "PASS" in statuses, f"valid events lost when a sibling was bad: {parsed}"
    print("PASS log_parse_bad_json no TypeError on null/array counts")


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
    test_write_stock_config_escapes_values()
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
