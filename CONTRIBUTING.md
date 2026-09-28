# Contributing

The short path: branch, change, `make check`, open a PR. Every command below
is the repo's own gate surface; nothing here needs a game install.

## Setup

1. Linux x86_64 host with `make` and `git`.
2. Install [uv](https://docs.astral.sh/uv/), `shellcheck` and `yamllint`
   (e.g. `sudo apt install shellcheck yamllint`). These three are the only
   host tools the offline gates need beyond make/git: uv fetches the
   interpreter pinned by `.python-version` and every locked dev dependency on
   first use, shellcheck lints `scripts/*.sh`, and yamllint lints the shipped
   workflow YAML under `.github/`.
3. Check the host in one run, which names every missing tool and how to
   install it:

```bash
make doctor
```

4. Run every offline gate:

```bash
make test
```

`make lint` names a missing tool and how to install it instead of failing
somewhere inside the gate. A host that cannot install `yamllint` runs
`make lint SKIP_YAML=1`; CI never sets it, so the skipped gate still blocks
the merge.

For the mod build and live suites you additionally need dotnet SDK 8.0.400
or newer (`global.json`) and the game at `GAME=`; see README Requirements.

## The edit-test loop

| Command | What it does |
|---|---|
| `make test` | All offline gates (lint, typecheck, every suite script) |
| `make test-one GATE=test_dst.py` | One gate file while iterating |
| `make test-one GATE=test_playtest_compare.py ARGS="-k identical"` | One test inside the pytest-backed gate |
| `make lint` / `make typecheck` | The analysis gates alone |
| `make coverage` | Line coverage of `scripts/` under the same gates |
| `make check` | Exactly what CI runs (`test` + 200-seed DST sweep) |

## Before you open a PR

Run `make check`. Beyond style, the gates enforce repo conventions that are
easy to miss and will fail CI otherwise:

- **Changelog:** add a note under the existing `## [Unreleased]` heading in
  CHANGELOG.md. `test_version_surface.py` fails without one.
- **Catalog/doc sync:** adding or changing a case in
  `Source/PlayTestMod/Catalog.cs` requires the matching row in SCENARIOS.md;
  live rows and counts total must match (`test_catalog_surface.py`).
- **Version bumps:** ModInfo.xml, `ModIdentity.cs` `Version`, and CHANGELOG.md
  move together in one change (same gate).
- **Removing a public symbol:** every release is `0.Y.Z` and a minor may
  remove a symbol on the consumer contracts (`Helpers`, the log tokens, the
  suite/env surface, the lock payload), but the entry must say so: a
  `### Removed` section needs a `**Breaking.**` first line and a
  removed-to-replacement table, or `test_version_surface.py` fails the gate.
  One section per impact class per release (Added, Changed, Deprecated,
  Removed, Fixed, Security, in that order): a second `### Fixed` reads as a
  second group of notes and hides the removal, and the gate fails it. The
  pre-1.0 policy is written out at the top of CHANGELOG.md.
- **New offline gate:** a new `scripts/test_*.py` must be added to the
  `GATES` list in the Makefile. `test_gate_list.py` fails otherwise: an
  unlisted gate file runs under neither `make test` nor CI, so it would sit
  green and unexecuted. Copy an existing gate's shape (plain asserts, a
  `RESULT PASS` last line) rather than inventing a second runner.

## Live suites

Suites that drive the real client and server are documented in the README
(Requirements, One-command suites). They hold an exclusive lock on the shared
client, so never start one while another playtest may be running; the rules
are in AGENTS.md (Playtest / live-client exclusivity).
