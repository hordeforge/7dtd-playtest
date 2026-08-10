# zdtd-playtest

Stock-client **gameplay automation** for **zdtd** (EAC off). Drives real client
APIs, waits for server-visible state where it matters, and emits structured
`[zdtd-playtest]` results for a host orchestrator.

Join/auto-connect is **not** here: install [`../7dtd-connect/`](../7dtd-connect/)
as well. Design: [`../zdtd/docs/CLIENT_PLAYTEST.md`](../zdtd/docs/CLIENT_PLAYTEST.md).

## Requirements

- Stock client V3.x, EAC off (`-noeac`)
- `0_TFP_Harmony`
- `7dtd-connect` installed
- Game: `~/.local/share/Steam/steamapps/common/7 Days To Die` (`GAME=`)
- For orchestrated runs: built `zdtd` at `../zdtd/zig-out/bin/zdtd`
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
| `persist` | Multi-phase rejoin verify |
| `mp` | Multi-peer via loadgen |
| `soak_long` | ≥15 min host soak digs |
| `apm` | zdtd APM dump barrier |

Orchestrator exit codes:

| Code | Meaning |
|---|---|
| 0 | All cases passed (`DONE exit_hint=0`) |
| 1 | One or more case failures |
| 2 | Harness error (no DONE, server/client missing, timeout) |

Reports land under `~/.cache/zdtd-playtest/report-*.json` (override `LOGDIR=`).

## Manual / pair launch

```bash
# terminal 1: zdtd with admin if you want
# terminal 2:
ZDTD_CONNECT=127.0.0.1:27025 ZDTD_PLAYTEST_SUITE=smoke,core \
  ../7dtd-connect/scripts/launch_client.sh
```

Legacy: `ZDTD_PLAYTEST=1` arms `smoke,core`.

## Suites (Phase A)

| Suite | Cases |
|---|---|
| `smoke` | `join_ready`, `ground`, `stats` |
| `core` | `look`, `walk_motor`, `inventory`, `dig_confirm`, `place_confirm`, `craft_open`, `quests`, `buffs` |

`dig_confirm` / `place_confirm` **wait** until `World.GetBlock` reflects the
RPC (or timeout fail). That is the fidelity gate unit tests cannot replace.

## Log contract

```text
[zdtd-playtest] armed suites=smoke,core ...
[zdtd-playtest] PASS smoke/join_ready ...
[zdtd-playtest] {"v":1,"t":"result","suite":"smoke","case":"join_ready",...}
[zdtd-playtest] SUMMARY pass=N fail=M skip=K total=T
[zdtd-playtest] DONE exit_hint=0
```

## Rules

- Drive + assert only. No invented chunks, signs, or swallowed NREs.
- Server gaps are fixed in **zdtd**, not this mod.
- See [AGENTS.md](AGENTS.md).
