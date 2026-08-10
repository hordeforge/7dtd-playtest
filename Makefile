ROOT := $(CURDIR)
GAME ?= $(HOME)/.local/share/Steam/steamapps/common/7 Days To Die
MOD_NAME := zdtd-playtest
DIST := $(ROOT)/dist/$(MOD_NAME)
INSTALL_DIR := $(GAME)/Mods/$(MOD_NAME)
CONNECT_DIR := $(ROOT)/../7dtd-connect
SUITE ?= demo
# Default target is stock dedicated (Navezgane). Override SERVER=zdtd for zig dedi.
SERVER ?= stock
WORLD_NAME ?= Navezgane
GAME_NAME ?= PlaytestNav
WORLD ?= $(ROOT)/../zdtd/worlds/playtest_auto
# stock ServerPort default 26900; zdtd often 27025
PORT ?=
ADMIN_PORT ?= 8081
# Start every run from a clean world. The suites dig and place blocks, so a
# reused save accumulates holes under the test area until dig/place fail on the
# previous run's terrain rather than on anything the server did. FRESH=0 keeps
# the existing save when you deliberately want to inspect one.
FRESH ?= 1
LAPS ?= 1

DOTNET_ROOT ?= $(firstword \
  $(wildcard $(HOME)/.cache/dotnet-sdk) \
  $(wildcard $(HOME)/.dotnet) \
)
ifneq ($(DOTNET_ROOT),)
  export DOTNET_ROOT
  export PATH := $(DOTNET_ROOT):$(PATH)
endif

.PHONY: build install uninstall clean playtest playtest-smoke playtest-core \
	playtest-demo playtest-demo-fresh playtest-bench playtest-gate playtest-full \
	playtest-zdtd playtest-persist playtest-mp playtest-soak-long playtest-apm \
	playtest-residual install-pair

build:
	dotnet build "$(ROOT)/Source/PlayTestMod/PlayTestMod.csproj" -c Release -v q \
		-p:GameRoot="$(GAME)"
	cp -f "$(ROOT)/ModInfo.xml" "$(DIST)/"
	@echo "OK → $(DIST)"

install: build
	mkdir -p "$(INSTALL_DIR)"
	cp -f "$(DIST)/ModInfo.xml" "$(DIST)/zdtd-playtest.dll" "$(INSTALL_DIR)/"
	@echo "Installed → $(INSTALL_DIR)"

install-pair: install
	$(MAKE) -C "$(CONNECT_DIR)" install GAME="$(GAME)"

uninstall:
	rm -rf "$(INSTALL_DIR)"
	@echo "Removed $(INSTALL_DIR)"

clean:
	rm -rf "$(ROOT)/dist" "$(ROOT)/Source/PlayTestMod/bin" "$(ROOT)/Source/PlayTestMod/obj"

# Full host orchestration: stock dedicated (default) + client, score logs.
# SERVER=stock|zdtd  WORLD_NAME=Navezgane  PORT= (empty → backend default)
playtest: install-pair
	@mkdir -p "$(WORLD)"
	ZDTD_PLAYTEST_LAPS="$(LAPS)" \
	uv run --project "$(ROOT)" python "$(ROOT)/scripts/playtest_run.py" \
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

# All residual suites (mp + short soak in residual alias; persist/soak_long/apm separate).
playtest-residual:
	$(MAKE) playtest-persist SERVER="$(SERVER)"
	$(MAKE) playtest-mp SERVER="$(SERVER)"
	$(MAKE) playtest-apm
	$(MAKE) playtest-soak-long SERVER="$(SERVER)"
