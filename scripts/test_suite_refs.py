#!/usr/bin/env python3
"""Gate: every declarative suite ref resolves to a real Catalog case.

This is what makes ``suites/*.json`` authoritative rather than decorative. The
orchestrator hands the declared refs to the client, which runs only the cases
whose ref appears among them, so:

- a ref with no implementation would silently drop a case a suite claims to run
- a case added to Catalog.cs but declared in no suite never runs

Both are failures here, offline, instead of a green run that measured less than
it claimed. The ref format is pinned on both sides: ``catalog.SUITE.CASE``,
built by ``Runner.CaseRef``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import suite_loader
from catalog_surface import (
    CATALOG,
    REF_PREFIX,
    add_method_case_ids,
    add_method_case_kinds,
    append_suite_map,
)

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "Source" / "PlayTestMod" / "Runner.cs"

# The stock-fidelity suites every checkout must be able to discover. If these
# are gone the four tests below iterate an empty dict and pass vacuously.
REQUIRED_SUITE_IDS = {"smoke", "core", "parachute"}

# Cases that exist for a suite the declarative layer does not own yet. Each entry
# is a suite id still built only from Catalog.cs; drop it when the suite gets a
# suites/*.json document.
UNDECLARED_SUITES = {
    "world",
    "ui",
    "combat",
    "economy",
    "quest",
    "vehicle",
    "power",
    "finale",
    "persist_setup",
    "persist",
    "mp",
    "soak",
    "soak_long",
    "apm",
    "bot",
    "benchmark",
}


def catalog_refs() -> dict[str, set[str]]:
    """Every implemented case ref, grouped by suite id."""
    src = CATALOG.read_text(encoding="utf-8")
    by_method = add_method_case_ids(src)
    out: dict[str, set[str]] = {}
    for suite, adds in append_suite_map(src).items():
        ids: set[str] = set()
        for add in adds:
            ids |= by_method.get(add, set())
        out[suite] = ids
    return out


def test_ref_format_is_pinned_on_both_sides() -> None:
    """The client builds the same ref string the suite files declare."""
    src = RUNNER.read_text(encoding="utf-8")
    assert 'return "catalog." + suite + "." + (c == null ? "" : c.Id);' in src, (
        "Runner.CaseRef no longer builds catalog.SUITE.CASE; the declarative "
        "suites' ref format and the client's filter have drifted"
    )
    print("PASS ref_format_is_pinned_on_both_sides")


def test_every_declared_ref_has_an_implementation() -> None:
    implemented = catalog_refs()
    missing: list[str] = []
    for suite_id, doc in sorted(suite_loader.discover_suites().items()):
        for case in doc.cases:
            if not case.ref.startswith(REF_PREFIX):
                missing.append(f"{suite_id}/{case.id}: ref must start with {REF_PREFIX!r}")
                continue
            rest = case.ref[len(REF_PREFIX) :]
            suite, _, case_name = rest.partition(".")
            if not suite or not case_name:
                missing.append(f"{suite_id}/{case.id}: malformed ref {case.ref!r}")
                continue
            if suite not in implemented:
                missing.append(
                    f"{suite_id}/{case.id}: ref names suite {suite!r}, which "
                    "Catalog.AppendSuite does not build"
                )
                continue
            if case_name not in implemented[suite]:
                missing.append(
                    f"{suite_id}/{case.id}: no Catalog case {case_name!r} in suite {suite!r}"
                )
    assert not missing, "declared refs without an implementation:\n  " + "\n  ".join(missing)
    print("PASS every_declared_ref_has_an_implementation")


def test_every_declared_suite_declares_all_its_cases() -> None:
    """A suite with a JSON document must declare every case its Add builds.

    Otherwise the filter drops the undeclared case and the suite quietly runs
    less than the catalog says it does.
    """
    implemented = catalog_refs()
    gaps: list[str] = []
    for suite_id, doc in sorted(suite_loader.discover_suites().items()):
        if suite_id not in implemented:
            continue
        declared = {
            case.ref[len(REF_PREFIX) :].partition(".")[2]
            for case in doc.cases
            if case.ref.startswith(REF_PREFIX)
        }
        undeclared = sorted(implemented[suite_id] - declared)
        if undeclared:
            gaps.append(f"{suite_id}: {', '.join(undeclared)}")
    assert not gaps, (
        "Catalog cases missing from their suite document (they would not run):\n  "
        + "\n  ".join(gaps)
    )
    print("PASS every_declared_suite_declares_all_its_cases")


def test_discovery_finds_the_stock_fidelity_suites() -> None:
    """``suites/`` must actually be discovered.

    The other four tests iterate ``discover_suites()``; a renamed or missing
    directory makes that dict empty and they pass while checking nothing. The
    two stock-fidelity suites are the floor.
    """
    found = set(suite_loader.discover_suites())
    assert found >= REQUIRED_SUITE_IDS, (
        f"suite discovery found {sorted(found)}, missing the stock-fidelity "
        f"ids {sorted(REQUIRED_SUITE_IDS - found)}"
    )
    print("PASS discovery_finds_the_stock_fidelity_suites")


def test_undeclared_suites_are_listed() -> None:
    """Every catalog suite is either declared in JSON or listed as not yet.

    A new suite added to Catalog.AppendSuite lands here, so the choice to leave
    it out of the declarative layer stays explicit instead of implicit.
    """
    implemented = set(catalog_refs())
    declared = set(suite_loader.discover_suites())
    unaccounted = sorted(implemented - declared - UNDECLARED_SUITES)
    assert not unaccounted, (
        "catalog suites neither declared in suites/*.json nor listed in "
        f"UNDECLARED_SUITES: {', '.join(unaccounted)}"
    )
    stale = sorted(UNDECLARED_SUITES & declared)
    assert not stale, (
        f"suites now declared in suites/*.json but still listed as undeclared: "
        f"{', '.join(stale)}"
    )
    print("PASS undeclared_suites_are_listed")


def catalog_case_kinds() -> dict[str, dict[str, str]]:
    """Suite id -> case id -> the kind its factory builds, from Catalog.cs."""
    src = CATALOG.read_text(encoding="utf-8")
    by_method = add_method_case_kinds(src)
    out: dict[str, dict[str, str]] = {}
    for suite, adds in append_suite_map(src).items():
        kinds: dict[str, str] = {}
        for add in adds:
            for case_id, kind in by_method.get(add, {}).items():
                if case_id in kinds and kinds[case_id] != kind:
                    raise AssertionError(
                        f"catalog case {suite}.{case_id} is built both live and "
                        "deferred; its declared kind is ambiguous"
                    )
                kinds[case_id] = kind
        out[suite] = kinds
    return out


def test_declared_kind_matches_the_implementation() -> None:
    """A case's declared kind is the kind its CaseDef factory builds.

    ``kind`` is the only field in a suite document that states how a case
    runs, and it is a copy: CaseDef.Live runs, CaseDef.Defer records a skip.
    Nothing consumed the copy, so a document could claim ``live`` for a case
    the client defers (the report then shows a skip the suite never declared)
    or ``defer`` for a live one (a green run that measured nothing). Pinning
    them here is what keeps the copy true.
    """
    implemented = catalog_case_kinds()
    drift: list[str] = []
    for suite_id, doc in sorted(suite_loader.discover_suites().items()):
        for case in doc.cases:
            if not case.ref.startswith(REF_PREFIX):
                # An external provider owns how its case runs; no catalog ref
                # pins it offline.
                continue
            case_name = case.ref[len(REF_PREFIX) :].partition(".")[2]
            expected = implemented.get(suite_id, {}).get(case_name)
            if expected is None:
                continue  # reported by the implementation gate
            if case.kind != expected:
                drift.append(
                    f"{suite_id}/{case.id}: declares kind {case.kind!r}, "
                    f"Catalog builds it {expected!r}"
                )
    assert not drift, "declared case kind does not match Catalog.cs:\n  " + "\n  ".join(drift)
    print("PASS declared_kind_matches_the_implementation")


TESTS = (
    test_ref_format_is_pinned_on_both_sides,
    test_discovery_finds_the_stock_fidelity_suites,
    test_every_declared_ref_has_an_implementation,
    test_every_declared_suite_declares_all_its_cases,
    test_declared_kind_matches_the_implementation,
    test_undeclared_suites_are_listed,
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
        print(f"test_suite_refs: FAILED ({failed})", file=sys.stderr)
        return 1
    print("test_suite_refs: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
