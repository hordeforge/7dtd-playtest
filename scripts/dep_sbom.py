#!/usr/bin/env python3
"""CycloneDX SBOM for the two lockfiles this repository ships.

Both manifests are already hash-pinned (uv.lock per wheel/sdist,
Source/PlayTestMod/packages.lock.json per NuGet contentHash), so the inventory
is a read of committed bytes rather than a resolution: it names exactly what
`uv run --locked` and `dotnet restore -p:RestoreLockedMode=true` install, with
no network and no scanner in the loop.

Emitted per release so a consumer or a vulnerability scanner can name what
shipped without checking out the tag. No component is in `required` scope by
design: pyproject declares no production dependencies and the csproj marks its
one package reference PrivateAssets="All", so everything inventoried here is
build-time only and never reaches a player.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
import uuid
from pathlib import Path
from typing import TypeAlias

ROOT = Path(__file__).resolve().parents[1]
UV_LOCK = ROOT / "uv.lock"
NUGET_LOCK = ROOT / "Source" / "PlayTestMod" / "packages.lock.json"
MOD_INFO = ROOT / "ModInfo.xml"

PROJECT_NAME = "7dtd-playtest"
NUGET_FRAMEWORK = ".NETFramework,Version=v4.8"

PUPI = "pypi"
NUGET = "nuget"

TYPE_LIBRARY = "library"
TYPE_APPLICATION = "application"

# The two property names a consumer filters on: was it declared in a manifest
# (direct), and is it absent from the shipped artifact (dev).
PROP_DIRECT = "7dtd-playtest:direct"
PROP_DEV = "7dtd-playtest:dev"

# SPDX id per package, read from the license file the artifact itself ships
# (the wheel's dist-info/licenses/ for PyPI, the nuspec's licenseUrl for NuGet)
# because none of these lockfiles carry a license field and no PEP 639
# `License-Expression` is present in the installed metadata. A dependency added
# without an entry here fails the build rather than shipping an unlabeled
# component, which is the state that makes a consumer's license scan guess.
PIP_LICENSES: dict[str, str] = {
    "ast-serialize": "MIT",
    "colorama": "BSD-3-Clause",
    "coverage": "Apache-2.0",
    "iniconfig": "MIT",
    "librt": "MIT",
    "mypy": "MIT",
    "mypy-extensions": "MIT",
    "packaging": "Apache-2.0 OR BSD-2-Clause",
    "pathspec": "MPL-2.0",
    "pluggy": "MIT",
    "pygments": "BSD-2-Clause",
    "pytest": "MIT",
    "ruff": "MIT",
    "typing-extensions": "PSF-2.0",
}

NUGET_LICENSES: dict[str, str] = {
    "Microsoft.NETFramework.ReferenceAssemblies": "MIT",
    "Microsoft.NETFramework.ReferenceAssemblies.net48": "MIT",
}

JsonValue: TypeAlias = "str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None"
JsonObject: TypeAlias = dict[str, JsonValue]


def as_object(value: JsonValue, where: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def _text(obj: JsonObject, key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{where}.{key}: expected a string, got {type(value).__name__}")
    return value


def as_list(value: JsonValue, where: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError(f"{where}: expected a list, got {type(value).__name__}")
    return value


def as_objects(obj: JsonObject, key: str, where: str) -> list[JsonObject]:
    value = obj.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{where}.{key}: expected a list")
    return [as_object(entry, f"{where}.{key}[]") for entry in value]


def _has(obj: JsonObject, key: str) -> bool:
    """True when a lockfile package carries `key` in its `source` table.

    uv distinguishes a registry install (`registry = ...`) from the virtual root
    (`virtual = "."`) and from an editable or path source; only the registry
    one reaches the SBOM.
    """
    return key in as_object(obj.get("source", {}), "package.source")


def project_version() -> str:
    """The version a release ships, read from the mod manifest.

    ModInfo.xml is already authoritative for the release workflow: a vX.Y.Z tag
    is rejected unless it matches this value, so the SBOM cannot disagree with
    the tag.
    """
    match = re.search(r"<Version[^>]*value=\"([^\"]+)\"", MOD_INFO.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"no <Version value=...> in {MOD_INFO}")
    return match.group(1)


def _purl(name: str, version: str, ecosystem: str) -> str:
    if ecosystem == NUGET:
        return f"pkg:{NUGET}/{name}@{version}"
    if ecosystem == PUPI:
        return f"pkg:{PUPI}/{name}@{version}"
    raise ValueError(f"no package-url scheme for ecosystem {ecosystem!r}")


def _license(name: str, ecosystem: str) -> str:
    """The SPDX id recorded for a package.

    Unrecorded is a build failure, not a missing field: a component with no
    license is the one a downstream scan cannot act on.
    """
    recorded = (PIP_LICENSES if ecosystem == PUPI else NUGET_LICENSES).get(name)
    if recorded is None:
        raise ValueError(
            f"{name}: no license recorded in dep_sbom. Add the SPDX id read from "
            f"the license file the artifact ships"
        )
    return recorded


def _component(
    name: str,
    version: str,
    ecosystem: str,
    *,
    direct: bool,
    dev: bool,
) -> JsonObject:
    return {
        "type": TYPE_LIBRARY,
        "name": name,
        "version": version,
        "purl": _purl(name, version, ecosystem),
        "scope": "excluded" if dev else "required",
        "licenses": [{"license": {"id": _license(name, ecosystem)}}],
        "properties": [
            {"name": PROP_DIRECT, "value": "true" if direct else "false"},
            {"name": PROP_DEV, "value": "true" if dev else "false"},
        ],
    }


def _dev_edges(package: JsonObject) -> list[JsonObject]:
    """The dependency entries under every dev group of the root package."""
    groups = as_object(package.get("dev-dependencies", {}), "dev-dependencies")
    edges: list[JsonObject] = []
    for group, entries in groups.items():
        if not isinstance(entries, list):
            raise ValueError(f"dev-dependencies.{group} is not a list")
        edges.extend(as_object(entry, f"dev-dependencies.{group}[]") for entry in entries)
    return edges


def uv_direct(lock: JsonObject) -> tuple[set[str], set[str]]:
    """The runtime and dev-direct package names uv.lock resolves for the root.

    The root is the only virtual-source package in the lock, and its
    `dependencies` / `dev-dependencies` tables are where uv records what
    pyproject declared, so those tables are the authority for direct versus
    transitive.
    """
    runtime: set[str] = set()
    dev: set[str] = set()
    for package in as_objects(lock, "package", "uv.lock"):
        if not _has(package, "virtual"):
            continue
        for edge in as_objects(package, "dependencies", "uv.lock.package[virtual]"):
            runtime.add(_text(edge, "name", "uv.lock.package[virtual].dependencies[]"))
        for edge in _dev_edges(package):
            dev.add(_text(edge, "name", "dev-dependencies[]"))
    return runtime, dev


def uv_components(lock: JsonObject) -> list[JsonValue]:
    """Every registry-sourced package in uv.lock, flagged direct and dev.

    Dev scope is by reachability, not by declaration: a transitive of a dev
    tool is build-time only too, so only the runtime set is `required`.
    """
    runtime, dev = uv_direct(lock)

    components: list[JsonValue] = []
    for package in as_objects(lock, "package", "uv.lock"):
        if not _has(package, "registry"):
            continue
        name = _text(package, "name", "uv.lock.package")
        components.append(
            _component(
                name,
                _text(package, "version", f"uv.lock.package[{name}]"),
                PUPI,
                direct=name in runtime or name in dev,
                dev=name not in runtime,
            )
        )
    return components


def nuget_components(lock: JsonObject) -> list[JsonValue]:
    """Every package the net48 restore resolves, at its locked version.

    All dev scope: the csproj's single direct reference is PrivateAssets="All",
    so nothing from this ecosystem is copied into dist/.
    """
    frameworks = as_object(lock.get("dependencies", {}), "packages.lock.json.dependencies")
    framework = frameworks.get(NUGET_FRAMEWORK)
    if not isinstance(framework, dict):
        raise ValueError(f"no {NUGET_FRAMEWORK} entry in the NuGet lockfile")

    components: list[JsonValue] = []
    for name in sorted(framework):
        entry = as_object(framework[name], f"packages.lock.json[{name}]")
        components.append(
            _component(
                name,
                _text(entry, "resolved", f"packages.lock.json[{name}]"),
                NUGET,
                direct=entry.get("type") == "Direct",
                dev=True,
            )
        )
    return components


def _serial_number(uv_lock: JsonObject, nuget_lock: JsonObject) -> str:
    """A v5-shaped id derived from both lockfiles.

    Deterministic, so re-running on an unchanged tree diffs to nothing, and
    content-derived, so two different dependency sets never share an id.
    """
    canonical = json.dumps([uv_lock, nuget_lock], sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return str(uuid.UUID(hex=digest[:32], version=5))


def build_sbom(uv_lock: JsonObject, nuget_lock: JsonObject) -> JsonObject:
    components = uv_components(uv_lock) + nuget_components(nuget_lock)
    if not components:
        raise ValueError("no components resolved from the lockfiles")
    metadata: JsonObject = {
        "component": {
            "type": TYPE_APPLICATION,
            "name": PROJECT_NAME,
            "version": project_version(),
        },
        "tools": {
            "components": [
                {"type": TYPE_APPLICATION, "name": f"{PROJECT_NAME}/dep_sbom"}
            ]
        },
    }
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": "urn:uuid:" + _serial_number(uv_lock, nuget_lock),
        "version": 1,
        "metadata": metadata,
        "components": components,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "examples:\n"
            "  dep_sbom.py                      # CycloneDX JSON on stdout\n"
            "  dep_sbom.py dist/app.cdx.json    # same document, written to a file\n"
            "exit codes: 0 inventory written, 1 a committed input is missing or\n"
            "does not describe the tree it should (an unreadable lockfile, an\n"
            "unrecorded license, a version the mod never declares) or the output\n"
            "could not be written, 2 bad usage"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "output",
        nargs="?",
        help="write the SBOM here instead of stdout; missing parent directories are created",
    )
    args = parser.parse_args(argv)

    for path in (UV_LOCK, NUGET_LOCK, MOD_INFO):
        if not path.is_file():
            # 1, not 2: nothing about the invocation was wrong, the tree is.
            # is_file() is False for any stat failure, not only ENOENT, so
            # say "missing" as a fact about the checkout and let the read
            # below name the real cause when there is one.
            print(f"dep_sbom: missing or unreadable {path}", file=sys.stderr)
            return 1

    # Every failure below is a 1 with a message on stderr, as the epilog
    # promises: build_sbom raises ValueError for a lockfile that does not
    # describe the tree (a truncated TOML document, a package with no
    # recorded license, a ModInfo.xml with no <Version>), and without this
    # the traceback was the whole interface for the case the help text names.
    # A read failure is named the same way: an unreadable lockfile is a fact
    # about the tree, and a traceback past that contract makes a release job
    # report a crash instead of a missing inventory input.
    try:
        document = build_sbom(
            as_object(tomllib.loads(UV_LOCK.read_text(encoding="utf-8")), "uv.lock"),
            as_object(json.loads(NUGET_LOCK.read_text(encoding="utf-8")), "packages.lock.json"),
        )
    except (OSError, ValueError) as ex:
        print(f"dep_sbom: {ex}", file=sys.stderr)
        return 1
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output:
        try:
            # The documented example writes dist/app.cdx.json into a tree that
            # has no dist/ until a build makes one, so the parent is created
            # here the way mod_package.py and playtest_compare.py create theirs.
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
        except OSError as ex:
            print(f"dep_sbom: cannot write {args.output}: {ex}", file=sys.stderr)
            return 1
    else:
        sys.stdout.write(text)
    # A release log line a human can read without opening the file.
    print(
        f"dep_sbom: {len(as_objects(document, 'components', 'sbom'))} components, "
        f"{document['serialNumber']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
