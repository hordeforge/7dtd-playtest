#!/usr/bin/env python3
"""Structural guard for the public external-scenario provider contract."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from playtest_log import barrier_hits_prefix

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "Source" / "PlayTestMod" / "Runner.cs"
CASEDEF = ROOT / "Source" / "PlayTestMod" / "CaseDef.cs"
CATALOG = ROOT / "Source" / "PlayTestMod" / "Catalog.cs"
PROVIDER = ROOT / "Source" / "PlayTestMod" / "ScenarioProvider.cs"
HELPERS_GLOB = sorted((ROOT / "Source" / "PlayTestMod").glob("Helpers*.cs"))
REPORT = ROOT / "Source" / "PlayTestMod" / "Report.cs"
README = ROOT / "README.md"
AGENTS = ROOT / "AGENTS.md"
MAKEFILE = ROOT / "Makefile"
SCENARIOS = ROOT / "SCENARIOS.md"


def method_body(src: str, signature_re: str) -> str:
    """Return the C# method body between the first matching signature and its closing brace."""
    m = re.search(signature_re, src)
    assert m, f"method not found: {signature_re}"
    i = m.end()
    # skip whitespace to opening brace
    while i < len(src) and src[i] in " \t\r\n":
        i += 1
    assert i < len(src) and src[i] == "{", f"expected '{{' after {signature_re}"
    depth = 0
    start = i
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"unclosed body for {signature_re}")


def main() -> int:
    runner = RUNNER.read_text(encoding="utf-8")
    casedef = CASEDEF.read_text(encoding="utf-8")
    catalog = CATALOG.read_text(encoding="utf-8")
    provider = PROVIDER.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    parameterized_log = "\n".join(
        (
            "[7dtd-playtest] barrier spawn_vehicle:vehicleGyrocopter",
            '[7dtd-playtest] {"v":1,"t":"barrier","name":"spawn_vehicle:vehicleGyrocopter"}',
            "[7dtd-playtest] barrier spawn_vehicle:vehicleGyrocopter",
            "[7dtd-playtest] barrier spawn_vehicle:vehicleBicycle",
        )
    )
    assert barrier_hits_prefix(parameterized_log, "spawn_vehicle:") == [
        "spawn_vehicle:vehicleGyrocopter",
        "spawn_vehicle:vehicleGyrocopter",
        "spawn_vehicle:vehicleBicycle",
    ], "repeated parameterized barriers must remain separate fixture requests"

    # The provider-facing case contract lives in its own file (CaseDef.cs),
    # beside the other public surfaces (Report.cs, MiningProbe.cs, Helpers).
    assert "public sealed class CaseDef" in casedef
    assert "public sealed class CaseCtx" in casedef
    assert "public enum PlayerGate" in casedef
    assert "public interface IScenarioProvider" in provider
    assert "IEnumerable<string> SuiteIds" in provider
    assert "void AppendSuite(List<CaseDef> queue, string suite, int lap)" in provider
    assert "AppDomain.CurrentDomain.GetAssemblies()" in provider
    assert "ScenarioProviders.AppendSuite(q, suite, lap);" in catalog
    assert "ScenarioProviders.SuiteIds()" in catalog

    # Public factories on CaseDef (external providers must not hand-build fields).
    assert re.search(
        r"public\s+static\s+CaseDef\s+Live\s*\(",
        casedef,
    ), "CaseDef.Live must be a public static factory"
    assert re.search(
        r"public\s+static\s+CaseDef\s+Defer\s*\(",
        casedef,
    ), "CaseDef.Defer must be a public static factory"
    assert re.search(
        r"public\s+static\s+CaseDef\s+Staged\s*\(",
        casedef,
    ), "CaseDef.Staged must be a public static factory"
    assert re.search(
        r"public\s+static\s+CaseDef\s+StagedClip\s*\(",
        casedef,
    ), "CaseDef.StagedClip must be a public static factory"

    # The clip contract is part of the public surface and the log contract:
    # a StagedClip case writes frames from inside the game and emits the
    # completion marker capture_video.sh waits for.
    helpers = "\n".join(p.read_text(encoding="utf-8") for p in HELPERS_GLOB)
    assert "public static string CaptureClipFrame" in helpers, (
        "Helpers.CaptureClipFrame must be public for staged-clip providers"
    )
    assert "clip complete " in casedef, (
        "CaseDef.StagedClip must emit the 'clip complete' completion line"
    )
    assert "clip complete" in readme, (
        "README's log contract must document the 'clip complete' line"
    )
    assert "StagedClip" in readme, (
        "README's public surface must document CaseDef.StagedClip"
    )
    clip_body = method_body(
        casedef,
        r"public\s+static\s+CaseDef\s+StagedClip\s*\([^)]*\)",
    )
    assert "CaptureClipFrame(id, ctx.IntB" in clip_body, (
        "StagedClip must capture a frame sequence, not one shot"
    )
    assert "clipFps" in clip_body, "StagedClip must take a clipFps cadence"

    live_body = method_body(
        casedef,
        r"public\s+static\s+CaseDef\s+Live\s*\([^)]*\)",
    )
    defer_body = method_body(
        casedef,
        r"public\s+static\s+CaseDef\s+Defer\s*\([^)]*\)",
    )

    # Live path: non-deferred CaseDef (explicit Deferred = false, never true).
    assert "Deferred = false" in live_body or "Deferred=false" in live_body.replace(
        " ", ""
    ), "CaseDef.Live must set Deferred = false"
    assert not re.search(r"Deferred\s*=\s*true", live_body), (
        "CaseDef.Live must not set Deferred = true"
    )
    assert "Act = act" in live_body or "Act=act" in live_body.replace(" ", "")
    assert "new CaseDef" in live_body

    # Deferred path: skip with reason.
    assert re.search(r"Deferred\s*=\s*true", defer_body), (
        "CaseDef.Defer must set Deferred = true"
    )
    assert "DeferReason" in defer_body, "CaseDef.Defer must set DeferReason"
    assert "new CaseDef" in defer_body
    # Deferred cases do not require Act/Wait/Assert.
    assert not re.search(r"\bAct\s*=", defer_body), (
        "CaseDef.Defer should not assign Act"
    )

    # Built-in catalog shares the public path (thin wrappers, not a second oracle).
    cat_live = method_body(
        catalog,
        r"static\s+CaseDef\s+Live\s*\([^)]*\)",
    )
    cat_defer = method_body(
        catalog,
        r"static\s+CaseDef\s+Defer\s*\([^)]*\)",
    )
    assert "CaseDef.Live(" in cat_live, (
        "Catalog.Live must delegate to CaseDef.Live"
    )
    assert "CaseDef.Defer(" in cat_defer, (
        "Catalog.Defer must delegate to CaseDef.Defer"
    )
    # Catalog wrappers must not re-implement field assignment.
    assert "new CaseDef" not in cat_live, (
        "Catalog.Live must not construct CaseDef by hand"
    )
    assert "new CaseDef" not in cat_defer, (
        "Catalog.Defer must not construct CaseDef by hand"
    )

    # Helpers is one public static class split across partial-class files
    # (Helpers.Ui.cs, Helpers.World.cs, ...); assert against the joined text.
    assert HELPERS_GLOB, "Helpers partial files missing"
    helpers = "\n".join(p.read_text(encoding="utf-8") for p in HELPERS_GLOB)
    report = REPORT.read_text(encoding="utf-8")
    agents = AGENTS.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")
    scenarios = SCENARIOS.read_text(encoding="utf-8")

    # README documents the public entry points for provider authors.
    assert "CaseDef.Live" in readme, "README must document CaseDef.Live"
    assert "CaseDef.Defer" in readme, "README must document CaseDef.Defer"

    # Public Helpers + Report for external providers (give/equip/vehicle/barriers).
    assert re.search(r"public\s+static\s+(partial\s+)?class\s+Helpers\b", helpers), (
        "Helpers must be public static for external providers"
    )
    assert re.search(r"public\s+static\s+class\s+Report\b", report), (
        "Report must be public static for external providers"
    )
    assert "public static void Barrier" in report or re.search(
        r"public\s+static\s+void\s+Barrier\s*\(", report
    ), "Report.Barrier must stay public"
    for name in (
        "TryGiveItem",
        "TryEquipItemType",
        "PlayerInVehicle",
        "TryEnterVehicle",
        "FindNearestVehicle",
        "LookAt",
        "CountItemType",
        "PushPlayerInventory",
        "SetBlockRpc",
        "FreeBagSlots",
    ):
        assert "public static" in helpers and name in helpers, (
            f"Helpers must expose {name} for providers"
        )

    probe = (ROOT / "Source" / "PlayTestMod" / "MiningProbe.cs").read_text(
        encoding="utf-8"
    )
    assert "public sealed class MiningProbe" in probe
    assert "public sealed class MiningSpec" in probe
    assert "public sealed class MiningResult" in probe
    assert "MiningProbe" in readme, "README must document MiningProbe"

    # EntityPlayerLocal.SetRotation uses negative X below the horizon. Keep
    # LookAt aligned with that stock convention: a target with dir.y < 0 must
    # produce a negative pitch, not the skyward positive pitch used before.
    look_at = method_body(
        helpers,
        r"public\s+static\s+void\s+LookAt\s*\(\s*EntityPlayerLocal\s+player\s*,"
        r"\s*Vector3\s+worldPos\s*\)",
    )
    assert "float pitch = Mathf.Asin(" in look_at, (
        "LookAt must preserve the stock vertical convention: below is negative X pitch"
    )
    assert "float pitch = -Mathf.Asin(" not in look_at, (
        "LookAt inverted vertical pitch and will aim below-horizon targets into the sky"
    )

    # Dual suite env: PLAYTEST_SUITE and ZDTD_PLAYTEST_SUITE both arm the runner.
    arm = method_body(runner, r"public\s+static\s+void\s+ArmFromEnv\s*\(\s*\)")
    assert "PLAYTEST_SUITE" in arm and "ZDTD_PLAYTEST_SUITE" in arm, (
        "ArmFromEnv must accept PLAYTEST_SUITE and ZDTD_PLAYTEST_SUITE"
    )
    assert "ZDTD_PLAYTEST_LAPS" in arm or "PLAYTEST_LAPS" in arm

    # A suite that appends nothing (typo'd id, uninstalled provider) must be a
    # recorded failure, never a silent green run with zero cases.
    build_body = method_body(runner, r"static\s+void\s+BuildQueue\s*\(\s*\)")
    assert '"(unknown)"' in build_body and "Report.Result(" in build_body, (
        "BuildQueue must record a FAIL row for every suite that produced no cases"
    )
    assert "_queue.Count == 0" in arm and "Report.Done()" in arm, (
        "an entirely empty queue must finish at arm time (DONE exit_hint=1), "
        "not wait out the join for an empty pass"
    )

    # Residual client alias stays light; Make residual is multi-target.
    m_res = re.search(
        r'case\s+"residual"\s*:(.*?)break\s*;',
        catalog,
        flags=re.DOTALL,
    )
    assert m_res, "Catalog ExpandSuites must have residual case"
    res_snip = m_res.group(1)
    adds = re.findall(r'AddUnique\s*\(\s*list\s*,([^)]+)\)', res_snip)
    add_blob = " ".join(adds)
    assert '"mp"' in add_blob and '"soak"' in add_blob, (
        "residual client alias must expand to mp + soak"
    )
    assert '"persist"' not in add_blob and '"soak_long"' not in add_blob, (
        "residual client alias must not expand to persist/soak_long "
        "(those need host multi-target make playtest-residual)"
    )
    # Dead code guard removed: comments may mention host residual suites.
    assert "playtest-persist" in makefile and "playtest-residual:" in makefile
    assert "playtest-mp" in makefile and "playtest-soak-long" in makefile

    # Docs contracts for external providers (Atomic upstream gaps).
    for needle in (
        "fresh-save",
        "FRESH",
        "Report.Barrier",
        "persist_setup",
        "--rejoin-setup-suite",
        "--rejoin-setup-barrier",
        "--rejoin-teleport",
        "timeout:",
        "Stable log contract",
        "ZDTD_PLAYTEST_SUITE",
        "residual",
        "playtest-residual",
    ):
        assert needle in readme or needle.lower() in readme.lower(), (
            f"README must document provider contract: {needle}"
        )
    assert "ZDTD_PLAYTEST_SUITE" in agents
    assert "residual" in scenarios.lower() and "playtest-residual" in scenarios

    # Long-timeout Live factory parameter still present.
    assert "timeout" in live_body or "TimeoutSec = timeout" in live_body

    # Fail-fast construction: a case with no callback would record a green
    # pass while running nothing, and a non-positive timeout has no meaning.
    assert "throw new ArgumentException" in live_body, (
        "CaseDef.Live must reject a case with no act/wait/assert"
    )
    assert "throw new ArgumentOutOfRangeException" in live_body, (
        "CaseDef.Live must reject timeout <= 0"
    )

    # Provider error-surface docs: exception behavior + diagnostics helper.
    for needle in ("CaseStartUnscaled", "Report.Info", "Provider error behavior"):
        assert needle in readme, (
            f"README must document provider surface detail: {needle}"
        )

    print("OK external scenario-provider surface")
    print("OK public CaseDef.Live / CaseDef.Defer factories")
    print("OK Live is non-deferred; Defer sets Deferred+reason")
    print("OK Catalog Live/Defer share CaseDef factories")
    print("OK README documents CaseDef.Live/Defer")
    print("OK public Helpers + Report.Barrier for providers")
    print("OK dual PLAYTEST_SUITE / ZDTD_PLAYTEST_SUITE arming")
    print("OK unknown/empty suite is a recorded failure, not a green pass")
    print("OK residual client alias vs make playtest-residual split")
    print("OK provider fresh-save / barrier / long-timeout docs")

    # Client mute default-on contract (opt-out via CLIENT_MUTE=0).
    orch = (ROOT / "scripts" / "playtest_run.py").read_text(encoding="utf-8")
    assert "def client_mute_enabled" in orch
    assert "def mute_client_audio_async" in orch
    assert "mute_client_audio_async()" in orch
    assert 'or "1"' in orch
    assert "CLIENT_MUTE" in orch and "CLIENT_MUTE=0" in readme
    assert "mute" in readme.lower() and "default" in readme.lower()
    print("OK client mute default-on (opt-out CLIENT_MUTE=0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
