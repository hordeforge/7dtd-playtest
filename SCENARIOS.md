# Playtest scenarios: demos, benchmarks, full catalog

Built-in **demo mode** and **benchmark** style suites for the stock client
against a real server (default: stock dedicated Navezgane).

Design: [`../zdtd-server-server/docs/CLIENT_PLAYTEST.md`](../zdtd-server-server/docs/CLIENT_PLAYTEST.md)  
Code: `Source/PlayTestMod/Catalog.cs`

## How to think about this

| Mode | Analogy | What it does |
|---|---|---|
| **smoke** | boot check | Join + mesh + ground + stats |
| **demo** | game demo / attract mode | Fixed cinematic: look → walk → dig/place → UI tour → world probes |
| **benchmark** | built-in bench | Same path as demo core/world, timed; repeat with `PLAYTEST_LAPS` |
| **gate** | CI / PR | Live smoke+core only (no deferred skip noise) |
| **full** | major catalog | Demo domains + soak (not persist/mp; those need host orch) |
| **catalog** | list only | Log every case id and exit (no join required if armed at menu… still needs mod load) |

Results always end with `SUMMARY` + `DONE exit_hint=0|1`. Deferred cases are
`SKIP` (do not fail the suite).

## Suite aliases

| Alias | Expands to |
|---|---|
| `demo` / `demo_mode` | `smoke…quest,vehicle,power,finale` (telnet spawn/kill/settime) |
| `demo_min` | `smoke,core,world,ui` (no combat wait) |
| `benchmark` / `bench` | smoke+core+world+ui; multiply by `PLAYTEST_LAPS` |
| `gate` / `ci` | `smoke,core` |
| `live` | same as `full` / `all` |
| `full` / `all` | smoke…finale + soak (not persist/mp/apm/soak_long) |
| `residual` / `residual_light` | **client only:** `mp` + short `soak` (not Make residual gate) |
| `catalog` / `list` | dump case list to log |

`make playtest-residual` is a **host** sequence: persist + mp + apm +
soak_long. It is not `ExpandSuites("residual")`.

Env:

```bash
PLAYTEST_SUITE=demo
PLAYTEST_LAPS=3          # benchmark repeats
PLAYTEST=1               # legacy → demo
```

Make:

```bash
make playtest-demo
make playtest-bench LAPS=3
make playtest-gate
make playtest-full            # long (85 cases); no persist/mp/apm/bot
make playtest SUITE=combat
```

## Status legend

| Status | Meaning |
|---|---|
| **live** | Runs Act→Wait→Assert on stock or zdtd |
| **deferred** | Recorded as SKIP until admin fixture / server feature / host multi-phase |

---

## smoke: boot / join gate

| Case | Status | Tags | Assert |
|---|---|---|---|
| `join_ready` | live | join, demo | Spawned, hp &gt; 0 |
| `cgo_ready` | live | join, mesh, demo | CGO ≥ gate (or fixedSize) |
| `ground` | live | world, demo | Block under feet solid |
| `stats` | live | player, demo | hp/stamina sane |
| `day_clock` | live | world, demo | Decode `worldTime` → day/hour/minute |

---

## core: play loop (demo spine)

| Case | Status | Tags | Assert |
|---|---|---|---|
| `look` | live | input, demo | Yaw set |
| `look_pitch` | live | input, demo | Pitch down then level |
| `look_yaw_sweep` | live | input, demo, bench | 4-cardinal camera pan |
| `walk_motor` | live | move, demo, bench, locomotion | **Motor walk** (`isAutorun`+`MovementInput`); ≥1.5 m, multi-tick, hopMax&lt;2 m |
| `walk_ring` | live | move, demo, bench, locomotion | **Motor ring**: four yaw legs; path ≥2 m |
| `sprint_motor` | live | move, demo, locomotion | Sprint 2 s; ≥3.5 m smooth |
| `stamina_drains_sprint` | live | player, demo, locomotion | Sprint pulse; `Stamina` drops ≥1.5 |
| `sneak_motor` | live | move, demo, locomotion | Sneak 2 s; moves but slower than sprint |
| `walk_lateral` | live | move, demo, locomotion | Motor walk facing yaw=90; ≥1.5 m smooth (axis free) |
| `jump_motor` | live | move, demo, locomotion | Pulse `MovementInput.jump`; peak Y rise ≥0.35 m |
| `inventory` | live | inv, demo | Inventory non-null + held type |
| `bag_present` | live | inv, demo | Bag slot array length &gt; 0 |
| `dig_confirm` | live | world, c2s, setblock, demo, bench | Seed solid then dig; GetBlock → air (self-contained) |
| `place_confirm` | live | world, c2s, setblock, demo, bench | GetBlock solid after place |
| `block_damage_melee` | live | world, c2s, demo, melee | Primary attack raises `BlockValue.damage` or clears block |
| `held_slot_report` | live | inv, demo | Holding slot index + item type readable |
| `buffs` | live | player, demo | Buff manager live |
| `quests_journal` | live | quest, demo | QuestJournal non-null |

---

## world: world / mesh / content probes

| Case | Status | Tags | Assert |
|---|---|---|---|
| `chunk_under_player` | live | world, demo | Chunk/block readable at feet |
| `block_sample_ring` | live | world, demo, bench | Solid blocks in 5×5 sample |
| `entities_in_radius` | live | entity, demo | ≥1 entity in 64 m (self) |
| `world_time` | live | world, demo | `worldTime` readable |
| `world_time_advances` | live | world, demo, bench | `worldTime` increases while waiting |
| `biome_id` | live | world | Biome id ≥ 0 at player |
| `poi_textures_non_terrain` | live | world, poi | Tele to POI; block id ≥ 256 |
| `weather_array` | live | world, weather | S2C weather residual |
| `deco_trees` | live | world, deco | AssignIds match |
| `water_plane` | live | world, water | WaterSet + mass/isWater/block sample (not package-only) |

---

## ui: window tour (demo-style)

| Case | Status | Tags | Assert |
|---|---|---|---|
| `craft_open` | live | ui, craft, demo | Crafting window opens |
| `inventory_open` | live | ui, inv, demo | Backpack/inventory opens |
| `character_open` | live | ui, demo | Character screen opens |
| `map_open` | live | ui, demo | Map opens |
| `ui_close_all` | live | ui, demo | Close modals requested |
| `quest_log_open` | live | ui, quest, demo | Quest UI Open (soft name list) |
| `skills_open` | live | ui, progression, demo | Skills/progression Open (soft) |
| `creative_menu` | live | ui, creative, demo | Creative window_group Open (soft) |

---

## combat

| Case | Status | Tags | Notes |
|---|---|---|---|
| `alive_flags_self` | live | combat, player | Alive, not dead, hp &gt; 0 |
| `held_item_type` | live | combat, inv | Holding type ≥ 0 |
| `zombie_or_npc_nearby` | live | combat, entity, admin | barrier + telnet spawn; wait EntityAlive |
| `zombie_target_has_health` | live | combat, entity, demo | nearest NPC `Health > 0` + class name |
| `melee_damage_out` | live | combat, c2s, demo, melee | Setup near target; stock `UseHoldingItem`/`Attack`; HP drops |
| `ranged_shot` | live | combat, c2s, demo, ranged | Pipe pistol + mag Meta; fire; Meta drop and/or target HP |
| `zombie_death_loot` | live | combat, loot | Kill → ECD loot bag (RNG); controlled drop is `loot_bag_pickup` |
| `explosion_client` | live | combat, c2s | Soft block seed + melee damage/break (no admin Air clear) |
| `sleeper_wake` | live | combat, sleeper | TriggerSleeperPose then ConditionalTriggerSleeperWakeUp |
| `blood_moon_music` | live | combat, bm | Host settime night (observed hour) then restore day |

---

## finale: death / respawn (runs last in demo)

Suite id `finale`. Death and respawn close the attract path so earlier
suites stay healthy.

| Case | Status | Tags | Notes |
|---|---|---|---|
| `player_death_screen` | live | combat, player, admin | Admin kill player |
| `player_respawn` | live | combat, player | After death |

---

## economy: craft / TE / trade

| Case | Status | Tags | Notes |
|---|---|---|---|
| `craft_window_recipes` | live | economy, craft, ui, demo | Craft UI |
| `bag_add_item` | live | economy, inv, demo | `bag.AddItem(resourceWood…)` increases occupied slots |
| `item_drop_entity` | live | economy, inv, c2s, demo | `ItemDropServer` → nearby `EntityItem` |
| `eat_food_consume` | live | economy, inv, demo, consume | Give canned food, eat; count drops or Food rises |
| `loot_bag_pickup` | live | economy, loot, c2s, demo | ItemDrop + `Entity.Collect` → drop gone / bag up |
| `land_claim_place` | live | economy, claim, demo, c2s | Place `keystoneBlock`; solid or claim table |
| `zombie_removed_after_kill` | live | economy, loot, demo, admin | barrier kill fixture zombie; EntityAlive other drops |
| `craft_consume_output` | live | economy, craft, demo | Give wood; queue wooden club; wood↓ or club↑ |
| `workstation_burn` | live | economy, te, craft, demo | Place campfire; solid (+ TE if ready) |
| `chest_open_loot` | live | economy, te, admin | TE lock + loot |
| `trader_stock_ui` | live | economy, trader | EntityTrader in range (+ TraderData) |
| `trader_buy` | live | economy, trader | Coins spent + stock/goods change |

---

## quest

| Case | Status | Tags | Notes |
|---|---|---|---|
| `journal_exists` | live | quest, demo | Journal object |
| `journal_iterate` | live | quest, demo | Iterate quest list without throw (count may be 0) |
| `starter_quest_active` | live | quest | Seeded starter in journal (count &gt; 0) |
| `quest_goto_progress` | live | quest | Phase bump and/or move ≥1.5 m |
| `quest_kill_progress` | live | quest, combat | Phase/objective/state change after kill nudge |
| `quest_turn_in` | live | quest, trader | CompleteQuest → Completed state |
| `quest_nav_marker` | live | quest, ui | NavObjectManager register |

(`shared_quest` is documented under `mp`; it needs a peer fixture and the
catalog registers it there.)

---

## vehicle

| Case | Status | Notes |
|---|---|---|
| `vehicle_spawn_visible` | live | Host/client EntityVehicle in range |
| `vehicle_enter_exit` | live | HasDriver / attach then exit |
| `vehicle_drive` | live | Seated horiz travel ≥0.4 m |
| `vehicle_fuel_burn` | live | Fuel/stall |
| `vehicle_terrain_clamp` | live | Gravity clamp |

---

## power

| Case | Status | Notes |
|---|---|---|
| `place_generator` | live | PowerGrid node |
| `wire_set_parent` | live | WireActions op 0 |
| `wire_remove_parent` | live | WireActions op 1 |
| `turret_place` | live | autoTurret |
| `generator_fuel` | live | Fuel/SoC depth |
| `trigger_actuation` | live | Triggers/timers |

---

## persist_setup: host multi-phase setup (orchestrator phase A)

Suite id `persist_setup`. Prepares world/player state, then emits the
`persist_setup_done` barrier so the host can save and rejoin.

| Case | Status | Notes |
|---|---|---|
| `persist_setup_dig` | live | Seed solid then dig pad cell to air |
| `persist_setup_inv` | live | Give scrap iron into bag |
| `persist_setup_pos` | live | Barrier teleport to fixed pad (server-authoritative) |
| `persist_setup_te` | live | Place storage chest TE near pad |
| `persist_setup_blockmeta` | live | Seed block and apply damage meta |
| `persist_setup_done` | live | Emit checkpoint barrier for host |

## persist: host multi-phase verify (orchestrator phase B)

Suite id `persist`. Asserts setup state after save + client rejoin.

| Case | Status | Notes |
|---|---|---|
| `dig_survives_rejoin` | live | dig → save → rejoin → air |
| `inv_survives_rejoin` | live | item → rejoin bag |
| `pos_survives_rejoin` | live | telnet/pad → save → rejoin near pad (default spawn must fail) |
| `te_survives_rejoin` | live | chest after restart |
| `blockmeta_survives` | live | damage block after restart |

These need orchestrator phases (restart pair mid-suite), not only in-mod steps.

---

## mp: multiplayer

| Case | Status | Notes |
|---|---|---|
| `second_client_visible` | live | loadgen peer EntityPlayer |
| `chat_roundtrip` | live | say/chat token echo |
| `setblock_interest` | live | dig under multi-peer |
| `lock_contention` | live | TE lock while peer present |
| `shared_quest` | live | quest seed under multi-peer |
| `bots_plus_playtest` | live | loadgen N bots + client alive |

Make target: `make playtest-mp` (stock + loadgen).

---

## soak

| Case | Status | Notes |
|---|---|---|
| `soak_walk_look_cycle` | live | ~9s walk+look loop |
| `soak_still_alive` | live | Still spawned after soak |
| `soak_15min_host` | live | ≥15 min wall + periodic dig (suite soak_long) |
| `soak_apm_budget` | live | zdtd APM dump (suite apm, SERVER=zdtd) |

Make targets: `make playtest-soak-long`, `make playtest-apm`, `make playtest-residual`.

---

## bot: BotManager visibility / parity

Cases that observe the dedicated server's `BotMod` bots (BotManager auto-spawn
plus orchestrator telnet spawn requests) from the playtest client's point of
view. Requires `BotMod` in the dedicated server's `Mods/`.

| Case | Status | Tags | Assert |
|---|---|---|---|
| `bot_spawn_visible` | live | bot, demo | BotManager auto-spawned TargetBotCount + explicit spawn near player; client sees a bot |
| `bot_moves` | live | bot, locomotion | Nearest living bot within 120 m tracked across ticks (position delta) |
| `bot_physics_parity` | live | bot, physics | Nearest living bot within 120 m has a sane physics/position state |
| `bot_player_near` | live | bot, demo | Telnet-requested bot spawns near this player; client observes it |

---

## Demo sequence (fixed order)

When `SUITE=demo`, the client runs this attract-mode path:

```text
smoke → core → world (incl. water/weather/poi/deco) → ui (+ creative)
combat → economy (chest/trader/craft/campfire) → quest seed/progress
vehicle → power → finale (death + respawn last)
```

Residual infrastructure (multi-peer, multi-phase rejoin, ≥15m soak, zdtd APM)
lives in dedicated suites: `mp`, `persist`, `soak_long`, `apm` (not in demo).

## Benchmark

`SUITE=benchmark` with `PLAYTEST_LAPS=N`:

- Repeats smoke+core+world **N** times (suite labels `benchmark@1` …)
- Each case reports `ms` in JSON result lines
- Host report can rank slowest cases (orchestrator already stores `ms`)

## Counts (from `Catalog.cs` Live / Defer)

`Defer(` registrations: **0**. Every built-in case is `Live(...)`.

| Suite | Live | Deferred |
|---|---:|---:|
| smoke | 5 | 0 |
| core | 18 | 0 |
| world | 10 | 0 |
| ui | 8 | 0 |
| combat | 10 | 0 |
| economy | 12 | 0 |
| quest | 7 | 0 |
| vehicle | 5 | 0 |
| power | 6 | 0 |
| finale | 2 | 0 |
| persist_setup | 6 | 0 |
| persist | 5 | 0 |
| mp | 6 | 0 |
| soak | 2 | 0 |
| soak_long | 1 | 0 |
| apm | 1 | 0 |
| bot | 4 | 0 |
| **catalog total** | **108** | **0** |

Demo scoreboard on stock dedicated is the acceptance gate for gameplay surface
(smoke…finale attract path; residual suites separate). Residual promotion gate:
`make playtest-residual` (persist+mp+apm+soak_long, fail=0).

## Adding a scenario

1. Add a row here (status live or deferred).
2. Implement in `Catalog.cs` via `Live(...)` or `Defer(...)`.
3. Keep names honest; setup tele ≠ locomotion proof.
4. Never invent S2C to force PASS (AGENTS).
