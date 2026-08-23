#!/usr/bin/env python3
"""Structural tests: demo catalog ships live cases; residual infra cases are
live too; the host fixture arm-list matches Catalog barrier emissions."""
from __future__ import annotations

import re
import sys
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "Source" / "PlayTestMod" / "Catalog.cs"
SCENARIOS = ROOT / "SCENARIOS.md"
ORCH = ROOT / "scripts" / "playtest_run.py"

REQUIRED_LIVE = (
    "world_time_advances",
    "zombie_target_has_health",
    "bag_add_item",
    "zombie_removed_after_kill",
    "journal_iterate",
    "walk_motor",
    "sprint_motor",
    "sneak_motor",
    "jump_motor",
    "melee_damage_out",
    "stamina_drains_sprint",
    "quest_log_open",
    "skills_open",
    "block_damage_melee",
    "item_drop_entity",
    "eat_food_consume",
    "ranged_shot",
    "loot_bag_pickup",
    "land_claim_place",
    "creative_menu",
    "craft_consume_output",
    "workstation_burn",
    "poi_textures_non_terrain",
    "weather_array",
    "deco_trees",
    "water_plane",
    "zombie_death_loot",
    "explosion_client",
    "sleeper_wake",
    "blood_moon_music",
    "chest_open_loot",
    "trader_stock_ui",
    "trader_buy",
    "starter_quest_active",
    "quest_goto_progress",
    "quest_kill_progress",
    "quest_turn_in",
    "quest_nav_marker",
    "vehicle_spawn_visible",
    "vehicle_enter_exit",
    "vehicle_drive",
    "vehicle_fuel_burn",
    "vehicle_terrain_clamp",
    "place_generator",
    "wire_set_parent",
    "wire_remove_parent",
    "turret_place",
    "generator_fuel",
    "trigger_actuation",
    "player_death_screen",
    "player_respawn",
    # residual infrastructure promotions
    "dig_survives_rejoin",
    "inv_survives_rejoin",
    "pos_survives_rejoin",
    "te_survives_rejoin",
    "blockmeta_survives",
    "second_client_visible",
    "chat_roundtrip",
    "setblock_interest",
    "lock_contention",
    "shared_quest",
    "bots_plus_playtest",
    "soak_15min_host",
    "soak_apm_budget",
)

# Former residual ids must not remain Defer.
RESIDUAL_MUST_BE_LIVE = (
    "lock_contention",
    "shared_quest",
    "second_client_visible",
    "chat_roundtrip",
    "setblock_interest",
    "bots_plus_playtest",
    "dig_survives_rejoin",
    "inv_survives_rejoin",
    "pos_survives_rejoin",
    "te_survives_rejoin",
    "blockmeta_survives",
    "soak_15min_host",
    "soak_apm_budget",
)


def live_case_ids_from_catalog(src: str) -> set[str]:
    return set(re.findall(r'\bLive\s*\(\s*suite\s*,\s*"([a-z0-9_]+)"', src))


def defer_case_ids(src: str) -> set[str]:
    return set(
        re.findall(
            r'\bDefer\s*\(\s*suite\s*,\s*"([a-z0-9_]+)"',
            src,
        )
    )


def defer_reasons(src: str) -> list[tuple[str, str]]:
    return re.findall(
        r'\bDefer\s*\(\s*suite\s*,\s*"([a-z0-9_]+)"\s*,\s*new\s*\[\s*\]\s*\{[^}]*\}\s*,\s*"([^"]*)"\s*\)',
        src,
        flags=re.S,
    )


def persist_pad_from_catalog(src: str) -> tuple[int, int, int] | None:
    m = re.search(
        r"PersistPlayerPos\s*=\s*new Vector3\(\s*([\d.]+)f?\s*,"
        r"\s*([\d.]+)f?\s*,\s*([\d.]+)f?\s*\)",
        src,
    )
    if not m:
        return None
    x, y, z = (int(float(g)) for g in m.groups())
    return (x, y, z)


def persist_pad_from_orchestrator(src: str) -> tuple[int, int, int] | None:
    m = re.search(
        r"PERSIST_PAD_XYZ\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", src
    )
    if not m:
        return None
    x, y, z = (int(g) for g in m.groups())
    return (x, y, z)


def suite_names_from_catalog(src: str) -> set[str]:
    m = re.search(r"static readonly string\[\] SuiteNames\s*=\s*\{([^}]*)\}", src, re.S)
    assert m, "Catalog.cs lost the SuiteNames table"
    return set(re.findall(r'"([a-z0-9_]+)"', m.group(1)))


def expand_alias_ids_from_catalog(src: str) -> set[str]:
    """Alias labels accepted by Catalog.ExpandSuites (case labels in its switch)."""
    start = src.index("public static string[] ExpandSuites")
    end = src.index("static void AddUnique", start)
    body = src[start:end]
    return set(re.findall(r'case "([a-z0-9_]+)":', body))


def append_suite_map(src: str) -> dict[str, list[str]]:
    """Map each concrete AppendSuite case to the Add* methods it composes.

    Most cases call one Add method; benchmark composes four. The default arm
    (external scenario providers) has no built-in body and is omitted.
    """
    start = src.index("public static void AppendSuite")
    end = src.index("static CaseDef Live", start)
    body = src[start:end]
    marks = [(m.start(), m.group(1)) for m in re.finditer(r'case "([a-z0-9_]+)":', body)]
    marks.append((len(body), ""))
    result: dict[str, list[str]] = {}
    for (s, suite), (e, _) in pairwise(marks):
        adds = re.findall(r"\bAdd([A-Z]\w*)\s*\(", body[s:e])
        if adds:
            result[suite] = adds
    return result


def add_method_barriers(src: str) -> dict[str, set[str]]:
    """Attribute every Report.Barrier literal in Catalog.cs to its Add method.

    Catalog.cs declares the Add* methods sequentially and only case bodies
    emit barriers, so the nearest preceding `static void AddX(` header is the
    owner. Prefix only (before any ':' or concatenation): parameterized names
    like "chat_echo:<token>" still match their host BARRIER_NAMES entry.
    """
    headers = [
        (m.start(), m.group(1))
        for m in re.finditer(r"\bstatic void Add([A-Z]\w*)\s*\(", src)
    ]
    emissions = [
        (m.start(), m.group(1))
        for m in re.finditer(r'Report\.Barrier\(\s*"([A-Za-z0-9_]+)', src)
    ]
    assert emissions, "Catalog.cs lost every Report.Barrier emission"
    out: dict[str, set[str]] = {}
    for pos, prefix in emissions:
        owner = None
        for hpos, name in headers:
            if hpos < pos:
                owner = name
            else:
                break
        assert owner, f"Report.Barrier({prefix!r}) before any Add method header"
        out.setdefault(owner, set()).add(prefix)
    return out


def quoted_ids_after_assignment(src: str, var: str) -> set[str]:
    """Parse quoted ids from a host table whose literal closes on a bare `)` line."""
    m = re.search(re.escape(var) + r"\b[^\n]*=", src)
    assert m, f"playtest_run.py lost the {var} table"
    tail = src[m.end() :]
    return set(re.findall(r'"([a-z0-9_]+)"', tail[: tail.index("\n)")]))


def main() -> int:
    assert CATALOG.is_file(), f"missing {CATALOG}"
    assert SCENARIOS.is_file(), f"missing {SCENARIOS}"
    cat = CATALOG.read_text(encoding="utf-8")
    doc = SCENARIOS.read_text(encoding="utf-8")
    orch = ORCH.read_text(encoding="utf-8")

    assert (
        'AddUnique(list, "smoke", "core", "world", "ui", "combat",'
        in cat
        and '"economy", "quest", "vehicle", "power", "finale"' in cat
    ), "demo alias must expand to vehicle+power+finale"

    live = live_case_ids_from_catalog(cat)
    deferred = defer_case_ids(cat)
    missing = [c for c in REQUIRED_LIVE if c not in live]
    assert not missing, f"Catalog.cs missing Live cases: {missing}"

    still_deferred = [c for c in RESIDUAL_MUST_BE_LIVE if c in deferred]
    assert not still_deferred, f"residual ids still Defer: {still_deferred}"

    # Full catalog↔docs surface: every built-in Live id must appear as live in
    # SCENARIOS.md (not only the historical REQUIRED_LIVE allowlist).
    undoc_live = sorted(
        c for c in live if not re.search(rf"`{re.escape(c)}`\s*\|\s*live\b", doc)
    )
    assert not undoc_live, (
        f"SCENARIOS.md missing live rows for Catalog Live cases: {undoc_live}"
    )

    # The Counts table total must equal the parsed Live set (drift guard).
    total_m = re.search(r"\*\*catalog total\*\*\s*\|\s*\*\*(\d+)\*\*", doc)
    assert total_m and int(total_m.group(1)) == len(live), (
        f"Counts 'catalog total' {total_m.group(1) if total_m else '?'} != "
        f"Catalog.cs Live count {len(live)} - stale suite row in SCENARIOS.md"
    )

    # Built-in Defer set is empty today; any new Defer must be documented.
    undoc_defer = sorted(
        c
        for c in deferred
        if not re.search(rf"`{re.escape(c)}`\s*\|\s*deferred\b", doc)
    )
    assert not undoc_defer, (
        f"SCENARIOS.md missing deferred rows for Catalog Defer cases: {undoc_defer}"
    )

    # No multi-peer / rejoin / soak / apm Defer leftovers among residual ids.
    for cid, reason in defer_reasons(cat):
        if cid in RESIDUAL_MUST_BE_LIVE:
            raise AssertionError(f"residual {cid} still deferred: {reason}")

    # Cross-language pad contract: the host teleports players to
    # PERSIST_PAD_XYZ over telnet while persist_setup_pos and
    # pos_survives_rejoin assert proximity to Catalog.PersistPlayerPos. A
    # drift between the two copies of this constant fails persist runs with
    # an opaque "far from pad", so pin them equal like every other surface.
    cat_pad = persist_pad_from_catalog(cat)
    orch_pad = persist_pad_from_orchestrator(orch)
    assert cat_pad is not None, "Catalog.cs lost the PersistPlayerPos constant"
    assert orch_pad is not None, "playtest_run.py lost the PERSIST_PAD_XYZ constant"
    assert cat_pad == orch_pad, (
        f"persist pad drift: Catalog.cs PersistPlayerPos {cat_pad} != "
        f"playtest_run.py PERSIST_PAD_XYZ {orch_pad}"
    )

    # Cross-language fixture contract: the host arms telnet fixtures only for
    # suites listed in playtest_run.py FIXTURE_SUITE_IDS, while Catalog.cs
    # decides per suite which cases emit barrier lines. A suite that emits a
    # fixture-serviced barrier without being listed runs telnet-free and its
    # cases hang on a barrier no host ever services, so pin the two tables
    # together like every other surface.
    #
    # Deliberately NOT required in FIXTURE_SUITE_IDS (each has its own
    # unconditional host path instead of the want_fixtures loop):
    #   persist_setup_done - serviced by the dedicated rejoin flow
    #     (playtest_run.py opens TelnetAdmin itself; SUITE=persist never
    #     sets want_fixtures);
    #   teleport_persist_pad, apm_dump - serviced in the poll loop outside
    #     `if want_fixtures` (apm also preseeds ZDTD_APM_DUMP env by suite
    #     token, independent of this list).
    exempt_barriers = {
        "persist_setup_done",
        "teleport_persist_pad",
        "apm_dump",
    }
    barrier_names = quoted_ids_after_assignment(orch, "BARRIER_NAMES")
    fixture_ids = quoted_ids_after_assignment(orch, "FIXTURE_SUITE_IDS")
    assert "spawn_zombie" in barrier_names and "kill_player" in barrier_names, (
        "BARRIER_NAMES parse drifted; check playtest_run.py table shape"
    )
    suite_adds = append_suite_map(cat)
    method_barriers = add_method_barriers(cat)
    fixture_gated = barrier_names - exempt_barriers
    need_fixtures = sorted(
        suite
        for suite, adds in suite_adds.items()
        if set().union(*(method_barriers.get(a, set()) for a in adds)) & fixture_gated
    )
    unlisted = [s for s in need_fixtures if s not in fixture_ids]
    assert not unlisted, (
        "Catalog suites emit fixture-serviced barriers but are missing from "
        f"playtest_run.py FIXTURE_SUITE_IDS (their barriers would never fire): "
        f"{unlisted}"
    )
    known_suites = suite_names_from_catalog(cat)
    aliases = expand_alias_ids_from_catalog(cat)
    unknown_fixture = sorted(fixture_ids - known_suites - aliases)
    assert not unknown_fixture, (
        f"playtest_run.py FIXTURE_SUITE_IDS lists ids unknown to Catalog.cs "
        f"(typo or removed suite): {unknown_fixture}"
    )

    for name in (
        "kill_fixture_zombie",
        "spawn_zombie",
        "kill_player",
        "settime_bloodmoon",
        "spawn_vehicle",
        "spawn_trader",
        "spawn_loadgen_peer",
        "spawn_loadgen_bots",
        "persist_setup_done",
        "apm_dump",
        "chat_echo",
        "write_zdtd_apm_dump",
        "start_loadgen",
    ):
        assert name in orch, f"orchestrator missing {name}"

    print(
        "OK catalog surface:",
        ", ".join(REQUIRED_LIVE[:8]),
        "… +" + str(len(REQUIRED_LIVE) - 8),
    )
    print("OK all", len(live), "Live case ids documented as live in SCENARIOS.md")
    print("OK Defer count:", len(deferred), "(undocumented:", len(undoc_defer), ")")
    print("OK demo alias includes vehicle+power+finale")
    print("OK residual 13 ids are Live:", ", ".join(RESIDUAL_MUST_BE_LIVE[:4]), "…")
    print("OK orchestrator has persist/loadgen/apm barriers")
    print("OK persist pad matches Catalog.cs PersistPlayerPos:", cat_pad)
    print(
        "OK fixture suites pinned to Catalog barrier emissions:",
        ", ".join(need_fixtures),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
