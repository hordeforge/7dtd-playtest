# TODO — 7dtd-playtest

Agent ownership: mark `[-] in progress — <agent>, YYYY-MM-DD` before editing;
`[x]` on completion; restore `[ ]` with a reason if released or blocked.

## Backlog

- [x] Catalog↔SCENARIOS surface sync: every built-in `Live(...)` id documented as `live` in `SCENARIOS.md` (add `day_clock`, `look_pitch`, `held_slot_report`, `persist_setup_*`); fix stale deferred counts (`Defer` count is 0); gate with structural test so Live↔doc drift fails `make test` — done, 2026-08-10
- [x] Public `CaseDef` factories for external `IScenarioProvider` mods (`Live`/`Defer` are private static on `Catalog`; external providers currently construct `CaseDef` by hand) — done, 2026-08-10 (`CaseDef.Live` / `CaseDef.Defer` public; Catalog delegates)
- [x] Residual alias clarity: `ExpandSuites("residual")` = `mp,soak` only; `make playtest-residual` is a separate multi-target host gate — documented in README/SCENARIOS/Makefile; synonym `residual_light` — done, 2026-08-10
- [x] README Phase A suite table replaced with catalog summary + SCENARIOS pointer — done, 2026-08-10
- [x] Upstream provider gaps: dual suite env (`ZDTD_PLAYTEST_SUITE`), public `Helpers`/`Report`, fresh-save/barrier/mp/long-timeout/log-contract docs + structural tests — done, 2026-08-10

## Playtest-compare integrity (2026-08-18)

- [x] Stale-evidence guard: `playtest-compare` now wipes the suite's stock/
  zdtd side dirs before each side runs, so a side that fails to start (port
  collision, missing binary, refused lock) leaves NO report; playtest_compare
  exits 2 naming the missing side instead of re-diffing previous-session logs.
  `--require-fresh-minutes` (make passes 180) refuses reports older than the
  bound (exit 3); a failed diff removes the stale md/json so a phantom
  "compared" result cannot survive. Reports carry ran_epoch + a `ran (UTC)`
  axis in the diff. Root trigger: 2026-08-18 re-run where docker containers
  (`mk-katamaran-2node` uvicorn agent) had taken host ports 8081/8082, both
  sides refused to start, and the harness re-diffed 2026-08-12 logs byte-near
  (only the template's new wall-time row appeared) and overwrote the committed
  combat evidence. Fixed + 3 new offline gates (missing-side, stale-guard,
  ran-at), all green.
- [ ] Host-level: ports 8081/8082 are now owned by docker-proxy on this
  machine (unrelated uvicorn agent containers). The harness already refuses
  loudly with "pick a different --port/--admin-port"; for live compares pass
  ADMIN_PORT=8084 (verified free). If the containers are intentional, consider
  permanently moving the harness's default ADMIN_PORT off 8081/8082.
