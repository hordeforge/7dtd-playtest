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

- `PLAYTEST_TELNET_PASSWORD` env and `--telnet-password`: one value feeds
  both the generated server config and the orchestrator telnet client.
  Default stays `retest`.
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

### Changed

- Host Python runs through `uv` only (requires Python >= 3.11).
- Entity probes, fixture equips, and barrier bookkeeping share one
  implementation; repeated parameterized barriers
  (`barrier spawn_vehicle:<class>`) each reach the host as separate fixture
  requests.

### Fixed

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

[Unreleased]: https://github.com/maci0/7dtd-playtest/compare/v0.7.1...HEAD
[0.7.1]: https://github.com/maci0/7dtd-playtest/releases/tag/v0.7.1
