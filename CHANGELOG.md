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
- **Pre-1.0 policy (de facto, stated here so consumers need not infer it).**
  Every release so far is `0.Y.Z`. Nothing here promises SemVer stability
  before `1.0.0`: a **minor** may remove or re-sign a symbol on those
  contracts, and a patch may change a default only when the entry says so.
  What a consumer is promised instead is the announcement: a release that
  removes a public symbol carries a `### Removed` section whose first line is
  `**Breaking.**`, and a table naming the replacement for each removed
  symbol. Each release has one section per impact class (Keep a Changelog
  order: Added, Changed, Deprecated, Removed, Fixed, Security), so the
  removal note is not filed under a second `### Fixed`.
  `scripts/test_version_surface.py` fails a release that breaks any of those,
  so "removed dead code" can no longer ship undeclared, as `0.13.0` did before
  the marker existed and `ChatProbe.Last` did under a plain `### Fixed`.
- Once `1.0.0` ships, the contracts named above become SemVer: a removal needs
  a major bump, and a deprecation needs a release that announces the
  replacement and a later release that removes it.

## [Unreleased]

### Security

- **A suite can no longer declare the telnet admin plane.** A `server` block
  naming `TelnetEnabled`, `TelnetRemoteAllowedIPs` or `TelnetPassword` in any
  capitalisation now fails closed with a `SuiteLoadError`. The orchestrator
  sets all three after copying the suite's block in, so a declaration was
  either silently overridden or, with a different capitalisation, a second
  property line the orchestrator's exact-case filter did not replace, leaving
  which value the game honours to the server's config reader. A suite-declared
  password was also echoed verbatim into `report-*.json`. The shipped suites
  declared only the redundant `TelnetEnabled`, now dropped.
  `schema/suite.schema.json` states the three names so an external suite
  author is refused by their editor too.
- **Run artifacts are published 0600.** `write_text_atomic` created its temp
  file with the umask's mode and followed anything already sitting at the temp
  name, so the report, junit XML, run-ended marker and apm dump landed
  world-readable (they carry the operator's home directory, the world name and
  the server's internal dump), and a file or symlink planted at the temp name
  would be written through and then renamed into place as this run's report.
  The temp file is now opened `O_EXCL` at 0600.
- **A player's display name no longer reaches the run transcript.** Every
  admin reply names the entities it talks about (`'Alice' (id=171, ...)` for
  stock, `(entity 107) Alice` for zdtd), and the name is whatever a remote LAN
  peer chose, so each logged slice of a `listplayers`, `listents`, `kill` or
  `teleportplayer` reply carried a person's name into a log CI uploads. The
  caller-facing behaviour is unchanged: a redacted line keeps its entity id,
  which is all the parsing and the counts read, and only the name and the tail
  past the id are dropped. Committed fixtures used the repository owner's own
  account name; they now use synthetic ones.

- **Windows device names no longer reach a staged frame or clip path.**
  `Helpers.AssetName` (the client, a Windows process under Proton) filters a
  staged frame or clip id to a safe character set, but a name that filters to
  nothing collapsed onto its parent directory, and a reserved device name
  (`AUX`, `nul`, `COM1` ... `LPT9`, with any casing) is the device rather than
  a file, so `CreateDirectory` failed and the case photographed nothing. Both
  now get a name the client filesystem accepts, and the rules now live in
  the one name the clip marker, the staged scene and the frames directory
  already share, so a collector reading a directory out of `clip complete`
  reads the directory the frames were written to.
  `scripts/test_windows_path_surface.py` pins the rules offline (the mod
  cannot be compiled without the game assemblies).

- **Host CLI help and exit codes, aligned across the scripts.**
  Every other CLI in `scripts/` already printed its exit codes from
  `--help`; these did not, and one of them used the usage code for something
  that is not a usage error. `dep_sbom.py` now documents its exit codes and
  its stdout/stderr split (CycloneDX JSON on stdout, the component count on
  stderr) and exits 1 rather than 2 when a committed lockfile or
  `ModInfo.xml` is missing, since nothing about the invocation was wrong;
  `dst_run.py` and `coverage_badge.py` document theirs, and
  `playtest_lock.py --help` now says what `live` reports and what its
  nonzero exit means, which the one-line usage previously left unsaid for
  the probe the capture scripts gate on. `capture_frames.sh`,
  `capture_video.sh` and `capture_audio.sh` document the 0/1/2 scheme they
  already used. `quarantine_restore.py` documents its exit codes too, and
  `restore <entry>` on an entry carrying no `restore.jsonl` now exits 1 like
  `show` on the same entry instead of 2, because nothing about the command
  line was wrong. `playtest_run.py --help` describes the six
  `--loadgen-observe-*` / `--loadgen-expect-*` flags, which carried no help
  text at all. `dep_sbom.py` names the reason on stderr and exits 1 for a
  lockfile it cannot parse (the `ValueError` behind its own exit-code
  promise used to reach the operator as a traceback), and creates the
  output's parent directory, so the `dep_sbom.py dist/app.cdx.json` example
  in its epilog works in a tree that has no `dist/` yet.
- **The capture teardown grace is resolved and validated once, before the run
  starts.** `RUN_STOP_TIMEOUT_SEC` reached `$(( SECONDS + RUN_STOP_TIMEOUT_SEC ))`
  inside the `capture_stop_run` EXIT trap with no check, and each of the three
  capture scripts carried its own `:-30` default. A non-numeric value (`30s`, a
  stale shell default) aborts bash on the arithmetic error, so the trap never
  signalled the run it exists to stop and the suite kept the exclusivity lock
  and the live client with it. `capture_common.sh` now owns the default and a
  `capture_init_run_timeout` that refuses a non-positive or non-integer value by
  name, exit 2, before anything is started.
- **`PLAYTEST_LAP_MARK_STALE_SEC` is checked like `--laps`.** The value is
  arithmetic, not text: a non-numeric one aborted the abandoned-lap-mark sweep
  on a bash arithmetic error with nothing said (the script runs without
  `set -e`), and a value under a minute read as "sweep off" rather than the
  near-instant sweep it asked for. Both now exit 2 naming the variable, and the
  variable is listed in the script's own help along with the other env knobs.

### Added

- **`make package`: the release archive, built the same way twice.**
  The release workflow documented a `make package` target that did not exist,
  so the maintainer path to a shipped zip stopped at "attach something". The
  target wraps the dist tree `make install` copies through
  `scripts/mod_package.py`: the entry set is named rather than globbed (a
  `.deps.json` or a stale file left in `dist/` cannot ride along), entries are
  written in sorted order, and every entry carries the same timestamp, the
  same 0644 mode and the same create_system byte, so two builds of one source
  produce the same archive. The recorded timestamp comes from
  `SOURCE_DATE_EPOCH` and falls back to the zip epoch (1980-01-01) rather than
  the clock, so a build that does not set it still agrees with itself. The
  archive is published by rename, and an incomplete dist fails by name
  instead of shipping a partial one. Gated offline by
  `scripts/test_mod_package.py`.

- **`scripts/quarantine_restore.py`: put a swept-aside world back.**
  `--fresh-save` moves the zdtd world state, its chunk overlays and the
  previous client log under `<logdir>/quarantine/` instead of deleting them,
  but nothing recorded where each file came from, so the copy-back depended
  on an operator remembering the `--world` (or log path) that produced an
  entry. Every move now appends `{src, dest}` to the entry's `restore.jsonl`,
  fsynced, and the new CLI reads it: `list`, `show <entry>`, and
  `restore <entry> --apply` (dry run by default, refuses to overwrite an
  existing original without `--force`, `--move` reclaims the quarantined
  copy). A quarantine prune that drops a restorable entry now names the
  paths it held on the run log instead of deleting the only copy silently.
  The compare baselines under `workspace/comparison-playtest/` are published
  by atomic rename, so a killed comparison cannot leave a truncated document
  that git records as the long-lived record. Gated by
  `test_quarantine_restore.py` and `test_playtest_compare.py`; README
  "State, backups, and recovery" is the runbook.

- **`scripts/test_coverage_badge.py`: the coverage badge is gated.**
  `scripts/coverage_badge.py` was the one shipped `scripts/` module no offline
  gate read, so the number CI publishes into the README was unverified. The
  gate drives the shipped script: each colour band's inclusive lower bound,
  the rounding of the measured total, the `0` / `1` / `2` exit codes its own
  usage line promises, and the `.coverage.json` scratch, which must be gone
  after a read that failed as well as after one that succeeded. Only the
  measured report is substituted, and the gate is registered in the Makefile
  `GATES` list, so it runs under `make test` and `make check`.

- **`make package` builds the release archive.** The release workflow told a
  maintainer to run a target the Makefile never had, so the mod zip had no
  command to produce it. The target builds the mod, then writes
  `dist/7dtd-playtest-<version>.zip` (version read from `ModInfo.xml`) with the
  `Mods/7dtd-playtest/` layout the game extracts from, using stdlib
  `python -m zipfile` so packaging needs no host `zip` on top of uv, and
  leaving the build's pdb out of the archive. The
  gate-list gate now also fails when a workflow names a make target the
  Makefile does not define, so the same drift cannot come back. `make sbom`
  also gained the `require-uv` preflight every other uv target has.

- **Injectable clock for the orchestrator** (`playtest_run.use_clock()`).
  Every time read and wait in `playtest_run.py` now goes through
  `monotonic_now()`, `epoch_now()`, or `pause()`; `SystemClock` is the only
  place `time` is touched. A run that installs a clock which moves only when
  slept on executes its whole poll-loop timing surface (phase deadlines,
  readiness and barrier polls, progress crumbs, the soak window) without
  spending a real second, which is what a simulation of the orchestrator
  needs first. Production behaviour is unchanged: the default is the real
  clock. Gated by `test_playtest_run_units.py`, which drives a real poll loop
  on a virtual clock and fails if any `time` call reappears outside the seam.

- **Injectable byte source for the log reader**
  (`playtest_log.LogBytes` / `PathLogBytes`). `LogTail` read its bytes with
  `Path.stat` and `Path.open` directly, so the only way to reach the logic
  that matters to the orchestrator - a line held back until its newline, a
  UTF-8 character torn across two polls, a truncate that restarts the
  generation - was to write a real file and hand-time the append. The tail
  now reads through a two-method port whose production implementation is the
  file it always read; a caller with the bytes in hand supplies its own. A
  `None` from either method is what an `OSError` always meant: skip this poll
  and retry, not an empty log. Production behaviour is unchanged, and
  `test_report_surface.py` drives one tail over the same arrival schedule
  through both sources and fails if a single poll diverges.

- **`make sbom` and the release-time dependency inventory.**
  `scripts/dep_sbom.py` writes a CycloneDX 1.6 SBOM from the two committed,
  hash-pinned lockfiles (`uv.lock`, `Source/PlayTestMod/packages.lock.json`)
  with no network and no scanner, so a tag publishes what it depends on and a
  vulnerability scanner can read it without a checkout. Every component is
  marked dev scope (pyproject declares no runtime dependency; the csproj
  reference is `PrivateAssets="All"`), and the serial number is a content hash,
  so an unchanged tree re-runs to the same id. Gated offline by
  `scripts/test_dep_sbom.py`, and built for the tag in
  `.github/workflows/release.yml`.
- **One uv pin across the toolchain, checked.** The workflows install the uv
  that wrote `uv.lock` from a workflow-level `UV_VERSION` instead of a literal
  repeated in every `setup-uv` step, and `scripts/test_version_surface.py`
  fails when that value drifts from the `[tool.uv] required-version` floor in
  `pyproject.toml` or when a step pins a second literal beside it. The tag's
  SBOM also records its serial number and component count in the run summary,
  which the runner temp dir alone did not leave behind.
- **SPDX license on every SBOM component.** `scripts/dep_sbom.py` records
  the license of each package in the CycloneDX inventory, read from the
  license file the artifact itself ships. A dependency added with no recorded
  license, or with one outside the permissive set `scripts/test_dep_sbom.py`
  allows, fails the gate instead of shipping an unlabeled component.
- **Gate-list surface** (`scripts/test_gate_list.py`, wired into the Makefile
  `GATES` list): fails when a `scripts/test_*.py` file is missing from `GATES`
  (it would run under neither `make test` nor CI) or when a `GATES` entry has
  no file, that a `test_*` defined in a gate is never named by that gate's own
  runner (an unregistered test reads as coverage and never executes), and pins
  that `test`/`coverage`/`test-one` share one gate list and that CI runs the
  same steps `make check` does.
- **Gates that read the orchestrator's source now read its code.**
  `scripts/test_stock_peer_client.py` blanked the comments out before matching,
  so a contract that was written up, commented out, or left behind by a
  refactor no longer satisfies the check. `scripts/test_catalog_surface.py`
  requires each barrier name as a quoted literal and each fixture handler as a
  definition rather than as a bare substring, and pins the reverse direction of
  the barrier contract: every barrier `Catalog.cs` emits must be a name the
  host's `BARRIER_NAMES` routes, or its case waits on a barrier nobody
  services. `scripts/test_no_unbound_locals.py` takes its file list from
  `scripts/` instead of a hand-maintained tuple, so all twelve host modules it
  was missing are checked and the next one is checked the day it lands.
- **A removal can no longer ship undeclared.** The release model now states
  the pre-1.0 policy in full (a minor may remove a public symbol; the entry
  has to say so and name the replacement), and
  `scripts/test_version_surface.py` fails a release whose `### Removed`
  section lacks the `**Breaking.**` marker, or declares the break without a
  table naming the replacement, or repeats an impact heading inside one
  release (a second `### Changed` is where a `### Removed` gets skimmed past,
  which is how the `ChatProbe.Last` break below arrived under `### Fixed`).
  `0.13.0` removed three `Helpers` methods and two host helpers under a plain
  `### Removed` heading, so its entry now declares the break and carries a
  symbol-to-replacement table. The tag-coverage half of the gate read its
  refs from the working tree's own git dir, so in a linked worktree it found
  no tags and reported "not applicable" while the releases sat in the main
  checkout; it follows `commondir` now.
- **Two new public members for providers.** `ChatProbe.LastLength` is the
  length of the last captured remote-player message (the text it counts stays
  private, see `### Removed` below), and `CaseDef.DefaultSpawnOffset` is the
  placement `CaseDef.WalkEntity` now uses when its `spawnOffset` is
  `Vector3.zero`, in front of and above the player and clear of the first-person
  camera. A provider that wants the old fixed offset passes it explicitly.
- **`require-uv` preflight.** `make test-one`, `dst`, `dst-soak`, `coverage`,
  `playtest` and `playtest-repeat` now check for `uv` up front and name it,
  instead of printing a bare "not found" from the interpreter call. A mistyped
  `make test-one GATE=` lists the known gates.
- **One test inside a gate is reachable.** `scripts/test_playtest_compare.py`
  ran `pytest.main([__file__, "-q"])` and dropped its own argv, so
  `make test-one GATE=test_playtest_compare.py ARGS="-k identical_sides"`
  reported 20 passing dots and looked filtered while running the whole gate.
  The gate forwards argv to pytest now, and `make test-one` passes `ARGS` to
  whichever gate it is running. CONTRIBUTING's setup step also names
  `yamllint` alongside `shellcheck` and `uv`: `make test` runs the YAML gate,
  so a host set up from the old list failed its first gate.
- **`make doctor`: every missing host tool in one run.** Each offline target
  already names the tool it cannot run without, but it stops at the first, so
  a host missing both `shellcheck` and `yamllint` needs two failed `make test`
  runs to learn about two tools. `doctor` reports all three gaps with their
  install lines, plus the pinned Python and the dotnet SDK, in pure shell (the
  tool it reports a missing `uv` for cannot be reached through `uv`).

### Changed

- **The WalkEntity renderer/grounding probe moved out of the provider
  contract.** `ReportWalkEntityRenderProbe`, `GroundYFor` and
  `TryGroundSurface` were private members of `CaseDef` (CaseDef.cs), the type
  external scenario providers build cases from, so physics and mesh inspection
  sat beside the case contract they serve. They are now `EntityProbe`
  (`Source/PlayTestMod/EntityProbe.cs`), an internal type that
  `CaseDef.WalkEntity` calls. No public symbol moved: the C# provider API
  `CaseDef`/`CaseCtx`/`IScenarioProvider`/`Helpers`/`Report` is unchanged, and
  the render-probe evidence lines are byte-for-byte the same.
- **The mod build answers the pinned SDK again.** `global.json` said
  `rollForward: latestMajor`, so a host that happened to have a .NET 9 or 10
  SDK installed compiled this source with that Roslyn, and the "byte-
  reproducible across checkouts" claim only held between hosts that agreed on
  a major version. It rolls forward inside the pinned 8.0 feature band again,
  as the release that introduced the pin recorded, and
  `scripts/test_version_surface_units.py` fails if it drifts back. A host
  with only a newer major installed now gets dotnet's own "SDK not found"
  rather than a silently different compiler.
- **`make lint` also lints the workflow YAML.** `.github/` was the one
  shipped language with no analyzer: a duplicate key, a wrong indent or an
  unbalanced bracket in a workflow parsed only when the push ran. `yamllint`
  now covers it (config in `.yamllint.yml`, extending the `default` rules so
  the structural checks stay at full strength), with a 100-column cap to
  match `[tool.ruff]`. A host without `yamllint` runs
  `make lint SKIP_YAML=1`; CI never sets it, so a skipped gate still blocks
  the merge. `FIX` and `TD` join the ruff `select` (no `TODO`/`FIXME`
  markers, siblings of the `ERA` group already selected), both clean on the
  current tree.
- **The coverage number can now fail.** `make coverage` printed a percentage
  and published it as a badge; a dropped line lowered the badge and nothing
  else. `[tool.coverage.report] fail_under` pins a floor one point under the
  measured 82.75%, so a collapse exits non-zero instead of publishing.
- **One copy of the capture scripts' shared plumbing.**
  `scripts/capture_common.sh` holds the live-run guard (three identical
  copies), the byte-offset log gate and the `stop_run` teardown (two copies
  each), and `capture_audio.sh`, `capture_frames.sh` and `capture_video.sh`
  source it. A fix to one copy used to leave the others answering a question
  they no longer asked. `scripts/test_capture_video_surface.py` runs the
  shared text and fails if a script goes back to a private copy.
- **`CaseDef.WalkEntity` places and validates its spawn.** Same signature, two
  behaviour changes a provider can see: `spawnOffset: Vector3.zero` now
  resolves to `CaseDef.DefaultSpawnOffset` rather than spawning inside the
  player, and a `clipFps` of zero or less raises `ArgumentOutOfRangeException`
  (naming `suite/id`) where it previously produced a case that recorded
  nothing. The drive also orbits the spawn point on the terrain rather than
  walking the creature out of the camera, which is what makes the clip
  judgeable; README describes the current behaviour.
- **Host CLI help and exit codes, aligned across the scripts.**
  Every other CLI in `scripts/` already printed its exit codes from
  `--help`; these did not, and one of them used the usage code for something
  that is not a usage error. `dep_sbom.py` now documents its exit codes and
  its stdout/stderr split (CycloneDX JSON on stdout, the component count on
  stderr) and exits 1 rather than 2 when a committed lockfile or
  `ModInfo.xml` is missing, since nothing about the invocation was wrong;
  `dst_run.py` and `coverage_badge.py` document theirs, and
  `playtest_lock.py --help` now says what `live` reports and what its
  nonzero exit means, which the one-line usage previously left unsaid for
  the probe the capture scripts gate on. `capture_frames.sh`,
  `capture_video.sh` and `capture_audio.sh` document the 0/1/2 scheme they
  already used.

- **One boolean spelling table for every env knob.** `PLAYTEST_READONLY`,
  `PLAYTEST_TRACE_ENTITY` and `CLIENT_MUTE` accepted different token sets, so
  `PLAYTEST_READONLY=false` armed readonly while `PLAYTEST_TRACE_ENTITY=yes`
  armed nothing. All three now read `1/true/yes/on` and `0/false/no/off`, and
  anything else is a harness error naming the variable rather than a silent
  default. Empty still means unset.
- **`PLAYTEST_PROVISION` / `PLAYTEST_BACKEND` are validated before argparse
  runs.** argparse checks `choices` on a flag but not on a string default, so
  a typo in either env var reached the run as a live value.
- The startup `config:` line also reports the sandbox client instance, the
  resolved lock file, and whether the client mute is on.
- `CLIENT_MUTE_TIMEOUT` is validated as finite seconds > 0; a junk value warns
  and keeps the default instead of handing the helper a wait argument that
  does nothing.
- **The vision-review consent text names the whole payload.** `--allow-network`
  covers the entire clip directory, not "the clip": `capture_video.sh` leaves
  the run log and a copy of the client log beside the mp4, and the client log
  carries whatever the game and any remote LAN player wrote into it. The CLI
  description, the `--allow-network` help, the refusal and the pre-upload
  notice now say so, and README plus `docs/VIDEO_MODEL_FEEDBACK.md` match.
- **Shared Catalog.cs readers moved out of a gate.** The two offline gates
  that read `Catalog.cs` now import the readers from `scripts/catalog_surface.py`
  instead of one of them importing the other (`test_suite_refs` pulled
  `append_suite_map` out of `test_catalog_surface`, and both pinned the
  `catalog.` ref prefix separately). The ref prefix that `Runner.CaseRef`
  builds lives beside the parsers now. No gate assertion changed.
- **Help text that named a flag that no longer exists.** `playtest_run.py
  --help` told the reader to "prefer --target" and to use `--sandbox-name`
  "for --target sandbox"; the two-axis replacement has no `--target` flag,
  so both strings now name `--provision`.
- **`make playtest*` no longer overrides the backend with its own default.**
  Every target passed `--server "$(SERVER)"` with `SERVER ?= stock`, so a
  documented `PLAYTEST_BACKEND=zdtd` was inert under make. `SERVER` now
  defaults to `PLAYTEST_BACKEND`, and the flag is only passed when set.
  `READONLY=` takes the same on/off spellings as `PLAYTEST_READONLY`, so
  `READONLY=false` no longer arms `--readonly`.
- `review_video.py` documents `--keep-raw-response` and `--force` (both are
  gateway passthroughs) and closes with examples and exit codes; `make help`
  lists `playtest-review-video`; the `playtest-repeat` comment no longer
  claims a `LAPS=3` default the Makefile does not have.
- Offline gate strictness: ruff now also enforces `ERA` (commented-out code)
  and `ISC001`; mypy adds the `mutable-override` and `type-abstract` error
  codes and drops `playtest_log` and `test_suite_loader` from the
  type-argument opt-out list, since both modules pass it.
- `playtest_targets.apply_plan_to_args` / `overlay_instance_env` take a typed
  `argparse.Namespace` instead of `object` plus twelve `attr-defined` ignores.
- The detached child the orchestrator launches carries its log handle as a
  declared `DetachedPopen.log_fh` attribute rather than one attached to a
  plain `Popen` at runtime.

### Removed

**Breaking.** The removal below is allowed without a major bump under the
pre-1.0 policy above, but it does break an external `IScenarioProvider` that
read `ChatProbe.Last`, and the entry says so rather than filing it as an
ordinary security fix.

Migration, by symbol:

| Removed | Use instead |
|---|---|
| `ChatProbe.Last` (`public static string`) | `ChatProbe.LastLength` (`public static int`), the length of the last captured message, never its text |

- **The captured chat text is private.** `ChatProbe.Last` was a public field
  holding whatever a remote LAN player typed, and a provider putting it in
  `ctx.Detail` published it to the run log, the JSON result event, the JUnit
  report and `report-*.json`, all of which leave the machine. The field is
  private now, the stock chat cases report `chat_len=`, and a provider that
  needs a predicate reads `ChatProbe.Contains` / `ChatProbe.LastLength` rather
  than the raw string. Gated by `scripts/test_chat_probe_surface.py`.

### Fixed

- **A vision review can no longer run without a deadline, and no longer runs
  on a model nobody named.** `review_video.py --timeout` reached
  `subprocess.run` unvalidated: `nan` and `inf` are added to the current time
  and the child's own deadline check never fires, so the one cost cap on a
  paid upload could be removed by a value no human typed. `--timeout` is now a
  finite 1-900 seconds, refused before the gateway is invoked, and the value in
  force is recorded in the evidence. `--model` is held to the same plain-name
  rule the provider name already had (it reaches the gateway argv and the
  evidence document), and every validated envelope carries a `review_request`
  block recording the provider, the model asked for and whether it was pinned,
  so an unpinned provider default reads as the drift it is rather than as two
  comparable reviews.
- **A gateway refusal can no longer repaint the operator's terminal.** The
  refusal message and the non-JSON envelope error carried provider prose (and,
  in the second case, a fragment of the provider's own response) straight to
  stderr, while the result path flattened the same class of text through
  `terminal_safe`. Both now go through it, so an escape sequence or a forged
  `PASS` line in a refusal is flattened like any other model output.
- **A Unicode line separator in chat text can no longer forge a log line.**
  `str.splitlines()` also breaks on U+000B, U+000C, U+001C-U+001E, NEL,
  U+2028 and U+2029, and the game's logger emits none of them, so a peer who
  typed one inside a single chat message had its tail promoted to a line of
  its own: the contract-line anchor then accepted it as a genuine `[7dtd-playtest]`
  emission, and a U+2028 inside a chosen character name split the `listents`
  reply so the fragment holding the name no longer matched as a player line,
  leaving the name in a transcript that leaves the machine. Protocol text now
  splits through `playtest_log.split_log_lines`, which cuts on CR and LF only.
- **A truncation no longer cuts inside a character.** `video_review.terminal_safe`
  capped a model verdict at a code point, which could land between a base and
  its combining mark, inside an emoji ZWJ sequence, or between the two halves
  of a flag. The cut now backs out of the half cluster it would have left.
- **A clip id could name a Windows device.** `Helpers.AssetName` maps a name
  to ASCII letters, digits, `-` and `_`, so `con`, `prn`, `aux`, `nul`,
  `com1`-`com9` and `lpt1`-`lpt9` survive it unchanged, and the mod runs on the
  Windows client: `CreateDirectory` on `playtest-shots/clips/aux` fails and a
  take that recorded frames had no directory a collector could look in. A
  surviving device name is now prefixed with `_` (`ReservedDeviceNames`,
  compared case-insensitively, since the device matches any casing and any
  extension). `COM0` and `LPT0` stay ordinary names; Windows does not reserve
  them. The `scene staged` name, the `clip complete` line and the frames
  directory remain that one string, so the prefix reaches all three.

- **`uv.lock` was covered by the `*.lock` ignore rule.** The rule exists for
  the runtime lock file, and it also matched the committed dependency lock
  every `uv run --locked` gate reads. The file is tracked, so nothing changed
  while it existed, but a regenerated one could not be re-added without `-f`,
  and a tree missing it looks like a tree that still has one. `.gitignore`
  re-includes it, and `scripts/test_dep_sbom.py` fails if either committed
  lockfile falls under an ignore rule again.

- **A clip marker named a directory that was never created.** Frames were
  written to `playtest-shots/clips/<id>` under a sanitized name while the
  `clip complete` line, the `scene staged` name and the on-demand recorder's
  three log lines carried the raw id. `scripts/capture_video.sh` takes the
  directory from that line and looks the frames up under it, so any id holding
  a character the sanitizer rewrites (`motion.2`, `walk cycle`, or an `é`
  typed decomposed as `e` + U+0301) reported "no frames found" for a take that
  had recorded them. `Helpers.AssetName` is now the single name: normalized to
  NFC, ASCII letters/digits/`-`/`_` kept, everything else `_`, idempotent, and
  used for the directory, the staged name and every marker line.
  `test_scenario_provider_surface.py` pins that they cannot drift apart again.
- **Lengths reported in the wrong unit.** `ChatProbe.LastLength` counted UTF-16
  code units, so one emoji in a LAN player's chat read as `chat_len=2`, and the
  `soak_apm_budget` detail labelled `text.Length` as `bytes=`, a figure that
  was never the dump's size on disk. It is now the file length.
- **`scrub` let C1 controls through.** The orchestrator's terminal echo
  stripped C0 and DEL but not U+0080-U+009F, whose U+009B is the 8-bit CSI, so
  a log line carrying one could still repaint the operator's screen.
  `video_review.terminal_safe` already covered the range; the two now agree.
- **A rejoin run killed a client by pattern, on the managed path too.** The
  launch clean and the teardown sweep stop a managed run's own Safehouse client
  by instance name, because `GAME_PROC_PATTERNS` matches a sandbox client's
  Proton command line exactly as it matches the operator's Steam client and
  would take down a concurrent sandbox run's client with it. The two rejoin
  paths (the setup-incomplete abort and the restart that verifies persistence)
  answered that rule themselves and swept by pattern, so the invariant held
  everywhere except there. `game_sweep_patterns(plan)` is now the single answer
  and every kill site, `stop_run_client` included, goes through it.
- **The run report's server config omitted the keys the harness forced.** The
  `server_config` snapshot was taken before `TelnetEnabled`,
  `TelnetRemoteAllowedIPs` and `TelnetPassword` were applied, so the recorded
  "what the world actually was" list could not reproduce the run it described.
  The snapshot now follows the forced keys and still drops the per-run
  password, which must not leave the machine.
- **The quarantine stamp read the real clock.** `_quarantine_entry` named its
  directory from `time.strftime`/`time.gmtime` instead of the injected clock,
  so a run on a simulated clock stamped its evidence with host time while
  `prune_quarantine` keeps the newest entries by name. It now reads
  `epoch_now()` and formats in UTC, which is what the name ordering already
  assumed. `test_playtest_run_units.py` widened its seam pin from three named
  `time` attributes to every `time.<attr>(` call, which is the gap that let
  this through.
- **One run could name three different epochs.** The report filename, the junit
  filename and the report's `ran_epoch` field each read the clock separately, so
  a run straddling a second boundary was written as `report-<A>.json` carrying
  `ran_epoch=B` beside a `junit-<C>.xml`. `playtest_compare` treats the name
  and the field as the same value. All four now read one `run_epoch`.
- **A clock step could cut or stretch `playtest_lock.py wait`.** The wait
  measured its budget with two wall-clock reads, so an NTP correction mid-wait
  ended a 30-minute wait early or stretched it by the size of the step. The
  budget is now a countdown of what the loop actually slept, using the
  existing `sleeper` seam.
- **The vision-review intent had no size bound.** Everything in an intent goes
  into the review prompt verbatim, so a pasted log in `purpose` or a thousand
  `questions` was an unbounded provider request. `parse_intent` now refuses an
  over-long field, item, list, or total, and an intent file or `--intent-text`
  over 64 KiB is refused before it is read into memory.
- **A provider name could write outside the clip folder.** The default
  evidence name embeds the `--provider` string, and a failed review deletes
  that file, so `--provider ../../escape` put the evidence (or its removal)
  outside the clip directory. The name must now be a plain filename token,
  refused before the review starts.
- **`--attach-reviews` crashed the report on a hostile envelope.**
  `collect_visual_reviews` assumed a JSON object with a nested object at
  `intent.content`, so a `review-*.json` holding a list, `null`, or a dict
  where a suite name belongs raised `AttributeError` after the run's own
  result was known. Each level is shape-checked, an envelope over 8 MiB is
  skipped with a warning, and a file whose intent is unusable still appears in
  the report keyed by its stem.
- **A malformed port in the instance contract read as "not allocated".**
  `playtest_targets._optional_int` returned `None` for a `SERVER_PORT` /
  `SERVER_TELNET_PORT` that did not parse, so a corrupt `instance.env` left a
  managed run on the pre-`sb up` placeholder (or the lab default) and the only
  complaint named an unusable number rather than the line that caused it. A
  set-but-unparseable value is now a `TargetError` naming the key and the
  value; unset stays unset. `test_playtest_targets.py` covers both the
  post-`sb up` overlay and target resolution.
- **`sb` had no wall-clock bound.** `scripts/playtest_targets.py` ran every
  Safehouse call without a timeout, so a `sb up`, `sb stage` or `sb stop` that
  never returned blocked the orchestrator before its poll loop started, and the
  run's own deadline could not fire: the live client and the exclusive-lock
  claim were stranded. Each call is now bounded by `SB_COMMAND_TIMEOUT_SEC`,
  and an overrun raises `TargetError` naming the command and whatever `sb`
  printed before it wedged.
- **A teardown that left an instance running said nothing.**
  `_stop_sandbox_instance` suppressed `TargetError` and, with `check=False`,
  ignored a non-zero `sb stop` exit, so a dedicated still holding its port
  block looked identical to a clean stop. Both now report on stderr.
- **Failures that silently became a green run.** An unreadable rejoin setup
  suite dropped `PLAYTEST_CASE_REFS` with no message, which reads as "run
  every case" rather than "the setup was never armed"; an unparseable
  `review-*.json` envelope vanished from the report. Both now warn and name
  the file.
- **The run report could not be reloaded.** `suite_to_report` carried a
  `source` key that `parse_suite_dict` rejects as unknown, so the run's own
  record of the suite it ran was not a valid suite document. Provenance moved
  out to `suite_doc_source` beside the mapping, and
  `scripts/test_suite_loader.py` now pins the round trip.
- **A lap could be graded on another run's report.**
  `scripts/playtest_repeat.sh` selected the newest `report-*.json` from the
  shared report directory, so a previous lap or a concurrent session supplied
  the verdict. Each lap now stamps a mark before it runs and only accepts
  reports newer than that.
- **Capture scripts reported artifacts they did not produce.**
  `capture_frames.sh` printed the contact-sheet path whether or not `montage`
  ran, and `capture_audio.sh` exited 0 on an empty recording, so a recorder
  that died on a busy monitor shipped as a passing run. The audio capture now
  exits 1 on an empty recording.

- **`--no-server` attach still fell back to the published `retest` telnet
  password.** The 0.8.0 entry that removed the static default only changed the
  orchestrator-started path; an attach run, which writes no config and so
  cannot mint a per-run secret, kept using the old default against whatever
  host it joined, including a production one. `resolve_telnet_password` now
  refuses an attach run with no `PLAYTEST_TELNET_PASSWORD` / `--telnet-password`
  rather than connecting with a password an attacker can read in this
  repository. A managed run is unaffected: unset still means a fresh
  `secrets.token_urlsafe(15)`.
- **The run report could not be reparsed as the suite that ran.**
  `suite_to_report` carried a `source` key that `parse_suite_dict` rejects as
  unknown, so the round trip its own docstring promised failed on every
  document. Provenance is the loader's knowledge, not a field a document may
  declare about itself, so `source` left the document mapping and the run
  report records the file under `suite_file` beside it.
- **An unbounded JSON integer in one client-log line cost the whole report.**
  The slowest-case table read `ms` out of every parsed result event and only
  caught `ValueError`; a JSON integer is arbitrary precision, so a `ms` past
  the float range raised `OverflowError` out of the report writer instead of
  dropping its own row. The table is now `slowest_cases`, which drops an
  unusable `ms` and keeps the rest.
- **A port read from `instance.env` was never range-checked.** The `tcp_port`
  argparse type bounds 1..65535, but a managed run's game and telnet ports
  come from `sb env` as plain ints and only the upper LiteNet bound was
  applied. A `0` (the pre-`sb up` placeholder, and what an empty
  `SERVER_PORT` leaves behind) or a negative value reached the bind and
  connect calls. `require_litenet_room` now checks the whole range and
  `require_telnet_port` guards the admin port beside it.
- **A future-dated heartbeat could never be reclaimed.** `is_stale` compared
  `now - heartbeat` against the window, so a stamp ahead of the reader's
  clock (a clock step, a restored backup, a host whose clock was wrong) was
  never old enough, and a crashed holder's claim blocked the machine until
  someone deleted the file by hand. Such a stamp is now stale, on the same
  reclaim path as an old one and still gated on no live process.
- **Tag versions sorted as strings.** `discover_tag_versions` promises its
  versions oldest first but used `sorted()` on the text, which puts `1.10.0`
  before `1.9.0`. It now orders by component.
- **`--require-fresh-minutes` could raise instead of reporting usage.** The
  value is scaled into float seconds and a Python int is unbounded, so one
  past the float range crashed the diff with an `OverflowError` traceback.
  It is now a usage error like every other bad value.
- **The backend a run targeted was not the one that was asked for.**
  `playtest_run.py` decided whether the operator had passed `--server` by
  searching the argument list for that exact token, so `--server=zdtd` read as
  "not given" and the suite document's `backend` quietly sent the run to the
  stock dedicated instead. `PLAYTEST_BACKEND` was dead for the same reason:
  the argparse default was a literal `stock`, so `resolve_target` never
  reached its env fallback. The parser now reports a `None` default and
  `resolve_backend` ranks flag, then `PLAYTEST_BACKEND`, then suite document;
  the undocumented second name for that knob is gone.
- **A suite document's `kind` could contradict the case it names.** `kind` is
  the one field in a document that states how a case runs, and it is a copy of
  what `CaseDef.Live` / `CaseDef.Defer` already decided, with nothing
  comparing the two: a document could declare `live` for a deferred case (the
  report then shows a skip the suite never declared) or `defer` for a live
  one (a green run that measured nothing). `catalog_surface` reads the factory
  per case and `test_suite_refs` fails on the drift.
- **A misspelled suite field no longer reads as unset.** The declarative
  suite loader ignored unknown keys, so `provison: attach` on a suite that
  meant to join a production host read as "no provision" and the run went
  managed (wiping the world it was supposed to leave alone). Unknown keys in
  the document, in `host` and in a case now fail at load naming the key;
  `server` stays open, since its keys are stock serverconfig properties.
  `schema/suite.schema.json` says the same (`additionalProperties: false`).
- **A capture script could outlive the playtest it started.**
  `capture_frames.sh` and `capture_video.sh` run the suite in the background
  and have several ways out before the `wait`: an unparseable completion
  marker, missing frames, ffmpeg failing, or Ctrl+C. None of them stopped the
  run, which then held the playtest exclusivity lock and a live client and
  dedicated on the machine's one shared client until its own timeout. Both now
  start the run in its own process group (`setsid`, with the plain background
  form as fallback) and trap EXIT/INT/TERM to `stop_run`: SIGTERM first, so the
  orchestrator runs its own teardown, escalating to a group SIGKILL after
  `RUN_STOP_TIMEOUT_SEC` (default 30) so a wedged run cannot hold the exit
  open. The trap is dropped once the run is reaped. Pinned by
  `scripts/test_capture_video_surface.py`, which runs each script's real
  `stop_run` against a process group that ignores SIGTERM.
- **Staged instances survived a case that failed.** `CaseDef.Staged` and
  `StagedClip` clear their staged GameObjects when a hold completes, but a
  case that threw while staging, timed out, or lost the player never reached
  that line, so the objects stood in the player's face for the rest of the
  run and the next look case photographed them. `Runner.FinishCase` and the
  early suite abort (which bypasses `FinishCase`) now clear them, next to the
  motor-drive release they already do.
- **Remote player chat no longer rides out in the reports.** `chat_roundtrip`
  and `parachute_fall_announce` put `ChatProbe.Last`, the text a remote LAN
  player typed, into `ctx.Detail`, which is flushed to the run log, the JSON
  result event, the JUnit report and `report-*.json`; all of those leave the
  machine for CI and for the vision-review upload. `ChatProbe.Last` is private
  now and the cases report `chat_len=` instead, so a failed run still says how
  much chat arrived without republishing what it said. Gated offline by
  `scripts/test_chat_probe_surface.py`.
- **A sampled exception line no longer rides a whole chat message into the
  report.** `nre_like_sample` copied each matching client-log line verbatim,
  and remote LAN chat reaches that log (R2 in `docs/THREAT_MODEL.md`), so a
  long chat line naming an exception was stored whole in `report-*.json`.
  Each sample is now cut at `playtest_log.NRE_SAMPLE_CHARS` (200), which keeps
  the exception name and its frames and drops the tail.
- **Reruns no longer answer with the previous run's state.** A run that died
  before its poll loop ended left `<logdir>/run-ended` behind, so a rerun's
  capture loop saw a stale end marker and stopped instead of photographing
  this run; the marker is cleared once the run holds the exclusivity lock.
  `<logdir>/loadgen_events.jsonl` is emptied at run start like the client and
  peer logs: the observer verdict is a whole-file read, so a rerun that never
  reached a loadgen barrier used to check CVars and buffs against the
  previous run's `joined` bot.
- **An unvalidated review could be left on disk as evidence.** The gateway
  writes `--output` before `video_review.run_review` checks the envelope, so a
  provider response that failed schema validation stayed in the clip folder
  under a `review-<provider>-<timestamp>.json` name and read as a verdict. The
  validated envelope is now what gets written, and a failure removes the
  evidence this call created (an earlier review at the same path is never
  touched). `terminal_safe` flattens control characters and truncates
  model-authored text before `review_video.py` prints it, and the human
  output now states the reported token count and that confidence is advisory.
- **CLI usage errors that read as success.** `playtest_lock.py` with no
  command printed its usage on stdout and exited 0, so a polling caller read
  that as "the shared client is free"; it now writes the usage to stderr and
  exits 2, and `live` rejects stray arguments. `playtest_repeat.sh --help`
  fell through to the orchestrator and ran three laps that could never pass;
  it now prints its own options, env vars and exit codes.
- **Host Python outside uv in the capture helpers.**
  `capture_frames.sh`, `capture_video.sh` and `capture_audio.sh` invoked
  `python3` directly for the runtime probe and the default runner while
  every other host script went through `uv run --locked`; the project needs
  >=3.11 and a distro `python3` can be older. They now name the missing tool
  instead of failing with an interpreter syntax error.
- **Cryptic coverage failure.** `coverage_badge.py` leaked a
  `CalledProcessError` traceback when `coverage` was missing or no data had
  been measured; it now says which command to run, exits 1, and no longer
  leaves `.coverage.json` behind. `report_summary.py --help` was parsed as a
  report path and reported "unreadable summary in --help"; `-h` prints usage
  and a missing file is named as an unreadable file rather than a bad
  summary.
- **`--quiet` that was not quiet.** `dst_run.py --quiet` still printed the
  start-seed and regression-replay banners, which its own help says it
  suppresses.
- **The mod dll was not reproducible across hosts.** `global.json` rolls
  forward to whatever major SDK is installed, so `<LangVersion>latest</LangVersion>`
  and `<AnalysisLevel>latest</AnalysisLevel>` picked the C# version and the
  analyzer rule set from that host's SDK: two machines built from the same
  source produced different dll bytes, and a new SDK could fail the
  `TreatWarningsAsErrors` gate on a diagnostic nobody opted into. Both are
  pinned to the recorded floor SDK (C# 12, 8.0 analysis rules).
- **`make build` failed deep in the compiler on an incomplete game install.**
  The preflight only checked for the game executable, so a missing
  `7DaysToDie_Data/Managed/Assembly-CSharp.dll` or `0_TFP_Harmony/0Harmony.dll`
  surfaced as a wall of CS0246. Both reference assemblies are named up front.
- **Targets that reached the build without their preflight or their path.**
  `playtest-review-video` was missing from `.PHONY` (a file of that name would
  make make treat it as up to date), `playtest-review-video` and
  `playtest-compare` invoke `uv` without the `require-uv` preflight, and
  `playtest-repeat` called `scripts/playtest_repeat.sh` by a path relative to
  the caller's working directory while every other target uses `$(ROOT)`.
  `make coverage` also wrote `.coverage` to the caller's directory rather than
  the repo root. `dotnet build` now runs with telemetry and the first-run
  banner off, so a build does not write first-run sentinels to `$HOME`.
## [0.13.0] - 2026-09-21



### Removed

**Breaking.** This is a pre-1.0 minor (see the release model above), so the
removals below are allowed without a major bump, but they do break an external
`IScenarioProvider` that called the removed symbols, and the entry says so
rather than shipping them as an ordinary "removed dead code" note.

Migration, by symbol:

| Removed | Use instead |
|---|---|
| `Helpers.ShowHud` | none; nothing in the tree needed it, an external provider that did must carry its own |
| `Helpers.CloseWindowGroup` | `Helpers.OpenWindowGroup` / `OpenWindowNames` (kept), or close the group from the case's own teardown |
| `Helpers.GetWaterValue` | none; nothing in the tree needed it |
| `suite_loader.suite_ids` | `suite_loader.discover_suites()` |
| `video_review.ReviewIntent.as_dict` | `dataclasses.asdict(intent)` |
| `playtest_run.read_loadgen_latest_state` | `loadgen_latest_state(read_loadgen_events(path))` |

- **Dead C# helpers.** `Helpers.ShowHud`, `Helpers.CloseWindowGroup` and
  `Helpers.GetWaterValue` had no caller in the catalog, the providers or any
  external consumer's documented surface; the public window helpers
  `OpenWindowGroup` / `OpenWindowNames` stay.
- **Dead host helpers.** `scripts/suite_loader.suite_ids` (callers use
  `discover_suites()`) and `ReviewIntent.as_dict` (equivalent to
  `dataclasses.asdict`); the dead `list` / `catalog` alias mapping in
  `Catalog.ExpandSuites` (the Runner's own list/catalog special-case covers
  it), and the two-pass streaming form of
  `playtest_run.read_loadgen_latest_state` collapsed back to
  `loadgen_latest_state(read_loadgen_events(path))`.
## [0.12.0] - 2026-09-20



### Added

- Public **PlayerSurvivability** helper (`AddSurvivabilityGuard`,
  `TryPressSpawn`, `Ensure`). The runner recovers a `PlayerGate.LivePlayer`
  case through `TryPressSpawn` (`XUiC_SpawnSelectionWindow.SpawnButtonPressed`
  only while that window is open) instead of `Respawn`/`SetAlive` alone.
  God Mode writes fly/noclip from the requested `fly` flag (default off).
  `AllowDead` / `WorldOnly` / `NoAutoHeal` are unchanged.

### Fixed

- **Spawn recovery spammed the spawn button.** `TryPressSpawn` had no
  interval of its own; WaitReady and mid-case recovery call it every
  `gmUpdate`, so `SpawnButtonPressed` ran every frame and the window never
  got a frame to act. The 2s interval now lives in `TryPressSpawn`.
- **A LivePlayer case could start on the spawn screen.** `playerOk` treated
  HP>0 as live, and `AdvanceToNextCase` moved to `RunCase` after one press.
  LivePlayer now waits until the spawn-selection window is closed, and does
  not invoke `Act` until then.
- **Runner recovery turned God Mode on for every LivePlayer case.** Combat
  and `NoAutoHeal` survival claims would then be invulnerable. Spawn
  recovery restores vitals only; God Mode stays on
  `PlayerSurvivability.Ensure` / `AddSurvivabilityGuard`.
## [0.11.0] - 2026-09-11



### Added

- A managed **parachute** suite (`suites/parachute.json`) with a safe-landing
  case, so the zdtd parachute module is driven end to end by a stock client.

### Fixed

- **Rejoin setup used the verify suite's `PLAYTEST_CASE_REFS`.** A provider
  `--rejoin-setup-suite` is a different catalog id than `--suite`. The
  setup client then filtered out its own cases and failed the verify refs
  as unimplemented (`unknown or empty suite`). Each client now gets the
  refs for the suite it is armed with, loading `{id}.json` beside
  `--suite-file` when the ids differ.
- **Rejoin did not restart the Safehouse dedicated.** The first
  `start_server` set `args.no_server = True` so mid-run polling would not
  expect a Popen handle. The rejoin `start_server(wipe=False)` then
  returned immediately, the dedicated stayed down, and the verify client
  got `Connection Failed`. Sandbox has no Popen either way;
  `note_backend_exit` already no-ops on a null handle.
## [0.10.0] - 2026-09-02

Sandbox pairs: a managed run now drives both halves from Safehouse instances,
several runs share a machine, and the stock-peer path was repaired.

**Requires** `7dtd-sandbox` >= 0.2.0.

### Added

- A managed run drives a Safehouse **pair**: `srv-<name>` and `client-<name>`.
  The client is the instance's own Windows depot under Proton, wiped to the
  same starting prefix each run and carrying exactly the mods the suite
  declared. The operator's Steam install is no longer consulted, and need not
  even be the same build: a Linux native install has no `7DaysToDie.exe` for
  Proton to launch, which is what a live run hit.
- Suites declare mods per side: `mods` for the client instance, `server_mods`
  for the server. Both default to the same set, which is this workspace's
  documented practice (7dtd-asset-pipeline's acceptance script: "the modlet
  itself must also sit in the dedicated server's Mods folder"). A suite that
  genuinely wants an asymmetric pair sets `server_mods` explicitly.

### Changed

- **Several sandbox runs can now share a machine.** The lock is scoped to the
  client a run drives, not the host: a managed run locks
  `playtest_running-<client-instance>` and probes that prefix's own
  `STEAM_COMPAT_DATA_PATH`, so runs on different instances share nothing and
  both proceed. A run on the operator's Steam client keeps the machine-wide
  lock, because that client really is shared. `PLAYTEST_LOCK_FILE` still
  overrides everything.
- Nothing on the managed path kills by pattern. `clean_processes` and the
  teardown sweep would take down a concurrent run's client and dedicated, so a
  managed run stops its own pair with `sb stop <instance>`.
- `default_live_runtime_running` is gone. It refused a run whenever any
  dedicated was up anywhere, which under per-instance port blocks blocks
  nobody. The probes are now `client_running_for_compat` (a managed run),
  `client_running` (the shared Steam client), `zdtd_running` (added wherever
  the orchestrator still starts a zdtd on a caller-chosen port) and
  `dedicated_running` (diagnostics only, never a lock input).

### Fixed

- **A stock peer ran the wrong game tree.** `launch_client.sh` defaults `GAME`
  to the operator's Steam install, and the peer was given only `COMPAT`, so it
  ran that install against a sandbox Proton prefix. On a host whose Steam copy
  is the Linux build there is no `7DaysToDie.exe` for Proton at all: the peer
  silently never started, while the suite still passed on the primary client
  alone. `peer_client_game` derives the tree beside the peer's prefix, and the
  peer gets the same windowed-720p arguments as the primary.
- **The peer raced the engine's connect rate limit.** Same-IP connects less
  than 500 ms apart are rejected, and staggering the *launches* by a second did
  not stagger the *connects*: two clients booting from identical instances
  reach the menu together, so the peer was rejected with `ConnectionRejected`.
  It now waits for the primary to be in the world
  (`Respawning: EnterMultiplayer`, a client-side marker; the server's
  `PlayerSpawnedInWorld` never appears in a client log and waiting on it timed
  out every run), with the one-second sleep kept as a floor.

### Known limitation

- One server with several clients is **not yet proven**. With both fixes the
  peer starts correctly and is spaced past the rate limit, but a suite that
  does not wait for it (`smoke` finishes its five cases immediately) tears the
  run down before the peer finishes booting.
- The `mp` suite, which exists for exactly this, was run against a sandbox pair
  and failed differently: `timeout after 1292s waiting for DONE`, with the
  *primary* client never reaching `Respawning: EnterMultiplayer` at all while
  the peer did auto-join. That is a suite-semantics problem rather than the
  peer plumbing this release fixed, and it is unexplored.
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

[Unreleased]: https://github.com/hordeforge/7dtd-playtest/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/hordeforge/7dtd-playtest/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/hordeforge/7dtd-playtest/releases/tag/v0.10.0
[0.9.0]: https://github.com/hordeforge/7dtd-playtest/releases/tag/v0.9.0
[0.7.2]: https://github.com/hordeforge/7dtd-playtest/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/hordeforge/7dtd-playtest/releases/tag/v0.7.1
