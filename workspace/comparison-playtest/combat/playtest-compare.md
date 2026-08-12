# Stock-vs-zdtd playtest comparison

| axis | stock | zdtd |
|---|---|---|
| cases PASS | 9 | 9 |
| cases FAIL | 1 | 1 |
| cases SKIP | 0 | 0 |

## Per-case

| case | stock | zdtd |
|---|---|---|
| `combat/alive_flags_self` | PASS | PASS |
| `combat/blood_moon_music` | PASS | PASS |
| `combat/explosion_client` | PASS | PASS |
| `combat/held_item_type` | PASS | PASS |
| `combat/melee_damage_out` | PASS | PASS |
| `combat/ranged_shot` | PASS | PASS |
| `combat/sleeper_wake` | FAIL | PASS |
| `combat/zombie_death_loot` | PASS | FAIL |
| `combat/zombie_or_npc_nearby` | PASS | PASS |
| `combat/zombie_target_has_health` | PASS | PASS |

## Findings

- combat/sleeper_wake: status differs (FAIL vs PASS)
- combat/zombie_death_loot: status differs (PASS vs FAIL)

*Triage each finding: zdtd bug vs harness artifact vs known divergence. Known divergences are recorded in zdtd/docs/PROVENANCE.md (divergence register).*
