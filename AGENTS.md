# AGENTS.md - 7dtd-playtest

Stock-client **gameplay automation** against real servers. Drive stock APIs
and assert observable state. Prefer missing over fakes.

Tier 2 of the workspace testing stack
([ADR 0001](https://github.com/hordeforge/.github/blob/main/docs/adr/0001-test-tiers-and-declarative-suites.md)).
Two axes, not one fused target:

| Axis | Values | Meaning |
|---|---|---|
| `--provision` (`PLAYTEST_PROVISION`) | `managed` \| `attach` | Safehouse brings the server up and tears it down, or it is already running and this repo touches no lifecycle. `--no-server` is the shorthand for attach |
| `--server` (`PLAYTEST_BACKEND`) | `stock` \| `zdtd` | which server is under test (`PLAYTEST_BACKEND` only pins it against a suite document's `backend`) |
| `--readonly` | attach-only flag | the host must never be written to (production `7dtd-server-container`) |

A managed stock run is always a Safehouse **pair**: the server instance
`srv-<name>` and the client instance `client-<name>`. Both come from `sb`, so
this repo does not open a serverconfig, allocate a port, exec a dedicated or
reach into a game install: `sb up` / `sb stage` / `sb render-config` / `sb stop`
do that. The client is the instance's own Windows depot under Proton, not the
operator's Steam install, which may be a different build entirely.

A suite declares its mods per side: `mods` for the client instance,
`server_mods` for the server. Both default to the same set, which is this
workspace's documented practice ("the modlet itself must also sit in the
dedicated server's Mods folder"). Set `server_mods` explicitly for an
asymmetric pair. Declarative suites under
`suites/*.json` say what runs and where; C# `IScenarioProvider` / Catalog own
the case `ref` implementations. Fresh save is a hard rule for a managed run
(no reuse-save path); an attach run does not own the save it joins and must
declare `fresh: false`.

Canonical modding guide: [MODDING_BEST_PRACTICES.md](https://github.com/hordeforge/.github/blob/main/MODDING_BEST_PRACTICES.md)

Workspace: [`hordeforge/.github` AGENTS.md](https://github.com/hordeforge/.github/blob/main/AGENTS.md).
Design: [`../zdtd-server/docs/CLIENT_PLAYTEST.md`](../zdtd-server/docs/CLIENT_PLAYTEST.md).
Join plumbing: [`../7dtd-fastconnect/`](../7dtd-fastconnect/).
Lab isolation: [`../7dtd-sandbox/`](../7dtd-sandbox/).
Production host: [`../7dtd-server-container/`](../7dtd-server-container/).

## Owns

- Client mod `Mods/7dtd-playtest` (scenario runner, oracles, structured logs)
- Host orchestrator (`scripts/playtest_run.py`) and make targets
- Target adapters (`scripts/playtest_targets.py`) and declarative suites
  (`suites/`, `schema/suite.schema.json`, `scripts/suite_loader.py`)
- Stock-fidelity suite definitions (smoke, core, later combat/economy)
- The exclusivity lock (`scripts/playtest_lock.py`) and the run report

## Does not own

- Instance isolation, ports, serverconfig or `serveradmin.xml` rendering, mod
  staging, bring-up, teardown (**7dtd-sandbox** / Safehouse). There is no
  serverconfig writer or dedicated-server exec in this repo, and no blanket
  `pkill 7DaysToDieServer`: that pattern reaches every other sandbox instance
  on the machine.
- IP connect / intro skip (that is **7dtd-fastconnect**)
- Server implementation (zdtd) or production deploy (**7dtd-server-container**)
- Load volume bots (**7dtd-loadgen**; demand only, not the world under test)
- Mod-local playtest cases (stay in the owning mod, see below)
- Inventing world/chunk/sign/inventory S2C to keep tests green

## Rules

1. **EAC off** (any C# client mod).
2. **Drive and assert only.** No local terrain/deco generation, no fake packages,
   no Harmony that swallows protocol NREs or replaces missing server data.
3. Prefer public stock gameplay APIs over private field hacks.
4. Cases that only prepare state must be labeled setup in comments; under-test
   steps must wait on real predicates when the claim is server fidelity.
5. Package ids / wire gaps are **server** bugs: open zdtd work, do not patch client.
6. Host Python via **`uv`** only. Secrets via env.
7. No em dashes. No AI attribution.
8. Name for what it does (suite ids, case ids, env vars).
9. **Exclusive live client** (see below). Only one orchestrated playtest (or
   other exclusive client drive) at a time on this machine.
10. **Every managed run starts on a fresh save: a hard rule, no opt-out.**
    `sb wipe` resets the instance (stock) or `fresh_zdtd_world` the zdtd world
    before each run. A reused world is a reused set of registered blocks, item
    ids and chunk state, so a dig/place suite then measures the previous run's
    terrain or stale blocks rather than this run. Do not add a
    `--reuse-save`/`FRESH=0` path; `--fresh-save` exists only as a no-op
    back-compat flag. The one exception is the rejoin flow's restart, which
    verifies persistence and therefore keeps the save its setup phase wrote
    (`start_server(wipe=False)`).
11. **A declared case ref must resolve, and a declared suite must declare all
    its cases.** The orchestrator hands `PLAYTEST_CASE_REFS` to the client and
    `Runner` keeps only cases whose `catalog.SUITE.CASE` ref appears there, so
    a case added to `Catalog.cs` and declared in no suite is inert rather than
    a case riding along with someone else's suite.
    `scripts/test_suite_refs.py` fails on both offline.
12. **Never pkill a dedicated by pattern.** `GAME_PROC_PATTERNS` is client-only
    on purpose. A managed dedicated is stopped with `sb stop <instance>`, which
    matches that instance's own `SB_INSTANCE`; the pattern form takes down
    every other sandbox instance on the machine, another agent's run included.
13. **`--readonly` means it.** Against a production `7dtd-server-container`
    host: no wipe, no mod staging, no config rewrite, no restart, no deploy.

## Playtest / live-client exclusivity

**The lock covers one client, not the machine.** It is scoped to the client a
run actually drives, so several sandbox runs can share a host.

- A **managed** run drives its own Safehouse client instance: its own game
  tree, Proton prefix, window and port block. Its lock file is
  `playtest_running-<client-instance>` and its live probe matches that
  prefix's `STEAM_COMPAT_DATA_PATH`. Two runs on different instances share
  nothing and both proceed; two on the same instance collide.
- A run on the **operator's Steam client** keeps the machine-wide lock, because
  that client is genuinely shared: one install, one display.
- `PLAYTEST_LOCK_FILE` still overrides everything, so a shared-machine
  convention that points several stacks at one file keeps working.

Nothing on the managed path kills by pattern. `clean_processes` and the
teardown sweep would take down a concurrent run's client and dedicated, so a
managed run stops its own pair with `sb stop <instance>` instead.

A managed stock run's dedicated is *not* in the lock. It belongs to a Safehouse
instance that holds a unique name-derived port block and refuses a second start
of itself (`sb up`), and teardown is `sb stop <instance>` rather than a pattern
pkill. Two server-only runs on disjoint instances are meant to be able to
proceed, and the lock no longer stands in the way of that; what still
serialises them is the one client, whenever a run drives one.

`zdtd` is the exception. The orchestrator still starts it itself on a port the
caller chose, so a managed zdtd run gates on `client_or_zdtd_running` rather
than the client alone.

| Probe | What it means | In the lock gate |
|---|---|---|
| `client_running_for_compat` | a client is up against *this* Proton prefix | a managed run |
| `client_running` | any stock/Proton client is up | a run on the shared Steam client |
| `zdtd_running` | a zdtd server is up | added for a managed zdtd run |
| `dedicated_running` | a stock dedicated is up, on any instance | never (diagnostics only) |

### Lock file

Default path (live state; not committed):

```text
~/.cache/7dtd-playtest/playtest_running
```

Override with **`PLAYTEST_LOCK_FILE`** (same env name as Atomic / 7dtd-mods
helpers). Point both stacks at one path on a shared machine (for example a
monorepo `.local/playtest_running`) so there is not a free-looking second lock.

### Format (parseable key=value)

```text
running=yes
session=grok-20260810-231500-a1b2c3d4e5f6
acquired=2026-08-10T15:00:00Z
heartbeat=2026-08-10T15:00:30Z
```

When free: `running=no` (omit session / timestamps).

| Field | Meaning |
|---|---|
| `session` | Holder id (`<agent>-<UTC YYYYMMDD-HHMMSS>-<hex>`) |
| `acquired` | UTC ISO8601 when this hold started |
| `heartbeat` | UTC ISO8601 last refreshed while the run is still active |

`playtest_run.py` refreshes `heartbeat` about every 30s
(`PLAYTEST_LOCK_HEARTBEAT_SEC`). Generate session with
`playtest_lock.new_session_id(prefix)` or `--session` / `PLAYTEST_SESSION_ID`.

### Fresh vs stale

- **Fresh:** `running=yes` and `heartbeat` age ≤ `PLAYTEST_LOCK_STALE_SEC`
  (default **120**). Holder is still alive → **wait**, do not start.
- **Stale:** heartbeat missing, older than the stale window, or dated
  further ahead of the reader's clock than the window (a clock step or a
  restored backup is not a fresh heartbeat). Often a crashed
  agent that never released. Orchestrator may **take over** only if there is
  also **no** live client process. If a client is still up, do not clear the
  lock; stop and record the mismatch (`stale_but_live`).

Inspect: `cat` the lock file (the format above is the contract), or ask the
shipped CLI, never a `python -c` one-liner:

```bash
python3 scripts/playtest_lock.py live   # exit 1 = the shared client is up
python3 scripts/playtest_lock.py wait   # block until a new session could acquire
```

Do not parse `running=` / `heartbeat=` in a consumer.

### Acquire / release / process check

1. **Before** `clean_processes`, client launch, or any exclusive live-client
   work: acquire the lock with your session id.
2. If `running=yes` for another **fresh** session → **do not start**. Read
   `session=` and `heartbeat=` for the holder. Exit non-zero (harness code **2**).
3. If a live **client** is present and you do not already hold the lock → **do
   not start**, even when the file says free or looks stale (`7DaysToDie.exe` /
   Proton wrappers). A stock dedicated does not block you: it belongs to an
   instance with its own ports. A `zdtd` does block a managed zdtd run, which
   starts one on a caller-chosen port.
4. Only the lock holder may clean leftover game processes before launch
   (`playtest_run.py` acquires first, then cleans client **and** dedicated).
5. On finish or failure after acquire: release (`running=no`) if you still own
   the lock. Heartbeat thread stops before release.
6. After clean, orchestrator also refuses if ServerPort / telnet port is still
   bound (leftover outside pkill patterns).

### Two ways agents have broken this

- **Never read the lock and write it in separate steps.** An agent that printed
  `running=yes` and then wrote its own claim in the same command clobbered a
  hold that had started 10 s earlier, killing that playtest. Acquire only
  through `playtest_lock.acquire()`, which serializes on flock and refuses a
  fresh foreign claim; a shell read-then-write has no such window closed.
- **A heartbeat only lives as long as its process.** Backgrounding a refresh
  loop with `nohup ... &` from a tool call that then returns leaves the loop
  dead and the hold going stale under a still-running client, the
  `stale_but_live` mismatch. Either hold the lock from a process that outlives
  the run (`playtest_run.py` does), or refresh `heartbeat` explicitly from each
  step of a manual session.

`playtest_lock.py live` exits 1 when the shared client is up, 0 when it is
free; a dedicated is not consulted.

`scripts/playtest_run.py` / `make playtest*` enforce acquire/heartbeat/release.
Manual client starts still require agents to hold the same file and refresh
heartbeat (or release promptly). This is **client exclusivity only**, not task
ownership in a project TODO.

Shipped module: `scripts/playtest_lock.py` (unit-tested; flock-serialized
acquire + heartbeat). Its concurrency, crash, and corruption behaviour is
covered by deterministic simulation - see [DST.md](DST.md) and `make dst`.
Everything the lock cannot reproduce (clock, entropy, pid, disk, the
cross-process mutex) is injected through `playtest_lock.LockEnv`; do not
reintroduce a direct `time.time()`, `secrets`, or `Path.write_text` call in
that module, or the simulation stops covering it.

## Commands

```bash
make test                 # offline gates (lint + typecheck + suites), no game install needed
make lint                 # ruff + shellcheck over scripts/, yamllint over .github/
make typecheck            # mypy over scripts/ ([tool.mypy])
make test-one GATE=test_dst.py   # run one gate while iterating
make check                # exactly what CI runs: test + dst DST_SEEDS=200
make install              # build + install playtest mod
make install-pair         # playtest + connect
make package              # reproducible release zip from dist/ ([PACKAGE=path])
make playtest-smoke       # stock dedicated + smoke (exit 0/1/2)
make playtest-core        # stock dedicated + gate alias (live-only smoke+core)
make playtest-zdtd        # demo suite against zdtd (port 27025)
make sbom                       # CycloneDX inventory of both committed lockfiles
make package                    # release zip: dist/7dtd-playtest-<version>.zip (needs GAME=)
make verify-reproducible        # build + package twice from a clean tree, compare the zips (needs GAME=)
make playtest-review-video SUITE=<id> INTENT=<path>  # capture staged clips, then vision-review them through deadeye
make playtest SUITE=core SERVER=stock       # managed Safehouse instance
make playtest SUITE=smoke PROVISION=attach READONLY=1  # live host, attach-only
```

A mod repo runs its own cases without a wrapper script: keep the provider and
the suite JSON beside the mod, then

```bash
uv run scripts/playtest_run.py --suite-file ../7dtd-fps-bots/playtest/suites/bot_engage.json
```

The suite's `mods` list names what gets staged (a short name resolves to a
sibling repo's `dist/`, anything else is a path relative to the suite file).
`load_external_suite` refuses a suite id that shadows a built-in
stock-fidelity suite.

External scenario providers whose cases emit host barriers pass
`--host-fixtures` to `scripts/playtest_run.py`. Built-in fixture suites are
recognized automatically. `--no-fixtures` remains the overriding opt-out.

## Env (client)

| Var | Meaning |
|---|---|
| `PLAYTEST_SUITE` | Canonical suite list / aliases (`smoke`, `core`, `demo`, …) |
| `PLAYTEST_PROVISION` | Who owns the server process: `managed`/`attach` (or `--provision`) |
| `PLAYTEST_BACKEND` | Not a selector: setting it pins the backend so a suite document's `backend` no longer overrides it |
| `PLAYTEST_READONLY` | Attach-only: never write to this host (or `--readonly`) |
| `PLAYTEST_SANDBOX_NAME` | Safehouse pair base name (creates `srv-<name>` / `client-<name>`) |
| `PLAYTEST_SANDBOX_ROOT` | Safehouse checkout that owns the instances (or `--sandbox-root`) |
| `PLAYTEST_LOCK_FILE` | Override the lock path; without it a managed run locks per client instance |
| `PLAYTEST_CASE_REFS` | Set by the orchestrator for the suite id that client is armed with (rejoin setup loads `{id}.json` beside `--suite-file` when the ids differ) |
| `PLAYTEST_SUITE_FILE` | Optional declarative suite JSON path (or `--suite-file`) |
| `ZDTD_PLAYTEST_SUITE` | Accepted alias of `PLAYTEST_SUITE` (older Atomic hosts) |
| `PLAYTEST=1` / `ZDTD_PLAYTEST=1` | Legacy: arms `demo` |
| `PLAYTEST_LAPS` / `ZDTD_PLAYTEST_LAPS` | Benchmark repeats |
| `7DTD_CONNECT` | Set by orchestrator / connect |
| `PLAYTEST_LOCK_FILE` | Override exclusivity lock path (default under `~/.cache/7dtd-playtest/`) |
| `PLAYTEST_SESSION_ID` | Lock holder session id (or `--session`; auto-generated if empty) |
| `PLAYTEST_LOCK_STALE_SEC` | Heartbeat age after which a lock is stale (default 120) |
| `PLAYTEST_LOCK_HEARTBEAT_SEC` | How often the orchestrator refreshes heartbeat (default 30) |
| `PLAYTEST_TELNET_PASSWORD` | Stock dedicated telnet password (unset: ephemeral per-run secret rendered into the instance config; an attach run requires an explicit value) |
| `PLAYTEST_CLIENT_LOG` | Client log the run and `scripts/capture_*.sh` watch. Unset resolves through `COMPAT`, then the discovered client install; `--print-client-log` prints it and exits, so no helper carries a hardcoded Steam root |

`residual` expands to `mp,soak` only. `make playtest-residual` is a **different**
multi-target host gate (persist + mp + apm + soak_long). See README.

## Log contract

**Host transcript** lines prefixed `[playtest-orch]` (do not reorder the
fields): `[playtest-orch] <UTC ISO8601 Z> t+<elapsed>s session=<id> <msg>`
(`warn:` after the fields on stderr). The timestamp, the elapsed offset from
the monotonic clock and the lock session are what make a captured CI
transcript pivotiable to `report-<epoch>.json` and the lock file; the same
`session` and `run_end_reason` are fields in that report. A line emitted
before the run binds its session (argparse-level refusal, harness crash at
import) carries the timestamp and nothing else.

**Stable** lines prefixed `[7dtd-playtest]` (do not rename tokens):

- Human: `PASS|FAIL|SKIP suite/case detail`
- Barrier: `barrier <name>` (host greps for telnet/admin phases).
  `spawn_vehicle:<entityClass>` asks the host for one vehicle of that class
  (the bare `spawn_vehicle` spawns a bicycle); client-created vehicles are
  unknown to a dedicated server and cannot be driven there. A handler that
  wants a *state* rather than one more entity reads it first (`bot list`) and
  acts only on the deficit, so a barrier emitted twice leaves the world as one
  emission left it. `bot spawn 1` adds a bot and must stay behind such a read.
- Staged frame: `scene staged <name> <detail>` (`Report.Staged`). Emitted the
  moment a scene is on screen, for an external screenshot loop to key on. A
  case detail is flushed with its *result*, tens of seconds later, so a loop
  waiting on the result photographs the disconnect dialog instead. A suite
  proves data, never appearance; see "Visual confirmation" in the README.
  `scripts/capture_frames.sh` is the supported loop that waits for it. Do not
  write a per-project screenshot loop keyed on bespoke wording.
- JSON: `{"v":1,"t":"result|summary|done|log|barrier|staged",...}`
- Terminal: `SUMMARY ...` then `DONE exit_hint=0|1`
- Run ended: `<logdir>/run-ended` written when the orchestrator's poll loop
  ends, containing the reason on one line: `done`, `timeout`, `client_exit`,
  or `lock_lost` (heartbeat saw a foreign holder take the claim; the run
  aborts instead of sharing the machine). The marker is cleared once the run
  holds the exclusivity lock, so its presence always means *this* run ended
  and a run killed before its poll loop ends cannot poison a rerun. This is
  the deterministic end of
  the run for consumers that
  key on the staged marker (a screenshot loop exits when it appears instead
  of waiting out its own timeout). `--logdir` defaults to `$LOGDIR` or
  `~/.cache/7dtd-playtest`.

Public API for external providers: `CaseDef.Live`/`Staged`/`Defer`, `Helpers`,
`Report`, `MiningSpec`/`MiningProbe`/`MiningResult`,
`PlayerSurvivability`/`AddSurvivabilityGuard`/`TryPressSpawn`. A capability that more
than one consumer needs (real mining, staged frames, the exclusivity lock)
belongs **here**, not in the consumer. Visual evidence uses `CaseDef.Staged`. Never hand-roll the
marker/hold/assert triple, that is what made every screenshot loop grep a
different sentence.

### Text at the boundaries

Every byte that enters this repo is UTF-8, and every boundary says so:
`read_text(encoding="utf-8")`, `subprocess.run(..., encoding="utf-8",
errors="replace")` (`text=True` alone decodes with the process locale and
raises on the first non-ASCII byte, which a C-locale service or CI job
guarantees). Detail text reaches the log from remote LAN players, block and
item names, and a verdict from the deadeye gateway, so it is not ASCII by
assumption: the JSON encoder, the JUnit escaper (which also drops the
characters XML 1.0 cannot represent) and the telnet reader all take it as it
arrives. A file that will not decode is a load error naming the file, not a
traceback past the caller's `SuiteLoadError` / `TargetError` handler.

Lines are split with `playtest_log.split_log_lines`, never `str.splitlines()`.
`splitlines()` also breaks on U+000B, U+000C, U+001C-U+001E, NEL, U+2028 and
U+2029, none of which the game's logger emits: a peer who types one of those
inside a single chat message had its tail promoted to a line of its own, and
the contract-line anchor (which holds against CR/LF only) then accepted it as
a genuine emission. The same split backs the redaction of a peer's chosen
character name and the AI/player classification of a `listents` reply, so it
is one call rather than a habit per parser. A limit stated in characters is
a code point, not a grapheme: a truncation that would cut a cluster backs
over the combining mark, joiner or lone flag half it lands in
(`video_review._truncate_at_cluster`).

### One concern per run. Do not mix tests.

A playtest invocation proves **one concern**. Do not pile unrelated cases
into one `PLAYTEST_SUITE` because the client is already up. A person
watching cannot tell which picture they are signing off, and shared
world/inventory state makes a borrowed case change every case after it.

Two things **are** one concern, and belong together:

- consecutive actions of one feature (equip, then use, then capture; place
  the bomb, then arm the detonator, then watch the fuse)
- a child that is already **part of** the built object (a particle system
  on the entity prefab, not spawned as a second instantiate next to it)

Two things that are **not** one concern, and must be separate invocations:

- a placed block on a voxel, and a prefab hanging in the player's face
- a load-only mechanical case, and a staged visual of a different asset
  "so there is something to photograph"

Look-versus-block is the form the harness can gate by name. A suite that
hangs a prefab in the player's face ends in `_look`; a suite that
`SetBlockRpc`s or has the player place a block contains `_block_`.
`playtest_run.mixed_visual_suites` refuses both in one list. The name **is**
the picture: putting `Object.Instantiate` / `CaseDef.Staged` of a floating
prefab into a suite that is **not** named `*_look`, so it can ride along
with `*_block_*`, is the mix with the gate turned off.

The general rule is not limited to those suffixes. Do not comma-list
unrelated features. Do not smuggle a second picture into a suite whose
name does not say so. Do not instantiate two look-prefabs at the same
world point: `CaseDef.RegisterStaged` on every camera-staged instance so
`ClearStaged` can destroy it before the next hold. Consumers
(7dtd-asset-pipeline) generate **one look suite per prefab** and refuse
an undeclared comma-list. Point a block look at the voxel
(`Helpers.LookAt`). Run a floating-prefab look as its own invocation.

How to run it:

```bash
make playtest SUITE=one_id
```

The only undeclared multi-id list the harness accepts is `smoke,core`.
Any other comma-list must be declared as exactly one concern (consecutive
steps of one feature), via `--concern-suites` or `PLAYTEST_CONCERN_SUITES`,
with the **same tokens** as `--suite` / `PLAYTEST_SUITE`. Look plus block
cannot be declared. Unrelated features: separate invocations, not one list.
README "Visual confirmation" has the `RegisterStaged` sample.

## Offline gates (no game install)

`make test` runs lint + typecheck plus every offline gate file in the Makefile
`GATES` list on every push (CI: `.github/workflows/ci.yml`). The analysis gates
come first and are blocking:

0. ruff over `scripts/`, shellcheck over the bash helpers and yamllint over
   the shipped workflow YAML (`.github/`, config in `.yamllint.yml`), all
   three under `make lint` with `[tool.ruff]` in pyproject.toml, plus mypy
   over `scripts/` (`make typecheck`, `[tool.mypy]`); ruff and mypy are
   pinned in the dev dependency-group so local and CI versions match
   uv.lock. A host with no `yamllint` runs `make lint SKIP_YAML=1`; CI never
   sets it. A ruff rule group lands in `select` only after a run proves the
   tree clean.

Then the gates, grouped by the surface each pins (`GATES` in the Makefile is
the run order that both `make test` and `make coverage` expand):

1. catalog<->SCENARIOS surface (`scripts/test_catalog_surface.py`): live rows
   + counts total must equal Catalog.cs. A catalog addition that skips
   SCENARIOS.md fails CI. It pins both directions of the barrier contract: a
   suite that emits a fixture-serviced barrier must be listed in the host's
   `FIXTURE_SUITE_IDS`, and every barrier Catalog emits must be a name the
   host's `BARRIER_NAMES` routes (an unrouted one makes its case hang rather
   than fail). The Catalog.cs readers both this gate and gate 17
   need live in `scripts/catalog_surface.py`, so a gate imports a parser
   rather than another gate.
2. mod version surface (`scripts/test_version_surface.py`): ModInfo.xml ==
   ModIdentity.Version == dist manifest, every visible `vX.Y.Z` tag has a
   CHANGELOG entry (units in `scripts/test_version_surface_units.py`), every
   `### Removed` section carries the `**Breaking.**` marker the pre-1.0 policy
   in CHANGELOG.md requires and a table naming the replacement, no release
   repeats an impact heading, and CHANGELOG.md must carry an [Unreleased]
   section plus the current release entry. Tags are read from the git common
   dir, so a linked worktree checks coverage instead of reporting it
   inapplicable. It also pins the uv the workflows
   install (`UV_VERSION`) to the `[tool.uv] required-version` floor in
   `pyproject.toml`, so a bump that moves one and not the other fails here.
3. scenario-provider env surface (`scripts/test_scenario_provider_surface.py`)
3z. documented provider surface (`scripts/test_readme_surface.py`): every
    `Helpers.` / `CaseDef.` / `Report.` / `PlayerSurvivability.` call the README
    snippets show resolves to a public member of the mod assembly, every named
    argument in those snippets is a real parameter, and every `new MiningSpec
    { … }` field is a real public field. The README is the provider SDK's only
    reference, so a snippet that names a member the dll does not export is a
    doc bug an external author only discovers in their own build.
3a. chat-probe surface (`scripts/test_chat_probe_surface.py`): the text a
    remote LAN player typed stays inside `ChatProbe`. A case detail reaches
    the run log, the JSON result event, the JUnit report and
    `report-*.json`, all of which leave the machine, so the captured text
    has no public accessor and the chat cases report `chat_len=` only.
4. mining-probe provider surface (`scripts/test_mining_probe_surface.py`)
5. player-survivability surface (`scripts/test_player_survivability_surface.py`):
   the god-mode and spawn-recovery contract the combat and `NoAutoHeal` cases
   rely on, pinned against the runner arming it outside a case that asked.
6. stock-peer orchestration surface (`scripts/test_stock_peer_client.py`)
7. host lock (`scripts/test_playtest_lock.py`)
8. deterministic simulation (`scripts/test_dst.py`)
9. orchestrator local-init order gate (`scripts/test_no_unbound_locals.py`):
   catches the read-before-assignment crash class that once shipped in
   `playtest_run.py` main(); only fires with real game binaries present. Every
   host module under `scripts/` is in scope, taken from the directory, so a new
   script is checked the day it lands rather than when someone remembers.
10. orchestrator report/log surface (`scripts/test_report_surface.py`): JUnit
   and serverconfig XML attribute escaping plus parser survival on malformed
   JSON events, plus `scripts/report_summary.py` failing closed on a hostile
   or malformed lap summary. Seeded grammar fuzzers drive the log parser, the
   JUnit renderer, the loadgen event reader and the lap counter, asserting
   structural invariants and determinism under input doubling, not just
   absence of a crash.
11. orchestrator pure-logic units (`scripts/test_playtest_run_units.py`):
   fresh-save removes only every world's copy of the named game save
   (quarantined under `<logdir>/quarantine`, newest 5 kept, never
   hard-deleted).
11b. quarantine restore (`scripts/test_quarantine_restore.py`): every
   quarantined move records `{src, dest}` in the entry's `restore.jsonl`, and
   `scripts/quarantine_restore.py` puts a swept-aside world back from it (dry
   run without `--apply`, never clobbers an existing original). A quarantine
   is the only copy of what a run moved aside, so a regression here is
   unrecoverable data, not a wrong verdict.
12. compare diff (`scripts/test_playtest_compare.py`, pytest via uv), which
   also pins that the committed baselines under `workspace/comparison-playtest/`
   are published by atomic rename.
13. capture-clip marker surface (`scripts/test_capture_video_surface.py`):
    the `scene staged` line `capture_frames.sh` keys on parses the clip id
    from the trailing directory, CRLF-safe.
14. video-review surface (`scripts/test_video_review.py`): intent parsing and
    the deadeye review runner fail closed on malformed input.
15. playtest target adapters (`scripts/test_playtest_targets.py`): resolve /
    apply / report fields for `stock|sandbox|attach|zdtd|live`, Safehouse
    path defaults, and missing-`sb` failure.
16. declarative suite loader (`scripts/test_suite_loader.py`): `suites/*.json`
    discover/load/report, and every contradiction refused: a managed run that
    is not fresh, an attach run that claims to be, an attach run carrying a
    `server` block or `mods` list it does not own, `readonly` outside attach,
    an external suite shadowing a built-in id, an unknown key anywhere but the
    `server` block (a misspelled `provision` or `readonly` reads as unset, and
    unset means a managed run that wipes the host), and a `server` block that
    declares the admin plane (`TelnetEnabled`, `TelnetRemoteAllowedIPs`,
    `TelnetPassword`, in any capitalisation: the orchestrator owns those, and a
    suite-declared password would be echoed into the run report in plaintext). A seeded grammar fuzzer drives
    hostile suite documents (and raw/truncated/non-UTF-8 file bytes): each one
    either fails closed with `SuiteLoadError` or, when accepted, holds every
    cross-field rule and round-trips file -> `SuiteDoc` -> run report -> doc.
17. declared-ref surface (`scripts/test_suite_refs.py`): every `ref` in
    `suites/*.json` resolves to a real Catalog case, every declared suite
    declares all its cases, the `catalog.SUITE.CASE` format is pinned on both
    sides (loader and `Runner.CaseRef`), and every catalog suite is either
    declared or listed in `UNDECLARED_SUITES`.
18. gate-list surface (`scripts/test_gate_list.py`): every `scripts/test_*.py`
    is listed in the Makefile `GATES`, every `GATES` entry exists and is
    listed once, every `test_*` a gate defines is named by that gate's runner
    (an unregistered test never executes), `test`/`coverage`/`test-one` share
    that one list, CI runs the same steps `make check` does, and every
    `make <target>` a workflow names (step or backticked comment) is a
    Makefile target. A new gate
    file that nobody added to `GATES` runs under neither `make test` nor CI.
19. dependency inventory (`scripts/test_dep_sbom.py`): the CycloneDX SBOM
    `make sbom` writes from `uv.lock` and `Source/PlayTestMod/packages.lock.json`
    lists every package both lockfiles resolve, carries the locked version in
    each purl, marks nothing `required` (pyproject declares no runtime
    dependency and the csproj reference is `PrivateAssets="All"`), and derives
    a content-hash serial number. A dependency added without regenerating the
    lock, or a lockfile that stopped describing the tree, fails here offline.
20. client-side path surface (`scripts/test_windows_path_surface.py`): the
    host is Linux-only, but the mod runs on the client, which is a Windows
    process under Proton. It pins what that filesystem accepts for a staged
    frame or clip path: no empty name (which collapses onto the parent
    directory), no reserved device name (`AUX`, `nul`, `COM1`...`LPT9`, any
    casing, extension or not), and no path assembled from a hardcoded
    separator. Read from the shipped C# because the mod cannot be compiled
    offline.
21. release-archive surface (`scripts/test_mod_package.py`): the zip
    `make package` attaches to a tag is a function of the built mod, not of
    the machine that built it. The gate runs the real CLI over a stand-in
    dist and reads the bytes back: the entry set is the two files
    `make install` copies (a `.deps.json` left in `dist/` does not ship),
    order is sorted, and every entry carries one timestamp, mode 0644 and
    create_system 3, with `SOURCE_DATE_EPOCH` honored and the zip epoch as
    the fallback. Two runs with different source mtimes and timezones hash
    the same.
22. coverage-badge surface (`scripts/test_coverage_badge.py`): the number CI
    publishes into the README is only as honest as its renderer. The gate
    drives the shipped script: each colour band's inclusive lower bound, the
    rounding of the measured total, the `0` / `1` / `2` exit codes its own
    usage line promises, and the `.coverage.json` scratch, which must be gone
    after a read that failed as well as after one that succeeded. Only the one
    thing a test cannot have, a measured `.coverage` file, is substituted.
23. mod build surface (`scripts/test_build_surface.py`): the mod cannot
    compile on a hosted runner, so the build config has no CI job and every
    property that makes its output trustworthy is one nobody notices losing.
    The gate reads the committed config: `RestorePackagesWithLockFile` plus
    `-p:RestoreLockedMode=true` on the recipe, every `PackageReference` an
    exact range the committed `packages.lock.json` already resolved with a
    content hash, `Deterministic` / `PathMap` /
    `EnableSourceControlManagerQueries=false` / a numeric `LangVersion`,
    Release output under `dist/`, the `AssemblyName` the file both shipping
    paths copy, `make install` and `scripts/mod_package.py` shipping one list,
    the `SOURCE_DATE_EPOCH` / `LC_ALL=C` / `TZ=UTC` the build recipe pins, and
    an exact `global.json` SDK whose `rollForward` stays inside the band.

CI also runs a wider seed sweep with `make dst`. The mod build itself is not
CI-able (game DLLs).
