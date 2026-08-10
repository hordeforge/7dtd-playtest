# TODO — 7dtd-playtest

Agent ownership: mark `[-] in progress — <agent>, YYYY-MM-DD` before editing;
`[x]` on completion; restore `[ ]` with a reason if released or blocked.

## Backlog

- [x] Catalog↔SCENARIOS surface sync: every built-in `Live(...)` id documented as `live` in `SCENARIOS.md` (add `day_clock`, `look_pitch`, `held_slot_report`, `persist_setup_*`); fix stale deferred counts (`Defer` count is 0); gate with structural test so Live↔doc drift fails `make test` — done, 2026-08-10
- [ ] Public `CaseDef` factories for external `IScenarioProvider` mods (`Live`/`Defer` are private static on `Catalog`; external providers currently construct `CaseDef` by hand)
- [ ] Residual alias clarity: `ExpandSuites("residual")` expands to `mp,soak` only, while `make playtest-residual` runs persist+mp+apm+soak_long — document or align
- [ ] README Phase A suite table is stale vs full catalog (smoke/core rows omit many live cases)
