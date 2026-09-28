#!/usr/bin/env python3
"""The mod build's own contract, checked offline.

`make build` cannot run on a hosted runner and cannot run here: it compiles
against the game's own assemblies, which live in a Steam install. So the mod
build has no CI job, and every property that makes its output trustworthy is
one nobody would notice losing until a rebuild stopped matching a release:

* the lockfile is honored (RestorePackagesWithLockFile in the csproj,
  -p:RestoreLockedMode=true in the Makefile recipe), and every
  PackageReference is an exact range the committed packages.lock.json already
  resolved with a content hash. A version bump that does not regenerate the
  lock, or a bare `Version="1.0.3"` (NuGet's minimum range, so any later 1.x
  is legal at the next restore), is what lets two restores of one commit
  install different bytes.
* the output is reproducible: Deterministic, PathMap (so a tarball export and
  a clone of the same tree build the same dll), and no SDK git query (which
  bakes the checkout path and remote URL into the pdb). SourceControlManager
  queries off, an exact LangVersion, and a clock the recipe pins rather than
  inheriting from the host.
* the artifact is one directory and one file set: the Release OutputPath under
  dist/, the AssemblyName the Makefile and the archiver both name, and those
  two shipping paths agreeing on what ships. `make install` and `make package`
  are two copies of the same list; when they drift, a developer's install
  proves the dll loads while the release zip is missing the manifest.
* the toolchain is pinned: an exact SDK in global.json, and a rollForward that
  stays inside the pinned band instead of floating to whatever the host has.

Everything here is read from the committed build config. Nothing needs the
game, so the checks run in `make test` and in CI.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mod_package

ROOT = Path(__file__).resolve().parents[1]
CSPROJ = ROOT / "Source" / "PlayTestMod" / "PlayTestMod.csproj"
NUGET_LOCK = ROOT / "Source" / "PlayTestMod" / "packages.lock.json"
GLOBAL_JSON = ROOT / "global.json"
MAKEFILE = ROOT / "Makefile"

# The net48 reference assemblies the csproj declares build-only. dep_sbom.py
# already reads the lock; this gate reads the csproj side of the same contract.
NUGET_FRAMEWORK = ".NETFramework,Version=v4.8"

# MSBuild's own spelling for an exact version. A bare "1.0.3" is [1.0.3, ),
# which any later 1.x satisfies at restore time.
EXACT_RANGE_RE = re.compile(r"^\[(\d+\.\d+\.\d+(?:\.\d+)?)\]$")

# A rollForward that stays inside the pinned band. `latestMajor`/`major` would
# let a host with only a 9.0 SDK answer the build, compiling this source under
# a different C# version and a different analyzer set than the release.
FLOATING_ROLLFORWARD = {"latestPatch", "feature", "minor", "latestMinor", "major", "latestMajor"}


def csproj_text() -> str:
    return CSPROJ.read_text(encoding="utf-8")


def _property(tree: ElementTree.Element, name: str) -> str | None:
    """One MSBuild property value, or None when the csproj never sets it.

    A property is empty when it is unset, when it is set to an empty value, and
    when it is a Condition that does not hold; the caller decides which of those
    is acceptable, so this returns the raw text either way.
    """
    for element in tree.iter("PropertyGroup"):
        for prop in element.findall(name):
            return (prop.text or "").strip()
    return None


def package_references() -> list[tuple[str, str | None]]:
    tree = ElementTree.fromstring(csproj_text())
    return [
        (ref.get("Include", ""), ref.get("Version"))
        for ref in tree.iter("PackageReference")
    ]


def test_lockfile_is_written_and_restore_is_locked() -> None:
    tree = ElementTree.fromstring(csproj_text())
    assert _property(tree, "RestorePackagesWithLockFile") == "true", (
        f"{CSPROJ.name} must set RestorePackagesWithLockFile=true, or no "
        "packages.lock.json is written and restore is free to drift"
    )
    build = _target_body("build")
    assert "-p:RestoreLockedMode=true" in build, (
        "`make build` must restore in locked mode: without it a stale or absent "
        "packages.lock.json re-resolves against the feed instead of failing"
    )


def test_package_references_are_exact_and_in_the_lock() -> None:
    lock = json.loads(NUGET_LOCK.read_text(encoding="utf-8"))
    frameworks = lock.get("dependencies", {})
    assert NUGET_FRAMEWORK in frameworks, (
        f"packages.lock.json has no {NUGET_FRAMEWORK} entry, so the lock does "
        f"not describe the target the csproj builds; it has: {sorted(frameworks)}"
    )
    locked = frameworks[NUGET_FRAMEWORK]
    for name, version in package_references():
        exact = EXACT_RANGE_RE.match(version or "")
        assert exact, (
            f"PackageReference {name} has Version={version!r}, which is not an "
            "exact [x.y.z] range; a bare version is NuGet's minimum range, so a "
            "later release on the feed is legal at the next restore"
        )
        entry = locked.get(name)
        assert entry is not None, (
            f"{name} is referenced by {CSPROJ.name} but absent from the committed "
            f"packages.lock.json ({NUGET_FRAMEWORK}); regenerate the lock with "
            "`dotnet restore` and commit it"
        )
        assert entry.get("type") == "Direct", (
            f"{name} is locked as {entry.get('type')!r}, so the lock disagrees "
            "with the csproj about whether it is a direct reference"
        )
        assert entry.get("resolved") == exact.group(1), (
            f"{name} is pinned to {exact.group(1)} in the csproj but "
            f"{entry.get('resolved')!r} in the lock; regenerate the lock"
        )
        assert entry.get("contentHash"), (
            f"{name} is locked without a contentHash, so the lock does not pin "
            "the package bytes"
        )


def test_output_is_deterministic_and_path_mapped() -> None:
    tree = ElementTree.fromstring(csproj_text())
    assert _property(tree, "Deterministic") == "true", (
        f"{CSPROJ.name} must set Deterministic=true, or the dll carries a "
        "timestamp-and-address MVID and no two builds agree"
    )
    path_map = _property(tree, "PathMap")
    assert path_map and "=" in path_map, (
        f"{CSPROJ.name} must set PathMap to a fixed root, or the compiler bakes "
        "the absolute checkout path into the dll and pdb and two checkouts of "
        "one commit hash differently"
    )
    assert _property(tree, "EnableSourceControlManagerQueries") == "false", (
        f"{CSPROJ.name} must set EnableSourceControlManagerQueries=false, or the "
        "SDK queries git mid-build and bakes the checkout path and remote URL "
        "into the pdb (a tarball export then builds differently from a clone)"
    )
    lang = _property(tree, "LangVersion")
    assert lang and re.match(r"^\d+(\.\d+)*$", lang), (
        f"LangVersion={lang!r} is not a numeric version; 'latest'/'default' "
        "follows whatever C# version the host's SDK band defaults to, so two "
        "hosts compile this source under different language rules"
    )


def test_build_recipe_pins_the_clock() -> None:
    build = _target_body("build")
    for setting in ("SOURCE_DATE_EPOCH", "LC_ALL=C", "TZ=UTC"):
        assert setting in build, (
            f"`make build` must set {setting} on the dotnet invocation; without "
            "it the build reads the host clock, locale and timezone"
        )
    assert "SOURCE_DATE_EPOCH ?=" in MAKEFILE.read_text(encoding="utf-8"), (
        "the Makefile must give SOURCE_DATE_EPOCH a fixed default and export it, "
        "so the compile and scripts/mod_package.py stamp from one clock"
    )


def test_release_output_lands_in_dist() -> None:
    tree = ElementTree.fromstring(csproj_text())
    assert _property(tree, "AppendTargetFrameworkToOutputPath") == "false", (
        f"{CSPROJ.name} must set AppendTargetFrameworkToOutputPath=false, or "
        "Release lands in dist/7dtd-playtest/net48/ and nothing the Makefile "
        "names is where it looks"
    )
    output = _property(tree, "OutputPath")
    assert output and "dist" in output, (
        f"OutputPath={output!r} does not put the shippable Release output under "
        "dist/, so `make install` and `make package` read a different tree than "
        "the one the build wrote"
    )


def test_install_and_package_ship_the_same_files() -> None:
    """One list, named once. Two copies of it drift silently."""
    install = _target_body("install")
    shipped = sorted(mod_package.MOD_FILES)
    copied = re.findall(r'\$\(DIST\)/([^"\s]+)', install)
    assert sorted(copied) == shipped, (
        f"`make install` copies {sorted(copied)} but scripts/mod_package.py "
        f"archives {shipped}; the two shipping paths must name the same files"
    )
    assembly = _property(ElementTree.fromstring(csproj_text()), "AssemblyName")
    assert assembly and f"{assembly}.dll" in shipped, (
        f"AssemblyName={assembly!r} is not in the shipped file list {shipped}, so "
        "the build writes a dll neither install nor package copies"
    )
    assert f"dist/{assembly}" in MAKEFILE.read_text(encoding="utf-8"), (
        f"the Makefile's DIST must be dist/{assembly}, the directory the csproj "
        "writes Release output to"
    )


def test_sdk_is_pinned() -> None:
    sdk = json.loads(GLOBAL_JSON.read_text(encoding="utf-8"))["sdk"]
    version = sdk.get("version", "")
    assert EXACT_RANGE_RE.match(f"[{version}]"), (
        f"global.json pins SDK version {version!r}, which is not an exact "
        "x.y.z build; every host then answers the build with a different "
        "compiler and analyzer set"
    )
    roll = sdk.get("rollForward", "")
    assert roll not in FLOATING_ROLLFORWARD, (
        f"global.json rollForward={roll!r} can leave the pinned band, so a host "
        "with a newer SDK installed compiles this source under a different "
        "toolchain than the release"
    )
    assert sdk.get("allowPrerelease") is False, (
        "global.json must set allowPrerelease=false, or a preview SDK satisfies "
        "the pin"
    )


def _target_body(name: str) -> str:
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    body: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith(f"{name}:"):
            for follow in lines[i + 1 :]:
                if not follow.startswith("\t"):
                    break
                body.append(follow)
            break
    return "\n".join(body)


def main() -> int:
    test_lockfile_is_written_and_restore_is_locked()
    test_package_references_are_exact_and_in_the_lock()
    test_output_is_deterministic_and_path_mapped()
    test_build_recipe_pins_the_clock()
    test_release_output_lands_in_dist()
    test_install_and_package_ship_the_same_files()
    test_sdk_is_pinned()
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
