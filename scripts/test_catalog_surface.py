#!/usr/bin/env python3
"""Structural tests: demo catalog ships live cases; residual infra cases are live too."""
from __future__ import annotations

import re
import sys
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
    missing = [c for c in REQUIRED_LIVE if c not in live]
    assert not missing, f"Catalog.cs missing Live cases: {missing}"

    still_deferred = [c for c in RESIDUAL_MUST_BE_LIVE if c in defer_case_ids(cat)]
    assert not still_deferred, f"residual ids still Defer: {still_deferred}"

    for case in REQUIRED_LIVE:
        assert re.search(rf"`{case}`\s*\|\s*live\b", doc), (
            f"SCENARIOS.md must document {case} as live"
        )

    # No multi-peer / rejoin / soak / apm Defer leftovers among residual ids.
    for cid, reason in defer_reasons(cat):
        if cid in RESIDUAL_MUST_BE_LIVE:
            raise AssertionError(f"residual {cid} still deferred: {reason}")

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

    print("OK catalog surface:", ", ".join(REQUIRED_LIVE[:8]), "… +" + str(len(REQUIRED_LIVE) - 8))
    print("OK demo alias includes vehicle+power+finale")
    print("OK residual 13 ids are Live:", ", ".join(RESIDUAL_MUST_BE_LIVE[:4]), "…")
    print("OK orchestrator has persist/loadgen/apm barriers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
