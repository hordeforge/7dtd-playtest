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

TAG_RE = re.compile(r"v(\d+\.\d+\.\d+)")

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


def _git_dir(root: Path) -> Path | None:
    """Resolve root/.git to a directory, following a worktree pointer file."""
    dot_git = root / ".git"
    if dot_git.is_file():
        text = dot_git.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return None
        target = Path(text.removeprefix("gitdir:").strip())
        return target if target.is_absolute() else root / target
    if dot_git.is_dir():
        return dot_git
    return None


def discover_tag_versions(root: Path) -> list[str]:
    """X.Y.Z versions of the local ``vX.Y.Z`` tags, oldest first.

    Reads refs straight off disk so the gate stays offline and dependency-
    free; returns [] where no git metadata is reachable (tarball download,
    shallow CI checkout that did not fetch tags), which makes the
    tag-coverage check vacuous there rather than wrong.
    """
    git_dir = _git_dir(root)
    if git_dir is None:
        return []
    names: set[str] = set()
    tags_dir = git_dir / "refs" / "tags"
    if tags_dir.is_dir():
        names.update(
            p.relative_to(tags_dir).as_posix()
            for p in tags_dir.rglob("*")
            if p.is_file()
        )
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            fields = line.split(maxsplit=1)  # "<sha> <ref>"; peel lines "^<sha>"
            if len(fields) == 2 and fields[1].startswith("refs/tags/"):
                names.add(fields[1].removeprefix("refs/tags/").strip())
    versions = [m.group(1) for name in names if (m := TAG_RE.fullmatch(name))]
    return sorted(versions)


def uncovered_tag_versions(tag_versions: list[str], headings: list[str]) -> list[str]:
    """Tagged versions without a ``## [<version>]`` changelog entry."""
    known = set(headings)
    return [v for v in tag_versions if v not in known]


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
