#!/usr/bin/env python3
"""Offline gate: one mod version everywhere consumers can see it.

The released version is declared in three places (ModInfo.xml, ModIdentity.cs
Version, dist manifest) and described by CHANGELOG.md. This gate fails when
they drift, when a visible vX.Y.Z git tag has no changelog entry, or when the
shipped dist manifest went stale, so a bump cannot ship half-applied or
without consumer-facing notes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from version_surface import (
    BREAKING_MARKER,
    discover_tag_versions,
    required_uv_floor,
    uncovered_tag_versions,
    undeclared_breaking_sections,
    uv_pin_problems,
)

ROOT = Path(__file__).resolve().parents[1]
MOD_INFO = ROOT / "ModInfo.xml"
DIST_MOD_INFO = ROOT / "dist" / "7dtd-playtest" / "ModInfo.xml"
MOD_API = ROOT / "Source" / "PlayTestMod" / "ModIdentity.cs"
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOWS = ROOT / ".github" / "workflows"


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

    undeclared = undeclared_breaking_sections(changelog)
    assert not undeclared, (
        "CHANGELOG.md has a ### Removed section with no " + BREAKING_MARKER + " marker in: "
        + ", ".join(undeclared)
        + "; a pre-1.0 minor may remove a public symbol, but the entry has to say so "
        "and name the replacement (see the release model at the top of the file)"
    )
    print("OK every ### Removed section declares itself breaking")

    tag_versions = discover_tag_versions(ROOT)
    if tag_versions:
        uncovered = uncovered_tag_versions(tag_versions, headings)
        assert not uncovered, (
            "CHANGELOG.md has no ## [<version>] entry for tagged version(s): "
            + ", ".join(uncovered)
            + "; every vX.Y.Z tag points at a commit consumers can check out "
            "and needs notes"
        )
        print(f"OK all {len(tag_versions)} vX.Y.Z tags have changelog entries")
    else:
        print("OK no vX.Y.Z tags visible; tag-coverage check not applicable")

    print(f"OK mod version {manifest} matches ModIdentity.Version")
    print("OK CHANGELOG.md has [Unreleased] and the current release entry")

    assert PYPROJECT.is_file(), "pyproject.toml is missing; the uv pin has no source of truth"
    floor = required_uv_floor(PYPROJECT.read_text(encoding="utf-8"))
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        problems = uv_pin_problems(workflow.read_text(encoding="utf-8"), floor)
        assert not problems, (
            f"{workflow.relative_to(ROOT)}: " + "; ".join(problems) + f". Bump UV_VERSION to "
            f'{floor} (pyproject [tool.uv] required-version) in the same commit as the workflows.'
        )
        print(f"OK {workflow.relative_to(ROOT)} pins uv {floor} like pyproject")
    return 0


if __name__ == "__main__":
    sys.exit(main())
