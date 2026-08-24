ROOT := $(CURDIR)
GAME ?= $(HOME)/.local/share/Steam/steamapps/common/7 Days To Die
MOD_NAME := 7dtd-playtest
DIST := $(ROOT)/dist/$(MOD_NAME)
INSTALL_DIR := $(GAME)/Mods/$(MOD_NAME)
CONNECT_DIR := $(ROOT)/../7dtd-fastconnect
SUITE ?= demo
# Default target is stock dedicated (Navezgane). Override SERVER=zdtd for zig dedi.
SERVER ?= stock
WORLD_NAME ?= Navezgane
GAME_NAME ?= PlaytestNav
WORLD ?= $(ROOT)/../zdtd-server/worlds/playtest_auto
# stock ServerPort default 26900; zdtd often 27025
PORT ?=
ADMIN_PORT ?= 8081
# Start every run from a clean world. The suites dig and place blocks, so a
# reused save accumulates holes under the test area until dig/place fail on the
# previous run's terrain rather than on anything the server did. FRESH=0 keeps
# the existing save when you deliberately want to inspect one.
FRESH ?= 1
LAPS ?= 1

# Bare `make` prints the target list instead of starting a dotnet build that
# fails cryptically on machines without the game SDK layout.
.DEFAULT_GOAL := help

DOTNET_ROOT ?= $(firstword \
  $(wildcard $(HOME)/.cache/dotnet-sdk) \
  $(wildcard $(HOME)/.dotnet) \
)
ifneq ($(DOTNET_ROOT),)
  export DOTNET_ROOT
  export PATH := $(DOTNET_ROOT):$(PATH)
endif

.PHONY: help build install uninstall clean test test-one coverage lint typecheck check dst dst-soak playtest playtest-smoke \
	playtest-core \
	playtest-demo playtest-demo-fresh playtest-bench playtest-gate playtest-full \
	playtest-zdtd playtest-persist playtest-mp playtest-soak-long playtest-apm \
	playtest-residual install-pair playtest-compare playtest-repeat

help:
	@echo "Offline dev loop (no game install needed):"
	@echo "  make test                        run all offline gates (lint + typecheck + suites)"
	@echo "  make test-one GATE=test_dst.py   run one gate (file name under scripts/)"
	@echo "  make lint                        ruff over scripts/ ([tool.ruff] in pyproject.toml)"
	@echo "  make typecheck                   mypy over scripts/ ([tool.mypy] in pyproject.toml)"
	@echo "  make dst [DST_SEEDS=200]         lock deterministic-simulation sweep"
	@echo "  make dst-soak [DST_SOAK_SEC=300] tail-bug hunt: fresh seeds until stopped"
	@echo "  make check                       everything CI runs: test + dst DST_SEEDS=200"
	@echo
	@echo "Mod build (needs dotnet SDK 8.0.x + game at GAME=):"
	@echo "  make build | install | install-pair | uninstall | clean"
	@echo
	@echo "Live suites (needs game client; see README):"
	@echo "  make playtest SUITE=demo SERVER=stock|zdtd"
	@echo "  make playtest-smoke | playtest-gate | playtest-demo | playtest-bench LAPS=3"
	@echo "  make playtest-zdtd | playtest-compare | playtest-repeat LAPS=3"
	@echo "  make playtest-residual           persist + mp + apm + soak_long"

build:
	@test -f "$(GAME)/7DaysToDie.x86_64" -o -f "$(GAME)/7DaysToDie.exe" || { \
		echo "game not found at GAME=$(GAME)"; \
		echo "set GAME=/path/to/7 Days To Die (csproj needs its DLLs to reference)"; \
		exit 2; }
	dotnet build "$(ROOT)/Source/PlayTestMod/PlayTestMod.csproj" -c Release -v q \
		-p:GameRoot="$(GAME)" -p:RestoreLockedMode=true
	cp -f "$(ROOT)/ModInfo.xml" "$(DIST)/"
	@echo "OK → $(DIST)"

install: build
	mkdir -p "$(INSTALL_DIR)"
	cp -f "$(DIST)/ModInfo.xml" "$(DIST)/7dtd-playtest.dll" "$(INSTALL_DIR)/"
	@echo "Installed → $(INSTALL_DIR)"

install-pair:
	@test -d "$(CONNECT_DIR)" || { \
		echo "7dtd-fastconnect not found at $(CONNECT_DIR)"; \
		echo "clone it first (see README: Join/auto-connect), or install playtest only:"; \
		echo "  make install"; \
		exit 2; }
	$(MAKE) install
	$(MAKE) -C "$(CONNECT_DIR)" install GAME="$(GAME)"

uninstall:
	rm -rf "$(INSTALL_DIR)"
	@echo "Removed $(INSTALL_DIR)"

clean:
	rm -rf "$(ROOT)/dist" "$(ROOT)/Source/PlayTestMod/bin" "$(ROOT)/Source/PlayTestMod/obj"

# All host Python goes through uv so every machine uses one interpreter
# honoring requires-python >=3.11 (bare python3 may be older on some distros).
# --locked fails instead of silently re-resolving when pyproject.toml and
# uv.lock disagree, so a build can never drift from the committed lock.
UV := uv run --locked --project "$(ROOT)" python

# Lint gate: ruff with the defect-oriented rule set from pyproject.toml
# ([tool.ruff]). Same locked tool version locally and in CI.
lint:
	@cd "$(ROOT)" && uv run --locked ruff check scripts

# Type gate: mypy baseline strictness from pyproject.toml ([tool.mypy]).
typecheck:
	@cd "$(ROOT)" && uv run --locked python -m mypy scripts

test: lint typecheck
	$(UV) "$(ROOT)/scripts/test_catalog_surface.py"
	$(UV) "$(ROOT)/scripts/test_version_surface.py"
	$(UV) "$(ROOT)/scripts/test_scenario_provider_surface.py"
	$(UV) "$(ROOT)/scripts/test_mining_probe_surface.py"
	$(UV) "$(ROOT)/scripts/test_stock_peer_client.py"
	$(UV) "$(ROOT)/scripts/test_playtest_lock.py"
	$(UV) "$(ROOT)/scripts/test_dst.py"
	$(UV) "$(ROOT)/scripts/test_no_unbound_locals.py"
	$(UV) "$(ROOT)/scripts/test_report_surface.py"
	$(UV) "$(ROOT)/scripts/test_playtest_run_units.py"
	$(UV) "$(ROOT)/scripts/test_playtest_compare.py"

# Line coverage of the orchestrator modules under the same offline suites
# `make test` runs (same order, same interpreter pin). Writes .coverage in
# the repo root; CI renders it into the README badge with
# scripts/coverage_badge.py. Subprocess-based DST simulation is not traced.
COV := uv run --locked --project "$(ROOT)" --with coverage python

coverage:
	rm -f .coverage .coverage.*
	$(COV) -m coverage run --append --source=scripts "$(ROOT)/scripts/test_catalog_surface.py"
	$(COV) -m coverage run --append --source=scripts "$(ROOT)/scripts/test_version_surface.py"
	$(COV) -m coverage run --append --source=scripts "$(ROOT)/scripts/test_scenario_provider_surface.py"
	$(COV) -m coverage run --append --source=scripts "$(ROOT)/scripts/test_stock_peer_client.py"
	$(COV) -m coverage run --append --source=scripts "$(ROOT)/scripts/test_playtest_lock.py"
	$(COV) -m coverage run --append --source=scripts "$(ROOT)/scripts/test_dst.py"
	$(COV) -m coverage run --append --source=scripts "$(ROOT)/scripts/test_no_unbound_locals.py"
	$(COV) -m coverage run --append --source=scripts "$(ROOT)/scripts/test_report_surface.py"
	$(COV) -m coverage run --append --source=scripts "$(ROOT)/scripts/test_playtest_run_units.py"
	$(COV) -m coverage run --append --source=scripts "$(ROOT)/scripts/test_playtest_compare.py"
	$(COV) -m coverage report -m

# One gate while iterating: make test-one GATE=test_dst.py
GATE ?=
test-one:
	@test -n "$(GATE)" || { \
		echo "usage: make test-one GATE=<gate file name, e.g. GATE=test_dst.py>"; \
		exit 2; }
	@test -f "$(ROOT)/scripts/$(GATE)" || { \
		echo "unknown gate: scripts/$(GATE)"; \
		exit 2; }
	$(UV) "$(ROOT)/scripts/$(GATE)"

# The full local verification, identical to .github/workflows/ci.yml.
check:
	$(MAKE) test
	$(MAKE) dst DST_SEEDS=200

# Deterministic simulation of the exclusivity lock. No game, no server, no
# wall-clock waiting: DST_SEEDS runs of simulated multi-agent contention with
# crash / torn-write / corruption / clock-skew faults, all driven by one seed
# each. A failure prints the seed and the command to replay it exactly.
DST_SEEDS ?= 200
DST_AGENTS ?= 3
dst:
	$(UV) "$(ROOT)/scripts/dst_run.py" --regressions
	$(UV) "$(ROOT)/scripts/dst_run.py" --iterations "$(DST_SEEDS)" --agents "$(DST_AGENTS)"

# Tail-bug hunt: keep drawing fresh seeds for DST_SOAK_SEC wall seconds and
# record any failing seed in scripts/dst_seeds.txt.
DST_SOAK_SEC ?= 300
dst-soak:
	$(UV) "$(ROOT)/scripts/dst_run.py" --soak "$(DST_SOAK_SEC)" \
		--agents "$(DST_AGENTS)" --record --quiet

# Full host orchestration: stock dedicated (default) + client, score logs.
# SERVER=stock|zdtd  WORLD_NAME=Navezgane  PORT= (empty → backend default)
playtest: install-pair
	@mkdir -p "$(WORLD)"
	PLAYTEST_LAPS="$(LAPS)" \
	$(UV) "$(ROOT)/scripts/playtest_run.py" \
		--server "$(SERVER)" \
		--suite "$(SUITE)" \
		--world-name "$(WORLD_NAME)" \
		--game-name "$(GAME_NAME)" \
		--world "$(WORLD)" \
		$(if $(PORT),--port "$(PORT)",) \
		--admin-port "$(ADMIN_PORT)" \
		$(if $(filter-out 0,$(FRESH)),--fresh-save,) \
		$(EXTRA_ARGS)

playtest-smoke:
	$(MAKE) playtest SUITE=smoke SERVER="$(SERVER)"

playtest-core:
	$(MAKE) playtest SUITE=gate SERVER="$(SERVER)"

playtest-demo:
	$(MAKE) playtest SUITE=demo SERVER="$(SERVER)"

playtest-demo-fresh:
	$(MAKE) playtest SUITE=demo SERVER="$(SERVER)" EXTRA_ARGS="--fresh-save"

playtest-bench:
	$(MAKE) playtest SUITE=benchmark SERVER="$(SERVER)" LAPS="$(LAPS)"

playtest-gate:
	$(MAKE) playtest SUITE=gate SERVER="$(SERVER)"

playtest-full:
	$(MAKE) playtest SUITE=full SERVER="$(SERVER)"

playtest-zdtd:
	$(MAKE) playtest SUITE=demo SERVER=zdtd PORT=27025

# Multi-phase rejoin (setup → saveworld → rejoin verify).
playtest-persist:
	$(MAKE) playtest SUITE=persist SERVER="$(SERVER)" EXTRA_ARGS="--fresh-save --timeout 600"

# Multi-peer via loadgen bots.
playtest-mp:
	$(MAKE) playtest SUITE=mp SERVER="$(SERVER)" EXTRA_ARGS="--fresh-save --timeout 400"

# Real ≥15 min host soak (wall clock).
playtest-soak-long:
	$(MAKE) playtest SUITE=soak_long SERVER="$(SERVER)" EXTRA_ARGS="--fresh-save --timeout 1200"

# Flake detection: run a suite LAPS times, fresh server each lap, aggregate
# the per-lap report JSON (playtest_repeat.sh). Exit nonzero unless every lap
# is clean. LAPS?=3; SUITE?=demo; extra orchestrator args via EXTRA_ARGS.
playtest-repeat:
	bash scripts/playtest_repeat.sh --laps "$(LAPS)" --suite "$(SUITE)" $(EXTRA_ARGS)

# zdtd APM dump attach.
playtest-apm:
	$(MAKE) playtest SUITE=apm SERVER=zdtd PORT=27025 EXTRA_ARGS="--timeout 300"

# Same suite against stock AND zdtd, diffed per case into
# workspace/comparison-playtest/<suite>/playtest-compare.{md,json}. A per-case
# delta is a finding to triage (zdtd bug vs harness artifact vs known
# divergence), never a pass to fake. SUITE?=smoke
#
# Integrity rule: the side dirs are wiped before each side runs, so a side
# that fails to start (port collision, missing binary, refused lock) leaves NO
# report; playtest_compare.py then refuses to diff (exit 2) instead of
# re-diffing stale logs from a previous session. The stale evidence is removed
# on failure so a phantom "compared" result cannot survive. The per-side `||
# true` only tolerates suites whose cases FAIL, not sides that never ran.
playtest-compare: install-pair
	@rm -rf "$(ROOT)/workspace/comparison-playtest/$(SUITE)"
	@mkdir -p "$(ROOT)/workspace/comparison-playtest/$(SUITE)/stock" \
		"$(ROOT)/workspace/comparison-playtest/$(SUITE)/zdtd"
	$(MAKE) playtest SUITE="$(SUITE)" SERVER=stock \
		EXTRA_ARGS="--logdir $(ROOT)/workspace/comparison-playtest/$(SUITE)/stock" || true
	$(MAKE) playtest SUITE="$(SUITE)" SERVER=zdtd PORT=27025 \
		EXTRA_ARGS="--logdir $(ROOT)/workspace/comparison-playtest/$(SUITE)/zdtd" || true
	$(UV) "$(ROOT)/scripts/playtest_compare.py" \
		--stock-dir "$(ROOT)/workspace/comparison-playtest/$(SUITE)/stock" \
		--zdtd-dir "$(ROOT)/workspace/comparison-playtest/$(SUITE)/zdtd" \
		--out "$(ROOT)/workspace/comparison-playtest/$(SUITE)" \
		--require-fresh-minutes 180 \
		|| { rm -f "$(ROOT)/workspace/comparison-playtest/$(SUITE)/playtest-compare.md" \
			"$(ROOT)/workspace/comparison-playtest/$(SUITE)/playtest-compare.json"; exit 1; }

# All residual suites (mp + short soak in residual alias; persist/soak_long/apm separate).
# Full residual promotion gate (four host targets). Not the same as client
# suite alias residual (= mp + short soak only). See README residual split.
playtest-residual:
	$(MAKE) playtest-persist SERVER="$(SERVER)"
	$(MAKE) playtest-mp SERVER="$(SERVER)"
	$(MAKE) playtest-apm
	$(MAKE) playtest-soak-long SERVER="$(SERVER)"
