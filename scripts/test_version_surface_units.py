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
    discover_lightweight_tag_versions,
    discover_tag_versions,
    duplicate_impact_headings,
    required_uv_floor,
    uncovered_tag_versions,
    undeclared_breaking_sections,
    undocumented_lightweight_tags,
    unlinked_release_headings,
    unnamed_replacement_rows,
    unreleased_compare_base,
    uv_pin_problems,
)

_ROOT = Path(__file__).resolve().parents[1]
GATE = Path(__file__).resolve().parent / "test_version_surface.py"
UV_FLOOR = "0.12.13"
PYPROJECT = '[tool.uv]\nrequired-version = ">=0.12.13,<0.13"\n'
# The synthetic tag fixture make_root writes: v0.8.0 annotated, v9.9.9 loose
# and therefore lightweight, so v9.9.9 is the newest tag.
LIGHTWEIGHT_TAG = "9.9.9"
NEWEST_TAG = LIGHTWEIGHT_TAG
REPO = "https://github.com/hordeforge/7dtd-playtest"
COMPARE_URL = f"{REPO}/compare/v%s...HEAD"
RELEASE_URL = f"{REPO}/releases/tag/v%s"


def make_uv_workflow(path: Path, *, uv_version: str, inline: bool) -> None:
    pin = f'"{uv_version}"' if inline else "${{ env.UV_VERSION }}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"env:\n  UV_VERSION: \"{uv_version}\"\n\njobs:\n  gate:\n    steps:\n"
        f"      - uses: astral-sh/setup-uv@deadbeef\n        with:\n"
        f"          version: {pin}\n      - run: make test\n",
        encoding="utf-8",
    )


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

    `rollForward: latestMajor` let a host that happened to have a 9 or 10
    SDK answer the build, and a different Roslyn means different dll bytes
    for the same source. The pin is the 8.0 feature band, and every knob
    below it that decides what the compiler emits is pinned in the csproj as
    well: `latest` for the language version or the analyzer set re-points
    both at whatever SDK answered, and a new SDK can fail the
    `TreatWarningsAsErrors` gate on a diagnostic nobody opted into.
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
    assert global_json["sdk"]["rollForward"] == "latestFeature", (
        "global.json must roll forward inside the pinned 8.0 feature band; "
        f"it rolls to {global_json['sdk']['rollForward']!r}, so the compiler "
        "follows whatever SDK the host has"
    )


def make_git_dir(
    git_dir: Path,
    loose: dict[str, str],
    packed: list[tuple[str, str]],
    *,
    annotated_loose: set[str] | None = None,
    annotated_packed: set[str] | None = None,
) -> None:
    """Write refs/tags and packed-refs for a synthetic repository.

    A loose annotated tag's ref file is a ``ref: refs/tags/...`` indirection
    to the tag object; a loose lightweight ref names the commit itself. In
    packed-refs the two are told apart by the ``^<sha>`` peel of the commit
    that follows an annotated tag's own line, so the peel is written per tag
    rather than once at the end.
    """
    loose_annotated = annotated_loose or set()
    packed_annotated = annotated_packed or set()
    tags = git_dir / "refs" / "tags"
    tags.mkdir(parents=True)
    for name, sha in loose.items():
        value = f"ref: refs/tags/{name}" if name in loose_annotated else sha
        (tags / name).write_text(value + "\n", encoding="utf-8")
    lines: list[str] = []
    for name, sha in packed:
        lines.append(f"{sha} refs/tags/{name}")
        if name in packed_annotated:
            lines.append("^" + "b" * 40)
    lines.append(f"{'a' * 40} refs/heads/main")
    (git_dir / "packed-refs").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_root(
    root: Path,
    *,
    version: str,
    headings: list[str],
    git: bool = True,
    removed_section: bool = False,
    breaking: bool = True,
    replacement_table: bool = True,
    lightweight: list[str] | None = None,
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
        table = (
            "\n| Removed | Use instead |\n|---|---|\n"
            "| `Old.Symbol` | `New.Symbol` |\n"
            if replacement_table
            else ""
        )
        entries = "\n".join(
            f"## [{h}]\n\n### Removed\n\n{marker}{table}\n- `Old.Symbol` removed\n"
            for h in headings
        )
    else:
        entries = "\n".join(f"## [{h}]\n\n- note\n" for h in headings)
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- note\n\n" + entries, encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    make_uv_workflow(root / ".github" / "workflows" / "ci.yml", uv_version=UV_FLOOR, inline=False)
    if git:
        make_git_dir(
            root / ".git",
            {"v9.9.9": "c" * 40},
            [("v0.8.0", "d" * 40)],
            annotated_packed={"v0.8.0"},
        )
    # The released entries carry link definitions, [Unreleased] compares
    # from the newest tag, and any lightweight tag is named, so a case that
    # is about some other rule does not fail on these three first. The tag
    # fixture below is v0.8.0 (annotated) and v9.9.9 (lightweight), so v9.9.9
    # is the newest tag whatever headings a case declares.
    links = [f"[Unreleased]: {COMPARE_URL % NEWEST_TAG}"]
    links += [f"[{h}]: {RELEASE_URL % h}" for h in headings]
    (root / "CHANGELOG.md").write_text(
        (root / "CHANGELOG.md").read_text(encoding="utf-8") + "\n".join(links) + "\n",
        encoding="utf-8",
    )
    named = lightweight if lightweight is not None else ([LIGHTWEIGHT_TAG] if git else [])
    if named:
        path = root / "CHANGELOG.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                "## [Unreleased]\n",
                "## [Unreleased]\n\n"
                + ", ".join(f"`v{tag}` is a lightweight tag" for tag in named)
                + "\n",
                1,
            ),
            encoding="utf-8",
        )


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

        # A linked worktree keeps refs/tags in the main checkout, one
        # directory up from the worktree's own git dir: reading refs there
        # found no tags, so the coverage check read as a pass in every
        # worktree (this repository ships its releases from one).
        linked = base / "linked"
        common = base / "main-checkout"
        make_git_dir(common, {"v3.1.0": "1" * 40}, [])
        worktree_git = common / "worktrees" / "lane-8"
        worktree_git.mkdir(parents=True)
        (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
        (worktree_git / "HEAD").write_text("ref: refs/heads/lane-8\n", encoding="utf-8")
        linked.mkdir()
        linked.joinpath(".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")
        assert discover_tag_versions(linked) == ["3.1.0"], discover_tag_versions(linked)
        print("OK a linked worktree reads the main checkout's tags")

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

        unnamed = base / "unnamed-replacement"
        make_root(
            unnamed,
            version="0.8.0",
            headings=["0.8.0", "9.9.9"],
            removed_section=True,
            replacement_table=False,
        )
        proc = run_gate(unnamed)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "no table naming a replacement" in proc.stderr, proc.stderr
        print("OK the gate fails a declared removal that names no replacement")

        repeated = base / "repeated-heading"
        make_root(repeated, version="0.8.0", headings=["0.8.0", "9.9.9"])
        changelog = (repeated / "CHANGELOG.md").read_text(encoding="utf-8")
        (repeated / "CHANGELOG.md").write_text(
            changelog.replace(
                "## [0.8.0]\n\n- note\n",
                "## [0.8.0]\n\n- note\n\n### Fixed\n\n- note\n\n### Fixed\n\n- note\n",
            ),
            encoding="utf-8",
        )
        proc = run_gate(repeated)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "one section per impact class" in proc.stderr, proc.stderr
        print("OK the gate fails a release that repeats an impact heading")

        kinds = base / "kinds"
        make_git_dir(
            kinds / ".git",
            {"v1.2.3": "a" * 40, "v1.2.4": "b" * 40},
            [("v0.8.0", "d" * 40), ("v0.9.0", "e" * 40)],
            annotated_loose={"v1.2.3"},
            annotated_packed={"v0.9.0"},
        )
        assert discover_lightweight_tag_versions(kinds) == ["0.8.0", "1.2.4"], (
            discover_lightweight_tag_versions(kinds)
        )
        print("OK a loose ref indirection and a peel line mark an annotated tag")

        no_git = base / "no-git"
        no_git.mkdir()
        assert discover_lightweight_tag_versions(no_git) == []
        print("OK no git metadata means the lightweight check is vacuous")

        unlinked_root = base / "unlinked"
        make_root(unlinked_root, version="0.8.0", headings=["0.8.0", "9.9.9"])
        path = unlinked_root / "CHANGELOG.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("[0.8.0]: ", "[0.8.0] "),
            encoding="utf-8",
        )
        proc = run_gate(unlinked_root)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "no `[<version>]:` link definition" in proc.stderr, proc.stderr
        print("OK the gate fails a release entry with no link definition")

        behind = base / "compare-behind"
        make_root(behind, version="0.8.0", headings=["0.8.0", "9.9.9"])
        path = behind / "CHANGELOG.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                f"[Unreleased]: {COMPARE_URL % NEWEST_TAG}",
                f"[Unreleased]: {COMPARE_URL % '0.8.0'}",
            ),
            encoding="utf-8",
        )
        proc = run_gate(behind)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "newest tag is v9.9.9" in proc.stderr, proc.stderr
        print("OK the gate fails an [Unreleased] range that starts behind the newest tag")

        unsaid = base / "unsaid-lightweight"
        make_root(
            unsaid,
            version="0.8.0",
            headings=["0.8.0", "9.9.9"],
            lightweight=[],
        )
        proc = run_gate(unsaid)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "lightweight tag" in proc.stderr and "v9.9.9" in proc.stderr, proc.stderr
        print("OK the gate fails a lightweight tag the notes do not name")

    real = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert undeclared_breaking_sections(real) == [], undeclared_breaking_sections(real)
    assert unnamed_replacement_rows(real) == [], unnamed_replacement_rows(real)
    assert duplicate_impact_headings(real) == [], duplicate_impact_headings(real)
    assert "### Removed" in real, "CHANGELOG.md must keep the removal this gate polices"
    print("OK the shipped changelog declares every removal and repeats no heading")

    assert unlinked_release_headings(real) == [], unlinked_release_headings(real)
    tags = discover_tag_versions(_ROOT)
    assert tags, "this repository ships releases; no tag visible means none was fetched"
    assert unreleased_compare_base(real) == tags[-1], (
        f"[Unreleased] compares from {unreleased_compare_base(real)!r}, "
        f"newest tag is v{tags[-1]}"
    )
    light = discover_lightweight_tag_versions(_ROOT)
    assert light, "the changelog names lightweight tags; none are visible here"
    assert undocumented_lightweight_tags(real, light) == []
    print("OK every shipped release links, and every lightweight tag is named")

    with tempfile.TemporaryDirectory(prefix="version-surface-uv-") as td:
        base = Path(td)

        assert required_uv_floor(PYPROJECT) == UV_FLOOR
        print("OK the uv floor is read out of pyproject's required-version")

        no_uv = "jobs:\n  gate:\n    steps:\n      - run: make test\n"
        assert uv_pin_problems(no_uv, UV_FLOOR) == []
        print("OK a workflow that never sets uv up has nothing to pin")

        matched = base / "matched.yml"
        make_uv_workflow(matched, uv_version=UV_FLOOR, inline=False)
        assert uv_pin_problems(matched.read_text(encoding="utf-8"), UV_FLOOR) == []
        print("OK a workflow reading the shared UV_VERSION pin passes")

        stale = base / "stale.yml"
        make_uv_workflow(stale, uv_version="0.11.9", inline=False)
        problems = uv_pin_problems(stale.read_text(encoding="utf-8"), UV_FLOOR)
        assert len(problems) == 1 and "0.11.9" in problems[0], problems
        print("OK a workflow pinned below the pyproject floor is reported")

        inline = base / "inline.yml"
        make_uv_workflow(inline, uv_version=UV_FLOOR, inline=True)
        problems = uv_pin_problems(inline.read_text(encoding="utf-8"), UV_FLOOR)
        assert len(problems) == 1 and "not ${{ env.UV_VERSION }}" in problems[0], problems
        print("OK a second literal uv pin beside the shared one is reported")

        drifted_root = base / "drifted"
        make_root(drifted_root, version="0.8.0", headings=["0.8.0", "9.9.9"])
        make_uv_workflow(
            drifted_root / ".github" / "workflows" / "ci.yml",
            uv_version=UV_FLOOR,
            inline=True,
        )
        proc = run_gate(drifted_root)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "UV_VERSION" in proc.stderr, proc.stderr
        print("OK the gate fails a workflow whose uv pin drifted from pyproject")

    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
