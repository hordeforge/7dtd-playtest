#!/usr/bin/env python3
"""Offline gate: one mod version everywhere consumers can see it.

The released version is declared in three places (ModInfo.xml, ModIdentity.cs
Version, dist manifest) and described by CHANGELOG.md. This gate fails when
they drift, so a bump cannot ship half-applied or without changelog notes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD_INFO = ROOT / "ModInfo.xml"
DIST_MOD_INFO = ROOT / "dist" / "7dtd-playtest" / "ModInfo.xml"
MOD_API = ROOT / "Source" / "PlayTestMod" / "ModIdentity.cs"
CHANGELOG = ROOT / "CHANGELOG.md"


def read_mod_info_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r'<Version\s+value="([^"]+)"', text)
    assert m, f"{path.relative_to(ROOT)}: <Version value=\"...\" /> element missing"
    return m.group(1)


def main() -> int:
    manifest = read_mod_info_version(MOD_INFO)
    api = MOD_API.read_text(encoding="utf-8")
    m = re.search(r'public\s+const\s+string\s+Version\s*=\s*"([^"]+)"\s*;', api)
    assert m, f"{MOD_API.relative_to(ROOT)}: public const string Version missing"
    code = m.group(1)

    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest), (
        f"ModInfo.xml version {manifest!r} is not X.Y.Z semver"
    )
    assert manifest == code, (
        f"version drift: ModInfo.xml {manifest} != ModIdentity.Version {code}; "
        "bump both together (game mod list and the runner banner show them)"
    )
    # dist/ is a build artifact (gitignored): absent on a clean clone and in
    # CI, where nothing was built yet. Only a machine that has built can have
    # a stale shipped manifest to catch.
    if DIST_MOD_INFO.is_file():
        dist_manifest = read_mod_info_version(DIST_MOD_INFO)
        assert manifest == dist_manifest, (
            f"stale shipped manifest: dist/7dtd-playtest/ModInfo.xml has "
            f"{dist_manifest} but ModInfo.xml has {manifest}; run make build "
            "after bumping so the installed artifact matches"
        )
    else:
        print("OK no dist build present; shipped-manifest check not applicable")

    changelog = CHANGELOG.read_text(encoding="utf-8")
    headings = re.findall(r"^##\s+\[([^\]]+)\]", changelog, flags=re.MULTILINE)
    assert "Unreleased" in headings, "CHANGELOG.md needs an [Unreleased] section"
    assert manifest in headings, (
        f"CHANGELOG.md has no ## [{manifest}] entry; every released version "
        "needs consumer-facing notes before it ships"
    )

    print(f"OK mod version {manifest} matches ModIdentity.Version")
    print("OK CHANGELOG.md has [Unreleased] and the current release entry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
