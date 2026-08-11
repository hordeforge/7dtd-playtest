# 7dtd-playtest

Stock-client **gameplay automation** for 7 Days to Die servers (EAC off).
Works against the **stock dedicated server** (the default) and against
**zdtd** (`--server zdtd` / `make playtest-zdtd`). Drives real client APIs,
waits for server-visible state where it matters, and emits structured
`[7dtd-playtest]` results for a host orchestrator.

Join/auto-connect is **not** here: install [`../7dtd-connect/`](../7dtd-connect/)
as well. Design: [`../zdtd/docs/CLIENT_PLAYTEST.md`](../zdtd/docs/CLIENT_PLAYTEST.md).

## Requirements

- Stock client V3.x, EAC off (`-noeac`)
- `0_TFP_Harmony`
- `7dtd-connect` installed
- Game: `~/.local/share/Steam/steamapps/common/7 Days To Die` (`GAME=`)
- Only for zdtd-target runs (`playtest-zdtd`, `playtest-apm`): built `zdtd`
  at `../zdtd/zig-out/bin/zdtd`
- Host Python 3.11+ via **`uv`**

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
make playtest-full           # entire catalog (many intentional SKIPs)
make playtest-smoke          # boot only
make playtest SUITE=combat
make playtest-zdtd           # demo against zdtd on 27025
make playtest-persist        # multi-phase rejoin (setup → save → verify)
make playtest-mp             # loadgen multi-peer
make playtest-apm            # zdtd APM dump attach (SERVER=zdtd)
make playtest-soak-long      # ≥15 min host soak
make playtest-residual       # persist + mp + apm + soak_long
```

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

### Client audio mute (default on)

Automated client launches **mute the game process at the OS audio layer by
default** (PipeWire/Pulse sink-input via `7dtd-connect` `launch_client.sh` +
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

### Build cases (public factories + helpers)

Do **not** assign `CaseDef` fields by hand. Use:

```csharp
// Live case with long wait (e.g. propagation / fallout wave).
queue.Add(CaseDef.Live(suite, "my_wave", new[] { "bench" },
    act: ctx => { /* setup */ },
    wait: ctx => /* server-visible predicate */,
    assert: ctx => /* ok? */,
    timeout: 120f,   // case Wait budget (seconds); default 8
    fail: "wave did not finish",
    pause: 0.5f));

queue.Add(CaseDef.Defer(suite, "later", new[] { "todo" }, "needs admin fixture"));

// Host orchestration barriers (grep'd by scripts/playtest_run.py).
Report.Barrier("my_provider_ready");

// Stock-API glue shared with the built-in catalog (no invented S2C).
Helpers.TryGiveItem(ctx.Player, stack);
Helpers.TryEquipItemType(ctx.Player, itemType);
Helpers.PlayerInVehicle(ctx.Player, vehicle);
Helpers.TryEnterVehicle(ctx.Player, vehicle, out var detail);
```

Public surface for providers: `CaseDef.Live` / `CaseDef.Defer`, `CaseCtx`,
`IScenarioProvider`, `Helpers`, `Report` (including `Report.Barrier`).

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
Proton) also arm correctly. Prefer **7dtd-connect** `launch_client.sh` so the
variable reaches the game process (`steam -applaunch` often drops it).

### Fresh / disposable world (no OCR New Game)

Wipe the orchestrator save before launch so dig pads and fixtures start clean:

```bash
make playtest SUITE=your_suite FRESH=1          # default FRESH=1
# or
uv run --project . python scripts/playtest_run.py --suite your_suite --fresh-save ...
```

`FRESH=0` keeps the existing save when you deliberately inspect one. Providers
do not need Atomic’s OCR `create-smoke-world.py` when using this host path.

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

For one passive **stock** peer, provide both a distinct Local-platform player
name and an already initialized, separate Proton compat profile. The runner
starts that client without `PLAYTEST_*`, so it joins and remains in the world
without executing a duplicate scenario suite. The connect mod reads the peer
name from `ZDTD_PLAYER_NAME`; use a current `7dtd-connect` install that
supports that variable. This is a genuine second client, not a loadgen bot.

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
[7dtd-playtest] {"v":1,"t":"result|summary|done|log|barrier",…}
[7dtd-playtest] SUMMARY pass=N fail=M skip=K total=T wall_ms=…
[7dtd-playtest] DONE exit_hint=0|1
```

Legacy log prefix `[zdtd-playtest]` may appear in older builds; new code emits
`[7dtd-playtest]` only. JSON `t` values and human `PASS|FAIL|SKIP` /
`SUMMARY` / `DONE` / `barrier` tokens are part of the contract. Optional host
reports: `~/.cache/7dtd-playtest/report-*.json` (`LOGDIR=`).

## Manual / pair launch

```bash
# terminal 1: stock dedicated or zdtd
# terminal 2 (connect preserves env into the game process):
ZDTD_CONNECT=127.0.0.1:27025 PLAYTEST_SUITE=smoke,core \
  ../7dtd-connect/scripts/launch_client.sh
# also accepted:
# ZDTD_PLAYTEST_SUITE=smoke,core ...
```

Legacy: `PLAYTEST=1` or `ZDTD_PLAYTEST=1` arms `demo`.

## Suites (catalog summary)

Full tables: **[SCENARIOS.md](SCENARIOS.md)** (every Live case id). Approximate
built-in counts from `Catalog.cs` (104 Live, 0 Defer):

| Suite | Live cases (approx) |
|---|---:|
| `smoke` | 5 (`join_ready`, `cgo_ready`, `ground`, `stats`, `day_clock`, …) |
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
