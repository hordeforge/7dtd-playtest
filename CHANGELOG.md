# Changelog

All notable changes to the `7dtd-playtest` client mod are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Release model (inferred practice, now pinned by `make test`):

- One version per artifact. The client mod version lives in `ModInfo.xml`
  and `ModApi.Version`; git tags are annotated `vX.Y.Z` refs pointing at the
  released commit. `scripts/test_version_surface.py` fails the offline gates
  if these disagree or if a tagged version has no entry below.
- The host orchestrator package (`pyproject.toml`) is an unpublished,
  independently versioned helper; consumers interact with it through the
  stable log contract and exit codes documented in the README, not through
  its package version.
- Consumer-facing contracts (stable `[7dtd-playtest]` log tokens, JSON event
  schema `"v":1`, suite/env surface, exclusivity lock payload format, and the
  C# provider API `CaseDef`/`CaseCtx`/`IScenarioProvider`/`Helpers`/`Report`)
  may only change in a release whose entry below says so explicitly.

## [Unreleased]

No breaking changes: verified against the `v0.7.1` tag, the public C#
provider surface, log contract tokens, lock payload keys
(`running`/`session`/`acquired`/`heartbeat`), and suite env names are
unchanged.

### Added

- `--loadgen-server-cvar-tolerance` for bounded comparison of time-varying
  server and peer CVar samples; the exact default remains `0.0001`.
- Generic loadgen replicated-state orchestration: exact CVar/buff filters and
  expectations consume structured joined/state events, teleport the exact
  joined entity, and fail the harness on missing or contradictory peer state.
- Relational loadgen assertions for positive and equal CVar values, plus an
  opt-in server-authority oracle that compares `cvar get` with the exact
  joined peer's decoded CVar state.
- Barrier-parameter sanitization: `chat_echo:<token>` and
  `spawn_vehicle:<class>` parameters lifted from client-log lines are
  validated as plain identifiers (`[A-Za-z0-9_]{1,64}`) before they reach a
  telnet console command, closing the log-to-console injection path where
  remote chat text could seed extra admin commands. Unsafe parameters are
  dropped with a warning.
- Control-character scrubbing on log-derived text echoed to orchestrator
  stdout (progress crumbs, failure dumps): remote chat can no longer inject
  terminal escape sequences into the run log.
- `PLAYTEST_TELNET_PASSWORD` env and `--telnet-password`: one value feeds
  both the generated server config and the orchestrator telnet client.

### Changed

- Loadgen launch now rebuilds when its C# project or source is newer than the
  existing Release executable, preventing a freshly updated checkout from
  silently running an incompatible stale observer binary.

- `Helpers.LookAt` now follows the stock player rotation convention for
  vertical aim. Targets below the player produce negative X pitch, so overhead
  ground and block scenes no longer turn the camera into the sky.
- `PLAYTEST_TELNET_PASSWORD` / `--telnet-password` unset no longer defaults
  to the static `retest`. Servers the orchestrator starts get an ephemeral
  per-run secret (written to the generated server config, chmod 0600, never
  logged); `--no-server` attach falls back to `retest`. Operator-supplied
  values win verbatim.
- Deterministic simulation of the exclusivity lock: `make dst`
  (`scripts/test_dst.py`, `DST.md`). Crash, torn-write, corruption, and
  clock-skew faults replay from recorded seeds.
- Offline local-init order gate for the orchestrator
  (`scripts/test_no_unbound_locals.py`).
- Stock-peer orchestration surface gate (`scripts/test_stock_peer_client.py`)
  now runs as part of `make test`; it existed but nothing invoked it, so the
  peer-rename commit silently broke it.
- Seeded grammar fuzzers for the log-derived report surface in
  `scripts/test_report_surface.py`: hostile client-log blobs against
  `parse_client_log` (shape, determinism, doubling invariants) and hostile
  strings through `write_junit` (well-formedness, round-trip), both offline
  and deterministic under `make test`.
- README provider section now ships a complete minimal `IScenarioProvider`
  example, a `CaseCtx` member reference, and the full list of barrier names
  the stock orchestrator answers.
- The orchestrator logs one effective `config:` line at startup (options
  only; the telnet password appears as set/unset, never its value), so a
  misread environment is visible without rerunning with `--help`.
- README now documents every host orchestrator environment variable
  (`PLAYTEST_SERVER`, `ZDTD`, `RE_DEDICATED_USERDATA`, `LOGDIR`,
  `PLAYTEST_TIMEOUT_SEC`, peer-client defaults) with defaults and the
  flag-overrides-env precedence.
- Quarantine-before-delete for the orchestrator's destructive pre-run paths:
  `--fresh-save` stock saves, zdtd world state (`players.zsv`,
  `containers.zct`, `blockmeta.zbm`, chunk overlays), and previous client
  logs move under `<logdir>/quarantine/` (newest 5 entries kept) instead of
  being hard-deleted or truncated. A mispointed `--userdata`/`--game-name`/
  `--world` now costs a copy-back; an unwritable quarantine keeps data in
  place and warns about stale reuse. README gains a "State, backups, and
  recovery" section (state inventory, RPO/RTO stance, restore steps).
- README provider docs now cover `CaseCtx.CaseStartUnscaled` and `IntC`
  (previously public but undocumented), a "Provider error behavior"
  section (callback exceptions fail only their case; provider/suite
  exceptions surface as log lines and FAIL rows), and `Report.Info` for
  diagnostics under the stable prefix.

### Changed

- `CaseDef.Live` now fails fast at queue build: a case with no `act`,
  `wait`, or `assert` callback (it would record a green pass while
  running nothing) throws `ArgumentException`, and `timeout <= 0` throws
  `ArgumentOutOfRangeException`, both naming the case. Provider
  `AppendSuite` calls are wrapped by discovery, so a rejected case
  surfaces as a `[7dtd-playtest] scenario provider …` log line plus the
  existing zero-cases FAIL row instead of a lying pass; built-in catalog
  cases all supply callbacks and positive timeouts, so no call site
  changes.

- Requesting a suite that produces zero cases (typo'd id, uninstalled
  provider) is now a recorded failure instead of a silent green run: the
  runner logs `unknown or empty suite: <id>` once, records a
  `FAIL <id>/(unknown)` row, and an entirely empty queue finishes at arm
  time with `DONE exit_hint=1`. Hosts exit 1 and see the offending suite by
  name; previously such runs waited out the join and exited 0.
- `CaseDef.Live` tags parameter is optional (informational only); act is
  optional too, so pure-wait observation cases no longer pass `null`
  positionally. Existing call sites are unaffected.
- Removed the dead internal `Suites` shim (no callers, invisible outside
  the assembly).
- Host Python runs through `uv` only (requires Python >= 3.11).
- Host Python invocations now pass `--locked` to uv (`make test`, repeat
  wrapper, README): a pyproject/uv.lock mismatch fails the run instead of
  silently re-resolving away from the committed lock.
- Mod builds are byte-reproducible across checkouts: the csproj pins
  `Deterministic`, maps the checkout path out of the dll/pdb (`PathMap`),
  disables the SDK's implicit git query that baked the absolute source
  path and remote URL into the pdb, and pins the net48 reference
  assemblies package explicitly. Verified: two builds from different
  checkout directories hash identically.
- `global.json` pins the dotnet SDK to the 8.0 feature band
  (`rollForward: latestFeature`), so compiler version no longer depends on
  whatever the host has installed.
- Mod build strictness raised to what the tree already passes: nullable
  annotation checking (`Nullable=annotations`), warning level 5, Roslyn
  NET analyzers enabled (they defaulted off for net48), and
  `TreatWarningsAsErrors`, so a new compiler or analyzer diagnostic fails
  `make build` instead of scrolling by. Full `Nullable=enable` stays off
  until the remaining CS86xx findings are worked off.
- CI runner image pinned (`ubuntu-24.04`) instead of the floating
  `ubuntu-latest`.
- Entity probes, fixture equips, and barrier bookkeeping share one
  implementation; repeated parameterized barriers
  (`barrier spawn_vehicle:<class>`) each reach the host as separate fixture
  requests.

### Fixed

- Server CVar oracle parsing accepts stock's live
  `name: True. Value: <number>` result format.
- The server CVar oracle now keeps its telnet session open until the stock
  command returns the requested value, instead of closing after the earlier
  command echo on a busy dedicated server.
- Server CVar oracle parsing now requires the value delimiter immediately
  after the requested name, so a telnet command echo cannot be mistaken for
  the player entity ID supplied through `-p`.

- `loot_bag_pickup` no longer parks the dropped item's entity id in the
  float context slot: entity ids above 2^24 lost precision through the
  `float`/`(int)` round-trip and corrupted the gone-check verdict. The id
  now lives in a new int slot (`CaseCtx.IntC`; additive, provider-visible).
- The `world_time` case dropped its dead `worldTime → float` parking; the
  full ulong value was already reported via Detail.

- `SUITE=economy`, `vehicle`, `finale`, and `bot` standalone runs now arm
  host telnet fixtures. The fixture gate (`suite_wants_zombie_fixture`) was a
  substring heuristic whose key list predated the current barrier set, so
  these suites' live cases fired barrier lines (`kill_fixture_zombie`,
  `spawn_trader`, `spawn_vehicle`, `kill_player`, `bot_spawn`,
  `bot_player_near`) that no orchestrator handler ever serviced:
  `bot_player_near` had no client-side fallback and timed out on every run.
  The gate now matches whole suite tokens against the full set of
  fixture-bearing suites (`FIXTURE_SUITE_IDS`), is named for what it does
  (`suite_wants_host_fixtures`), and an offline gate cross-checks the catalog
  so a new barrier-emitting suite cannot be missed again. Selections used by
  the make targets (demo, gate, benchmark, mp, persist, soak_long, apm,
  smoke/core lists) arm exactly as before.
- `parse_client_log` no longer aborts the run on a crafted log line: an
  infinite summary count or exit hint (`1e999`, bare `Infinity`) raised
  OverflowError past the bad-event handler, and non-string JSON status or
  detail values crashed `.upper()` and downstream string consumers. Bad
  events are skipped, garbage-typed fields coerced.
- `write_junit` drops characters illegal in XML 1.0 (NUL and other control
  bytes survive UTF-8 replace-decoding but cannot be escaped), so one binary
  log line can no longer make the whole JUnit report unparseable.
- SIGTERM/SIGHUP now shut down cleanly: detached client/server are stopped
  and the exclusivity lock is released instead of left stale-but-live with
  orphaned runtimes.
- Passive stock peer launch is spaced one second behind the primary client
  so the stock server's same-IP connection limiter (500 ms) no longer
  rejects the second localhost client before authentication.
- Sprint stamina drain guard added to the motor cases; malformed log events
  are tolerated instead of aborting the run.
- Telnet AI cleanup no longer falls back to `killall` (`clear_ai`,
  `kill_non_player_ai`): stock `killall` also kills the player entity (the
  exact failure the helpers document against) and left the demo on a death
  screen when `listents` output did not parse; an unmatched cleanup now
  fails only its own case instead of cascading.
- Provider rejoin with `SERVER=zdtd` now restarts the zdtd server between
  setup save and verify instead of leaving the verify client facing a dead
  server.
- `PLAYTEST_TIMEOUT_SEC` with a non-numeric value no longer crashes the
  orchestrator with a bare `float()` traceback at startup: it is a harness
  error (exit 2) naming the variable. Zero/negative/inf values and out-of-range
  `--port` / `--admin-port` are rejected the same way instead of producing an
  instant timeout or a late server-bind failure.
- `PLAYTEST_LOCK_STALE_SEC` / `PLAYTEST_LOCK_HEARTBEAT_SEC` set to an
  unparseable or non-finite value (`nan`, `inf`) now warn on stderr instead of
  silently falling back to the defaults (the fallback itself is unchanged).
  Previously `nan` collapsed through the 1s clamp into an instant-stale window
  and `inf` made a lock never stale (or froze the heartbeat wait).
- The generated stock serverconfig (`TelnetPassword` inside) is written with
  user-only permissions (0600) instead of inheriting a world-readable umask.

## [0.7.1] - 2026-08-22

Stock-client scenario suite snapshot. Gameplay surface (stock motor, stock
attack, real C2S; no tele-fakes):

- Locomotion, jump, stamina; entity/block melee; ranged (pipe pistol Meta).
- `ItemDropServer`, loot collect, keystone place, eat, dig/place.
- Creative UI; craft wooden club (queue plus output); campfire TE place.
- Runner recovers from player death mid-suite instead of hanging.
- Residual live coverage: multi-phase rejoin, multi-peer loadgen, 15 minute
  soak, zdtd APM dump attach.
- Demo suite against stock dedicated: 83 pass / 0 fail on a fresh save;
  residual suites separately fail=0.

[Unreleased]: https://github.com/hordeforge/7dtd-playtest/compare/v0.7.1...HEAD
[0.7.1]: https://github.com/hordeforge/7dtd-playtest/releases/tag/v0.7.1
