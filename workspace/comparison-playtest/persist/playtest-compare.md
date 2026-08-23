# Stock-vs-zdtd playtest comparison

| axis | stock | zdtd |
|---|---|---|
| cases PASS | 6 | 4 |
| cases FAIL | 1 | 2 |
| cases SKIP | 0 | 0 |

## Per-case

| case | stock | zdtd |
|---|---|---|
| `persist_setup/persist_setup_blockmeta` | PASS | FAIL |
| `persist_setup/persist_setup_dig` | PASS | PASS |
| `persist_setup/persist_setup_done` | PASS | PASS |
| `persist_setup/persist_setup_inv` | PASS | PASS |
| `persist_setup/persist_setup_pos` | PASS | PASS |
| `persist_setup/persist_setup_te` | PASS | FAIL |

## Findings

- persist_setup/persist_setup_blockmeta: status differs (PASS vs FAIL)
- persist_setup/persist_setup_te: status differs (PASS vs FAIL)

*Triage each finding: zdtd bug vs harness artifact vs known divergence. Known divergences are recorded in zdtd-server/docs/PROVENANCE.md (divergence register).*
