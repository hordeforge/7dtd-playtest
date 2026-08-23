#!/usr/bin/env python3
"""Unit tests for orchestrator log scoring: parse_client_log, write_junit, fixtures gate.

Covers the [7dtd-playtest] log contract (see AGENTS.md): human PASS|FAIL|SKIP,
JSON events, SUMMARY/DONE terminal lines, and the JUnit XML report surface.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import playtest_run as pr


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_human_log_results_summary_done(tmp: Path) -> None:
    text = "[7dtd-playtest] PASS smoke/world_time_advances ok\n[7dtd-playtest] FAIL core/melee_damage_out hp did not drop\n[7dtd-playtest] SKIP demo/creative_menu needs dm\n[7dtd-playtest] SUMMARY pass=1 fail=1\n[7dtd-playtest] DONE exit_hint=1"
    p = pr.parse_client_log(text)
    _assert([r["case"] for r in p["results"]] == [
        "smoke/world_time_advances",
        "core/melee_damage_out",
        "demo/creative_menu",
    ], f"cases parsed: {p['results']!r}")
    _assert(
        [r["status"] for r in p["results"]] == ["PASS", "FAIL", "SKIP"],
        f"statuses parsed: {p['results']!r}",
    )
    _assert(p["summary"] == {"pass": 1, "fail": 1, "skip": 0}, f"summary: {p['summary']!r}")
    _assert(p["done"] == {"exit_hint": 1}, f"done: {p['done']!r}")


def test_done_without_exit_hint(tmp: Path) -> None:
    p = pr.parse_client_log("[7dtd-playtest] DONE")
    _assert(p["done"] == {"exit_hint": None}, f"done: {p['done']!r}")


def test_json_events_preferred_over_human(tmp: Path) -> None:
    # Both shapes present (mod emits human + JSON): parser must count each once.
    text = '[7dtd-playtest] {"v":1,"t":"result","status":"pass","suite":"s","case":"a","detail":"ok","ms":12}\n[7dtd-playtest] PASS s/a ok\n[7dtd-playtest] {"v":1,"t":"result","status":"fail","suite":"s","case":"b","detail":"boom"}\n[7dtd-playtest] FAIL s/b boom\n[7dtd-playtest] {"v":1,"t":"summary","pass":1,"fail":1,"skip":0}\n[7dtd-playtest] SUMMARY pass=1 fail=1 skip=0\n[7dtd-playtest] {"v":1,"t":"done","exit_hint":1}\n[7dtd-playtest] DONE exit_hint=1'
    p = pr.parse_client_log(text)
    _assert(len(p["results"]) == 2, f"json preferred, no double count: {p['results']!r}")
    _assert(
        [(r["case"], r["status"]) for r in p["results"]]
        == [("s/a", "PASS"), ("s/b", "FAIL")],
        f"json cases normalized: {p['results']!r}",
    )
    _assert(p["summary"] == {"pass": 1, "fail": 1, "skip": 0}, f"summary: {p['summary']!r}")
    _assert(p["done"] == {"exit_hint": 1}, f"done: {p['done']!r}")
    _assert(len(p["json_events"]) == 4, f"raw events kept: {p['json_events']!r}")


def test_summary_fallback_counts_results(tmp: Path) -> None:
    text = '[7dtd-playtest] {"v":1,"t":"result","status":"pass","suite":"s","case":"a"}\n[7dtd-playtest] {"v":1,"t":"result","status":"skip","suite":"s","case":"c"}\n[7dtd-playtest] {"v":1,"t":"done","exit_hint":0}'
    p = pr.parse_client_log(text)
    _assert(p["summary"] == {"pass": 1, "fail": 0, "skip": 1}, f"fallback: {p['summary']!r}")
    _assert(p["done"] == {"exit_hint": 0}, f"done: {p['done']!r}")


def test_malformed_json_line_is_dropped_not_crash(tmp: Path) -> None:
    # Stream selection is all-or-nothing on JSON *results*: a lone valid
    # result event re-enables the JSON stream for summary/done too.
    text = '[7dtd-playtest] {"v":1,"t":"result","suite":"s"\n[7dtd-playtest] {"v":1,"t":"result","status":"pass","suite":"s","case":"a"}\n[7dtd-playtest] {"v":1,"t":"done","exit_hint":0}'
    p = pr.parse_client_log(text)
    _assert(len(p["results"]) == 1, f"truncated json not scored: {p['results']!r}")
    _assert(p["results"][0]["case"] == "s/a", f"valid result kept: {p['results']!r}")
    _assert(p["done"] == {"exit_hint": 0}, f"json stream still trusted: {p['done']!r}")


def test_empty_and_unrelated_text(tmp: Path) -> None:
    p = pr.parse_client_log("")
    _assert(p["results"] == [] and p["summary"] is None and p["done"] is None, "empty text")
    p2 = pr.parse_client_log("random game chatter\n[7dtd-playtest] barrier spawn_zombie")
    _assert(p2["results"] == [] and p2["done"] is None, f"non-terminal noise: {p2!r}")


def test_nre_like_lines_captured_and_capped(tmp: Path) -> None:
    nre = [f"NullReferenceException stack frame {i}" for i in range(60)]
    text = "[7dtd-playtest] PASS s/a ok\n" + "\n".join(nre)
    p = pr.parse_client_log(text)
    _assert(0 < len(p["nre_like"]) <= 50, f"capped at 50: {len(p['nre_like'])}")
    p2 = pr.parse_client_log("clean line\nanother clean line")
    _assert(p2["nre_like"] == [], "clean log has no nre hits")


def test_write_junit_escapes_details_into_valid_xml(tmp: Path) -> None:
    out = tmp / "junit.xml"
    results = [
        {
            "case": 'combat/melee "swing"',
            "status": "FAIL",
            "detail": 'zombie said "ouch" & <gone> & more',
        },
        {"case": "demo/skip_case", "status": "SKIP", "detail": "needs dm"},
        {"case": "demo/pass_case", "status": "PASS", "detail": ""},
    ]
    pr.write_junit(out, "demo", results, {"pass": 1, "fail": 1, "skip": 1})
    root = ET.fromstring(out.read_text(encoding="utf-8"))
    _assert(root.tag == "testsuite", "root element")
    _assert(root.get("tests") == "3", "tests attr")
    _assert(root.get("failures") == "1", "failures attr")
    _assert(root.get("skipped") == "1", "skipped attr")
    cases = root.findall("testcase")
    _assert(len(cases) == 3, f"three testcase elements: {len(cases)}")
    fail = [c for c in cases if c.find("failure") is not None]
    _assert(len(fail) == 1, "one failure element")
    _assert(
        fail[0].get("name") == 'combat/melee "swing"',
        f"name preserved verbatim: {fail[0].get('name')!r}",
    )
    _assert(
        fail[0].find("failure").get("message")
        == 'zombie said "ouch" & <gone> & more',
        "detail preserved verbatim through XML escaping",
    )


def test_suite_wants_zombie_fixture_gate(tmp: Path) -> None:
    for s in ("demo", "combat", "full", "mp", "residual"):
        _assert(pr.suite_wants_zombie_fixture(s), f"{s} wants fixtures")
    # First-class suite ids whose cases emit host-fixture barriers
    # (spawn_trader / spawn_vehicle / kill_player) must not silently run
    # without the admin fixture plane when invoked standalone.
    for s in ("economy", "vehicle", "finale"):
        _assert(pr.suite_wants_zombie_fixture(s), f"{s} wants fixtures")
    for s in ("smoke", "core", "demo_min", "gate"):
        _assert(not pr.suite_wants_zombie_fixture(s), f"{s} must not want fixtures")
    # Token matching, not substrings: provider suite ids that merely contain
    # a key or exclusion as a substring must not flip the fixture plane.
    for s in ("temp_check", "example_suite", "gateway_checks", "demolition_derby"):
        _assert(not pr.suite_wants_zombie_fixture(s), f"{s} must not match by substring")
    _assert(
        pr.suite_wants_zombie_fixture("smoke,combat"),
        "mixed list keyed by any member",
    )
    _assert(
        not pr.suite_wants_zombie_fixture("smoke,demo_min"),
        "demo_min excludes its whole list",
    )


def test_suite_tokens_matches_client_separators() -> None:
    """Tokenizer mirrors Catalog.ExpandSuites separators (',' ';' whitespace)."""
    _assert(pr.suite_tokens("smoke,core") == {"smoke", "core"}, "comma split")
    _assert(pr.suite_tokens("smoke;core extra") == {"smoke", "core", "extra"}, "; and space")
    _assert(pr.suite_tokens("  ") == set(), "empty arg")
    _assert(pr.suite_tokens("SOAK_LONG") == {"soak_long"}, "case-insensitive")
    _assert(pr.suite_tokens("") == set(), "none arg")


def test_stop_proc_reaps_after_sigkill_escalation(tmp: Path) -> None:
    """A TERM-immune child must end reaped (no zombie) after escalation."""
    proc = subprocess.Popen(
        ["bash", "-c", "trap '' TERM; sleep 30"],
        start_new_session=True,
    )
    time.sleep(0.3)  # let bash install the ignore-trap
    t0 = time.time()
    pr.stop_proc(proc, term_timeout=0.2)
    elapsed = time.time() - t0
    _assert(proc.returncode == -9, f"expected SIGKILL death, got {proc.returncode}")
    _assert(elapsed < 5.0, f"stop_proc took too long after KILL: {elapsed:.1f}s")


def test_stop_proc_reaps_on_graceful_term(tmp: Path) -> None:
    # A child that dies on SIGTERM is reaped by wait(): Popen encodes signal
    # deaths as negative return codes (-15 = SIGTERM).
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pr.stop_proc(proc, term_timeout=8.0)
    _assert(proc.returncode == -15, f"graceful path not reaped: {proc.returncode}")


def test_stop_proc_none_is_noop() -> None:
    pr.stop_proc(None)


def test_start_zdtd_failure_leaks_no_log_fd(tmp: Path) -> None:
    """Missing binary → Popen raises, but the opened log handle must be closed."""
    log_path = tmp / "zdtd.log"

    def open_fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    before = open_fds()
    raised = False
    try:
        pr.start_zdtd(
            tmp / "does-not-exist-zdtd",
            tmp / "world",
            27025,
            8081,
            tmp / "game-srv",
            log_path,
        )
    except OSError:
        raised = True
    _assert(raised, "start_zdtd must raise when the binary is missing")
    _assert(log_path.is_file(), "log file created before failure")
    after = open_fds()
    _assert(after <= before, f"log fh leaked on Popen failure: {before} -> {after}")


def test_log_tail_yields_only_appended_complete_lines(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    log = tmp / "client.log"
    tail = pr.LogTail(log)
    _assert(tail.read_new() == "", "missing file reads empty")
    log.write_text("[7dtd-playtest] barrier spawn_zombie\n", encoding="utf-8")
    _assert(
        tail.read_new() == "[7dtd-playtest] barrier spawn_zombie\n",
        "first read returns complete lines",
    )
    _assert(tail.read_new() == "", "no growth reads empty")
    # A partial trailing line must be held back until its newline arrives,
    # so no barrier pattern is ever split across polls.
    with log.open("a", encoding="utf-8") as fh:
        fh.write("[7dtd-playtest] barrier kil")
    _assert(tail.read_new() == "", "partial line buffered")
    with log.open("a", encoding="utf-8") as fh:
        fh.write("l_fixture_zombie\n[7dtd-playtest] DONE exit_hint=0\n")
    _assert(
        tail.read_new()
        == "[7dtd-playtest] barrier kill_fixture_zombie\n[7dtd-playtest] DONE exit_hint=0\n",
        "split line completed on next read",
    )
    # Truncation (fresh run reuses the path) restarts from zero.
    log.write_text("[7dtd-playtest] PASS s/a ok\n", encoding="utf-8")
    _assert(
        tail.read_new() == "[7dtd-playtest] PASS s/a ok\n",
        "shrink detected and tail restarted",
    )


def test_log_tail_barrier_count_matches_full_text_scan(tmp: Path) -> None:
    """Incremental chunk counts must equal one scan of the whole stream."""
    tmp.mkdir(parents=True, exist_ok=True)
    log = tmp / "client.log"
    lines = [
        "[7dtd-playtest] barrier spawn_zombie",
        "random game chatter",
        '[7dtd-playtest] {"v":1,"t":"result","status":"pass","suite":"s","case":"a","ms":5}',
        "[7dtd-playtest] barrier spawn_zombie",
        "[7dtd-playtest] PASS s/a ok",
        '[7dtd-playtest] {"v":1,"t":"summary","pass":1,"fail":0,"skip":0}',
        '[7dtd-playtest] {"v":1,"t":"done","exit_hint":0}',
        "[7dtd-playtest] DONE exit_hint=0",
    ]
    full = "\n".join(lines) + "\n"
    tail = pr.LogTail(log)
    acc: dict | None = None
    spawn_hits = 0
    # Feed in awkward small chunks like a live poll would see them.
    pos = 0
    step = 13
    while pos < len(full):
        with log.open("a", encoding="utf-8") as fh:
            fh.write(full[pos : pos + step])
        pos += step
        chunk = tail.read_new()
        if chunk:
            spawn_hits += pr.barrier_hits(chunk, "spawn_zombie")
            acc = pr.merge_parsed(acc, pr.parse_client_log(chunk))
    reference = pr.parse_client_log(full)
    _assert(spawn_hits == 2, f"barrier count across chunks: {spawn_hits}")
    _assert(acc is not None and len(acc.get("results") or []) == 1, "merged results")
    _assert(
        acc is not None and acc.get("done") == {"exit_hint": 0},
        f"merged done: {acc!r}",
    )
    _assert(
        acc is not None and acc.get("summary") == {"pass": 1, "fail": 0, "skip": 0},
        f"merged summary: {acc!r}",
    )
    _assert(
        [r["case"] for r in (reference["results"])]
        == [r["case"] for r in (acc or {}).get("results") or []],
        "incremental merge equals full parse",
    )


def test_barrier_ledger_fires_edges_split_across_polls() -> None:
    """Each emission fires exactly once, even across separate poll chunks.

    Regression: the old dispatch compared a cumulative action count against
    one chunk's hit count, so a barrier landing in a later poll was dropped
    whenever earlier polls had already fired as many actions as that chunk
    showed hits.
    """
    led = pr.BarrierLedger()
    led.observe("[7dtd-playtest] barrier spawn_zombie\n", "spawn_zombie")
    _assert(led.pending("spawn_zombie"), "first edge pending")
    led.mark_fired("spawn_zombie")
    _assert(not led.pending("spawn_zombie"), "caught up after first fire")
    # Second emission arrives in a later poll's chunk: must still fire.
    led.observe("noise\n[7dtd-playtest] barrier spawn_zombie\n", "spawn_zombie")
    _assert(led.pending("spawn_zombie"), "later edge must not be dropped")
    led.mark_fired("spawn_zombie")
    _assert(not led.pending("spawn_zombie"), "both edges consumed")


def test_barrier_ledger_counts_multiple_edges_in_one_chunk() -> None:
    led = pr.BarrierLedger()
    blob = (
        "[7dtd-playtest] barrier apm_dump\n"
        "chatter\n"
        "[7dtd-playtest] barrier apm_dump\n"
    )
    led.observe(blob, "apm_dump")
    _assert(led.seen["apm_dump"] == 2, f"both edges seen: {led.seen!r}")
    _assert(led.pending("apm_dump"), "first action pending")
    led.mark_fired("apm_dump")
    _assert(led.pending("apm_dump"), "second edge still pending after first fire")
    led.mark_fired("apm_dump")
    _assert(not led.pending("apm_dump"), "both edges consumed in one chunk")


def test_barrier_ledger_failed_action_stays_pending_for_retry() -> None:
    """A failed telnet attempt must not consume the edge."""
    led = pr.BarrierLedger()
    led.observe("[7dtd-playtest] barrier kill_player\n", "kill_player")
    # Simulate connect failure: observe without mark_fired.
    _assert(led.pending("kill_player"), "edge still pending after failed attempt")
    led.mark_fired("kill_player")
    _assert(not led.pending("kill_player"), "edge consumed only on success")


def test_barrier_ledger_cap_bounds_actions_not_seen() -> None:
    """Cap limits host actions; emissions beyond it are intentionally swallowed."""
    led = pr.BarrierLedger()
    for _ in range(5):
        led.observe("[7dtd-playtest] barrier settime_bloodmoon\n", "settime_bloodmoon")
    fired = 0
    while led.pending("settime_bloodmoon", cap=2):
        led.mark_fired("settime_bloodmoon")
        fired += 1
    _assert(fired == 2, f"actions capped at 2: {fired}")
    _assert(led.seen["settime_bloodmoon"] == 5, f"seen keeps full count: {led.seen!r}")
    _assert(not led.pending("settime_bloodmoon", cap=2), "cap holds across polls")


class _FakeAdmin:
    """TelnetAdmin stand-in: records closes, no sockets."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _open_admin_ok(admin: _FakeAdmin) -> object:
    def open_admin() -> object:
        return admin

    return open_admin


def _open_admin_dead() -> None:
    """Connect-failing admin factory (never returns an admin)."""


def test_fire_barrier_edges_fires_once_per_emission(tmp: Path) -> None:
    led = pr.BarrierLedger()
    blob = "[7dtd-playtest] barrier spawn_zombie\n" * 2
    calls: list[object] = []
    admin = _FakeAdmin()
    pr.fire_barrier_edges(
        led, blob, "spawn_zombie", _open_admin_ok(admin),
        lambda tn: calls.append(tn),
    )
    _assert(led.fired["spawn_zombie"] == 2, f"both edges fired: {led.fired!r}")
    _assert(len(calls) == 2, f"one action per edge: {calls!r}")
    _assert(admin.closed, "telnet closed after each action")


def test_fire_barrier_edges_false_action_stays_pending_then_consumes(tmp: Path) -> None:
    led = pr.BarrierLedger()
    state = {"n": 0}

    def not_yet(tn: object) -> bool | None:
        state["n"] += 1
        return False if state["n"] == 1 else None

    pr.fire_barrier_edges(
        led, "[7dtd-playtest] barrier kill_player\n", "kill_player",
        _open_admin_ok(_FakeAdmin()), not_yet,
    )
    _assert(led.fired.get("kill_player", 0) == 0, "False action must not consume edge")
    pr.fire_barrier_edges(
        led, "", "kill_player", _open_admin_ok(_FakeAdmin()), not_yet,
    )
    _assert(led.fired["kill_player"] == 1, f"edge consumed on retry: {led.fired!r}")


def test_fire_barrier_edges_connect_fail_keeps_edge_pending(tmp: Path) -> None:
    led = pr.BarrierLedger()
    pr.fire_barrier_edges(
        led, "[7dtd-playtest] barrier teleport_persist_pad\n",
        "teleport_persist_pad", _open_admin_dead, lambda tn: None,
    )
    _assert(led.seen["teleport_persist_pad"] == 1, "emission still counted")
    _assert(led.fired.get("teleport_persist_pad", 0) == 0, "failed connect not fired")
    pr.fire_barrier_edges(
        led, "", "teleport_persist_pad", _open_admin_ok(_FakeAdmin()),
        lambda tn: True,
    )
    _assert(led.fired["teleport_persist_pad"] == 1, "recovers on next poll")


def test_fire_barrier_edges_cap_bounds_actions(tmp: Path) -> None:
    led = pr.BarrierLedger()
    blob = "[7dtd-playtest] barrier settime_bloodmoon\n" * 5
    calls: list[object] = []
    pr.fire_barrier_edges(
        led, blob, "settime_bloodmoon", _open_admin_ok(_FakeAdmin()),
        lambda tn: calls.append(tn), cap=2,
    )
    _assert(len(calls) == 2, f"capped actions: {len(calls)}")
    _assert(led.fired["settime_bloodmoon"] == 2, f"fired capped: {led.fired!r}")
    _assert(led.seen["settime_bloodmoon"] == 5, f"seen keeps full count: {led.seen!r}")


def test_barrier_ledger_unknown_name_and_prefix_tokens() -> None:
    led = pr.BarrierLedger()
    _assert(not led.pending("never_observed"), "unknown name is not pending")
    led.mark_fired("chat_echo")  # fired-only counter (token set gates actual sends)
    _assert(
        led.fired["chat_echo"] == 1 and "chat_echo" not in led.seen,
        "fired-only counters are legal",
    )


def test_write_stock_config_rewrites_properties_and_inserts_userdata(tmp: Path) -> None:
    """Every managed property must be rewritten; silent re.sub no-ops here ship
    a config with EAC on / wrong ports and burn an exclusive runtime slot."""
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "serverconfig_src.xml"
    src.write_text(
        "<ServerSettings>\n"
        '\t<property name="ServerName" value="base"/>\n'
        '\t<property name="ServerPort" value="26900"/>\n'
        '\t<property name="TelnetPort" value="8081"/>\n'
        '\t<property name="EACEnabled" value="true"/>\n'
        '\t<property name="GameWorld" value="Other"/>\n'
        '\t<property name="GameName" value="OldSave"/>\n'
        "</ServerSettings>\n",
        encoding="utf-8",
    )
    out = tmp / "out" / "serverconfig_playtest.xml"
    userdata = tmp / "userdata"
    pr.write_stock_config(
        src,
        out,
        userdata,
        world_name="Navezgane",
        game_name="PlaytestNav",
        port=26901,
        telnet_port=8082,
    )
    text = out.read_text(encoding="utf-8")
    ud = str(userdata.resolve())
    # Insert path: source had no UserDataFolder property.
    _assert(
        f'name="UserDataFolder" value="{ud}"' in text,
        "UserDataFolder inserted when absent",
    )
    _assert(text.count("UserDataFolder") == 1, "exactly one UserDataFolder")
    _assert('name="ServerPort" value="26901"' in text, "port rewritten")
    _assert('name="TelnetPort" value="8082"' in text, "telnet port rewritten")
    _assert('name="EACEnabled" value="false"' in text, "EAC forced off")
    _assert('name="GameWorld" value="Navezgane"' in text, "world rewritten")
    _assert('name="GameName" value="PlaytestNav"' in text, "game name rewritten")
    _assert('value="26900"' not in text, "old port gone")
    _assert('value="true"' not in text, "stale true gone")
    _assert('value="OldSave"' not in text, "old game name gone")


def test_write_stock_config_replaces_existing_userdata(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "serverconfig_src.xml"
    src.write_text(
        "<ServerSettings>\n"
        '\t<property name="UserDataFolder" value="/old/place with spaces"/>\n'
        "</ServerSettings>\n",
        encoding="utf-8",
    )
    out = tmp / "serverconfig_playtest.xml"
    userdata = tmp / "userdata2"
    pr.write_stock_config(
        src,
        out,
        userdata,
        world_name="Navezgane",
        game_name="PlaytestNav",
        port=26900,
        telnet_port=8081,
    )
    text = out.read_text(encoding="utf-8")
    ud = str(userdata.resolve())
    _assert(
        f'name="UserDataFolder" value="{ud}"' in text,
        "existing UserDataFolder replaced",
    )
    _assert("/old/place" not in text, "previous userdata path gone")
    _assert(text.count("UserDataFolder") == 1, "not duplicated")


def test_write_stock_config_escapes_xml_metacharacters(tmp: Path) -> None:
    """Operator-supplied names/passwords must stay inside one XML attribute
    each and round-trip through a real XML parse (no breakout, no dropped
    properties). A custom telnet_password must also reach the config."""
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / "serverconfig_src.xml"
    src.write_text(
        "<ServerSettings>\n"
        '\t<property name="GameWorld" value="Other"/>\n'
        '\t<property name="GameName" value="OldSave"/>\n'
        '\t<property name="TelnetPassword" value=""/>\n'
        "</ServerSettings>\n",
        encoding="utf-8",
    )
    out = tmp / "out" / "serverconfig_playtest.xml"
    userdata = tmp / "user&data"
    pr.write_stock_config(
        src,
        out,
        userdata,
        world_name='Nave"gane&<w>',
        game_name='Play"test&<g>',
        port=26900,
        telnet_port=8081,
        telnet_password='re"test&<p>',
    )
    root = ET.parse(out).getroot()
    props = {p.get("name"): p.get("value") for p in root.findall("property")}
    _assert(props["GameWorld"] == 'Nave"gane&<w>', f"world round-trip: {props['GameWorld']!r}")
    _assert(props["GameName"] == 'Play"test&<g>', f"game round-trip: {props['GameName']!r}")
    _assert(
        props["TelnetPassword"] == 're"test&<p>',
        f"password round-trip: {props['TelnetPassword']!r}",
    )
    _assert(
        props["UserDataFolder"] == str(userdata.resolve()),
        "ampersand userdata path round-trips",
    )
    _assert(len(root.findall("property")) == 4, "no injected extra properties")


def test_barrier_hits_counts_human_line_once_per_emission(tmp: Path) -> None:
    human = "[7dtd-playtest] barrier spawn_zombie\n"
    json_echo = '[7dtd-playtest] {"v":1,"t":"barrier","name":"spawn_zombie"}\n'
    _assert(pr.barrier_hits(human, "spawn_zombie") == 1, "one emission counts once")
    _assert(pr.barrier_hits(human * 3, "spawn_zombie") == 3, "three emissions count three")
    # Report.Barrier emits human + JSON back to back; counting both double-fires
    # handlers (e.g. kills bots), so only the human line may count.
    _assert(pr.barrier_hits(json_echo, "spawn_zombie") == 0, "json event alone counts zero")
    _assert(
        pr.barrier_hits(human + json_echo, "spawn_zombie") == 1,
        "human+json pair counts once (no double fire)",
    )
    _assert(pr.barrier_hits(human, "kill_player") == 0, "other names do not count")


def test_barrier_hits_prefix_dedup_order_and_boundaries(tmp: Path) -> None:
    blob = (
        "[7dtd-playtest] barrier chat_echo:alpha\n"
        "noise without prefix\n"
        "[7dtd-playtest] barrier chat_echo:beta\n"
        "[7dtd-playtest] barrier chat_echo:alpha\n"
        '[7dtd-playtest] {"v":1,"t":"barrier","name":"chat_echo:gamma"}\n'
        '[7dtd-playtest] barrier chat_echo:"quoted"\n'
    )
    hits = pr.barrier_hits_prefix(blob, "chat_echo:")
    _assert(
        hits[:2] == ["chat_echo:alpha", "chat_echo:beta"],
        f"deduped, order preserved: {hits!r}",
    )
    _assert(all("{" not in h for h in hits), "json events excluded from prefix scan")
    _assert(
        "chat_echo:" in [h for h in hits if h.endswith(":")] or hits[-1] == "chat_echo:",
        f"trailing quote terminates token instead of leaking: {hits!r}",
    )
    _assert(pr.barrier_hits_prefix(blob, "apm_dump") == [], "non-matching prefix empty")
    _assert(pr.barrier_hits_prefix("", "chat_echo:") == [], "empty blob empty")


def test_client_mute_env_chain(tmp: Path) -> None:
    keys = ("CLIENT_MUTE", "PLAYTEST_MUTE", "SEVEN_DAYS_TO_DIE_CLIENT_MUTE")
    old = {k: os.environ.get(k) for k in keys}

    def set_env(vals: dict[str, str]) -> None:
        for k in keys:
            if k in vals:
                os.environ[k] = vals[k]
            else:
                os.environ.pop(k, None)

    try:
        set_env({})
        _assert(pr.client_mute_enabled(), "mute defaults on")
        for off in ("0", "false", "No", "OFF"):
            set_env({"CLIENT_MUTE": off})
            _assert(not pr.client_mute_enabled(), f"{off} opts out")
        set_env({"CLIENT_MUTE": "1"})
        _assert(pr.client_mute_enabled(), "explicit 1 stays on")
        # Empty first variable falls through to the next name in the chain.
        set_env({"CLIENT_MUTE": "", "PLAYTEST_MUTE": "off"})
        _assert(not pr.client_mute_enabled(), "empty CLIENT_MUTE falls through")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_fresh_save_removes_only_matching_game_dirs(tmp: Path) -> None:
    """fresh_save must delete Saves/<world>/<GameName> everywhere and nothing else."""
    userdata = tmp / "userdata"
    saves = userdata / "Saves"
    target = saves / "Navezgane" / "PlaytestNav"
    target.mkdir(parents=True)
    (target / "region.tsv").write_text("save data", encoding="utf-8")
    sibling = saves / "Navezgane" / "KeepThisGame"
    sibling.mkdir(parents=True)
    other_world = saves / "Pregen06k01" / "PlaytestNav"
    other_world.mkdir(parents=True)
    stray = saves / "stray-file.txt"
    stray.write_text("keep", encoding="utf-8")

    pr.fresh_save(userdata, "PlaytestNav")

    _assert(not target.exists(), "matching save dir removed")
    _assert(not other_world.exists(), "matching save removed in every world dir")
    _assert(sibling.is_dir(), "different GameName under same world kept")
    _assert(stray.is_file(), "non-directory entries kept")

    # Missing Saves dir is a quiet no-op, not a crash.
    pr.fresh_save(tmp / "empty-userdata", "PlaytestNav")


def test_wait_file_contains_finds_marker_across_polls(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    log = tmp / "server.log"
    log.write_bytes(b"world loading... StartGa")
    # Marker split across two writes must be found via the overlap window.
    import threading

    def finish_later() -> None:
        time.sleep(0.3)
        with log.open("ab") as fh:
            fh.write(b"me done\n")

    th = threading.Thread(target=finish_later)
    th.start()
    try:
        found = pr.wait_file_contains(log, "StartGame done", timeout=5.0)
    finally:
        th.join()
    _assert(found, "marker spanning poll boundary detected")


def main() -> int:
    fails = 0
    with tempfile.TemporaryDirectory(prefix="playtest-parse-") as td:
        tmp = Path(td)
        cases: list[tuple[str, object]] = [
            ("human_log_results_summary_done", lambda: test_human_log_results_summary_done(tmp / "human")),
            ("done_without_exit_hint", lambda: test_done_without_exit_hint(tmp / "hint")),
            ("json_events_preferred_over_human", lambda: test_json_events_preferred_over_human(tmp / "json")),
            ("summary_fallback_counts_results", lambda: test_summary_fallback_counts_results(tmp / "fallback")),
            ("malformed_json_line_is_dropped_not_crash", lambda: test_malformed_json_line_is_dropped_not_crash(tmp / "badjson")),
            ("empty_and_unrelated_text", lambda: test_empty_and_unrelated_text(tmp / "empty")),
            ("nre_like_lines_captured_and_capped", lambda: test_nre_like_lines_captured_and_capped(tmp / "nre")),
            ("write_junit_escapes_details_into_valid_xml", lambda: test_write_junit_escapes_details_into_valid_xml(tmp / "junit")),
            ("suite_wants_zombie_fixture_gate", lambda: test_suite_wants_zombie_fixture_gate(tmp / "gate")),
            (
                "suite_tokens_matches_client_separators",
                lambda: test_suite_tokens_matches_client_separators(),
            ),
            (
                "stop_proc_reaps_after_sigkill_escalation",
                lambda: test_stop_proc_reaps_after_sigkill_escalation(tmp / "reap"),
            ),
            (
                "stop_proc_reaps_on_graceful_term",
                lambda: test_stop_proc_reaps_on_graceful_term(tmp / "term"),
            ),
            ("stop_proc_none_is_noop", test_stop_proc_none_is_noop),
            (
                "start_zdtd_failure_leaks_no_log_fd",
                lambda: test_start_zdtd_failure_leaks_no_log_fd(tmp / "fdleak"),
            ),
            (
                "log_tail_yields_only_appended_complete_lines",
                lambda: test_log_tail_yields_only_appended_complete_lines(tmp / "tail"),
            ),
            (
                "log_tail_barrier_count_matches_full_text_scan",
                lambda: test_log_tail_barrier_count_matches_full_text_scan(tmp / "tailcount"),
            ),
            (
                "barrier_ledger_fires_edges_split_across_polls",
                lambda: test_barrier_ledger_fires_edges_split_across_polls(),
            ),
            (
                "barrier_ledger_counts_multiple_edges_in_one_chunk",
                lambda: test_barrier_ledger_counts_multiple_edges_in_one_chunk(),
            ),
            (
                "barrier_ledger_failed_action_stays_pending_for_retry",
                lambda: test_barrier_ledger_failed_action_stays_pending_for_retry(),
            ),
            (
                "barrier_ledger_cap_bounds_actions_not_seen",
                lambda: test_barrier_ledger_cap_bounds_actions_not_seen(),
            ),
            (
                "barrier_ledger_unknown_name_and_prefix_tokens",
                lambda: test_barrier_ledger_unknown_name_and_prefix_tokens(),
            ),
            (
                "fire_barrier_edges_fires_once_per_emission",
                lambda: test_fire_barrier_edges_fires_once_per_emission(tmp),
            ),
            (
                "fire_barrier_edges_false_action_stays_pending_then_consumes",
                lambda: test_fire_barrier_edges_false_action_stays_pending_then_consumes(tmp),
            ),
            (
                "fire_barrier_edges_connect_fail_keeps_edge_pending",
                lambda: test_fire_barrier_edges_connect_fail_keeps_edge_pending(tmp),
            ),
            (
                "fire_barrier_edges_cap_bounds_actions",
                lambda: test_fire_barrier_edges_cap_bounds_actions(tmp),
            ),
            (
                "wait_file_contains_finds_marker_across_polls",
                lambda: test_wait_file_contains_finds_marker_across_polls(tmp / "waitmark"),
            ),
            (
                "write_stock_config_rewrites_properties_and_inserts_userdata",
                lambda: test_write_stock_config_rewrites_properties_and_inserts_userdata(
                    tmp / "stockcfg"
                ),
            ),
            (
                "write_stock_config_replaces_existing_userdata",
                lambda: test_write_stock_config_replaces_existing_userdata(tmp / "stockcfg2"),
            ),
            (
                "write_stock_config_escapes_xml_metacharacters",
                lambda: test_write_stock_config_escapes_xml_metacharacters(tmp / "stockcfg3"),
            ),
            (
                "barrier_hits_counts_human_line_once_per_emission",
                lambda: test_barrier_hits_counts_human_line_once_per_emission(tmp / "hits"),
            ),
            (
                "barrier_hits_prefix_dedup_order_and_boundaries",
                lambda: test_barrier_hits_prefix_dedup_order_and_boundaries(tmp / "prefix"),
            ),
            ("client_mute_env_chain", lambda: test_client_mute_env_chain(tmp / "mute")),
            (
                "fresh_save_removes_only_matching_game_dirs",
                lambda: test_fresh_save_removes_only_matching_game_dirs(tmp / "fresh"),
            ),
        ]
        for name, fn in cases:
            try:
                fn()  # type: ignore[operator]
                print(f"PASS {name}")
            except Exception as ex:  # noqa: BLE001 — report each test
                fails += 1
                print(f"FAIL {name}: {ex}", file=sys.stderr)

    if fails:
        print(f"RESULT FAIL ({fails})", file=sys.stderr)
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
