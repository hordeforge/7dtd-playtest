# AGENTS.md - 7dtd-playtest

Stock-client **gameplay automation** against real servers (primarily **zdtd**).
Drive stock APIs and assert observable state. Prefer missing over fakes.

Workspace: [`../AGENTS.md`](../AGENTS.md).  
Design: [`../zdtd/docs/CLIENT_PLAYTEST.md`](../zdtd/docs/CLIENT_PLAYTEST.md).  
Join plumbing: [`../7dtd-connect/`](../7dtd-connect/).

## Owns

- Client mod `Mods/zdtd-playtest` (scenario runner, oracles, structured logs)
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
| `ZDTD_PLAYTEST_SUITE` | `smoke`, `core`, `smoke,core`, `all` |
| `ZDTD_PLAYTEST=1` | Legacy: arms `smoke,core` |
| `ZDTD_CONNECT` | Set by orchestrator / connect |

## Log contract

Lines prefixed `[zdtd-playtest]`:

- Human: `PASS|FAIL suite/case detail`
- JSON: `{"v":1,"t":"result|summary|done|log",...}`
- Terminal: `SUMMARY ...` then `DONE exit_hint=0|1`
