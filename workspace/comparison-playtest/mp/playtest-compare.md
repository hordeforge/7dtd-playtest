# Stock-vs-zdtd playtest comparison

| axis | stock | zdtd |
|---|---|---|
| cases PASS | 6 | 6 |
| cases FAIL | 0 | 0 |
| cases SKIP | 0 | 0 |

## Per-case

| case | stock | zdtd |
|---|---|---|
| `mp/bots_plus_playtest` | PASS | PASS |
| `mp/chat_roundtrip` | PASS | PASS |
| `mp/lock_contention` | PASS | PASS |
| `mp/second_client_visible` | PASS | PASS |
| `mp/setblock_interest` | PASS | PASS |
| `mp/shared_quest` | PASS | PASS |

## Findings

- no per-case status differences

*Triage each finding: zdtd bug vs harness artifact vs known divergence. Known divergences are recorded in zdtd-server/docs/PROVENANCE.md (divergence register).*
