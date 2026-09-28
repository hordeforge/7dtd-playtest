#!/usr/bin/env python3
"""Every offline gate in scripts/ runs under `make test`, and CI runs `make check`.

A new scripts/test_*.py that nobody added to the Makefile GATES list is a gate
that never runs locally or in CI, so a broken change passes green until it
reaches main. The inverse (a GATES entry with no file) makes `make test` fail
on a clean clone. Both are checked here, together with the CI steps that must
stay the ones `make check` performs.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
SCRIPTS = ROOT / "scripts"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CHANGELOG = ROOT / "CHANGELOG.md"

GATES_VAR = "GATES"
GATES_ASSIGN = re.compile(rf"^{GATES_VAR}[ \t]*:?=[ \t]*((?:.*\\\n)*.*)$", re.MULTILINE)


def gates_from_makefile(makefile: str) -> list[str]:
    m = GATES_ASSIGN.search(makefile)
    assert m, f"Makefile has no {GATES_VAR} := assignment"
    return re.findall(r"[\w./-]+\.py", m.group(1).replace("\\\n", " "))


def gate_files_on_disk() -> list[str]:
    return sorted(p.name for p in SCRIPTS.glob("test_*.py"))


def check_list() -> None:
    gates = gates_from_makefile(MAKEFILE.read_text(encoding="utf-8"))
    assert gates, f"Makefile {GATES_VAR} list is empty"
    dupes = sorted({g for g in gates if gates.count(g) > 1})
    assert not dupes, f"Makefile {GATES_VAR} lists a gate twice: {', '.join(dupes)}"

    missing = [g for g in gates if not (SCRIPTS / g).is_file()]
    assert not missing, f"Makefile {GATES_VAR} names a file that does not exist: {missing}"

    unlisted = sorted(set(gate_files_on_disk()) - set(gates))
    assert not unlisted, (
        "gate file(s) missing from the Makefile "
        f"{GATES_VAR} list, so they run under neither `make test` nor CI: "
        f"{', '.join(unlisted)}"
    )


def target_body(makefile: str, name: str) -> str:
    lines = makefile.splitlines()
    body: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith(f"{name}:"):
            for follow in lines[i + 1 :]:
                if not follow.startswith("\t"):
                    break
                body.append(follow)
            break
    return "\n".join(body)


def check_test_and_coverage_share_one_list() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    # `test` and `coverage` must both expand GATES, never their own copies, so a
    # gate added to one runs in CI but is not measured by the badge job.
    assert "$(GATES)" in target_body(makefile, "test"), "make test must iterate $(GATES)"
    coverage = target_body(makefile, "coverage")
    assert "$(GATES)" in coverage, "make coverage must iterate the same $(GATES) list"
    assert "-m coverage run" in coverage, "make coverage must run the gates under coverage"
    assert "$(COV)" in coverage, "make coverage must use the same interpreter pin as make test"
    assert target_body(makefile, "test-one"), (
        "make test-one must exist so one gate can be run while iterating"
    )
    assert "$(ROOT)/scripts/$(GATE)" in target_body(makefile, "test-one"), (
        "make test-one must run scripts/$(GATE)"
    )


def check_ci_matches_check_target() -> None:
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    steps = re.findall(r"^\s*- run: (make .+)$", ci, re.MULTILINE)
    assert "make test" in steps, f"CI does not run `make test`: {steps}"
    assert "make dst DST_SEEDS=200" in steps, (
        f"CI's DST sweep diverges from `make check` (which passes DST_SEEDS=200): {steps}"
    )
    # `make check` is the documented single local equivalent of CI; if it stops
    # running the same two commands the docs promise, say so here.
    makefile = MAKEFILE.read_text(encoding="utf-8")
    check_body = target_body(makefile, "check")
    assert check_body, "Makefile has no check: target body"
    for step in ("$(MAKE) test", "$(MAKE) dst DST_SEEDS=200"):
        assert step in check_body, f"make check no longer runs `{step}`"


def check_documented() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    for surface in (ROOT / "CONTRIBUTING.md", ROOT / "AGENTS.md"):
        text = surface.read_text(encoding="utf-8")
        assert "make check" in text, f"{surface.name} must name `make check` as the full gate"
    assert "test_gate_list.py" in makefile, "this gate is not wired into the Makefile GATES"
    assert "test_gate_list.py" in CHANGELOG.read_text(encoding="utf-8"), (
        "the new gate needs a CHANGELOG note"
    )


def orphan_tests(gate: Path) -> list[str]:
    """Module-level ``test_*`` functions the gate's own runner never names.

    A script gate dispatches through a hand-maintained registry (a ``TESTS``
    tuple, a list of cases, direct calls in ``main``). A test left out of that
    registry is dead weight: it reads as coverage in review and never runs, so
    the assertion it makes is the one nobody gets. Gates that hand themselves to
    pytest collect by definition, so they are exempt.
    """
    src = gate.read_text(encoding="utf-8")
    if "pytest.main" in src:
        return []
    tree = ast.parse(src, filename=str(gate))
    defined = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
    }
    if not defined:
        return []
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    return sorted(defined - referenced)


def check_every_gate_test_runs() -> None:
    orphans = {
        name: found
        for name in (p.name for p in sorted(SCRIPTS.glob("test_*.py")))
        if (found := orphan_tests(SCRIPTS / name))
    }
    detail = "\n  ".join(f"{n}: {', '.join(f)}" for n, f in orphans.items())
    assert not orphans, (
        "test function(s) defined but never named by their gate's runner, so "
        f"they never execute:\n  {detail}"
    )


def main() -> int:
    check_list()
    check_test_and_coverage_share_one_list()
    check_ci_matches_check_target()
    check_every_gate_test_runs()
    check_documented()
    print("PASS every scripts/test_*.py is listed in the Makefile GATES")
    print("PASS every GATES entry exists and is listed once")
    print("PASS make test / make coverage / make test-one share one gate list")
    print("PASS CI runs the same steps make check does")
    print("PASS every test_* in a gate is dispatched by that gate's runner")
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
