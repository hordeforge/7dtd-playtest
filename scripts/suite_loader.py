#!/usr/bin/env python3
"""Declarative playtest suite loader (JSON; no PyYAML dep).

A suite document declares *what* runs and *where*. C# ``IScenarioProvider`` /
Catalog still own *how* a case drives and asserts: ``ref`` points at those
implementations, and the orchestrator passes the declared refs to the client so
a case the catalog owns but no suite declares does not run.

Built-in stock-fidelity suites live under ``suites/``. A mod repo keeps its own
suites beside its provider and passes the path with ``--suite-file``.

Minimal document::

    {
      "id": "smoke",
      "provision": "managed",
      "backend": "stock",
      "fresh": true,
      "mods": ["playtest", "fastconnect"],
      "server": {"GameWorld": "Navezgane", "MaxSpawnedZombies": "0"},
      "host": {"fixtures": false, "loadgen": false},
      "cases": [
        {"id": "join_ready", "kind": "live", "ref": "catalog.smoke.join_ready"}
      ]
    }

``server`` is a flat map of stock serverconfig property names to values, handed
straight to ``sb render-config``. It is the only place a suite states the world
it needs, so an A/B of one config knob is two suites differing by one line.

Unknown fields are ignored. Missing or contradictory fields fail closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Keep in sync with playtest_targets (avoid an import cycle in the gates).
ALLOWED_PROVISIONS = ("managed", "attach")
ALLOWED_BACKENDS = ("stock", "zdtd")
ALLOWED_KINDS = ("live", "staged", "defer")

DEFAULT_MODS = ("playtest", "fastconnect")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITES_DIR = ROOT / "suites"


@dataclass(frozen=True)
class SuiteCase:
    id: str
    kind: str
    ref: str
    tags: tuple[str, ...] = ()
    barriers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SuiteHost:
    fixtures: bool = False
    loadgen: bool = False


@dataclass(frozen=True)
class SuiteDoc:
    id: str
    provision: str
    backend: str
    readonly: bool
    fresh: bool
    mods: tuple[str, ...]
    server: tuple[tuple[str, str], ...]
    host: SuiteHost
    cases: tuple[SuiteCase, ...]
    source: Path | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def case_refs(self) -> tuple[str, ...]:
        return tuple(c.ref for c in self.cases)

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.cases)

    @property
    def server_config(self) -> dict[str, str]:
        return dict(self.server)


class SuiteLoadError(ValueError):
    """Malformed, incomplete or self-contradictory suite document."""


def _require_str(obj: dict[str, Any], key: str, *, path: str) -> str:
    raw = obj.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise SuiteLoadError(f"{path}: missing or empty string field {key!r}")
    return raw.strip()


def _optional_str(obj: dict[str, Any], key: str, default: str, *, path: str) -> str:
    raw = obj.get(key, default)
    if not isinstance(raw, str) or not raw.strip():
        raise SuiteLoadError(f"{path}: field {key!r} must be a non-empty string")
    return raw.strip()


def _optional_bool(obj: dict[str, Any], key: str, default: bool, *, path: str) -> bool:
    raw = obj.get(key, default)
    if not isinstance(raw, bool):
        raise SuiteLoadError(f"{path}: field {key!r} must be a boolean")
    return raw


def _string_list(
    obj: dict[str, Any], key: str, *, default: tuple[str, ...] = (), path: str = ""
) -> tuple[str, ...]:
    raw = obj.get(key, list(default))
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise SuiteLoadError(f"{path}: field {key!r} must be a list of non-empty strings")
    return tuple(x.strip() for x in raw)


def _server_map(obj: dict[str, Any], *, path: str) -> tuple[tuple[str, str], ...]:
    raw = obj.get("server", {})
    if not isinstance(raw, dict):
        raise SuiteLoadError(
            f"{path}: server must be an object of serverconfig property names to values"
        )
    out: list[tuple[str, str]] = []
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise SuiteLoadError(f"{path}: server property names must be non-empty strings")
        if isinstance(value, bool):
            # Stock ParseBool accepts only true/false, never Python's True/False.
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, str):
            text = value
        else:
            raise SuiteLoadError(
                f"{path}: server property {key!r} must be a string, number or boolean"
            )
        out.append((key.strip(), text))
    return tuple(out)


def parse_suite_dict(data: dict[str, Any], *, source: Path | None = None) -> SuiteDoc:
    """Validate a suite mapping and return a SuiteDoc."""
    if not isinstance(data, dict):
        raise SuiteLoadError("suite document must be a JSON object")
    path = str(source) if source else "<suite>"
    suite_id = _require_str(data, "id", path=path)

    provision = _optional_str(data, "provision", "managed", path=path).lower()
    if provision not in ALLOWED_PROVISIONS:
        raise SuiteLoadError(
            f"{path}: provision {provision!r} not in {', '.join(ALLOWED_PROVISIONS)}"
        )
    backend = _optional_str(data, "backend", "stock", path=path).lower()
    if backend not in ALLOWED_BACKENDS:
        raise SuiteLoadError(
            f"{path}: backend {backend!r} not in {', '.join(ALLOWED_BACKENDS)}"
        )
    readonly = _optional_bool(data, "readonly", False, path=path)
    if readonly and provision != "attach":
        raise SuiteLoadError(
            f"{path}: readonly names a host playtest must not write to, so it "
            "requires provision 'attach'"
        )

    fresh = _optional_bool(data, "fresh", provision == "managed", path=path)
    if provision == "managed" and not fresh:
        raise SuiteLoadError(
            f"{path}: a managed run always starts on a fresh save (hard rule); "
            "fresh must be true"
        )
    if provision == "attach" and fresh:
        raise SuiteLoadError(
            f"{path}: an attach run does not own the save it joins, so it cannot "
            "claim fresh; set fresh false and wipe the host yourself"
        )

    server = _server_map(data, path=path)
    mods = _string_list(
        data, "mods", default=DEFAULT_MODS if provision == "managed" else (), path=path
    )
    if provision == "attach":
        if server:
            raise SuiteLoadError(
                f"{path}: an attach run must not rewrite the config of a server "
                "it does not own; drop the server block"
            )
        if mods:
            raise SuiteLoadError(
                f"{path}: an attach run must not stage mods into a server it does "
                "not own; drop the mods list"
            )

    host_raw = data.get("host", {})
    if not isinstance(host_raw, dict):
        raise SuiteLoadError(f"{path}: host must be an object")
    host = SuiteHost(
        fixtures=_optional_bool(host_raw, "fixtures", False, path=path),
        loadgen=_optional_bool(host_raw, "loadgen", False, path=path),
    )

    cases_raw = data.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise SuiteLoadError(f"{path}: cases must be a non-empty list")
    cases: list[SuiteCase] = []
    seen: set[str] = set()
    for i, row in enumerate(cases_raw):
        cpath = f"{path}.cases[{i}]"
        if not isinstance(row, dict):
            raise SuiteLoadError(f"{cpath}: case must be an object")
        cid = _require_str(row, "id", path=cpath)
        if cid in seen:
            raise SuiteLoadError(f"{cpath}: duplicate case id {cid!r}")
        seen.add(cid)
        kind = _require_str(row, "kind", path=cpath).lower()
        if kind not in ALLOWED_KINDS:
            raise SuiteLoadError(
                f"{cpath}: kind {kind!r} not in {', '.join(ALLOWED_KINDS)}"
            )
        cases.append(
            SuiteCase(
                id=cid,
                kind=kind,
                ref=_require_str(row, "ref", path=cpath),
                tags=_string_list(row, "tags", path=cpath),
                barriers=_string_list(row, "barriers", path=cpath),
            )
        )

    return SuiteDoc(
        id=suite_id,
        provision=provision,
        backend=backend,
        readonly=readonly,
        fresh=fresh,
        mods=mods,
        server=server,
        host=host,
        cases=tuple(cases),
        source=source,
        notes=_string_list(data, "notes", path=path),
    )


def load_suite_file(path: Path) -> SuiteDoc:
    """Load one suite JSON file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as ex:
        raise SuiteLoadError(f"cannot read suite file {path}: {ex}") from ex
    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        raise SuiteLoadError(f"{path}: invalid JSON: {ex}") from ex
    if not isinstance(data, dict):
        raise SuiteLoadError(f"{path}: top-level JSON must be an object")
    return parse_suite_dict(data, source=path)


def discover_suites(suites_dir: Path | None = None) -> dict[str, SuiteDoc]:
    """Load all ``*.json`` suite docs under suites_dir (id -> doc).

    Duplicate ids fail closed. Missing directory yields {}.
    """
    root = suites_dir or DEFAULT_SUITES_DIR
    if not root.is_dir():
        return {}
    found: dict[str, SuiteDoc] = {}
    for path in sorted(root.glob("*.json")):
        doc = load_suite_file(path)
        if doc.id in found:
            raise SuiteLoadError(
                f"duplicate suite id {doc.id!r}: {found[doc.id].source} and {path}"
            )
        found[doc.id] = doc
    return found


def suite_ids(suites_dir: Path | None = None) -> tuple[str, ...]:
    return tuple(sorted(discover_suites(suites_dir).keys()))


def load_suite_by_id(suite_id: str, suites_dir: Path | None = None) -> SuiteDoc | None:
    """Return the declarative suite for suite_id, or None if not declared."""
    return discover_suites(suites_dir).get(suite_id.strip())


def load_external_suite(path: Path) -> SuiteDoc:
    """Load a mod repo's own suite file and refuse a stock-suite id collision.

    A mod's suite may not claim a built-in id: the built-in is what a stock
    fidelity claim means, and shadowing it would silently rescore it.
    """
    doc = load_suite_file(path)
    builtin = discover_suites().get(doc.id)
    if builtin is not None and builtin.source != doc.source:
        raise SuiteLoadError(
            f"{path}: suite id {doc.id!r} is a built-in stock-fidelity suite "
            f"({builtin.source}); pick another id"
        )
    return doc


def resolve_mods(doc: SuiteDoc, *, workspace: Path, repo: Path) -> list[Path]:
    """Resolve the suite's mod names to built modlet directories.

    A short name resolves to the sibling repo's ``dist/`` output; anything with
    a separator is a path, taken relative to the suite file when relative.
    """
    known = {
        "playtest": repo / "dist" / "7dtd-playtest",
        "fastconnect": workspace / "7dtd-fastconnect" / "dist" / "7dtd-fastconnect",
    }
    base = doc.source.parent if doc.source is not None else workspace
    out: list[Path] = []
    for name in doc.mods:
        if name in known:
            out.append(known[name])
            continue
        candidate = Path(name)
        out.append(candidate if candidate.is_absolute() else (base / candidate).resolve())
    return out


def suite_to_report(doc: SuiteDoc) -> dict[str, object]:
    """JSON-serializable summary for the run report."""
    return {
        "id": doc.id,
        "provision": doc.provision,
        "backend": doc.backend,
        "readonly": doc.readonly,
        "fresh": doc.fresh,
        "mods": list(doc.mods),
        "server": doc.server_config,
        "host": {"fixtures": doc.host.fixtures, "loadgen": doc.host.loadgen},
        "cases": [
            {
                "id": c.id,
                "kind": c.kind,
                "ref": c.ref,
                "tags": list(c.tags),
                "barriers": list(c.barriers),
            }
            for c in doc.cases
        ],
        "source": str(doc.source) if doc.source else None,
    }
