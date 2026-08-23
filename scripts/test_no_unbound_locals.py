#!/usr/bin/env python3
"""Static gate: no local variable read before its first store in a scope.

A name assigned anywhere in a Python function is a local everywhere in it;
reading it on a path that runs before the first assignment raises
UnboundLocalError. This shipped once (orchestrator main() read
telnet_password two hundred lines before its only assignment), crashing
every default stock-dedicated run while all runtime gates stayed green,
because reaching the crash needs real game binaries.

This gate is offline and deterministic: parse each host script, and for
every function scope flag names LOADed lexically before their first STORE.
Comprehension targets, nested scopes, global/nonlocal declarations, module
globals, parameters, and except-handler bindings are handled per language
rules. Under-approximates by design: zero false positives beat catching
one more theoretical case.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# Host-side python entry points and libraries. The mod DLL is built by
# dotnet (its own compile-time checks); these files have no other gate.
GATED_FILES = (
    "playtest_run.py",
    "playtest_lock.py",
    "playtest_compare.py",
    "dst.py",
    "dst_run.py",
    "dst_sim.py",
)

NESTED_SCOPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)


def _scope_nodes(scope: ast.AST) -> ast.AST:
    """Yield nodes belonging to ``scope`` itself, not nested scopes."""
    body: list[ast.stmt] | None = getattr(scope, "body", None)
    if body is None:
        return
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, NESTED_SCOPES):
            # Comprehension iterables evaluate in the enclosing scope, and
            # default argument expressions too; keep those, skip the bodies.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.args.defaults:
                    stack.extend(ast.walk(d))
                for d in node.args.kw_defaults:
                    if d is not None:
                        stack.extend(ast.walk(d))
                for dec in node.decorator_list:
                    stack.extend(ast.walk(dec))
            elif isinstance(node, ast.Lambda):
                for d in node.args.defaults:
                    stack.extend(ast.walk(d))
                for d in node.args.kw_defaults:
                    if d is not None:
                        stack.extend(ast.walk(d))
            continue
        stack.extend(ast.iter_child_nodes(node))


def _comprehension_targets(scope: ast.AST) -> set[str]:
    """Names bound by comprehension ``for`` targets (own scope in py3)."""
    bound: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.comprehension):
            for name in ast.walk(node.target):
                if isinstance(name, ast.Name):
                    bound.add(name.id)
    return bound


def _declared_global_nonlocal(scope: ast.stmt) -> set[str]:
    declared: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
    return declared


def _stores_loads(scope: ast.AST) -> tuple[dict[str, int], dict[str, int]]:
    """First-store / first-load line per name inside one function scope."""
    stores: dict[str, int] = {}
    loads: dict[str, int] = {}

    def note(names: list[str], lineno: int, *, store: bool) -> None:
        table = stores if store else loads
        for name in names:
            if name not in table or lineno < table[name]:
                table[name] = lineno

    args: ast.arguments | None = getattr(scope, "args", None)
    if args is not None:
        params = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        if args.vararg:
            params.append(args.vararg)
        if args.kwarg:
            params.append(args.kwarg)
        note([a.arg for a in params], scope.lineno, store=True)

    for node in _scope_nodes(scope):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                note([node.id], node.lineno, store=True)
            elif isinstance(node.ctx, ast.Load):
                note([node.id], node.lineno, store=False)
        elif isinstance(node, ast.NamedExpr):
            tgt = node.target
            if isinstance(tgt, ast.Name):
                note([tgt.id], tgt.lineno, store=True)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                note([node.name], node.lineno, store=True)
    return stores, loads


def find_violations(tree: ast.Module) -> list[str]:
    problems: list[str] = []

    module_stores: dict[str, int] = {}
    module_scope = ast.Module(body=tree.body, type_ignores=[])
    m_stores, _ = _stores_loads(module_scope)
    module_stores.update(m_stores)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_stores.setdefault(node.name, node.lineno)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        comp_bound = _comprehension_targets(node)
        declared = _declared_global_nonlocal(node)
        stores, loads = _stores_loads(node)
        for name, load_line in sorted(loads.items(), key=lambda kv: kv[1]):
            if name in comp_bound or name in declared:
                continue
            if name not in stores:
                # Module-level global or builtin: fine unless shadowed later,
                # which the stores map would have captured.
                if name in module_stores:
                    continue
                continue
            if load_line < stores[name]:
                problems.append(
                    f"{node.name}: {name!r} loaded at line {load_line} "
                    f"but first stored at line {stores[name]}"
                )
    return problems


def _violations(src: str) -> list[str]:
    return find_violations(ast.parse(textwrap.dedent(src)))


def test_find_violations_flags_read_before_store() -> None:
    """The gate must fire on the crash class it exists for: a lexically
    earlier read of a name whose only store comes later (the shipped
    telnet_password shape)."""
    probs = _violations(
        """
        def f():
            print(a)
            a = 1
        """
    )
    assert any("'a'" in p for p in probs), f"plain load-before-store missed: {probs}"
    print("PASS unbound_locals_detects flags lexical read-before-store")


def test_find_violations_accepts_safe_scopes() -> None:
    """Under-approximation contract (zero false positives by design):
    parameters, global/nonlocal declarations, comprehension targets, walrus
    stores, nested-scope reads of enclosing names, and a store on a branch
    that lexically precedes the load are never flagged."""
    safe = [
        "def f(p):\n    return p\n",
        "g_val = 0\ndef f():\n    global g_val\n    print(g_val)\n",
        "def f(xs):\n    ys = [x for x in xs]\n    return x if False else len(ys)\n",
        "def f():\n    print((w := 5))\n    return w\n",
        "def outer():\n    a = 1\n    def inner():\n        return a\n    return inner\n",
        # Runtime-unsafe when c is falsy, but the store is lexically first;
        # path-sensitive detection is explicitly out of scope.
        "def g(c):\n    if c:\n        x = 1\n    return x\n",
    ]
    for src in safe:
        probs = _violations(src)
        assert probs == [], f"false positive on documented-safe shape:\n{src}{probs}"
    print("PASS unbound_locals_safe accepts params/global/comprehension/walrus/nested")


def main() -> int:
    test_find_violations_flags_read_before_store()
    test_find_violations_accepts_safe_scopes()
    failures = 0
    for fname in GATED_FILES:
        path = SCRIPTS / fname
        if not path.is_file():
            print(f"FAIL no_unbound_locals: missing gated file {fname}")
            failures += 1
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        problems = find_violations(tree)
        if problems:
            failures += 1
            print(f"FAIL no_unbound_locals {fname}")
            for p in problems:
                print(f"  {p}")
        else:
            print(f"PASS no_unbound_locals {fname}")
    if failures:
        print(
            "RESULT FAIL "
            f"({failures} file(s) with local-read-before-store; "
            "runtime effect is UnboundLocalError)"
        )
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
