#!/usr/bin/env python3
"""Declarative playtest suite loader (JSON; no PyYAML dep).

Suite documents live under ``suites/`` (built-in) or a path passed via
``--suite-file`` / ``PLAYTEST_SUITE_FILE``. They declare *what* runs and
*where*; C# ``IScenarioProvider`` / Catalog still own *how* cases drive
and assert (``ref`` points at those implementations).

Minimal document::

    {
      "id": "smoke",
      "target": "sandbox",
      "fresh": true,
      "mods": ["playtest", "fastconnect"],
      "host": {"fixtures": false, "loadgen": false},
      "cases": [
        {"id": "boot_to_world", "kind": "live", "ref": "stock.boot_to_world", "tags": ["smoke"]}
      ]
    }

Unknown fields are ignored. Missing required fields fail closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Keep in sync with playtest_targets.TARGETS (avoid import cycle in gates).
ALLOWED_TARGETS = ("stock", "sandbox", "attach", "zdtd", "live")
ALLOWED_KINDS = ("live", "staged", "defer")

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
    target: str
    fresh: bool
    mods: tuple[str, ...]
    host: SuiteHost
    cases: tuple[SuiteCase, ...]
    source: Path | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def case_refs(self) -> tuple[str, ...]:
        return tuple(c.ref for c in self.cases)


class SuiteLoadError(ValueError):
    """Malformed or incomplete suite document."""


def _require_str(obj: dict[str, Any], key: str, *, path: str) -> str:
    raw = obj.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise SuiteLoadError(f"{path}: missing or empty string field {key!r}")
    return raw.strip()


def _optional_bool(obj: dict[str, Any], key: str, default: bool) -> bool:
    raw = obj.get(key, default)
    if not isinstance(raw, bool):
        raise SuiteLoadError(f"field {key!r} must be a boolean")
    return raw


def _string_list(
    obj: dict[str, Any], key: str, *, default: list[str] | None = None
) -> tuple[str, ...]:
    raw = obj.get(key, default if default is not None else [])
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise SuiteLoadError(f"field {key!r} must be a list of non-empty strings")
    return tuple(x.strip() for x in raw)


def parse_suite_dict(data: dict[str, Any], *, source: Path | None = None) -> SuiteDoc:
    """Validate a suite mapping and return a SuiteDoc."""
    if not isinstance(data, dict):
        raise SuiteLoadError("suite document must be a JSON object")
    path = str(source) if source else "<suite>"
    suite_id = _require_str(data, "id", path=path)
    target = _require_str(data, "target", path=path).lower()
    if target not in ALLOWED_TARGETS:
        raise SuiteLoadError(
            f"{path}: target {target!r} not in {', '.join(ALLOWED_TARGETS)}"
        )
    fresh = _optional_bool(data, "fresh", True)
    if not fresh:
        raise SuiteLoadError(
            f"{path}: fresh must be true (playtest hard rule: no reused saves)"
        )
    mods = _string_list(data, "mods", default=["playtest", "fastconnect"])
    host_raw = data.get("host", {})
    if not isinstance(host_raw, dict):
        raise SuiteLoadError(f"{path}: host must be an object")
    host = SuiteHost(
        fixtures=_optional_bool(host_raw, "fixtures", False),
        loadgen=_optional_bool(host_raw, "loadgen", False),
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
        ref = _require_str(row, "ref", path=cpath)
        tags = _string_list(row, "tags", default=[])
        barriers = _string_list(row, "barriers", default=[])
        cases.append(
            SuiteCase(id=cid, kind=kind, ref=ref, tags=tags, barriers=barriers)
        )
    notes_raw = data.get("notes", [])
    notes: tuple[str, ...] = ()
    if notes_raw:
        notes = _string_list(data, "notes", default=[])
    return SuiteDoc(
        id=suite_id,
        target=target,
        fresh=fresh,
        mods=mods,
        host=host,
        cases=tuple(cases),
        source=source,
        notes=notes,
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


def suite_to_report(doc: SuiteDoc) -> dict[str, object]:
    """JSON-serializable summary for the run report."""
    return {
        "id": doc.id,
        "target": doc.target,
        "fresh": doc.fresh,
        "mods": list(doc.mods),
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
