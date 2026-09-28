#!/usr/bin/env python3
"""Units for the version-surface gate's tag discovery and coverage check.

CHANGELOG.md's release model promises that test_version_surface.py fails when
a visible vX.Y.Z tag has no changelog entry. That promise shipped unenforced
once already: the v0.7.2 tag sat on a tree still declaring 0.7.1 with no
[0.7.2] notes, and nothing noticed. These units pin the ref reading (loose,
packed, peel lines, worktree pointers) and the full gate verdict on a
synthetic tree, so the promise cannot quietly rot again.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from version_surface import (  # noqa: E402
    BREAKING_MARKER,
    discover_tag_versions,
    uncovered_tag_versions,
    undeclared_breaking_sections,
)

_ROOT = Path(__file__).resolve().parents[1]
GATE = Path(__file__).resolve().parent / "test_version_surface.py"


def assert_nuget_pins() -> None:
    """net48 reference assemblies stay exact-pinned, restore locked to nuget.org."""
    csproj = (_ROOT / "Source" / "PlayTestMod" / "PlayTestMod.csproj").read_text(
        encoding="utf-8"
    )
    assert re.search(
        r'Include="Microsoft\.NETFramework\.ReferenceAssemblies"'
        r'\s+Version="\[1\.0\.3\]"',
        csproj,
    ), "csproj must exact-pin Microsoft.NETFramework.ReferenceAssemblies to [1.0.3]"
    lock = json.loads(
        (_ROOT / "Source" / "PlayTestMod" / "packages.lock.json").read_text(
            encoding="utf-8"
        )
    )
    requested = lock["dependencies"][".NETFramework,Version=v4.8"][
        "Microsoft.NETFramework.ReferenceAssemblies"
    ]["requested"]
    assert requested == "[1.0.3, 1.0.3]", (
        f"packages.lock.json requested {requested!r}; expected '[1.0.3, 1.0.3]'"
    )
    nuget = (_ROOT / "nuget.config").read_text(encoding="utf-8")
    assert "<clear" in nuget, "nuget.config must <clear /> extra package sources"
    assert "api.nuget.org" in nuget, "nuget.config must name nuget.org as the only feed"
    release = (_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "ubuntu-latest" not in release, (
        "release.yml must not use floating ubuntu-latest"
    )
    assert "ubuntu-24.04" in release, "release.yml must pin ubuntu-24.04 like CI"


def assert_toolchain_pins() -> None:
    """The mod dll's bytes are a function of the tree, not of the host's SDK.

    global.json rolls forward to any installed major SDK on purpose (a
    distribution may ship a lower band), so every knob that decides what the
    compiler emits has to be pinned in the csproj instead. `latest` for the
    language version or the analyzer set silently re-points both at whatever
    SDK answered the build, and a different Roslyn means different dll bytes
    for the same source.
    """
    csproj = (_ROOT / "Source" / "PlayTestMod" / "PlayTestMod.csproj").read_text(
        encoding="utf-8"
    )
    for tag, value in (
        ("LangVersion", "12.0"),
        ("AnalysisLevel", "8.0"),
        ("Deterministic", "true"),
        ("EnableSourceControlManagerQueries", "false"),
    ):
        assert re.search(
            rf"<{tag}>{re.escape(value)}</{tag}>", csproj
        ), f"csproj must pin <{tag}> to {value}, not `latest` or a default"
    assert "<PathMap>$(MSBuildThisFileDirectory)=" in csproj, (
        "csproj must map the checkout path out of the dll/pdb via PathMap"
    )
    global_json = json.loads((_ROOT / "global.json").read_text(encoding="utf-8"))
    assert global_json["sdk"]["allowPrerelease"] is False, (
        "global.json must not resolve a prerelease SDK"
    )


def make_git_dir(git_dir: Path, loose: dict[str, str], packed: list[tuple[str, str]]) -> None:
    tags = git_dir / "refs" / "tags"
    tags.mkdir(parents=True)
    for name, sha in loose.items():
        (tags / name).write_text(sha + "\n", encoding="utf-8")
    lines = [f"{sha} refs/tags/{name}" for name, sha in packed]
    lines.append(f"{'a' * 40} refs/heads/main")
    if packed:
        # Annotated-tag peel line: must not be read as a ref name.
        lines.append("^" + "b" * 40)
    (git_dir / "packed-refs").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_root(
    root: Path,
    *,
    version: str,
    headings: list[str],
    git: bool = True,
    removed_section: bool = False,
    breaking: bool = True,
) -> None:
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(GATE, root / "scripts" / "test_version_surface.py")
    shutil.copy2(_SCRIPTS / "version_surface.py", root / "scripts" / "version_surface.py")
    (root / "ModInfo.xml").write_text(
        '<xml>\n  <Version value="' + version + '" />\n</xml>\n', encoding="utf-8"
    )
    api = root / "Source" / "PlayTestMod"
    api.mkdir(parents=True)
    (api / "ModIdentity.cs").write_text(
        f'public class ModIdentity {{ public const string Version = "{version}"; }}\n',
        encoding="utf-8",
    )
    if removed_section:
        marker = "**Breaking.** use the replacement instead\n\n" if breaking else ""
        entries = "\n".join(
            f"## [{h}]\n\n### Removed\n\n{marker}- `Old.Symbol` removed\n" for h in headings
        )
    else:
        entries = "\n".join(f"## [{h}]\n\n- note\n" for h in headings)
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- note\n\n" + entries, encoding="utf-8"
    )
    if git:
        make_git_dir(root / ".git", {"v9.9.9": "c" * 40}, [("v0.8.0", "d" * 40)])


def run_gate(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "test_version_surface.py")],
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    assert_nuget_pins()
    print("OK net48 reference assemblies are exact-pinned and restore is nuget.org-only")

    assert_toolchain_pins()
    print("OK language version, analyzer set and build paths are pinned in the csproj")

    with tempfile.TemporaryDirectory(prefix="version-surface-units-") as td:
        base = Path(td)

        loose = base / "loose"
        make_git_dir(loose / ".git", {"v1.2.3": "a" * 40, "not-semver": "e" * 40}, [])
        assert discover_tag_versions(loose) == ["1.2.3"], discover_tag_versions(loose)
        print("OK loose refs are discovered and non vX.Y.Z names ignored")

        mixed = base / "mixed"
        make_git_dir(mixed / ".git", {"v1.2.3": "a" * 40}, [("v0.8.0", "d" * 40)])
        assert discover_tag_versions(mixed) == ["0.8.0", "1.2.3"], discover_tag_versions(mixed)
        print("OK packed refs merge with loose refs, sorted oldest first")

        # "1.10.0" sorts before "1.9.0" as a string, so a plain sorted()
        # reports the tenth minor as older than the ninth.
        double = base / "double-digit"
        make_git_dir(
            double / ".git",
            {"v1.9.0": "a" * 40, "v1.10.0": "b" * 40, "v1.10.1": "c" * 40},
            [],
        )
        assert discover_tag_versions(double) == ["1.9.0", "1.10.0", "1.10.1"], (
            discover_tag_versions(double)
        )
        print("OK tag versions order by component, not as strings")

        pointer = base / "pointer"
        target = base / "elsewhere" / ".git"
        make_git_dir(target, {"v2.0.0": "f" * 40}, [])
        pointer.mkdir()
        pointer.joinpath(".git").write_text(
            f"gitdir: {target}\n", encoding="utf-8"
        )
        assert discover_tag_versions(pointer) == ["2.0.0"], discover_tag_versions(pointer)
        print("OK a .git worktree pointer file resolves to the real git dir")

        bare = base / "bare"
        bare.mkdir()
        assert discover_tag_versions(bare) == []
        print("OK no git metadata means the tag check is vacuous")

        assert uncovered_tag_versions(["0.7.1", "0.7.2"], ["Unreleased", "0.7.1"]) == ["0.7.2"]
        assert uncovered_tag_versions([], ["Unreleased"]) == []
        assert uncovered_tag_versions(["0.8.0"], ["Unreleased", "0.8.0"]) == []
        print("OK coverage diff reports only tagged versions without notes")

        missing = base / "missing-notes"
        make_root(missing, version="0.8.0", headings=["0.8.0"])
        proc = run_gate(missing)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "9.9.9" in proc.stderr, proc.stderr
        print("OK the gate fails when a visible tag has no changelog entry")

        covered = base / "covered"
        make_root(covered, version="0.8.0", headings=["0.8.0", "9.9.9"])
        proc = run_gate(covered)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "all 2 vX.Y.Z tags have changelog entries" in proc.stdout, proc.stdout
        print("OK the gate passes once every tagged version has notes")

        silent = base / "silent-removal"
        make_root(
            silent,
            version="0.8.0",
            headings=["0.8.0", "9.9.9"],
            removed_section=True,
            breaking=False,
        )
        proc = run_gate(silent)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "0.8.0" in proc.stderr and BREAKING_MARKER in proc.stderr, proc.stderr
        print("OK the gate fails an undeclared removal of a public symbol")

        declared = base / "declared-removal"
        make_root(declared, version="0.8.0", headings=["0.8.0", "9.9.9"], removed_section=True)
        proc = run_gate(declared)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "every ### Removed section declares itself breaking" in proc.stdout, proc.stdout
        print("OK the gate passes a removal that declares its replacement")

    real = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert undeclared_breaking_sections(real) == [], undeclared_breaking_sections(real)
    assert "### Removed" in real, "CHANGELOG.md must keep the removal this gate polices"
    print("OK the shipped changelog has no undeclared removal section")

    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
