# TODO — 7dtd-playtest

Agent ownership: mark `[-] in progress — <agent>, YYYY-MM-DD` before editing;
`[x]` on completion; restore `[ ]` with a reason if released or blocked.

## Backlog

- [x] Catalog↔SCENARIOS surface sync: every built-in `Live(...)` id documented as `live` in `SCENARIOS.md` (add `day_clock`, `look_pitch`, `held_slot_report`, `persist_setup_*`); fix stale deferred counts (`Defer` count is 0); gate with structural test so Live↔doc drift fails `make test` — done, 2026-08-10
- [x] Public `CaseDef` factories for external `IScenarioProvider` mods (`Live`/`Defer` are private static on `Catalog`; external providers currently construct `CaseDef` by hand) — done, 2026-08-10 (`CaseDef.Live` / `CaseDef.Defer` public; Catalog delegates)
- [x] Residual alias clarity: `ExpandSuites("residual")` = `mp,soak` only; `make playtest-residual` is a separate multi-target host gate — documented in README/SCENARIOS/Makefile; synonym `residual_light` — done, 2026-08-10
- [x] README Phase A suite table replaced with catalog summary + SCENARIOS pointer — done, 2026-08-10
- [x] Upstream provider gaps: dual suite env (`ZDTD_PLAYTEST_SUITE`), public `Helpers`/`Report`, fresh-save/barrier/mp/long-timeout/log-contract docs + structural tests — done, 2026-08-10
