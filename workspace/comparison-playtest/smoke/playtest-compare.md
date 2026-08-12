# Stock-vs-zdtd playtest comparison

| axis | stock | zdtd |
|---|---|---|
| cases PASS | 5 | 5 |
| cases FAIL | 0 | 0 |
| cases SKIP | 0 | 0 |

## Per-case

| case | stock | zdtd |
|---|---|---|
| `smoke/cgo_ready` | PASS | PASS |
| `smoke/day_clock` | PASS | PASS |
| `smoke/ground` | PASS | PASS |
| `smoke/join_ready` | PASS | PASS |
| `smoke/stats` | PASS | PASS |

## Findings

- no per-case status differences

*Triage each finding: zdtd bug vs harness artifact vs known divergence. Known divergences are recorded in zdtd/docs/PROVENANCE.md (divergence register).*
