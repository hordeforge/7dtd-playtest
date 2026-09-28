#!/usr/bin/env python3
"""Gate: the SBOM describes the lockfiles this repository actually ships.

The inventory is only worth trusting if it is a read of committed bytes, so
these cases pin what a vulnerability scanner and a consumer depend on: every
package in both lockfiles is present, each purl round-trips its locked version,
the runtime/dev split matches what pyproject declares, every component names an
SPDX license (an unrecorded one fails the inventory rather than shipping
blank), the serial number is a content hash
(same tree, same id; changed tree, different id), and neither lockfile falls
under an ignore rule that would let a regenerated one go uncommitted.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dep_sbom
from dep_sbom import JsonObject, JsonValue, as_object, as_objects

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYPI_DIRECT = {"coverage", "mypy", "pytest", "ruff"}
EXPECTED_NUGET_DIRECT = "Microsoft.NETFramework.ReferenceAssemblies"

# Every id a dependency here may carry. All of them are permissive or weak
# copyleft over their own files and none reaches a shipped artifact, which is
# what keeps this MIT project redistributable; an id outside the set is a
# license question to answer before it lands, not a typo to absorb.
ALLOWED_SPDX = {
    "Apache-2.0",
    "Apache-2.0 OR BSD-2-Clause",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "MIT",
    "MPL-2.0",
    "PSF-2.0",
}


def _locks() -> tuple[JsonObject, JsonObject]:
    return (
        as_object(tomllib.loads(dep_sbom.UV_LOCK.read_text(encoding="utf-8")), "uv.lock"),
        as_object(
            json.loads(dep_sbom.NUGET_LOCK.read_text(encoding="utf-8")),
            "packages.lock.json",
        ),
    )


def _components() -> list[JsonObject]:
    return as_objects(dep_sbom.build_sbom(*_locks()), "components", "sbom")


def _name(component: JsonObject) -> str:
    return str(component["name"])


def _version(component: JsonObject) -> str:
    return str(component["version"])


def _purl(component: JsonObject) -> str:
    return str(component["purl"])


def _ecosystem(component: JsonObject) -> str:
    scheme, _, tail = _purl(component).partition(":")
    assert scheme == "pkg", _purl(component)
    return tail.split("/", 1)[0]


def _flag(component: JsonObject, prop: str) -> str:
    for entry in as_objects(component, "properties", "component"):
        if entry["name"] == prop:
            return str(entry["value"])
    raise AssertionError(f"no {prop} property on {_name(component)}")


def test_bom_shape() -> None:
    doc = dep_sbom.build_sbom(*_locks())
    assert doc["bomFormat"] == "CycloneDX", doc["bomFormat"]
    assert doc["specVersion"] == "1.6", doc["specVersion"]
    assert str(doc["serialNumber"]).startswith("urn:uuid:"), doc["serialNumber"]
    root = as_object(doc["metadata"], "metadata")["component"]
    assert as_object(root, "metadata.component")["name"] == dep_sbom.PROJECT_NAME
    assert as_object(root, "metadata.component")["version"] == dep_sbom.project_version()


def test_every_uv_lock_package_is_listed() -> None:
    uv_lock, _ = _locks()
    expected = {
        str(package["name"])
        for package in as_objects(uv_lock, "package", "uv.lock")
        if "registry" in as_object(package["source"], "package.source")
    }
    listed = {_name(c) for c in _components() if _ecosystem(c) == dep_sbom.PUPI}
    assert expected == listed, (
        f"missing {sorted(expected - listed)}, extra {sorted(listed - expected)}"
    )


def test_every_nuget_package_is_listed_at_its_locked_version() -> None:
    _, nuget_lock = _locks()
    framework = as_object(
        as_object(nuget_lock["dependencies"], "dependencies")[dep_sbom.NUGET_FRAMEWORK],
        dep_sbom.NUGET_FRAMEWORK,
    )
    expected = {
        name: str(as_object(entry, f"packages.lock.json[{name}]")["resolved"])
        for name, entry in framework.items()
    }
    listed = {
        _name(c): _version(c)
        for c in _components()
        if _ecosystem(c) == dep_sbom.NUGET
    }
    assert expected == listed, f"missing {sorted(expected.keys() - listed.keys())}"


def test_purl_carries_version() -> None:
    for component in _components():
        purl = _purl(component)
        assert purl.endswith("@" + _version(component)), purl
        assert purl.count("@") == 1, purl


def test_nothing_is_required_scope() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject.get("project", {})
    assert project.get("dependencies", []) == [], "runtime dependencies are no longer empty"
    assert project.get("optional-dependencies", {}) == {}, (
        "an extra that ships in a wheel is a runtime dependency under another name"
    )
    required = [c for c in _components() if c["scope"] == "required"]
    assert required == [], f"runtime scope is no longer empty: {[_name(c) for c in required]}"


def test_direct_flags_match_the_declaration() -> None:
    direct: dict[str, set[str]] = {dep_sbom.PUPI: set(), dep_sbom.NUGET: set()}
    for component in _components():
        if _flag(component, dep_sbom.PROP_DIRECT) == "true":
            direct[_ecosystem(component)].add(_name(component))
    assert direct[dep_sbom.PUPI] == EXPECTED_PYPI_DIRECT, sorted(
        direct[dep_sbom.PUPI] ^ EXPECTED_PYPI_DIRECT
    )
    assert direct[dep_sbom.NUGET] == {EXPECTED_NUGET_DIRECT}, sorted(direct[dep_sbom.NUGET])


def test_serial_number_tracks_content() -> None:
    uv_lock, nuget_lock = _locks()
    same = str(dep_sbom.build_sbom(uv_lock, nuget_lock)["serialNumber"])
    assert same == str(dep_sbom.build_sbom(*_locks())["serialNumber"]), "not deterministic"
    bumped = as_object(json.loads(json.dumps(uv_lock)), "uv.lock")
    as_objects(bumped, "package", "uv.lock")[0]["version"] = "9.9.9"
    changed = dep_sbom.build_sbom(bumped, nuget_lock)["serialNumber"]
    assert str(changed) != same


def test_empty_inventory_fails_loud() -> None:
    try:
        dep_sbom.build_sbom({"package": []}, {"dependencies": {}})
    except ValueError:
        return
    raise AssertionError("build_sbom accepted a lockfile pair that resolves to nothing")


def _array(obj: JsonObject, key: str, where: str) -> list[JsonValue]:
    return dep_sbom.as_list(obj.get(key, []), f"{where}.{key}")


def _license(component: JsonObject) -> str:
    """The SPDX id on a component, read from its `licenses` entry."""
    entries = _array(component, "licenses", "component")
    assert len(entries) == 1, f"{_name(component)} has {len(entries)} license entries"
    return str(as_object(as_object(entries[0], "licenses[]")["license"], "license")["id"])


def test_every_component_carries_a_license() -> None:
    for component in _components():
        spdx = _license(component)
        assert spdx.strip(), f"{_name(component)} has an empty license id"
        assert spdx in ALLOWED_SPDX, f"{_name(component)}: unlisted SPDX id {spdx!r}"


def test_license_tables_match_the_lockfiles() -> None:
    uv_lock, nuget_lock = _locks()
    framework = as_object(
        as_object(nuget_lock["dependencies"], "dependencies")[dep_sbom.NUGET_FRAMEWORK],
        dep_sbom.NUGET_FRAMEWORK,
    )
    assert set(dep_sbom.PIP_LICENSES) == {
        str(p["name"])
        for p in as_objects(uv_lock, "package", "uv.lock")
        if "registry" in as_object(p["source"], "package.source")
    }
    assert set(dep_sbom.NUGET_LICENSES) == set(framework)


def test_an_unrecorded_license_fails_loud() -> None:
    uv_lock, nuget_lock = _locks()
    bumped = as_object(json.loads(json.dumps(uv_lock)), "uv.lock")
    name = "unrecorded-package-xyz"
    as_objects(bumped, "package", "uv.lock")[0]["name"] = name
    try:
        dep_sbom.build_sbom(bumped, nuget_lock)
    except ValueError as ex:
        assert name in str(ex), ex
        return
    raise AssertionError(f"build_sbom emitted {name} with no license recorded")


def test_the_committed_lockfiles_are_not_ignored() -> None:
    """A lockfile the ignore rules cover cannot be re-added after a regen.

    `uv.lock` matches the `*.lock` rule the runtime lock file needed, so
    `git add uv.lock` on a regenerated file is refused and the tree looks
    like it still has one when it has none. Every `--locked` gate then reads
    whatever uv resolves instead of the committed resolution.
    """
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!uv.lock" in ignore, ".gitignore must re-include uv.lock past the *.lock rule"
    for name in ("uv.lock", "Source/PlayTestMod/packages.lock.json"):
        # -q answers the question directly: nonzero means no rule ignores the
        # path, which is what a path matched by a `!` negation reports too.
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", name],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0, f"{name} is covered by an ignore rule"


TESTS = (
    test_the_committed_lockfiles_are_not_ignored,
    test_bom_shape,
    test_every_uv_lock_package_is_listed,
    test_every_nuget_package_is_listed_at_its_locked_version,
    test_purl_carries_version,
    test_nothing_is_required_scope,
    test_direct_flags_match_the_declaration,
    test_every_component_carries_a_license,
    test_license_tables_match_the_lockfiles,
    test_an_unrecorded_license_fails_loud,
    test_serial_number_tracks_content,
    test_empty_inventory_fails_loud,
)


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as ex:
            print(f"FAIL {test.__name__}: {ex}", file=sys.stderr)
            failed += 1
    if failed:
        print(f"test_dep_sbom: FAILED ({failed}/{len(TESTS)})", file=sys.stderr)
        return 1
    print(f"PASS dep_sbom ({len(TESTS)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
