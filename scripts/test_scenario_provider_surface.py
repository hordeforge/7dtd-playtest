#!/usr/bin/env python3
"""Structural guard for the public external-scenario provider contract."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from csharp_surface import method_body
from playtest_log import barrier_hits_prefix

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "Source" / "PlayTestMod" / "Runner.cs"
CASEDEF = ROOT / "Source" / "PlayTestMod" / "CaseDef.cs"
CATALOG = ROOT / "Source" / "PlayTestMod" / "Catalog.cs"
PROVIDER = ROOT / "Source" / "PlayTestMod" / "ScenarioProvider.cs"
HELPERS_GLOB = sorted((ROOT / "Source" / "PlayTestMod").glob("Helpers*.cs"))
CLIPRECORDER = ROOT / "Source" / "PlayTestMod" / "ClipRecorder.cs"
REPORT = ROOT / "Source" / "PlayTestMod" / "Report.cs"
README = ROOT / "README.md"
AGENTS = ROOT / "AGENTS.md"
MAKEFILE = ROOT / "Makefile"
SCENARIOS = ROOT / "SCENARIOS.md"


def check_asset_name_round_trip() -> None:
    """The clip id a log line names must be the directory the frames land in.

    A collector reads the trailing directory out of the `clip complete` line
    and looks the frames up under it, so those are two halves of one round
    trip. They were derived separately (frames written under a sanitized name,
    marker carrying the raw id), which sent a collector to a directory that
    was never created and reported a take as having no frames.
    """
    helpers = "\n".join(p.read_text(encoding="utf-8") for p in HELPERS_GLOB)
    casedef = (ROOT / "Source" / "PlayTestMod" / "CaseDef.cs").read_text(encoding="utf-8")
    cliprecorder = CLIPRECORDER.read_text(encoding="utf-8")

    assert "SafeFileName" not in helpers, (
        "the old name is back; the marker and the directory must share one "
        "function or they drift again"
    )
    asset_body = method_body(
        helpers, r"public\s+static\s+string\s+AssetName\s*\([^)]*\)"
    )
    assert "Normalize(NormalizationForm.FormC)" in asset_body, (
        "AssetName must normalize to NFC: the same name typed decomposed and "
        "composed is one character to a reader and two directories here"
    )
    for keep in ("'a'", "'z'", "'0'", "'9'", "'_'", "'-"):
        assert keep in asset_body, f"AssetName must keep {keep} verbatim"
    assert "IsLetterOrDigit" not in asset_body, (
        "IsLetterOrDigit is Unicode-aware, so the name stops being byte-stable "
        "across the hosts that store it"
    )

    # Every line that names a clip directory names the asset name.
    assert 'clip complete " + Helpers.AssetName(id)' in casedef, (
        "the completion line must carry the asset name, not the raw id"
    )
    assert '" -> playtest-shots/clips/" + Helpers.AssetName(id)' in casedef, (
        "the directory in the completion line is the one a collector looks up"
    )
    assert "Report.Staged(Helpers.AssetName(id)" in casedef, (
        "the staged scene a reviewer is told about is the clip directory"
    )
    for emitter in ("clip recording ", "clip complete ", "clip abandoned "):
        line = next(
            (ln for ln in cliprecorder.splitlines() if emitter in ln and "Log." in ln),
            None,
        )
        assert line is not None, f"ClipRecorder lost its {emitter!r} line"
        assert "_activeId" in line, (
            f"{emitter.strip()!r} must print the stored asset name, not the raw id"
        )
    assert "string safeId = Helpers.AssetName(id);" in cliprecorder, (
        "Begin must normalize once so recording, completion and abandonment "
        "all name the same directory"
    )
    assert "_activeId != Helpers.AssetName(id)" in cliprecorder, (
        "End must compare against the same name Begin stored, or a clip can "
        "never be ended"
    )
    print("OK clip id, staged name and frames directory are one AssetName")


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
    assert "public static void RegisterStaged" in casedef, (
        "CaseDef.RegisterStaged must exist so camera-staged prefabs are destroyed "
        "instead of piling up at the same world point"
    )
    assert "public static void ClearStaged" in casedef
    staged_body = method_body(
        casedef,
        r"public\s+static\s+CaseDef\s+Staged\s*\([^)]*\)",
    )
    assert "ClearStaged()" in staged_body, (
        "CaseDef.Staged must clear previously staged instances before a new hold"
    )
    # A hold that completes clears its own instances, but a case that throws
    # while staging, times out, or loses the player never reaches that line.
    # Every case ending goes through FinishCase (or the early suite abort,
    # which bypasses it), so both must release what the case staged or the
    # instances stand in the player's face for the rest of the run.
    finish_body = method_body(runner, r"static\s+void\s+FinishCase\s*\([^)]*\)")
    assert "ClearStaged()" in finish_body, (
        "Runner.FinishCase must destroy staged instances however the case ended"
    )
    abort_body = method_body(runner, r"public\s+static\s+void\s+Tick\s*\(\s*\)")
    assert abort_body.count("ClearStaged()") >= 1, (
        "the early suite abort bypasses FinishCase and must clear staged objects too"
    )
    assert re.search(
        r"public\s+static\s+CaseDef\s+StagedClip\s*\(",
        casedef,
    ), "CaseDef.StagedClip must be a public static factory"

    # The clip contract is part of the public surface and the log contract:
    # a StagedClip case writes frames from inside the game and emits the
    # completion marker capture_video.sh waits for; the on-demand recorder
    # (BeginClip/EndClip) exposes the same capability to any case, and
    # TryEquipItem is the name-based give+equip route generated walk-cycle
    # cases use.
    helpers = "\n".join(p.read_text(encoding="utf-8") for p in HELPERS_GLOB)
    assert "public static string CaptureClipFrame" in helpers, (
        "Helpers.CaptureClipFrame must be public for staged-clip providers"
    )
    assert "public static void BeginClip" in helpers, (
        "Helpers.BeginClip must be public for on-demand clip providers"
    )
    assert "public static void EndClip" in helpers, (
        "Helpers.EndClip must be public for on-demand clip providers"
    )
    assert "public static int TryEquipItem" in helpers, (
        "Helpers.TryEquipItem must be public for generated walk-cycle cases"
    )
    assert "public static void StartWalk" in helpers, (
        "Helpers.StartWalk must be public for generated walk-cycle cases"
    )
    assert "public static void StopWalk" in helpers, (
        "Helpers.StopWalk must be public for generated walk-cycle cases"
    )
    assert "public static bool FrameStagedObject" in helpers, (
        "staged providers need a detached, unobstructed subject camera"
    )
    assert "public static bool TryGetRenderedBounds" in helpers and "BakeMesh" in helpers, (
        "skinned subjects must be framed from their posed vertices, not a stale AABB"
    )
    assert "internal static bool TryGetCaptureCameraPose" in helpers, (
        "WalkEntity diagnostics must read the actual detached capture-camera pose"
    )
    assert "ClipRecorder.Tick(Runner.Finished)" in runner, (
        "the gmUpdate hook must tick the on-demand clip recorder"
    )
    assert "BeginClip" in readme, (
        "README must document the on-demand clip recorder"
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
    walk_body = method_body(
        casedef,
        r"public\s+static\s+CaseDef\s+WalkEntity\s*\([^)]*\)",
    )
    assert "ReportWalkEntityRenderProbe" in walk_body, (
        "WalkEntity must sample the renderer after the animator starts"
    )
    assert "FrameWorldBounds" in walk_body, (
        "WalkEntity must select a camera lane that is clear to the rendered body"
    )
    probe_body = method_body(
        casedef,
        r"static\s+bool\s+ReportWalkEntityRenderProbe\s*\([^)]*\)",
    )
    for evidence in (
        "render-probe",
        "BakeMesh",
        "shader.isSupported",
        "material.SetPass(0)",
        "Physics.Raycast",
        "TryGetCaptureCameraPose",
        "PhysicsCapsule",
        "collisionRay",
        "collisionReady",
        "voxelTop",
        "surfaceRay",
        "surfaceHit",
        "voxelMinusSurface",
        "visualBottom",
        "groundClearance",
        "groundReady",
    ):
        assert evidence in probe_body, (
            f"WalkEntity must keep {evidence} in its live renderer diagnostic"
        )
    ground_body = method_body(
        casedef,
        r"static\s+float\s+GroundYFor\s*\([^)]*\)",
    )
    assert "world.GetHeight(" in ground_body and "+ 1f" in ground_body, (
        "WalkEntity must ground on the loaded top voxel face"
    )
    assert "surface = world.GetHeightAt" not in ground_body, (
        "the generator heightmap can sit a full block below the visible surface"
    )
    assert "capsule.center.y - capsule.height * 0.5f" in ground_body, (
        "root placement must account for the authored Physics capsule bottom"
    )
    assert "TryGroundSurface" in ground_body, (
        "partial and sloped blocks need their actual collider surface, not a voxel ceiling"
    )
    surface_body = method_body(
        casedef,
        r"static\s+bool\s+TryGroundSurface\s*\([^)]*\)",
    )
    assert "Physics.RaycastAll" in surface_body and "268500992" in surface_body, (
        "grounding must query the game's traversable-surface collider mask"
    )
    assert "transform.IsChildOf(alive.transform)" in surface_body, (
        "a downward ground ray must ignore the spawned entity's own colliders"
    )
    assert "!float.IsNaN(surfaceRay)" in probe_body, (
        "a fallback voxel ceiling is diagnostic only and cannot pass precise grounding"
    )
    clear_camera_body = method_body(
        helpers,
        r"internal\s+static\s+bool\s+FrameWorldBounds\s*\([^)]*\)",
    )
    assert "Physics.CheckSphere" in clear_camera_body, (
        "the WalkEntity camera must not start inside a world prop"
    )
    assert "Physics.Raycast" in clear_camera_body, (
        "the WalkEntity camera must have line of sight to the rendered body"
    )
    assert "AttachCamera(ctx.Player)" in staged_body, (
        "CaseDef.Staged must restore the first-person camera after its hold"
    )
    assert "render-probe" in readme, (
        "README must document the WalkEntity renderer diagnostic line"
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

    # Built-in catalog shares the public Live path (thin wrapper, not a second oracle).
    cat_live = method_body(
        catalog,
        r"static\s+CaseDef\s+Live\s*\([^)]*\)",
    )
    assert "CaseDef.Live(" in cat_live, (
        "Catalog.Live must delegate to CaseDef.Live"
    )
    # Catalog wrappers must not re-implement field assignment.
    assert "new CaseDef" not in cat_live, (
        "Catalog.Live must not construct CaseDef by hand"
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
    assert "CaseDef.RegisterStaged" in readme, (
        "README must document CaseDef.RegisterStaged for camera-staged prefabs"
    )

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
        assert re.search(
            rf"public\s+static\s+[^\n;{{}}]*\b{re.escape(name)}\s*\(", helpers
        ), f"Helpers must expose a public static {name} for providers"

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
    # Catalog used to copy LookAt with the sign flipped; a hay bale one metre
    # below the camera (dir.y < 0) then received positive X pitch (sky).
    assert "float pitch = -Mathf.Asin(" not in catalog, (
        "Catalog melee aim inverted LookAt pitch and aims below-horizon blocks at the sky"
    )
    assert "Helpers.LookAt(ctx.Player, tp)" in catalog, (
        "block_damage_melee / explosion_client must aim through Helpers.LookAt"
    )

    # Dual suite env: PLAYTEST_SUITE and ZDTD_PLAYTEST_SUITE both arm the runner.
    arm = method_body(runner, r"public\s+static\s+void\s+ArmFromEnv\s*\(\s*\)")
    assert "PLAYTEST_SUITE" in arm and "ZDTD_PLAYTEST_SUITE" in arm, (
        "ArmFromEnv must accept PLAYTEST_SUITE and ZDTD_PLAYTEST_SUITE"
    )
    assert "ZDTD_PLAYTEST_LAPS" in arm or "PLAYTEST_LAPS" in arm
    assert "PLAYTEST_TRACE_ENTITY" in arm and "ZDTD_PLAYTEST_TRACE_ENTITY" in arm, (
        "ArmFromEnv must accept opt-in spawned-entity tracing"
    )

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
        # Exact match only: a case-insensitive fallback lets any prose
        # sentence containing the words satisfy a camelCase symbol.
        assert needle in readme, f"README must document provider contract: {needle}"
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
    print("OK Catalog.Live delegates to CaseDef.Live")
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
    assert "env_flag_from(CLIENT_MUTE_ENVVARS, True)" in orch, (
        "mute must default on through the shared boolean env reader"
    )
    assert "CLIENT_MUTE" in orch and "CLIENT_MUTE=0" in readme
    assert "mute" in readme.lower() and "default" in readme.lower()
    print("OK client mute default-on (opt-out CLIENT_MUTE=0)")

    # The mod reads the same booleans from the same env, so a host that
    # accepts `yes`/`on` must not hand the client a value it reads as false.
    assert "static bool EnvTrue(string value)" in runner
    for token in ('"1"', '"true"', '"yes"', '"on"'):
        assert f"v == {token}" in runner, f"EnvTrue must accept {token}"
    env_true_body = method_body(runner, r"static bool EnvTrue\(string value\)")
    for token in ('"0"', '"false"', '"no"', '"off"'):
        assert f"v == {token}" not in env_true_body, f"EnvTrue must not accept {token}"
    assert "TraceEntity = EnvTrue(traceEntity);" in runner
    assert "else if (EnvTrue(legacy))" in runner
    print("OK client boolean env shares the host on/off spellings")
    check_asset_name_round_trip()
    return 0


if __name__ == "__main__":
    sys.exit(main())
