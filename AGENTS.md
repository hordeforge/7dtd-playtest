# AGENTS.md - 7dtd-playtest

Stock-client **gameplay automation** against real servers: the **stock
dedicated** (default target) and **zdtd**. Drive stock APIs and assert
observable state. Prefer missing over fakes.

Workspace: [`../AGENTS.md`](../AGENTS.md).  
Design: [`../zdtd/docs/CLIENT_PLAYTEST.md`](../zdtd/docs/CLIENT_PLAYTEST.md).  
Join plumbing: [`../7dtd-connect/`](../7dtd-connect/).

## Owns

- Client mod `Mods/7dtd-playtest` (scenario runner, oracles, structured logs)
- Host orchestrator (`scripts/playtest_run.py`) and make targets
- Suite definitions (smoke, core, later combat/economy)

## Does not own

- IP connect / intro skip (that is **7dtd-connect**)
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
9. **Exclusive live client** — see below. Only one orchestrated playtest (or
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

Inspect: `cat` the lock file, or `python3 -c "import playtest_lock as p; print(p.read_lock())"`.

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
make install              # build + install playtest mod
make install-pair         # playtest + connect
make playtest-smoke       # stock dedicated + smoke (exit 0/1/2)
make playtest-core        # stock dedicated + smoke,core
make playtest-zdtd        # same against zdtd
make playtest SUITE=core SERVER=stock
```

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
| `PLAYTEST_TELNET_PASSWORD` | Stock dedicated telnet password (default `retest`; written to the generated server config and used by the orchestrator's telnet client) |

`residual` expands to `mp,soak` only. `make playtest-residual` is a **different**
multi-target host gate (persist + mp + apm + soak_long). See README.

## Log contract

**Stable** lines prefixed `[7dtd-playtest]` (do not rename tokens):

- Human: `PASS|FAIL|SKIP suite/case detail`
- Barrier: `barrier <name>` (host greps for telnet/admin phases).
  `spawn_vehicle:<entityClass>` asks the host for one vehicle of that class
  (the bare `spawn_vehicle` spawns a bicycle); client-created vehicles are
  unknown to a dedicated server and cannot be driven there.
- JSON: `{"v":1,"t":"result|summary|done|log|barrier",...}`
- Terminal: `SUMMARY ...` then `DONE exit_hint=0|1`

Public API for external providers: `CaseDef.Live`/`Defer`, `Helpers`, `Report`.

## Offline gates (no game install)

`make test` runs the eight offline gates on every push (CI:
`.github/workflows/ci.yml`): catalog<->SCENARIOS surface (live rows + counts
total must equal Catalog.cs), mod version surface (`scripts/test_version_surface.py`;
ModInfo.xml == ModApi.Version == dist manifest, and CHANGELOG.md must carry an
[Unreleased] section plus the current release entry), scenario-provider env surface,
the host lock,
the deterministic simulation (`scripts/test_dst.py`), the orchestrator
local-init order gate (`scripts/test_no_unbound_locals.py`; catches the
read-before-assignment crash class that once shipped in `playtest_run.py`
main() and only fires with real game binaries present), the orchestrator
report/log surface (`scripts/test_report_surface.py`; JUnit and serverconfig
XML attribute escaping plus parser survival on malformed JSON events), and the compare diff
(pytest via uv). CI also runs a wider seed sweep with `make dst`. A catalog addition that skips
SCENARIOS.md fails CI. The mod build itself is not CI-able (game DLLs).
