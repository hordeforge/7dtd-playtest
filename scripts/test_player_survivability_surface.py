#!/usr/bin/env python3
"""Structural guard for the public PlayerSurvivability helper.

Spawn-screen recovery is the runner's job: a LivePlayer case must not start
against a corpse or an open spawn-selection window. This gate reads the
shipped C#; it does not reimplement the helper. Failure-path checks mutate a
private copy, never the worktree.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SURV = ROOT / "Source" / "PlayTestMod" / "PlayerSurvivability.cs"
RUNNER = ROOT / "Source" / "PlayTestMod" / "Runner.cs"
MAKEFILE = ROOT / "Makefile"
README = ROOT / "README.md"
AGENTS = ROOT / "AGENTS.md"
CHANGELOG = ROOT / "CHANGELOG.md"


def method_body(src: str, signature_re: str) -> str:
    m = re.search(signature_re, src)
    assert m, f"method not found: {signature_re}"
    i = m.end()
    while i < len(src) and src[i] in " \t\r\n":
        i += 1
    assert i < len(src) and src[i] == "{", f"expected '{{' after {signature_re}"
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"unclosed body for {signature_re}")


def try_press_calls_spawn_button(src: str) -> bool:
    body = method_body(src, r"public\s+static\s+bool\s+TryPressSpawn\s*\([^)]*\)")
    return "SpawnButtonPressed" in body


def try_press_returns_when_closed(src: str) -> bool:
    body = method_body(src, r"public\s+static\s+bool\s+TryPressSpawn\s*\([^)]*\)")
    return "IsWindowOpen" in body and "return false" in body


def ensure_fly_from_flag(src: str) -> bool:
    body = method_body(src, r"public\s+static\s+bool\s+Ensure\s*\([^)]*\)")
    return (
        "IsFlyMode.Value = fly" in body
        and "IsNoCollisionMode.Value = fly" in body
        and "toggleGodMode" in body
    )


def runner_invokes_recovery(runner: str) -> bool:
    recover = method_body(runner, r"static\s+void\s+RecoverLivePlayer\s*\([^)]*\)")
    play_ready = method_body(runner, r"static\s+bool\s+IsPlayReady\s*\([^)]*\)")
    advance = method_body(runner, r"static\s+void\s+AdvanceToNextCase\s*\([^)]*\)")
    return (
        "PlayerSurvivability.TryPressSpawn" in recover
        and "PlayerSurvivability.Ensure" in recover
        and "PlayerSurvivability.TryPressSpawn" in play_ready
        and "spawn-window" in play_ready
        and "RecoverLivePlayer" in advance
        and "PlayerGate.LivePlayer" in advance
    )


def runner_skips_respawn_setalive(runner: str) -> bool:
    healthy = method_body(runner, r"static\s+void\s+RecoverLivePlayer\s*\([^)]*\)")
    play_ready = method_body(runner, r"static\s+bool\s+IsPlayReady\s*\([^)]*\)")
    return (
        "Respawn(" not in healthy
        and "SetAlive(" not in healthy
        and "Respawn(" not in play_ready
        and "SetAlive(" not in play_ready
        and "EnsurePlayerHealthy" not in runner
    )


def main() -> int:
    src = SURV.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    agents = AGENTS.read_text(encoding="utf-8")
    changelog = CHANGELOG.read_text(encoding="utf-8")

    assert "public static class PlayerSurvivability" in src
    assert re.search(r"public\s+static\s+void\s+AddSurvivabilityGuard\s*\(", src)
    assert re.search(r"public\s+static\s+bool\s+TryPressSpawn\s*\(", src)
    assert re.search(r"public\s+static\s+bool\s+Ensure\s*\(", src)
    assert try_press_calls_spawn_button(src)
    assert try_press_returns_when_closed(src)
    assert ensure_fly_from_flag(src)
    assert "fly: false" in src or "fly:false" in src.replace(" ", "")
    assert runner_invokes_recovery(runner)
    assert runner_skips_respawn_setalive(runner)
    assert "PlayerSurvivability.Ensure" in runner
    assert "AllowDead" in runner
    assert "NoAutoHeal" in runner or "survivalCase" in runner

    assert "PlayerSurvivability" in readme
    assert "AddSurvivabilityGuard" in readme
    assert "TryPressSpawn" in readme
    assert "PlayerSurvivability" in agents
    assert "test_player_survivability_surface.py" in makefile
    assert "PlayerSurvivability" in changelog

    # Failure path on a private copy: the same predicates must reject a helper
    # that skips the spawn button, a God Mode that leaves fly on, or a runner
    # that still Respawn/SetAlive.
    broken_press = src.replace("window.SpawnButtonPressed(method)", "return false")
    assert not try_press_calls_spawn_button(broken_press), (
        "gate must fail when TryPressSpawn does not call SpawnButtonPressed"
    )
    broken_closed = method_body(src, r"public\s+static\s+bool\s+TryPressSpawn\s*\([^)]*\)")
    closed_broken_src = src.replace(broken_closed, "{\n                return true;\n            }")
    assert not try_press_returns_when_closed(closed_broken_src), (
        "gate must fail when TryPressSpawn presses without checking the window"
    )
    broken_fly = src.replace("player.IsFlyMode.Value = fly", "player.IsFlyMode.Value = true")
    assert not ensure_fly_from_flag(broken_fly), (
        "gate must fail when Ensure forces fly on"
    )
    broken_runner = runner.replace(
        "PlayerSurvivability.TryPressSpawn(ctx);",
        "p.Respawn(RespawnType.Died); p.SetAlive();",
    )
    assert not runner_invokes_recovery(broken_runner), (
        "gate must fail when the runner drops TryPressSpawn"
    )
    respawn_runner = runner.replace(
        "PlayerSurvivability.TryPressSpawn(ctx);",
        "p.Respawn(RespawnType.Died);\n                PlayerSurvivability.TryPressSpawn(ctx);",
    )
    assert not runner_skips_respawn_setalive(respawn_runner), (
        "gate must fail when RecoverLivePlayer still calls Respawn"
    )

    print("PASS public PlayerSurvivability / AddSurvivabilityGuard / TryPressSpawn")
    print("PASS TryPressSpawn calls SpawnButtonPressed and returns when closed")
    print("PASS Ensure writes fly/noclip from the fly flag")
    print("PASS runner LivePlayer recovery uses the helper, not Respawn/SetAlive")
    print("PASS failure path on a private copy")
    print("PASS docs and make test wire-up")
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
