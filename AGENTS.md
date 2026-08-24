# AGENTS.md - 7dtd-playtest

Stock-client **gameplay automation** against real servers: the **stock
dedicated** (default target) and **zdtd-server**. Drive stock APIs and assert
observable state. Prefer missing over fakes.

Canonical modding guide: [MODDING_BEST_PRACTICES.md](https://github.com/hordeforge/.github/blob/main/MODDING_BEST_PRACTICES.md)

Workspace: [`../AGENTS.md`](../AGENTS.md).  
Design: [`../zdtd-server/docs/CLIENT_PLAYTEST.md`](../zdtd-server/docs/CLIENT_PLAYTEST.md).  
Join plumbing: [`../7dtd-fastconnect/`](../7dtd-fastconnect/).

## Owns

- Client mod `Mods/7dtd-playtest` (scenario runner, oracles, structured logs)
- Host orchestrator (`scripts/playtest_run.py`) and make targets
- Suite definitions (smoke, core, later combat/economy)

## Does not own

- IP connect / intro skip (that is **7dtd-fastconnect**)
- Server implementation (zdtd)
- Load volume bots (7dtd-loadgen)
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

## Playtest / live-client exclusivity

There is one shared stock **client** and, for host orchestration, typically
one **stock dedicated** (default port 26900) or **zdtd** server. Agents and
hosts must not start a second playtest while another holds that runtime.

`make playtest` / `scripts/playtest_run.py` **do start a dedicated server**
unless `--no-server`. That is inside the same exclusivity lock as the client:
two concurrent orchestrators mean duplicate servers, port fights, and
`clean_processes` killing the other run.

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
- **Stale:** heartbeat missing or older than the stale window. Often a crashed
  agent that never released. Orchestrator may **take over** only if there is
  also **no** live client process. If a client is still up, do not clear the
  lock; stop and record the mismatch (`stale_but_live`).

Inspect: `cat` the lock file, or from the repo root
`PYTHONPATH=scripts python3 -c "import playtest_lock as p; print(p.read_lock())"`.
Wait until a new session could acquire (missing heartbeat is stale):
`python3 scripts/playtest_lock.py wait`. Do not parse `running=` /
`heartbeat=` in a consumer.

### Acquire / release / process check

1. **Before** `clean_processes`, client launch, or any exclusive live-client
   work: acquire the lock with your session id.
2. If `running=yes` for another **fresh** session → **do not start**. Read
   `session=` and `heartbeat=` for the holder. Exit non-zero (harness code **2**).
3. If a live **runtime** process is present and you do not already hold the
   lock → **do not start**, even when the file says free or looks stale:
   - client: `7DaysToDie.exe` / Proton wrappers
   - server: `7DaysToDieServer.x86_64` (stock dedicated) or `zdtd`
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
  dead and the hold going stale under a still-running client — the
  `stale_but_live` mismatch. Either hold the lock from a process that outlives
  the run (`playtest_run.py` does), or refresh `heartbeat` explicitly from each
  step of a manual session.

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
make lint                 # ruff over scripts/ ([tool.ruff])
make typecheck            # mypy over scripts/ ([tool.mypy])
make test-one GATE=test_dst.py   # run one gate while iterating
make check                # exactly what CI runs: test + dst DST_SEEDS=200
make install              # build + install playtest mod
make install-pair         # playtest + connect
make playtest-smoke       # stock dedicated + smoke (exit 0/1/2)
make playtest-core        # stock dedicated + gate alias (live-only smoke+core)
make playtest-zdtd        # demo suite against zdtd (port 27025)
make playtest SUITE=core SERVER=stock
```

External scenario providers whose cases emit host barriers pass
`--host-fixtures` to `scripts/playtest_run.py`. Built-in fixture suites are
recognized automatically. `--no-fixtures` remains the overriding opt-out.

## Env (client)

| Var | Meaning |
|---|---|
| `PLAYTEST_SUITE` | Canonical suite list / aliases (`smoke`, `core`, `demo`, …) |
| `ZDTD_PLAYTEST_SUITE` | Accepted alias of `PLAYTEST_SUITE` (older Atomic hosts) |
| `PLAYTEST=1` / `ZDTD_PLAYTEST=1` | Legacy: arms `demo` |
| `PLAYTEST_LAPS` / `ZDTD_PLAYTEST_LAPS` | Benchmark repeats |
| `7DTD_CONNECT` | Set by orchestrator / connect |
| `PLAYTEST_LOCK_FILE` | Override exclusivity lock path (default under `~/.cache/7dtd-playtest/`) |
| `PLAYTEST_SESSION_ID` | Lock holder session id (or `--session`; auto-generated if empty) |
| `PLAYTEST_LOCK_STALE_SEC` | Heartbeat age after which a lock is stale (default 120) |
| `PLAYTEST_LOCK_HEARTBEAT_SEC` | How often the orchestrator refreshes heartbeat (default 30) |
| `PLAYTEST_TELNET_PASSWORD` | Stock dedicated telnet password (unset: ephemeral per-run secret written to the generated server config; `--no-server` attach falls back to `retest`) |

`residual` expands to `mp,soak` only. `make playtest-residual` is a **different**
multi-target host gate (persist + mp + apm + soak_long). See README.

## Log contract

**Stable** lines prefixed `[7dtd-playtest]` (do not rename tokens):

- Human: `PASS|FAIL|SKIP suite/case detail`
- Barrier: `barrier <name>` (host greps for telnet/admin phases).
  `spawn_vehicle:<entityClass>` asks the host for one vehicle of that class
  (the bare `spawn_vehicle` spawns a bicycle); client-created vehicles are
  unknown to a dedicated server and cannot be driven there.
- Staged frame: `scene staged <name> <detail>` (`Report.Staged`). Emitted the
  moment a scene is on screen, for an external screenshot loop to key on — a
  case's detail is flushed with its *result*, tens of seconds later, so a loop
  waiting on the result photographs the disconnect dialog instead. A suite
  proves data, never appearance; see "Visual confirmation" in the README.
  `scripts/capture_frames.sh` is the supported loop that waits for it — do not
  write a per-project screenshot loop keyed on bespoke wording.
- JSON: `{"v":1,"t":"result|summary|done|log|barrier|staged",...}`
- Terminal: `SUMMARY ...` then `DONE exit_hint=0|1`

Public API for external providers: `CaseDef.Live`/`Staged`/`Defer`, `Helpers`,
`Report`, `MiningSpec`/`MiningProbe`/`MiningResult`. A capability that more
than one consumer needs (real mining, staged frames, the exclusivity lock)
belongs **here**, not in the consumer. Visual evidence uses `CaseDef.Staged` — never hand-roll the
marker/hold/assert triple, that is what made every screenshot loop grep a
different sentence.

## Offline gates (no game install)

`make test` runs lint + typecheck plus the eleven offline suites on every push
(CI: `.github/workflows/ci.yml`). The analysis gates come first and are
blocking:

0. ruff over `scripts/` (`make lint`, `[tool.ruff]` in pyproject.toml) and
   mypy over `scripts/` (`make typecheck`, `[tool.mypy]`); both tools are
   pinned in the dev dependency-group so local and CI versions match uv.lock.

Then the eleven suites:

1. catalog<->SCENARIOS surface (`scripts/test_catalog_surface.py`): live rows
   + counts total must equal Catalog.cs. A catalog addition that skips
   SCENARIOS.md fails CI.
2. mod version surface (`scripts/test_version_surface.py`): ModInfo.xml ==
   ModApi.Version == dist manifest, and CHANGELOG.md must carry an
   [Unreleased] section plus the current release entry.
3. scenario-provider env surface (`scripts/test_scenario_provider_surface.py`)
4. mining-probe provider surface (`scripts/test_mining_probe_surface.py`)
5. stock-peer orchestration surface (`scripts/test_stock_peer_client.py`)
6. host lock (`scripts/test_playtest_lock.py`)
7. deterministic simulation (`scripts/test_dst.py`)
8. orchestrator local-init order gate (`scripts/test_no_unbound_locals.py`):
   catches the read-before-assignment crash class that once shipped in
   `playtest_run.py` main(); only fires with real game binaries present.
9. orchestrator report/log surface (`scripts/test_report_surface.py`): JUnit
   and serverconfig XML attribute escaping plus parser survival on malformed
   JSON events.
10. orchestrator pure-logic units (`scripts/test_playtest_run_units.py`):
   fresh-save removes only every world's copy of the named game save
   (quarantined under `<logdir>/quarantine`, newest 5 kept, never
   hard-deleted).
11. compare diff (`scripts/test_playtest_compare.py`, pytest via uv).

CI also runs a wider seed sweep with `make dst`. The mod build itself is not
CI-able (game DLLs).
