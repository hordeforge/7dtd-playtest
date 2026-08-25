# Contributing

The short path: branch, change, `make check`, open a PR. Every command below
is the repo's own gate surface; nothing here needs a game install.

## Setup

1. Linux x86_64 host with `make` and `git`.
2. Install [uv](https://docs.astral.sh/uv/) and `shellcheck`
   (e.g. `sudo apt install shellcheck`). These are the only two host tools
   the offline gates need beyond make/git: uv fetches the interpreter pinned
   by `.python-version` and every locked dev dependency on first use.
3. Sanity check:

```bash
make test    # lint + typecheck + all offline suites (~10 s)
```

For the mod build and live suites you additionally need dotnet SDK 8.0.x
(pinned by `global.json`) and the game at `GAME=`; see README Requirements.

## The edit-test loop

| Command | What it does |
|---|---|
| `make test` | All offline gates (lint, typecheck, every suite script) |
| `make test-one GATE=test_dst.py` | One gate file while iterating |
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

## Live suites

Suites that drive the real client and server are documented in the README
(Requirements, One-command suites). They hold an exclusive lock on the shared
client, so never start one while another playtest may be running; the rules
are in AGENTS.md (Playtest / live-client exclusivity).
