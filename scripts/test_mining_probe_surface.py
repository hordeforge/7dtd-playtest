#!/usr/bin/env python3
"""Structural guard for the public real-mining provider surface.

MiningProbe is the reusable held-tool harvest driver. External providers must
be able to pass Act/Wait/Assert to CaseDef.Live without copying the timing
state machine or falling back to SetBlockRpc damage. This gate reads the
shipped C#; it does not reimplement the probe.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from csharp_surface import method_body, without_comments

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "Source" / "PlayTestMod" / "MiningProbe.cs"
HELPERS_GLOB = sorted((ROOT / "Source" / "PlayTestMod").glob("Helpers*.cs"))
CATALOG = ROOT / "Source" / "PlayTestMod" / "Catalog.cs"
README = ROOT / "README.md"
SCENARIOS = ROOT / "SCENARIOS.md"
MAKEFILE = ROOT / "Makefile"
AGENTS = ROOT / "AGENTS.md"


def forbidden_in(body: str, needles: tuple[str, ...]) -> list[str]:
    return sorted({n for n in needles if n in body})


def declared(body: str, name: str) -> bool:
    """``name`` is a public field or property of the class body ``body``.

    A bare substring match also fires on a comment, a local, or a member of a
    neighbouring class, so a deleted public field would pass the gate.
    """
    return re.search(rf"public\s+[\w<>\[\]\.]+\s+{re.escape(name)}\s*[\;{{=]", body) is not None


def main() -> int:
    src = without_comments(PROBE.read_text(encoding="utf-8"))
    catalog = CATALOG.read_text(encoding="utf-8")
    # Helpers is one public static class split across partial-class files;
    # assert against the joined text.
    assert HELPERS_GLOB, "Helpers partial files missing"
    helpers = "\n".join(p.read_text(encoding="utf-8") for p in HELPERS_GLOB)
    readme = README.read_text(encoding="utf-8")
    scenarios = SCENARIOS.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")
    agents = AGENTS.read_text(encoding="utf-8")

    assert "public sealed class MiningSpec" in src
    assert "public sealed class MiningProbe" in src
    assert "public sealed class MiningResult" in src
    assert "public enum MiningPhase" in src
    spec_body = method_body(src, r"public\s+sealed\s+class\s+MiningSpec")
    for field in (
        "BlockName",
        "ToolName",
        "AwardItemName",
        "TargetOffset",
        "TimeoutSeconds",
        "MaxAttempts",
    ):
        assert declared(spec_body, field), f"MiningSpec missing field {field}"
    result_body = method_body(src, r"public\s+sealed\s+class\s+MiningResult")
    for field in (
        "Target",
        "InitialDamage",
        "CurrentDamage",
        "InitialAwardCount",
        "CurrentAwardCount",
        "AcceptedPresses",
        "CompletedAttempts",
        "Phase",
        "Detail",
    ):
        assert declared(result_body, field), f"MiningResult missing field {field}"

    assert re.search(r"public\s+void\s+Act\s*\(\s*CaseCtx\s+ctx\s*\)", src)
    assert re.search(r"public\s+bool\s+Wait\s*\(\s*CaseCtx\s+ctx\s*\)", src)
    assert re.search(r"public\s+bool\s+Assert\s*\(\s*CaseCtx\s+ctx\s*\)", src)
    assert "public static MiningSpec StockIron" in src.replace("  ", " ")
    assert "Helpers.CountItemType" in src
    assert "Helpers.TryGiveItem" in src
    assert "Helpers.TryEquipItemType" in src
    assert "Helpers.PushPlayerInventory" in src
    assert "could not capture mining target before seed" in src
    assert "cleanup failed restoring mining target" in src

    press = method_body(src, r"public\s+bool\s+PressPrimary\s*\([^)]*\)")
    release = method_body(src, r"public\s+void\s+ReleasePrimary\s*\([^)]*\)")
    attack = method_body(src, r"public\s+bool\s+TickAttack\s*\([^)]*\)")

    assert "UseHoldingItem(0, false)" in press, "press must be UseHoldingItem(0, false)"
    assert press.count("UseHoldingItem") == 1, "exactly one press call per attempt"
    assert "UseHoldingItem(0, true)" in release, "release must be UseHoldingItem(0, true)"
    assert release.count("UseHoldingItem") == 1, "exactly one release call"

    attack_forbidden = (
        "SetBlockRpc",
        "SetBlockLocal",
        "DamageBlock",
        "ItemActionAttack",
        "TryGiveItem",
        "PulsePrimaryAttack",
        ".Attack(",
        "inventory.Execute",
        "Execute(0",
    )
    hits = forbidden_in(attack + press + release, attack_forbidden)
    assert not hits, (
        "attack path must not write blocks, simulate hits, or give inventory; found "
        + repr(hits)
    )
    assert "PulsePrimaryAttack" in helpers, "stock pulse helper must remain (weaker combat path)"
    assert "PulsePrimaryAttack" not in src, "MiningProbe must not call PulsePrimaryAttack"

    assert "InitialAwardCount" in src and "CurrentAwardCount" in src
    assert "CountItemType" in src
    assert "missing inventory award" in src
    assert "raycast/block-damage miss" in src
    assert "rejected press" in src
    assert "unresolved block" in src
    assert "unresolved tool" in src
    assert "seed replication" in src
    assert "held-item readiness" in src

    # Negative control: the denylist function must reject a fake attack body.
    fake_attack = "Helpers.SetBlockRpc(world, pos, bv);\nplayer.Attack(false);"
    fake_hits = forbidden_in(fake_attack, attack_forbidden)
    assert "SetBlockRpc" in fake_hits and ".Attack(" in fake_hits, (
        "forbidden_in must catch a direct-damage attack body"
    )

    assert 'Live(suite, "mining_harvest"' in catalog, (
        "stock mining_harvest case must be registered in Catalog"
    )
    assert "new MiningProbe" in catalog and "MiningSpec.StockIron" in catalog
    assert "probe.Act" in catalog and "probe.Wait" in catalog and "probe.Assert" in catalog
    assert re.search(r"`mining_harvest`\s*\|\s*live\b", scenarios), (
        "SCENARIOS.md must document mining_harvest as live"
    )
    assert "terrOreIron" in src and "meleeToolPickT1IronPickaxe" in src
    assert "resourceScrapIron" in src

    assert "MiningProbe" in readme, "README must document MiningProbe for providers"
    assert "MiningSpec" in readme and "mining_harvest" in readme
    assert "test_mining_probe_surface.py" in makefile, (
        "make test must run this gate"
    )
    assert "MiningProbe" in agents, "AGENTS.md public API must name MiningProbe"

    # Do not park mining scratch on CaseCtx: the probe owns the state.
    ctx_additions = re.findall(r"public\s+\w+\s+Mining\w+", catalog)
    assert not ctx_additions, "do not add mining fields to CaseCtx: " + repr(ctx_additions)

    print("PASS mining probe public surface")
    print("PASS press/release are the only primary routes")
    print("PASS attack denylist")
    print("PASS stock mining_harvest case")
    print("PASS docs and make test wire-up")
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
