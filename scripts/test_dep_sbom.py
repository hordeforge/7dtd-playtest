#!/usr/bin/env python3
"""Gate: the SBOM describes the lockfiles this repository actually ships.

The inventory is only worth trusting if it is a read of committed bytes, so
these cases pin what a vulnerability scanner and a consumer depend on: every
package in both lockfiles is present, each purl round-trips its locked version,
the runtime/dev split matches what pyproject declares, and the serial number is
a content hash (same tree, same id; changed tree, different id).
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dep_sbom
from dep_sbom import JsonObject, as_object, as_objects

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PYPI_DIRECT = {"coverage", "mypy", "pytest", "ruff"}
EXPECTED_NUGET_DIRECT = "Microsoft.NETFramework.ReferenceAssemblies"


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
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in pyproject, "runtime dependencies are no longer empty"
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


TESTS = (
    test_bom_shape,
    test_every_uv_lock_package_is_listed,
    test_every_nuget_package_is_listed_at_its_locked_version,
    test_purl_carries_version,
    test_nothing_is_required_scope,
    test_direct_flags_match_the_declaration,
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
