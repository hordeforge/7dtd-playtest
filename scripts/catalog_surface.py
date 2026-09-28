#!/usr/bin/env python3
"""Structural readers over Catalog.cs.

The offline gates that pin the catalog (the SCENARIOS.md surface, the declared
ref surface) all need the same answers out of the same C# source: which cases
are Live, which Add method owns which case or barrier, which suites
AppendSuite builds and from which Adds. The parsers live here so a gate imports
a parser instead of importing another gate.
"""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "Source" / "PlayTestMod" / "Catalog.cs"

# The ref a suite document declares and `Runner.CaseRef` builds:
# catalog.SUITE.CASE. The C# literal is pinned in test_suite_refs.
REF_PREFIX = "catalog."


def live_case_ids(src: str) -> set[str]:
    return set(re.findall(r'\bLive\s*\(\s*suite\s*,\s*"([a-z0-9_]+)"', src))


def defer_case_ids(src: str) -> set[str]:
    return set(
        re.findall(
            r'\bDefer\s*\(\s*suite\s*,\s*"([a-z0-9_]+)"',
            src,
        )
    )


def defer_reasons(src: str) -> list[tuple[str, str]]:
    return re.findall(
        r'\bDefer\s*\(\s*suite\s*,\s*"([a-z0-9_]+)"\s*,\s*new\s*\[\s*\]\s*\{[^}]*\}\s*,\s*"([^"]*)"\s*\)',
        src,
        flags=re.S,
    )


def suite_names(src: str) -> set[str]:
    m = re.search(r"static readonly string\[\] SuiteNames\s*=\s*\{([^}]*)\}", src, re.S)
    assert m, "Catalog.cs lost the SuiteNames table"
    return set(re.findall(r'"([a-z0-9_]+)"', m.group(1)))


def expand_alias_ids(src: str) -> set[str]:
    """Alias labels accepted by Catalog.ExpandSuites (case labels in its switch)."""
    start = src.index("public static string[] ExpandSuites")
    end = src.index("static void AddUnique", start)
    body = src[start:end]
    return set(re.findall(r'case "([a-z0-9_]+)":', body))


def append_suite_map(src: str) -> dict[str, list[str]]:
    """Map each concrete AppendSuite case to the Add* methods it composes.

    Most cases call one Add method; benchmark composes four. The default arm
    (external scenario providers) has no built-in body and is omitted.
    """
    start = src.index("public static void AppendSuite")
    end = src.index("static CaseDef Live", start)
    body = src[start:end]
    marks = [(m.start(), m.group(1)) for m in re.finditer(r'case "([a-z0-9_]+)":', body)]
    marks.append((len(body), ""))
    result: dict[str, list[str]] = {}
    for (s, suite), (e, _) in pairwise(marks):
        adds = re.findall(r"\bAdd([A-Z]\w*)\s*\(", body[s:e])
        if adds:
            result[suite] = adds
    return result


def _add_headers(src: str) -> list[tuple[int, str]]:
    headers = [
        (m.start(), m.group(1))
        for m in re.finditer(r"\bstatic void Add([A-Z]\w*)\s*\(", src)
    ]
    assert headers, "Catalog.cs lost every Add method header"
    return headers


def _owning_add_method(
    headers: list[tuple[int, str]], pos: int, item: str
) -> str:
    owner = None
    for hpos, name in headers:
        if hpos < pos:
            owner = name
        else:
            break
    assert owner, f"{item!r} declared before any Add method header"
    return owner


def _attribute_to_add_method(src: str, items: list[tuple[int, str]]) -> dict[str, set[str]]:
    headers = _add_headers(src)
    out: dict[str, set[str]] = {}
    for pos, item in items:
        out.setdefault(_owning_add_method(headers, pos, item), set()).add(item)
    return out


def add_method_case_ids(src: str) -> dict[str, set[str]]:
    """Attribute every Live/Defer case id in Catalog.cs to its Add method.

    Same ownership rule as the barrier attribution: the Add* methods are
    declared sequentially, so the nearest preceding header owns the emission.
    """
    cases = [
        (m.start(), m.group(1))
        for m in re.finditer(r'\b(?:Live|Defer)\s*\(\s*suite\s*,\s*"([a-z0-9_]+)"', src)
    ]
    assert cases, "Catalog.cs lost every Live/Defer case"
    return _attribute_to_add_method(src, cases)


def add_method_case_kinds(src: str) -> dict[str, dict[str, str]]:
    """Case id -> the kind a suite document must declare, per Add method.

    A suite document repeats what the case factory already says: ``CaseDef
    .Defer`` builds a case that records skip, ``CaseDef.Live`` one that runs.
    The JSON is the tool-readable copy, so the two have to be pinned together
    offline or a document can claim ``live`` for a deferred case (the run
    reports a skip the suite never declared) or ``defer`` for a live one (a
    green run that measured nothing). Catalog.cs builds every case with one
    of those two factories; a staged case is declared by an external
    provider, which no catalog ref pins.
    """
    items = [
        (m.start(), m.group(2), "live" if m.group(1) == "Live" else "defer")
        for m in re.finditer(
            r'\b(Live|Defer)\s*\(\s*suite\s*,\s*"([a-z0-9_]+)"', src
        )
    ]
    assert items, "Catalog.cs lost every Live/Defer case"
    headers = _add_headers(src)
    kinds: dict[str, dict[str, str]] = {}
    for pos, case_id, kind in items:
        owner = _owning_add_method(headers, pos, case_id)
        kinds.setdefault(owner, {})[case_id] = kind
    return kinds


def add_method_barriers(src: str) -> dict[str, set[str]]:
    """Attribute every Report.Barrier literal in Catalog.cs to its Add method.

    Catalog.cs declares the Add* methods sequentially and only case bodies
    emit barriers, so the nearest preceding `static void AddX(` header is the
    owner. Prefix only (before any ':' or concatenation): parameterized names
    like "chat_echo:<token>" still match their host BARRIER_NAMES entry.
    """
    emissions = [
        (m.start(), m.group(1))
        for m in re.finditer(r'Report\.Barrier\(\s*"([A-Za-z0-9_]+)', src)
    ]
    assert emissions, "Catalog.cs lost every Report.Barrier emission"
    return _attribute_to_add_method(src, emissions)
