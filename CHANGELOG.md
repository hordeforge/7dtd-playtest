# Changelog

All notable changes to the `7dtd-playtest` client mod are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Release model (inferred practice, now pinned by `make test`):

- One version per artifact. The client mod version lives in `ModInfo.xml`
  and `ModIdentity.Version`; git tags are annotated `vX.Y.Z` refs pointing
  at the released commit (the stray lightweight `v0.7.2` predates this; see
  its entry below). `scripts/test_version_surface.py` fails the offline
  gates if these disagree, if the shipped manifest went stale, or if the
  manifest version or any visible `vX.Y.Z` tag has no entry below.
- The host orchestrator package (`pyproject.toml`) is an unpublished,
  independently versioned helper; consumers interact with it through the
  stable log contract and exit codes documented in the README, not through
  its package version.
- Consumer-facing contracts (stable `[7dtd-playtest]` log tokens, JSON event
  schema `"v":1`, suite/env surface, exclusivity lock payload format, and the
  C# provider API `CaseDef`/`CaseCtx`/`IScenarioProvider`/`Helpers`/`Report`)
  may only change in a release whose entry below says so explicitly.

## [Unreleased]

## [0.9.0] - 2026-09-01

The orchestrator stops being a second sandbox. Provisioning splits into the two
axes it was conflating, and declarative suites become the input that drives a
run rather than a mirror of the C# catalog. See
[ADR 0001](https://github.com/hordeforge/.github/blob/main/docs/adr/0001-test-tiers-and-declarative-suites.md).

**Breaking:** `--target` / `PLAYTEST_TARGET` are gone. `--target sandbox` and
`--target stock` are both `--provision managed` (always a Safehouse instance
now); `--target attach` is `--no-server` or `--provision attach`; `--target
live` is `--no-server --readonly`; `--target zdtd` is `--server zdtd`. The
Makefile's `TARGET=` became `PROVISION=` / `READONLY=`. `--server stock|zdtd`
and `--no-server` are unchanged, so external callers passing those keep
working.

**Breaking:** `--port` and `--admin-port` are refused on a managed run.
Safehouse allocates the instance's 5-port block; an operator port would send
the harness at a port the server never binds. Use `--no-server` to attach to a
server you started yourself.

**Requires** `7dtd-sandbox` >= 0.1.0 beside this repo (or `--sandbox-root` /
`PLAYTEST_SANDBOX_ROOT`) for any managed stock run.

### Added

- Two provisioning axes replace the five-value `--target`: `--provision`
  (`managed` | `attach`, env `PLAYTEST_PROVISION`), `--server`
  (`stock` | `zdtd`, env `PLAYTEST_BACKEND`), and an attach-only
  `--readonly` for a production host that must never be written to. A
  managed stock run is always a Safehouse instance. See
  [ADR 0001](https://github.com/hordeforge/.github/blob/main/docs/adr/0001-test-tiers-and-declarative-suites.md).
- Declarative suites are the input, not a mirror. A suite document declares
  `provision`, `backend`, `readonly`, `fresh`, the `mods` to stage, and a
  flat `server` map of serverconfig properties handed to `sb render-config`,
  so an A/B of one setting is two suite files differing by one line. The
  orchestrator passes the declared case refs to the client as
  `PLAYTEST_CASE_REFS`, and `Runner` runs only cases whose
  `catalog.SUITE.CASE` ref appears there.
- `--sandbox-root` / `PLAYTEST_SANDBOX_ROOT`: the Safehouse checkout that owns
  the instances, so a caller with a non-standard layout (and the offline gates)
  can point at one instead of assuming a sibling directory.
- The run report records the serverconfig properties actually applied (minus
  the per-run telnet secret), so a report names the world it measured.
- `scripts/test_suite_refs.py`: offline gate pinning that every declared ref
  resolves to a real Catalog case, that a declared suite declares every case
  its Add method builds, that the ref format matches `Runner.CaseRef`, and
  that every catalog suite is either declared or listed as not yet declared.
- Mod repos run their own cases with
  `playtest_run.py --suite-file <mod>/playtest/suites/<id>.json`; no wrapper
  script per repo. `load_external_suite` refuses a suite id that shadows a
  built-in stock-fidelity suite.

### Changed

- Bring-up, isolation, ports, config rendering, mod staging and teardown
  moved to Safehouse (`sb up` / `stage` / `render-config` / `wipe` / `stop`).
  Removed from the orchestrator: `write_stock_config`,
  `start_stock_dedicated`, `_rewrite_platform_cfg` (which rewrote
  `platform.cfg` inside the user's Steam install), `_atomic_write_bytes`,
  `_literal_replacement`, `fresh_save` and `wait_stock_dedicated_ready`. The
  orchestrator no longer reads
  `7dtd-loadgen/scripts/serverconfig_loadgen.xml`.
- `GAME_PROC_PATTERNS` is client-only. A managed dedicated is stopped with
  `sb stop <instance>`, which matches that instance's own `SB_INSTANCE`; the
  old blanket `pkill 7DaysToDieServer.x86_64` reached every other sandbox
  instance on the machine.
- `--port` and `--admin-port` are refused on a managed run: Safehouse
  allocates the instance's 5-port block, so an operator port would send the
  harness at a port the server never binds.
- `fresh` is checked rather than asserted: a managed run must be fresh, an
  attach run must declare `fresh: false` because it does not own the save it
  joins, and `readonly` requires attach. A live suite is now representable.
- Two offline gates no longer read the host process table
  (`live_probe=lambda: False`); they failed whenever any dedicated happened
  to be running on the machine.

- `parachute` suite: end-to-end check of the 7dtd-wasm bridge running the
  unmodified zdtd parachute module. The client wears the glider item
  (equipment slot, sense v4 wearing_glider) and lifts itself 60 blocks
  (client-side SetPosition, since the stock server's teleportplayer does
  not move remote-player entities), then asserts the mod's deploy announce
  through the stock chat broadcast while falling. Requires the
  `1_HordeForge_WasmHost` bridge + parachute wasm module on the server.
  See SCENARIOS.md.

### Fixed

- `CaseDef.WalkEntity` emits one live `render-probe` with the detached camera
  pose and line-of-sight hit, material/shader/pass state, and baked skinned-mesh
  bounds. A passing case whose clip contains no recognizable creature now says
  whether the camera is blocked, the pass is rejected, or deformation produced
  different geometry than the serialized renderer AABB.
- The walk probe now makes collision a real acceptance condition: it reports
  the root `Physics` capsule, active solid colliders and a physics ray into the
  spawned entity, and the case fails unless the ray hits. `--trace-entity`
  repeats the full pose/render/collision sample once per second; the default
  still emits one sample.
- `Helpers.TryGetRenderedBounds` bakes live skinned meshes before combining
  world bounds. Staged grounding and camera framing can now expose a position
  curve that moved the posed body outside its serialized AABB.
- `CaseDef.WalkEntity` grounds against `World.GetHeight(x,z) + 1`, the loaded
  top voxel face, and offsets the root by the authored `Physics` capsule
  bottom. The previous `World.GetHeightAt` generator heightmap measured Y 60.05
  under a road whose visible top was Y 61, so the harness forced a healthy
  creature almost one full block into the floor every tick. The probe now logs
  `voxelTop`, `visualBottom`, `groundClearance` and `groundReady`, and the
  case fails on clipping or floating.
- Uneven-ground placement now uses `Physics.RaycastAll` on the game's
  traversable-surface mask and ignores the entity's own colliders.
  `World.GetHeight + 1` is a top-voxel boundary, so a slope or partial road
  block made an invisible one-metre bump even after the buried-ground fix.
  The trace retains that value as `voxelTop` beside `surfaceRay` and
  `voxelMinusSurface`, with `groundClearance` measured from the ray surface.
  A fresh d3d11 run measured `voxelTop=62` while `surfaceRay=61`, kept the
  posed bottom 0.032 m above collision, passed both readiness probes, and was
  visually signed off without the previous excessive bump rise. A missing
  physics-surface hit now fails precise grounding instead of silently treating
  the fallback voxel ceiling as equivalent evidence.
- `CaseDef.WalkEntity` aims at the skinned renderer's actual center and chooses
  a nearby third-person camera lane only when its position is unoccupied and
  its ray reaches the creature. The previous fixed world -z offset could put
  terrain or a static car between camera and body while the look case passed.
- `Helpers.FrameStagedObject` gives external staged providers the same detached,
  bounds-centered, clear-line-of-sight camera, and `CaseDef.Staged` /
  `StagedClip` restore the player camera after their hold. A first-person hand
  overlay can no longer cover the staged subject while the case passes.
- `playtest_run.py` refuses a `PLAYTEST_SUITE` list that mixes a prefab-look
  suite (`*_look`) with a block-placement suite (`*_block_*`). Those are
  different pictures; hanging a prefab in front of the camera and placing a
  block on a voxel must be separate invocations.

### Changed

- README / AGENTS how-to: one suite id, `smoke,core` as the only
  undeclared combo, `--concern-suites` / `PLAYTEST_CONCERN_SUITES` in
  the env table, matrix as separate invocations, and a
  `CaseDef.RegisterStaged` sample on the public surface. `--suite` help
  names the 2+ list rule.
- One concern per playtest run is a gate, not a paragraph:
  `mixed_unrelated_suites` refuses an undeclared comma-list of suite ids.
  `--concern-suites` / `PLAYTEST_CONCERN_SUITES` is how consecutive steps of
  one feature declare themselves as one list. A child that is part of a built
  prefab is not a second suite. look-versus-block stays a hard incompatibility
  even when declared. Unrelated features run as separate invocations.
- `CaseDef.RegisterStaged` / `ClearStaged`: camera-staged instances are
  destroyed at the start of the next hold and when this hold ends, so a
  particle system, a mesh and a cube cannot occupy the same world point.
- `Microsoft.NETFramework.ReferenceAssemblies` is exact-pinned at `[1.0.3]`
  (a bare `1.0.3` is NuGet's minimum range `[1.0.3, )`). Restore uses a
  repo `nuget.config` that clears extra package sources so it cannot fall
  through to a user-level feed.
- Release tag verification runs on `ubuntu-24.04`, matching CI, instead of
  floating `ubuntu-latest`.

### Fixed

- Catalog melee aim (`block_damage_melee`, `explosion_client`) uses
  `Helpers.LookAt` so a block below the camera gets negative X pitch. The
  previous local `-Asin` looked at the sky; cases still passed because of
  the later SetBlockRpc damage fallback.

## [0.8.0] - 2026-08-26

### Added

- `Helpers.Blocks`: block-entity model access and placement support for
  suites that place a block and check its model - `BlockEntityDataAt`,
  `ActivateBlockEntityModel` (the chunk display pass can leave renderers
  disabled), `FindGroundedAir` (a support-checked spot ahead of the camera,
  so the server's stability pass does not drop the placement),
  `AimBlockPlacement` (fills the player HitInfo for the PlaceAsBlock action)
  and `CloseDebugConsole` (the console swallows input-driven actions while
  open). Generalized from the 7dtd-asset-pipeline SelfTestMod block
  acceptance suites.
- `CaseDef.StagedClip` and `Helpers.CaptureClipFrame`: a staged case that
  captures a sampled frame sequence of its hold from inside the game
  (`playtest-shots/clips/<id>/frame-XXXX.png`), with a single
  `clip complete <id> frames=N` completion line added to the stable log
  contract. See `docs/INGAME_VIDEO_CAPTURE.md`.
- `scripts/capture_video.sh`: waits for `clip complete`, polls for the last
  frame, and muxes the clip into an mp4 plus a contact sheet. Same runner
  contract and refuse-to-overlap guard as `capture_frames.sh`.
- `scripts/review_video.py` and `scripts/video_review.py`: prescreen a staged
  clip with a vision model through the deadeye gateway
  (`hordeforge/7dtd-vision-review`), with explicit `--allow-network` consent
  and the same advisory, never-accepting posture as the human-watch gate.
  See `docs/VIDEO_MODEL_FEEDBACK.md`.
- `playtest_run.py --attach-reviews DIR`: attaches review evidence paths to
  the report keyed by suite/case. Paths only: a review's verdict never
  reaches the report, so it can never change a case's result.
- `make playtest-review-video SUITE=<id> INTENT=<path>`: capture then review
  against one output directory.
- `Helpers.BeginClip` / `Helpers.EndClip` and `ClipRecorder`: on-demand
  in-game recording decoupled from staging, so any case (Live included) can
  record what the player actually does (walk a worn garment, fire a VFX,
  use an item). Same super-resolution in-game frames and `clip complete`
  marker as staged clips; a clip a failed case left active is abandoned at
  suite end (`clip abandoned`), never completed.
- `Helpers.TryEquipItem(player, name)`: the public give+equip-by-name route
  generated walk-cycle cases use.
- `Helpers.StartWalk` / `Helpers.StopWalk`: the public locomotion surface
  (stock autorun via LocomotionDrive, not teleport) generated walk-cycle
  recording cases use.
- The orchestrator writes `<logdir>/run-ended` when its poll loop ends -
  `done`, `timeout`, or `client_exit` on one line. This is the deterministic
  end of the run for consumers keyed on the staged marker (a screenshot loop
  exits on it instead of waiting out its own timeout).
- A new `lock_lost` run-ended reason: when the exclusivity heartbeat reports
  the lock file stopped naming our session (foreign takeover after a stale
  window, or the shared file was reset), the orchestrator aborts with exit 2
  instead of finishing the suite against a runtime it may no longer own.
  Previously the detection existed in `HeartbeatLoop.lost_claim` but no
  consumer read it, so two agents could drive one machine concurrently.
- `playtest_lock.wait_until_can_start` and CLI `playtest_lock.py wait`:
  poll `can_start` (missing heartbeat is stale) so consumers do not parse
  `running=` / `heartbeat=` themselves. Matrix runners call this instead
  of a local lock clone.
- `MiningSpec` / `MiningProbe` / `MiningResult`: public real-mining driver for
  external providers. Seeds a named block, equips a named tool, presses
  exactly one `UseHoldingItem(0, false)` per attempt, and requires both
  authoritative block damage and a named bag+toolbelt award. Stock case
  `mining_harvest` (iron ore / iron pickaxe / scrap iron).
  `PulsePrimaryAttack` + `SetBlockRpc` damage remains the weaker combat
  fixture and is not a harvest proof. Guarded by
  `scripts/test_mining_probe_surface.py`.
- `Report.Staged(name, detail)` and the stable `scene staged <name>` log line
  (JSON `"t":"staged"`), announcing that a scene is on screen *now* so an
  external screenshot loop can photograph it. A case's `Detail` is flushed with
  its result, after the hold, so a loop keyed on the result photographs
  whatever came next; providers had each worked around that with their own
  `Report.Info` wording, so every screenshot loop grepped a different sentence.
- `Helpers.RigPoseReport(entity)`: the wearer's rig as authoring reference, one
  line per bone with its parent, local position and **bind pose** (from
  `sharedMesh.bindposes`, not the live transform). Names alone cannot author a
  garment: a skinned mesh carries a bind pose, so an armature whose joints sit
  elsewhere deforms wrongly even when every name matches.
- `Helpers.RigBoneNames(entity)`: every bone name the wearer's skinned
  renderers are bound to, distinct and sorted. SDCS rebinds a gear prefab's
  bones to the wearer by name and a mismatch becomes a null bone with no error,
  so the exact spelling is a hard prerequisite for authoring a skinned garment,
  and it lives in the game's asset bundles, readable only off a real wearer
  in a running client.
- `Helpers.OpenWindowGroup` / `CloseWindowGroup` / `OpenWindowNames`: open a
  game UI window group and find out whether it really ended up open, and list
  what the window manager believes is on screen. `GUIWindowManager.Open`
  resolves an unknown name with only a log warning and does not open within
  the same call, so a hand-rolled open reports a closed window that is about
  to appear and a misspelled group looks identical to one that declined to
  draw. `OpenWindowGroup` therefore returns whether the *name is known*, and
  `OpenWindowNames` (sorted, so two identical runs read identically) is what
  answers "is it on screen", from a later tick.
- `CaseDef.Staged` takes an optional `onHold(ctx, fraction)`, called every tick
  of the hold. A fixed camera photographs one face of a subject and says
  nothing about the others; turning the subject here makes the frames a
  turntable. A throw is logged and the hold continues, because the scene is
  already staged and the frames already exist.
- `CaseDef.Staged(suite, id, tags, stage, holdSeconds)`: builds a staging case
  (put the scene up, announce it immediately, hold it still, fail if it did
  not stage). Providers had hand-rolled that triple, which is where the
  per-project marker wording came from. Its assert deliberately establishes
  only that there was something to photograph.
- `scripts/capture_frames.sh`: runs a suite, waits for the first staged scene,
  photographs the client window, crops the frames and builds a contact sheet.
  Every project using this harness needed that loop and only had it by writing
  its own; `--runner` lets a project keep its own entry point.
- README "Visual confirmation: what a suite cannot tell you" - a suite proves
  data and never appearance, the client is only up for as long as the cases
  take, and the supported staged-frame path for anything a person must judge by
  eye.
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
- `MiningResult.Damaged` / `.Awarded` / `.Harvested`: the probe's outcome
  predicates as public read-only properties on the result object, so a
  provider reading `probe.Result` after a case can branch programmatically
  instead of re-deriving the comparison from raw block-type and count ints.
  Same predicate `MiningProbe.Assert` uses.

### Changed

- `CaseDef.Staged` / `CaseDef.StagedClip`: the `tags` parameter is now
  optional (default null), matching `CaseDef.Live`, so a quick staged case
  no longer has to invent a tag array. Additive; existing call sites are
  unaffected.
- Fresh save per run is now a hard rule with no opt-out (#66): the
  orchestrator always wipes the named stock save / zdtd world before a run,
  because a reused world measures the previous run's terrain and stale
  blocks instead of this run. `--reuse-save` is removed; `--fresh-save`
  remains accepted as a no-op back-compat flag. Operators who relied on
  world reuse keep their own copies before upgrading; the pre-run
  quarantine (`<logdir>/quarantine`, newest 5 kept) is the only recovery
  path.
- The provider-facing case contract (`PlayerGate`, `CaseDef`, `CaseCtx`)
  moved verbatim from `Runner.cs` into its own `Source/PlayTestMod/CaseDef.cs`,
  matching the other public provider surfaces (`Report.cs`, `MiningProbe.cs`,
  the `Helpers.*.cs` partials). No type, member, or namespace changed; the
  scenario-provider surface gate now reads the new file.
- Static-analysis gates tightened where the tree already passes: ruff now
  enforces PGH (no blanket `# noqa` / bare `type: ignore`) and T10 (no
  debugger imports or breakpoints) over `scripts/`; mypy gains
  `disallow_any_unimported`. Three `type: ignore[attr-defined]` suppressions
  in `dst_sim.py` were removed by returning the invariant session set from
  `install_invariants` instead of monkey-patching it onto `Simulation`.
- Contributor path: `make lint` / `make typecheck` preflight `uv` and
  `shellcheck` and name the missing tool with an install hint instead of a
  bare `command not found`; `shellcheck` is now listed under README
  Requirements; README gains an offline dev loop section; new
  CONTRIBUTING.md documents setup, the edit-test loop, and the PR rules the
  gates enforce; `make coverage` appears in `make help`.
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
  stock saves, zdtd world state (`players.zsv`,
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

- The client-log parser locates the `[7dtd-playtest]` marker as the line's
  first bracketed token and parses the payload structurally (JSON via
  `json.loads`, human lines by whitespace tokens) instead of matching
  anchored regexes. The game's logger prefixes every line with a timestamp,
  game-time and level before the tag, so the old line-anchored patterns
  matched nothing and every run waited out its full `--timeout` even after
  the suite wrote `DONE`; the marker-as-first-bracket rule still keeps a
  chat message that merely contains the marker from forging results,
  SUMMARY/DONE verdicts, JSON events or barrier fires.
- Orchestrator fails fast when the client exits before the suite's `DONE`
  (both the main poll loop and the rejoin setup loop): a mid-suite client
  crash used to wait out the full `--timeout`, hiding the failure behind a
  15-minute stall. The 2s post-exit drain still gives a client that wrote
  `DONE` in its final moments its success break.
- The orchestrator now announces a dedicated/zdtd backend that exits after
  readiness (`<backend> backend exited mid-run code=…`, once per server
  process, echoed as `server_exited_mid_run` in the report JSON, including
  the rejoin setup-incomplete report). Previously nothing polled the backend
  after its ready wait, so a mid-run crash surfaced only as scattered case
  failures, telnet connect misses, or the full timeout without naming a cause.
- The loadgen observer verdict is computed from one snapshot of
  `loadgen_events.jsonl`. Two separate reads could straddle an append and
  make the expectation failures disagree with the CVar-oracle state they are
  judged against.
- SIGINT (Ctrl+C) joins SIGTERM/SIGHUP in the orchestrator's termination
  handling: converted to SystemExit during normal operation so teardown runs,
  and blocked/ignored inside main's finally. A KeyboardInterrupt landing
  mid-cleanup used to skip stop_proc / lock release and strand a live runtime
  under a published claim, the exact wedge the TERM/HUP machinery exists to
  prevent.
- The `spawn_loadgen_peer` rebind routes the prior (exited) loadgen instance
  through `stop_proc`, which reaps it. Dropping the `Popen` after only closing
  its log handle left one zombie per peer barrier fire until orchestrator exit
  on long soak / mp runs.
- `test_mining_probe_surface.py` now matches the full `PressPrimary` /
  `ReleasePrimary` / `TickAttack` signatures (the first landed regex stopped
  at `(` and never found the method body), and ruff F401/RET504 on that
  gate. `MiningProbe.TryResolveBlock` initializes `error` to `""`.
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
- Omitting `--port` no longer crashes `main()` with a `None > int` TypeError
  inside the LiteNet port-room validation before any preflight refusal:
  the backend default (26900 stock / 27025 zdtd) now resolves before that
  comparison. Every `make playtest` invocation without an explicit `PORT=`
  took the crashing path.
- The mod did not compile. Making the `tags` parameter of `CaseDef.Staged` and
  `CaseDef.StagedClip` optional left it ahead of the required `stage`
  callback, which is C# error CS1737 (`Optional parameters must appear after
  all required parameters`). `stage` now carries a `= null` default purely to
  satisfy that ordering rule; it stays required in practice, and the
  construction-time guard that rejects a null `stage` is unchanged. Parameter
  order is untouched, so provider call sites still read
  `Staged(suite, id, tags, stage, ...)`. No offline gate catches this: the
  build needs the game's assemblies and cannot run in CI.
- `make coverage` ran 12 of the 14 offline gates. It kept its own copy of the
  gate list, and the copy had drifted from `make test`, so
  `test_mining_probe_surface` and `test_version_surface_units` contributed
  nothing to the measured percentage while the target claimed to run the same
  suites. Both targets now expand one `GATES` variable in the Makefile.
- `playtest_repeat.sh` scored a lap from an unreadable report as clean. It
  parsed the report with an inline `python -c` that coerced whatever it found
  through `int()`, so a missing or wrong-typed summary became `0 0 0`, which
  reads as a lap with no failures. Parsing moved to
  `scripts/report_summary.py`, which exits non-zero and prints nothing when a
  count is absent, non-integral, negative or infinite; the aggregator already
  counts an unreadable lap as failed.

No breaking changes to the consumer contracts: verified against the
`v0.7.1` tag, the public C# provider surface, log contract tokens, lock
payload keys (`running`/`session`/`acquired`/`heartbeat`), and suite env
names are unchanged. One operator-facing default changed on purpose: every
run now wipes its save/world first (see Changed, #66), and `--reuse-save`
is gone.

## [0.7.2] - 2026-08-23

A tag without a version bump. The annotated-version convention and the
tag-vs-manifest gate below did not exist yet, and `v0.7.2` was placed on a
commit whose tree still ships **0.7.1**: `ModInfo.xml` says 0.7.1 and
`ModIdentity.Version` prints 0.7.1, so the game mod list and the runner
banner identify anything built from this ref as 0.7.1. Treat `v0.7.2` as
a re-issue of 0.7.1, not a distinct mod version. Tagging is now gated by
CI (`.github/workflows/release.yml`): a `vX.Y.Z` push whose tree does not
declare that exact version is rejected.

What the ref actually changed (repo/tooling only, no consumer-visible
delta):

- HordeForge branding across docs and configs: `ModInfo.xml` Author and
  Website fields, Makefile and orchestrator workspace paths, and the
  `7dtd-fastconnect` naming in comments and docs.

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

[Unreleased]: https://github.com/hordeforge/7dtd-playtest/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/hordeforge/7dtd-playtest/releases/tag/v0.9.0
[0.7.2]: https://github.com/hordeforge/7dtd-playtest/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/hordeforge/7dtd-playtest/releases/tag/v0.7.1
