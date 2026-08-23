# Stock-vs-zdtd playtest comparison

| axis | stock | zdtd |
|---|---|---|
| cases PASS | 1 | 0 |
| cases FAIL | 0 | 1 |
| cases SKIP | 0 | 0 |

## Per-case

| case | stock | zdtd |
|---|---|---|
| `soak_long/soak_15min_host` | PASS | FAIL |

## Findings

- soak_long/soak_15min_host: status differs (PASS vs FAIL)

*Triage each finding: zdtd bug vs harness artifact vs known divergence. Known divergences are recorded in zdtd-server/docs/PROVENANCE.md (divergence register).*
