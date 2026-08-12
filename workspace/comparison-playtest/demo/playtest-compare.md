# Stock-vs-zdtd playtest comparison

| axis | stock | zdtd |
|---|---|---|
| cases PASS | 79 | 79 |
| cases FAIL | 4 | 4 |
| cases SKIP | 0 | 0 |

## Per-case

| case | stock | zdtd |
|---|---|---|
| `combat/alive_flags_self` | PASS | PASS |
| `combat/blood_moon_music` | PASS | PASS |
| `combat/explosion_client` | PASS | PASS |
| `combat/held_item_type` | PASS | PASS |
| `combat/melee_damage_out` | FAIL | FAIL |
| `combat/ranged_shot` | PASS | PASS |
| `combat/sleeper_wake` | FAIL | PASS |
| `combat/zombie_death_loot` | PASS | FAIL |
| `combat/zombie_or_npc_nearby` | FAIL | PASS |
| `combat/zombie_target_has_health` | FAIL | PASS |
| `core/bag_present` | PASS | PASS |
| `core/block_damage_melee` | PASS | PASS |
| `core/buffs` | PASS | PASS |
| `core/dig_confirm` | PASS | PASS |
| `core/held_slot_report` | PASS | PASS |
| `core/inventory` | PASS | PASS |
| `core/jump_motor` | PASS | PASS |
| `core/look` | PASS | PASS |
| `core/look_pitch` | PASS | PASS |
| `core/look_yaw_sweep` | PASS | PASS |
| `core/place_confirm` | PASS | PASS |
| `core/quests_journal` | PASS | PASS |
| `core/sneak_motor` | PASS | PASS |
| `core/sprint_motor` | PASS | PASS |
| `core/stamina_drains_sprint` | PASS | PASS |
| `core/walk_lateral` | PASS | PASS |
| `core/walk_motor` | PASS | PASS |
| `core/walk_ring` | PASS | PASS |
| `economy/bag_add_item` | PASS | PASS |
| `economy/chest_open_loot` | PASS | PASS |
| `economy/craft_consume_output` | PASS | PASS |
| `economy/craft_window_recipes` | PASS | PASS |
| `economy/eat_food_consume` | PASS | PASS |
| `economy/item_drop_entity` | PASS | FAIL |
| `economy/land_claim_place` | PASS | PASS |
| `economy/loot_bag_pickup` | PASS | FAIL |
| `economy/trader_buy` | PASS | PASS |
| `economy/trader_stock_ui` | PASS | PASS |
| `economy/workstation_burn` | PASS | PASS |
| `economy/zombie_removed_after_kill` | PASS | PASS |
| `finale/player_death_screen` | PASS | PASS |
| `finale/player_respawn` | PASS | PASS |
| `power/generator_fuel` | PASS | PASS |
| `power/place_generator` | PASS | PASS |
| `power/trigger_actuation` | PASS | PASS |
| `power/turret_place` | PASS | PASS |
| `power/wire_remove_parent` | PASS | PASS |
| `power/wire_set_parent` | PASS | PASS |
| `quest/journal_exists` | PASS | PASS |
| `quest/journal_iterate` | PASS | PASS |
| `quest/quest_goto_progress` | PASS | PASS |
| `quest/quest_kill_progress` | PASS | PASS |
| `quest/quest_nav_marker` | PASS | PASS |
| `quest/quest_turn_in` | PASS | PASS |
| `quest/starter_quest_active` | PASS | PASS |
| `smoke/cgo_ready` | PASS | PASS |
| `smoke/day_clock` | PASS | PASS |
| `smoke/ground` | PASS | PASS |
| `smoke/join_ready` | PASS | PASS |
| `smoke/stats` | PASS | PASS |
| `ui/character_open` | PASS | PASS |
| `ui/craft_open` | PASS | PASS |
| `ui/creative_menu` | PASS | PASS |
| `ui/inventory_open` | PASS | PASS |
| `ui/map_open` | PASS | PASS |
| `ui/quest_log_open` | PASS | PASS |
| `ui/skills_open` | PASS | PASS |
| `ui/ui_close_all` | PASS | PASS |
| `vehicle/vehicle_drive` | PASS | PASS |
| `vehicle/vehicle_enter_exit` | PASS | PASS |
| `vehicle/vehicle_fuel_burn` | PASS | PASS |
| `vehicle/vehicle_spawn_visible` | PASS | PASS |
| `vehicle/vehicle_terrain_clamp` | PASS | PASS |
| `world/biome_id` | PASS | PASS |
| `world/block_sample_ring` | PASS | PASS |
| `world/chunk_under_player` | PASS | PASS |
| `world/deco_trees` | PASS | PASS |
| `world/entities_in_radius` | PASS | PASS |
| `world/poi_textures_non_terrain` | PASS | PASS |
| `world/water_plane` | PASS | PASS |
| `world/weather_array` | PASS | PASS |
| `world/world_time` | PASS | PASS |
| `world/world_time_advances` | PASS | PASS |

## Findings

- combat/sleeper_wake: status differs (FAIL vs PASS)
- combat/zombie_death_loot: status differs (PASS vs FAIL)
- combat/zombie_or_npc_nearby: status differs (FAIL vs PASS)
- combat/zombie_target_has_health: status differs (FAIL vs PASS)
- economy/item_drop_entity: status differs (PASS vs FAIL)
- economy/loot_bag_pickup: status differs (PASS vs FAIL)

*Triage each finding: zdtd bug vs harness artifact vs known divergence. Known divergences are recorded in zdtd/docs/PROVENANCE.md (divergence register).*
