#!/usr/bin/env python3
"""Structural guard for the public external-scenario provider contract."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "Source" / "PlayTestMod" / "Runner.cs"
CATALOG = ROOT / "Source" / "PlayTestMod" / "Catalog.cs"
PROVIDER = ROOT / "Source" / "PlayTestMod" / "ScenarioProvider.cs"
README = ROOT / "README.md"


def method_body(src: str, signature_re: str) -> str:
    """Return the C# method body between the first matching signature and its closing brace."""
    m = re.search(signature_re, src)
    assert m, f"method not found: {signature_re}"
    i = m.end()
    # skip whitespace to opening brace
    while i < len(src) and src[i] in " \t\r\n":
        i += 1
    assert i < len(src) and src[i] == "{", f"expected '{{' after {signature_re}"
    depth = 0
    start = i
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"unclosed body for {signature_re}")


def main() -> int:
    runner = RUNNER.read_text(encoding="utf-8")
    catalog = CATALOG.read_text(encoding="utf-8")
    provider = PROVIDER.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    assert "public sealed class CaseDef" in runner
    assert "public sealed class CaseCtx" in runner
    assert "public interface IScenarioProvider" in provider
    assert "IEnumerable<string> SuiteIds" in provider
    assert "void AppendSuite(List<CaseDef> queue, string suite, int lap)" in provider
    assert "AppDomain.CurrentDomain.GetAssemblies()" in provider
    assert "ScenarioProviders.AppendSuite(q, suite, lap);" in catalog
    assert "ScenarioProviders.SuiteIds()" in catalog

    # Public factories on CaseDef (external providers must not hand-build fields).
    assert re.search(
        r"public\s+static\s+CaseDef\s+Live\s*\(",
        runner,
    ), "CaseDef.Live must be a public static factory"
    assert re.search(
        r"public\s+static\s+CaseDef\s+Defer\s*\(",
        runner,
    ), "CaseDef.Defer must be a public static factory"

    live_body = method_body(
        runner,
        r"public\s+static\s+CaseDef\s+Live\s*\([^)]*\)",
    )
    defer_body = method_body(
        runner,
        r"public\s+static\s+CaseDef\s+Defer\s*\([^)]*\)",
    )

    # Live path: non-deferred CaseDef (explicit Deferred = false, never true).
    assert "Deferred = false" in live_body or "Deferred=false" in live_body.replace(
        " ", ""
    ), "CaseDef.Live must set Deferred = false"
    assert not re.search(r"Deferred\s*=\s*true", live_body), (
        "CaseDef.Live must not set Deferred = true"
    )
    assert "Act = act" in live_body or "Act=act" in live_body.replace(" ", "")
    assert "new CaseDef" in live_body

    # Deferred path: skip with reason.
    assert re.search(r"Deferred\s*=\s*true", defer_body), (
        "CaseDef.Defer must set Deferred = true"
    )
    assert "DeferReason" in defer_body, "CaseDef.Defer must set DeferReason"
    assert "new CaseDef" in defer_body
    # Deferred cases do not require Act/Wait/Assert.
    assert not re.search(r"\bAct\s*=", defer_body), (
        "CaseDef.Defer should not assign Act"
    )

    # Built-in catalog shares the public path (thin wrappers, not a second oracle).
    cat_live = method_body(
        catalog,
        r"static\s+CaseDef\s+Live\s*\([^)]*\)",
    )
    cat_defer = method_body(
        catalog,
        r"static\s+CaseDef\s+Defer\s*\([^)]*\)",
    )
    assert "CaseDef.Live(" in cat_live, (
        "Catalog.Live must delegate to CaseDef.Live"
    )
    assert "CaseDef.Defer(" in cat_defer, (
        "Catalog.Defer must delegate to CaseDef.Defer"
    )
    # Catalog wrappers must not re-implement field assignment.
    assert "new CaseDef" not in cat_live, (
        "Catalog.Live must not construct CaseDef by hand"
    )
    assert "new CaseDef" not in cat_defer, (
        "Catalog.Defer must not construct CaseDef by hand"
    )

    # README documents the public entry points for provider authors.
    assert "CaseDef.Live" in readme, "README must document CaseDef.Live"
    assert "CaseDef.Defer" in readme, "README must document CaseDef.Defer"

    print("OK external scenario-provider surface")
    print("OK public CaseDef.Live / CaseDef.Defer factories")
    print("OK Live is non-deferred; Defer sets Deferred+reason")
    print("OK Catalog Live/Defer share CaseDef factories")
    print("OK README documents CaseDef.Live/Defer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
