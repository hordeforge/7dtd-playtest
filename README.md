# 🛡️ Vanguard (7DTD Playtest Runner)

> **Part of [HordeForge](https://github.com/hordeforge)** — High-Performance Systems Engineering for 7 Days to Die.

![CI](https://github.com/hordeforge/7dtd-playtest/actions/workflows/ci.yml/badge.svg)
![license](https://img.shields.io/github/license/hordeforge/7dtd-playtest)
![release](https://img.shields.io/github/v/release/hordeforge/7dtd-playtest)
![languages](https://img.shields.io/github/languages/count/hordeforge/7dtd-playtest)
![top language](https://img.shields.io/github/languages/top/hordeforge/7dtd-playtest)

Stock-client **gameplay automation** for 7 Days to Die servers (EAC off). Works against the **stock dedicated server** (the default) and against **zdtd-server** (`--server zdtd` / `make playtest-zdtd`). Drives real client APIs, waits for server-visible state where it matters, and emits structured scenario test results for a host orchestrator.

Host-side concurrency (the exclusivity lock) is covered by deterministic
simulation: `make dst`, documented in **[DST.md](DST.md)**.

Join/auto-connect is **not** here: install [`../7dtd-fastconnect/`](../7dtd-fastconnect/)
as well. Design: [`../zdtd-server/docs/CLIENT_PLAYTEST.md`](../zdtd-server/docs/CLIENT_PLAYTEST.md).

## Requirements

- Host OS: **Linux** on x86_64. Host scripts assume bash, procps (`pkill`),
  GNU coreutils, and PulseAudio/PipeWire; the client runs through
  Steam/Proton and the dedicated server is the Linux build.
- Stock client V3.x, EAC off (`-noeac`)
- `0_TFP_Harmony`
- `7dtd-fastconnect` installed
- Game: `~/.local/share/Steam/steamapps/common/7 Days To Die` (`GAME=`)
- Only for zdtd-target runs (`playtest-zdtd`, `playtest-apm`): built `zdtd`
  at `../zdtd-server/zig-out/bin/zdtd`
- Only for the `bot` suite: server-side `BotMod` in the dedicated's `Mods/`
  (provides the `bot` telnet commands the orchestrator drives)
- Host Python via **`uv`**, pinned to CPython 3.13 by `.python-version`
  (uv fetches it automatically; gates must not rely on a newer interpreter)
- dotnet SDK 8.0.x for the mod build (pinned by `global.json`; found on
  `PATH` or under `$DOTNET_ROOT`, e.g. `~/.cache/dotnet-sdk`)

## Install

```bash
make install-pair    # playtest + connect into $GAME/Mods/
```

## One-command suites

Default server is the **stock dedicated** (Navezgane, EAC off, port **26900**).

Full scenario list: **[SCENARIOS.md](SCENARIOS.md)** (demo / benchmark / full catalog).

```bash
make playtest-demo           # attract-mode + combat wait (telnet spawn)
make playtest-demo-fresh     # wipe save first (clean dig pad)
make playtest-gate           # PR gate: live smoke+core only
make playtest-bench LAPS=3   # timed repeats of bench path
make playtest-full           # demo domains + soak (no persist/mp/apm/bot)
make playtest-smoke          # boot only
make playtest SUITE=combat
make playtest-zdtd           # demo against zdtd on 27025
make playtest-persist        # multi-phase rejoin (setup → save → verify)
make playtest-mp             # loadgen multi-peer
make playtest-apm            # zdtd APM dump attach (SERVER=zdtd)
make playtest-soak-long      # ≥15 min host soak
make playtest-residual       # persist + mp + apm + soak_long
make playtest-compare        # same suite vs stock AND zdtd, diffed per case
                             # (SUITE=smoke; report in
                             # workspace/comparison-playtest/<suite>/)
make playtest-repeat LAPS=3  # flake detection: N fresh-server laps, all must pass
```

`playtest-compare` diffs per case into `playtest-compare.{md,json}` and also
reports a wall-time axis (server session seconds, from the orchestrator
reports) - a cost observation, never a per-case finding (zdtd being faster is
a known divergence, not a mismatch).

v0.7.1 gameplay surface (stock motor / stock attack / real C2S, **not** tele-fakes):
- locomotion + jump + stamina; entity/block melee; **ranged** (pipe pistol Meta)
- **ItemDropServer**, **loot Collect**, **keystone place**, eat, dig/place
- **creative** UI; **craft wooden club** (queue + output); **campfire TE** place
- Runner recovers from player death mid-suite (no hang)
- Residual live: multi-phase rejoin, multi-peer loadgen, ≥15m soak, zdtd APM dump
- Demo stock: **83 pass / fail=0** (fresh-save); residual suites separate fail=0

| Suite alias | Meaning |
|---|---|
| `demo` | Cinematic + combat fixture wait |
| `demo_min` | Same without combat |
| `benchmark` | Timed path; `LAPS=` |
| `gate` | Live smoke+core |
| `full` | Demo domains + soak (not persist/mp; use dedicated targets) |
| `residual` | **Client alias only:** `mp` + short `soak` (not the full Make residual gate) |
| `residual_light` | Same as `residual` |
| `persist` | Multi-phase rejoin verify (needs host orch phases) |
| `mp` | Multi-peer via loadgen |
| `soak_long` | ≥15 min host soak digs |
| `apm` | zdtd APM dump barrier |

**Residual split (do not confuse):**

| Entry | What runs |
|---|---|
| `PLAYTEST_SUITE=residual` (in-client expand) | `mp` + short `soak` only |
| `make playtest-residual` | **Four host targets in order:** persist → mp → apm → soak_long |

Persist/apm/soak_long need host orchestration or long wall-clock; they are not
folded into the client `residual` alias.

Orchestrator exit codes:

| Code | Meaning |
|---|---|
| 0 | All cases passed (`DONE exit_hint=0`) |
| 1 | One or more case failures |
| 2 | Harness error (no DONE, server/client missing, timeout, **or lock refused**) |

Reports land under `~/.cache/7dtd-playtest/report-*.json` (override `LOGDIR=`).

### Host orchestrator environment

`scripts/playtest_run.py` reads these environment variables as defaults for
the matching CLI flags; a flag always overrides its env var. Every run logs
one effective `config:` line at startup (values only; the telnet password is
reported as set/unset, never its value), so a misread environment is visible
in the log without rerunning with `--help`.

| Env | Default | Meaning |
|---|---|---|
| `PLAYTEST_SERVER` | `stock` | Server backend, `stock` or `zdtd` (`--server`) |
| `ZDTD` | `../zdtd-server/zig-out/bin/zdtd` | zdtd server binary path (`--zdtd`) |
| `RE_DEDICATED_USERDATA` | `~/.cache/7dtd-playtest-dedicated` | Stock dedicated userdata dir (`--userdata`) |
| `LOGDIR` | `~/.cache/7dtd-playtest` | Report / server-log dir (`--logdir`) |
| `PLAYTEST_TIMEOUT_SEC` | `900` | Harness wall-clock timeout in seconds > 0 (`--timeout`). Invalid values are a harness error (exit 2) naming the variable |
| `PLAYTEST_TELNET_PASSWORD` | *(generated)* | Local telnet password (see [Host orchestrator secrets](#host-orchestrator-secrets); prefer the env var over `--telnet-password`, which is visible in process listings). Unset means an ephemeral per-run secret for servers the orchestrator starts; `--no-server` attach falls back to `retest` |
| `PLAYTEST_PEER_CLIENT_NAME` / `_COMPAT` / `_SUITE` | empty | Defaults for the matching `--peer-client-*` flags (all three must stay paired as documented below) |

Invalid numeric values in `PLAYTEST_LOCK_STALE_SEC` /
`PLAYTEST_LOCK_HEARTBEAT_SEC` fall back to their defaults (120 / 30) with a
warning on stderr instead of silently changing lock takeover timing.

### Client audio mute (default on)

Automated client launches **mute the game process at the OS audio layer by
default** (PipeWire/Pulse sink-input via `7dtd-fastconnect` `launch_client.sh` +
orchestrator helper). This does **not** change game client settings (no
GamePrefs / in-game audio sliders). Independent of master volume. Requires
`pactl` and `jq`.

| Env | Meaning |
|---|---|
| `CLIENT_MUTE` / `PLAYTEST_MUTE` / `SEVEN_DAYS_TO_DIE_CLIENT_MUTE` | Default `1`. Set `0` / `false` / `no` / `off` to keep sound |
| `CLIENT_MUTE_TIMEOUT` | Seconds to wait for the audio stream (default 60) |

```bash
CLIENT_MUTE=0 make playtest-demo   # keep speakers on
```

### Live-client exclusivity lock

Only one host playtest may drive the shared **client + dedicated/zdtd server**
at a time. `scripts/playtest_run.py` starts a stock dedicated by default
(or zdtd with `SERVER=zdtd`); that is under the same lock as the client.

It acquires the lock **before** cleaning processes or launching, refreshes a
**heartbeat** while the run is active, and releases when the run ends. Acquire
also fails if a client **or** dedicated process is already live (unless you
hold a fresh lock). After clean it refuses if ServerPort/telnet is still bound.

| | |
|---|---|
| Default file | `~/.cache/7dtd-playtest/playtest_running` |
| Override | `PLAYTEST_LOCK_FILE` (use the **same** path as Atomic/monorepo) |
| Session | `--session` / `PLAYTEST_SESSION_ID` (auto if empty) |
| Payload | `running`, `session`, `acquired`, `heartbeat` (UTC ISO) |
| Stale after | `PLAYTEST_LOCK_STALE_SEC` (default 120) without heartbeat refresh |

Agents: read `heartbeat=` to see if a hold is still live. A fresh heartbeat
means wait; a stale lock with no client/server process may be reclaimed.
Full rules: [AGENTS.md](AGENTS.md) § Playtest / live-client exclusivity.

## External scenario suites (providers)

Another client mod can add a suite without forking this harness by referencing
`7dtd-playtest.dll` (namespace `ZdtdPlaytest`) and implementing
`IScenarioProvider`. Install that mod **alongside** `7dtd-playtest`, then set
the suite env (see below) to your provider suite id.

### Minimal provider

Discovery scans loaded mod assemblies for public `IScenarioProvider`
implementations with a public parameterless constructor:

```csharp
using System.Collections.Generic;
using ZdtdPlaytest;

public sealed class MyProvider : IScenarioProvider
{
    // Suite ids are matched case-insensitively. Ids that collide with
    // built-in suites run the built-in instead; an id no provider owns and
    // that produces zero cases fails the run (FAIL row + DONE exit_hint=1),
    // so typos and uninstalled providers are never silent.
    public IEnumerable<string> SuiteIds
    {
        get { yield return "my_wave_suite"; }
    }

    public void AppendSuite(List<CaseDef> queue, string suite, int lap)
    {
        // lap > 0 means a benchmark-style repeat: suffix case ids with "@lap"
        // so per-lap results stay distinct in reports.
        string label = lap > 0 ? suite + "@" + lap : suite;
        queue.Add(CaseDef.Live(label, "my_case", new[] { "provider" },
            act: ctx => Report.Barrier("my_provider_ready"),
            assert: ctx => ctx.World != null && ctx.Player != null));
    }
}
```

Arm it with `PLAYTEST_SUITE=my_wave_suite` (`make playtest SUITE=my_wave_suite`).

### Build cases (public factories + helpers)

Do **not** assign `CaseDef` fields by hand. Use:

```csharp
// Live case with long wait (e.g. propagation / fallout wave).
// tags (3rd arg) is informational only and may be omitted.
queue.Add(CaseDef.Live(suite, "my_wave", new[] { "bench" },
    act: ctx => { /* setup */ },
    wait: ctx => /* server-visible predicate */,
    assert: ctx => /* ok? */,
    timeout: 120f,   // case Wait budget (seconds; must be > 0); default 8
    fail: "wave did not finish",
    pause: 0.5f));

queue.Add(CaseDef.Defer(suite, "later", new[] { "todo" }, "needs admin fixture"));

// Host orchestration barriers (grep'd by scripts/playtest_run.py).
Report.Barrier("my_provider_ready");
// Host-owned vehicle of a given class (bare "spawn_vehicle" = bicycle). A
// vehicle the client creates itself is unknown to the dedicated server and
// never moves there, so ask the host and then find the replicated entity.
Report.Barrier("spawn_vehicle:vehicleGyrocopter");

// Stock-API glue shared with the built-in catalog (no invented S2C).
Helpers.TryGiveItem(ctx.Player, stack);
Helpers.TryEquipItemType(ctx.Player, itemType);
Helpers.PlayerInVehicle(ctx.Player, vehicle);
Helpers.TryEnterVehicle(ctx.Player, vehicle, out var detail);
```

### Provider error behavior

- Exceptions thrown from `act` / `wait` / `assert` fail that case only
  (FAIL row, then the suite continues); the detail names stage and
  exception type (`act exception NullReferenceException: …`). They never
  take down the runner or the game.
- A provider whose constructor, `SuiteIds`, or `AppendSuite` throws is
  skipped with a `[7dtd-playtest] scenario provider …` log line. A suite
  that then produces zero cases is recorded FAIL (`unknown or empty
  suite`) with `DONE exit_hint=1`, never a silent green run.
- `CaseDef.Live` validates at call time (fail-fast): a case with no
  `act`, `wait`, or `assert` at all would record a pass while running
  nothing, and `timeout <= 0` has no meaning; both throw immediately.
- Diagnostics: `Report.Info("…")` emits a `[7dtd-playtest]` human line
  plus a JSON `"t":"log"` event under the stable prefix.

#### Barriers the stock host answers

`scripts/playtest_run.py` greps client log for `barrier <name>` and performs
telnet/admin setup. Barrier names it already handles (safe to emit from
provider cases):

| Barrier | Host action |
|---|---|
| `spawn_zombie` | Spawn a fixture zombie near the player |
| `kill_fixture_zombie` | Kill non-player AI (fixture cleanup) |
| `spawn_trader` | Spawn a trader fixture |
| `kill_player` | Kill the player entity (death / respawn cases) |
| `settime_day` / `settime_bloodmoon` | Set world time via telnet |
| `spawn_vehicle:<entityClass>` | Host-owned vehicle of that class (bare `spawn_vehicle` = bicycle) |
| `chat_echo:<token>` | Server chat `say <token>` (once per token) |
| `spawn_loadgen_peer` / `spawn_loadgen_bots` | Start loadgen peers/bots |
| `bot_spawn` / `bot_player_near` | Server-side `BotMod` commands |
| `teleport_persist_pad` | Teleport players to the persist pad |
| `apm_dump` | zdtd APM dump write (zdtd targets only) |

Any other name is inert on this host (third-party hosts may grep their own).
Repeated identical lines are separate requests; handlers count hits.

Built-in fixture suites arm these handlers automatically. An external provider
suite must opt in explicitly so an unrelated client-only suite remains
telnet-free:

```bash
uv run --locked --project . python scripts/playtest_run.py --suite your_suite --host-fixtures
```

`--no-fixtures` is the overriding opt-out when both options are present.

Public surface for providers: `CaseDef.Live` / `CaseDef.Defer`, `CaseCtx`,
`IScenarioProvider`, `Helpers`, `Report` (including `Report.Barrier`).

`Helpers.LookAt(player, worldPos)` aims the player camera at a world position.
It follows the stock `EntityPlayerLocal.SetRotation` convention: negative X
pitch looks below the horizon and positive X pitch looks above it. Use the
helper for ground-zero or block-facing scenes instead of deriving pitch with
the opposite Unity-camera sign.

#### CaseCtx members (fresh instance per case)

| Member | Meaning |
|---|---|
| `Gm`, `World`, `Player` | Live game objects, refreshed every tick; `Player` can go null/dead mid-case (the runner rescues or fails the case) |
| `StartPos` | Player position when the case started |
| `Detail` | Optional scratch string; appended to FAIL detail on timeout/failure |
| `TargetEntityId` | Entity id for combat fixtures (ranged target etc.) |
| `BenchmarkLap` | Lap number parsed from the `suite@N` label (0 outside laps) |
| `CaseStartUnscaled` | `Time.unscaledTime` when the case started; use for elapsed budgets in `wait` predicates (`Time.unscaledTime - ctx.CaseStartUnscaled`) |
| `WasBlockType`, `PlaceBlockType`, `IntA`, `IntB`, `IntC`, `FloatA`, `FloatB`, `TargetBlock`, `WorldTime0` | Built-in catalog scratch fields; providers may reuse them or capture closure locals instead |

### Suite environment (stock dedicated + connect)

The runner arms from the **first non-empty** of:

| Var | Role |
|---|---|
| `PLAYTEST_SUITE` | Canonical suite list / aliases |
| `ZDTD_PLAYTEST_SUITE` | Accepted alias (Atomic / older hosts) |
| `PLAYTEST=1` or `ZDTD_PLAYTEST=1` | Legacy arm → `demo` |
| `PLAYTEST_LAPS` / `ZDTD_PLAYTEST_LAPS` | Benchmark repeats |

`make playtest` / `scripts/playtest_run.py` set `PLAYTEST_SUITE`. Hosts that
only set `ZDTD_PLAYTEST_SUITE` (e.g. Atomic `playtest-run.sh` via connect
Proton) also arm correctly. Prefer **7dtd-fastconnect** `launch_client.sh` so the
variable reaches the game process (`steam -applaunch` often drops it).

### Fresh / disposable world (no OCR New Game)

Wipe the orchestrator save before launch so dig pads and fixtures start clean:

```bash
make playtest SUITE=your_suite FRESH=1          # default FRESH=1
# or
uv run --locked --project . python scripts/playtest_run.py --suite your_suite --fresh-save ...
```

`FRESH=0` keeps the existing save when you deliberately inspect one. Providers
do not need Atomic’s OCR `create-smoke-world.py` when using this host path.

### State, backups, and recovery

Durable state this system owns, and what an incident costs:

| State | Location | Survives instance loss? |
|---|---|---|
| Compare baselines (`playtest-compare.json/md` per suite) | `workspace/comparison-playtest/`, committed | Yes (git remote) |
| Run artifacts: `report-*.json`, `junit-*.xml`, server/client logs | `<logdir>` (default `~/.cache/7dtd-playtest`, env `LOGDIR`); timestamped reports/junit pruned to newest 50 per pattern per run | No |
| Wiped saves / zdtd worlds / previous client logs (soft-delete window) | `<logdir>/quarantine/<UTC-stamp>-<kind>/` | No |
| Exclusivity lock | `~/.cache/7dtd-playtest/playtest_running` | No (self-healing) |

Recovery facts:

- **RPO/RTO:** run artifacts are reproducible output, not records of record.
  Losing them costs a suite re-run (RTO = suite wall time). The compare
  baselines are the only long-lived results, and git carries those. Nothing
  here backs up sibling projects (`zdtd`, `7dtd-fastconnect`) or game installs.
- **Restore a wiped save:** `--fresh-save` no longer hard-deletes. The named
  stock save, zdtd `players.zsv`/`containers.zct`/`blockmeta.zbm`, chunk
  overlays, and the previous client log move into
  `<logdir>/quarantine/`; the newest `QUARANTINE_KEEP = 5` entries are kept
  (oldest pruned). Copy an entry's contents back to its original path to
  restore. If the quarantine itself is unwritable, data stays in place and
  the run warns about stale reuse instead of destroying anything.
- **Interrupted run:** kill leftovers with the orchestrator's own clean pass,
  then clear the lock per the [Live-client exclusivity lock](#live-client-exclusivity-lock)
  rules (fresh heartbeat means another holder is alive; stale plus no live
  client process may take over).

### Multi-phase rejoin (persist) for provider cases

Built-in flow (host-driven; do not invent client-only rejoin):

1. Client suite `persist_setup` prepares state and emits
   `Report.Barrier("persist_setup_done")`.
2. Host greps the barrier, `saveworld` / admin, restarts or rejoins the client.
3. Client suite `persist` runs verify cases.

External suites that need the same host phases should:

- Emit a **stable barrier name** via `Report.Barrier("…")` from a setup case.
- Split setup and verify into suite ids. Run the verify suite with both
  `--rejoin-setup-suite` and `--rejoin-setup-barrier`; the runner arms the
  declared setup suite, saves, restarts the server, then arms the requested
  verify suite. Both options are required together.
- If the verify predicate needs a previously unloaded server chunk, providers
  may also pass `--rejoin-teleport X Y Z`. After the replacement client has
  joined, the host uses `teleportplayer` before the verify suite continues.
- The provider's setup barrier means its fixture is durable enough to save.
  The runner does not manufacture terrain, entities, or server state for it.
  Built-in `persist` retains its dedicated position-pad handling.

### Multi-peer / second client

`make playtest-mp` starts loadgen peers and runs suite `mp`. Barriers such as
`spawn_loadgen_peer` are host-handled. Third-party suites that need a peer:

- Prefer adding cases to a suite id the host already arms with loadgen, **or**
- Run `make playtest-mp` / loadgen attach first, then arm your suite while peers
  remain (host composition). Cross-client terrain assertions stay real
  multi-client work (Human-Runtime / two clients); the harness does not fake
other players.

For a headless replication assertion, add exact state filters and expectations
to the same run. The host reads loadgen's `7dtd.loadgen.event.v1` JSON Lines,
retains the entity ID from its structured `joined` event, teleports that exact
entity, and fails the run when the observer exits or the final filtered state
does not match. CVar comparisons use a `0.0001` tolerance; buff expectations
accept `true` or `false`. Use `--loadgen-expect-cvar-positive NAME` for a
strictly positive value and `--loadgen-expect-cvar-equal LEFT=RIGHT` to
compare two decoded CVars. `--loadgen-server-cvar-oracle` additionally runs
`cvar get` for every observed CVar against the exact joined entity and
requires the server-authority value to match the peer's decoded value. Its
default absolute tolerance is `0.0001`; time-varying values may set
`--loadgen-server-cvar-tolerance VALUE` explicitly.

```bash
make playtest SUITE=your_suite EXTRA_ARGS="--host-fixtures --loadgen-observe-cvar protection --loadgen-observe-buff protected --loadgen-expect-cvar-positive protection --loadgen-expect-buff protected=true --loadgen-server-cvar-oracle --loadgen-teleport 520 62 950"
```

The provider emits `Report.Barrier("spawn_loadgen_peer")` before it needs the
peer. Ordinary loadgen barriers remain unchanged and quiet when no observer
options are supplied.

For one passive **stock** peer, provide both a distinct Local-platform player
name and an already initialized, separate Proton compat profile. The runner
starts that client without `PLAYTEST_*`, so it joins and remains in the world
without executing a duplicate scenario suite. The connect mod reads the peer
name from `7DTD_PLAYER_NAME`; use a current `7dtd-fastconnect` install that
supports that variable. This is a genuine second client, not a loadgen bot.
The runner leaves one second between the two launches because the stock V3.1
server has a 500 ms same-IP connection limiter; without that spacing the
second localhost client is rejected before authentication.

Run a suite with an isolated peer profile:

```bash
make playtest SUITE=mp EXTRA_ARGS="--peer-client-name atomic-peer --peer-client-compat /path/to/initialized/peer-compat"
```

When the peer must run a provider-side setup case, add
`--peer-client-suite <suite>` and give the runner its game log with
`--peer-client-log <path>` when the profile uses a nonstandard log location.
`--peer-client-teleport X Y Z` waits for both scenario clients to report ready
before teleporting all joined players to one fixture location.

### Long-running cases (benchmarks / waves)

- Per-case: `CaseDef.Live(..., timeout: 120f)` (or higher). The runner fails
  the case when `Wait` exceeds `TimeoutSec`.
- Host wall clock: `make playtest … EXTRA_ARGS="--timeout 1200"` (see
  `playtest-soak-long` / `playtest-persist` targets). Case timeouts and host
  timeouts are independent; both must be large enough for real waves.

### Stable log contract (do not rename)

Host runners (including third-party) scrape **stable** prefixes and tokens:

```text
[7dtd-playtest] armed suites=…
[7dtd-playtest] PASS suite/case detail
[7dtd-playtest] FAIL suite/case detail
[7dtd-playtest] SKIP suite/case detail
[7dtd-playtest] barrier name
[7dtd-playtest] scene staged name detail
[7dtd-playtest] {"v":1,"t":"result|summary|done|log|barrier|staged",…}
[7dtd-playtest] SUMMARY pass=N fail=M skip=K total=T wall_ms=…
[7dtd-playtest] DONE exit_hint=0|1
```

Legacy log prefix `[zdtd-playtest]` may appear in older builds; new code emits
`[7dtd-playtest]` only. JSON `t` values and human `PASS|FAIL|SKIP` /
`SUMMARY` / `DONE` / `barrier` tokens are part of the contract. Optional host
reports: `~/.cache/7dtd-playtest/report-*.json` (`LOGDIR=`).

### Visual confirmation: what a suite cannot tell you

**A suite proves data, never appearance.** Cases read loaded items, tags,
progression rows, buffs and server-written CVars. Nothing in this harness looks
at the screen, so a green run says the data is right and says nothing at all
about whether a model, an icon, a UI row or an effect *looks* right.

Two things follow, and both have been mistaken for faults:

- **The client is only up for as long as the cases take.** A data-only suite is
  mostly world load: a five-case suite can finish its cases in under two
  seconds of a seventy-second run, and the client is torn down the moment
  `DONE` is written. Someone watching the screen sees a window appear, sit on a
  loading screen, and vanish. That is the runner working, not failing — but it
  is also not evidence of anything visual.
- **Do not report a green suite as visual confirmation,** and do not tell
  someone who watched the screen what they saw. The log cannot answer whether a
  window appeared or what was in it.

#### The supported path

Anything a person has to judge by eye needs a **staged frame**: a case that
puts the scene on screen, holds it still, and announces itself so an external
screenshot loop can photograph it.

1. Build the case with **`CaseDef.Staged`**, not `CaseDef.Live`. It emits the
   marker the instant your callback returns, holds the scene, and fails the
   case if staging did not succeed:

```csharp
queue.Add(CaseDef.Staged(suite, "cbrn_suit", new[] { "capture", "models" },
    ctx => WearSuitAndOpenBackpack(ctx),   // true when the scene is really up
    holdSeconds: 10f));
```

2. Your callback returns whether the scene is genuinely on screen — the items
   were given, the window opened, the camera arrived — and sets `ctx.Detail`
   to whatever context helps the person reading the frame.
3. Never assert how it *looks*. No fixture here can see; `CaseDef.Staged`'s
   assert only establishes that there was something to photograph.
4. Photograph it with [`scripts/capture_frames.sh`](scripts/capture_frames.sh),
   which runs the suite, waits for that marker in a log written *after* the run
   started, shoots N frames, crops them to the client window and builds a
   contact sheet:

```bash
./scripts/capture_frames.sh --suite <id>
./scripts/capture_frames.sh --suite <id> --out ./frames --runner ./my-wrapper.sh
```

   `--runner` is for a project with its own entry point (deploys, `.local.env`,
   lock handling); it is invoked as `<cmd> --suite <id>`, and defaults to this
   repo's `scripts/playtest_run.py`. `CAPTURE_FRAMES`, `CAPTURE_INTERVAL` and
   `CAPTURE_CROP` tune the loop. It refuses to start while a client or
   dedicated server is already up, because overlapping runs photograph the
   wrong one.

`Report.Staged` exists because `ctx.Detail` does not work for this: a case's
detail is flushed **with its result**, which is after the hold, so a loop
waiting on the result photographs whatever came next — usually the disconnect
dialog. Providers each worked around that with their own bespoke `Report.Info`
wording, which meant every screenshot loop grepped a different sentence. The
marker is now spelled once, here, and is part of the log contract above.

A staging suite is a **fixture, not a proof**. Its assertions establish that
there was something to photograph; the verdict on the frame belongs to a
person, and should be tracked as such wherever that project records human
sign-off.

### Which client install a run uses

`launch_client.sh` reads **`GAME`** — not `SEVEN_DAYS_TO_DIE_DIR`, which it
ignores. When `GAME` is unset the orchestrator discovers the install by reading
Steam's own `steamapps/libraryfolders.vdf` under each standard Steam root and
taking the first library whose `common/` holds a `7DaysToDie.exe`, so a library
on a second disk works with no environment at all. `COMPAT` is derived from
whichever install is chosen, and `--client-log` defaults to the log inside that
prefix.

Both are preflighted before anything starts. An install that cannot be found,
or a `GAME` naming a directory with no client executable, is a startup error
(exit 2) naming the variable. Without that check the launcher exits with
`Game not found` into its own launch log, which nothing reads until the run is
over, and the harness spends its whole timeout waiting for a client log that
was never going to appear.

## Manual / pair launch

```bash
# terminal 1: stock dedicated or zdtd
# terminal 2 (connect preserves env into the game process):
7DTD_CONNECT=127.0.0.1:27025 PLAYTEST_SUITE=smoke,core \
  ../7dtd-fastconnect/scripts/launch_client.sh
# also accepted:
# ZDTD_PLAYTEST_SUITE=smoke,core ...
```

Legacy: `PLAYTEST=1` or `ZDTD_PLAYTEST=1` arms `demo`.

## Suites (catalog summary)

Full tables: **[SCENARIOS.md](SCENARIOS.md)** (every Live case id). Built-in
counts from `Catalog.cs` (108 Live, 0 Defer):

| Suite | Live cases |
|---|---:|
| `smoke` | 5 (`join_ready`, `cgo_ready`, `ground`, `stats`, `day_clock`) |
| `core` | 18 (look / motors / dig / place / inventory / …) |
| `world` / `ui` / `combat` / … | see SCENARIOS |
| `persist_setup` / `persist` | 6 setup + 5 verify |
| `mp` | 6 |
| `soak` / `soak_long` / `apm` | short loop + ≥15m + APM |

`dig_confirm` / `place_confirm` **wait** until `World.GetBlock` reflects the
RPC (or timeout fail). That is the fidelity gate unit tests cannot replace.

## Log contract

See [Stable log contract](#stable-log-contract-do-not-rename) above.

## Rules

- Drive + assert only. No invented chunks, signs, or swallowed NREs.
- Server gaps are fixed server-side (**zdtd** for zdtd targets), not papered
  over in this mod.
- See [AGENTS.md](AGENTS.md).

## CI

`.github/workflows/ci.yml` runs `make test` (the offline gates: catalog<->SCENARIOS
surface incl. live rows + counts total, mod version/changelog sync, scenario-provider
env surface, stock-peer orchestration surface, host lock, deterministic simulation,
orchestrator local-init order, report/log surface, orchestrator pure-logic units,
compare diff)
plus a wider `make dst DST_SEEDS=200` sweep on every push. Locally, `make check`
runs exactly what CI runs, in one step. No game install needed - these are pure Python. The mod
build itself is not CI-able (references game DLLs), so the offline gates are
the push-time guard for catalog/doc drift.

### Host orchestrator secrets

The stock dedicated telnet password is local-only. When the orchestrator
starts the dedicated itself, an unset `PLAYTEST_TELNET_PASSWORD` (and
`--telnet-password`) generates an ephemeral per-run secret: it is written
into the generated server config (chmod 0600) and used by the orchestrator's
telnet client, so the two can never diverge and a network-reachable telnet
listener never opens with a published default. `--no-server` runs attach to a
dedicated whose config this process did not write, so they fall back to the
loadgen template's lab default (`retest`). The same value is written into
the generated server config and used by the orchestrator's telnet client.
It is not a production secret: the server binds localhost in playtest runs
(`ServerVisibility=0`, Steam+LAN only).
