#!/usr/bin/env python3
"""Offline gate: every symbol the README's C# snippets call is exported.

The README is the provider SDK's only reference: an external mod author reads
the "Minimal provider" and "Build cases" snippets and writes against them
without the game install in front of them. A snippet that names a member the
dll does not export, or names an argument the method does not have, compiles
only inside the author's own build and reads as a real API until then, so the
documented surface is pinned here against the C# sources.

Only member *accesses* are checked (`Helpers.X`, `probe.Result` and friends
are provider API; the rest of a snippet is ordinary C#). A `Type.Member` call
naming neither a public mod type nor `NON_MOD_TYPES` fails the gate, so a
snippet that calls into a type that was renamed away is caught instead of
being skipped as "not ours".
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "Source" / "PlayTestMod"
README = ROOT / "README.md"

TYPE_DECL = re.compile(
    r"public\s+(?:sealed\s+|static\s+|abstract\s+|partial\s+)*"
    r"(?:class|struct|interface|enum)\s+(\w+)"
)
MEMBER_DECL = re.compile(r"\bpublic\b[^;{()]*?\b(\w+)\s*(?:\(|[=;])")
TYPE_REF = re.compile(r"\b([A-Z]\w*)\.(\w+)\s*\(")
NAMED_ARG = re.compile(r"(?<![:?\w])(\w+)\s*:")
STRING_LITERAL = re.compile(r'"[^"\n]*"')
FENCE = re.compile(r"```csharp\n(.*?)```", re.DOTALL)
INLINE_CODE = re.compile(r"`([^`\n]+)`")

# Types a snippet may call that are not part of the mod assembly. `Object` is
# Unity's, `World` is the game's terrain class. Anything else must resolve to a
# public mod type, or the gate cannot tell a typo from a deliberate call.
NON_MOD_TYPES = {"Object", "World"}


def public_types() -> dict[str, str]:
    """Every public type in the mod, mapped to the sources that declare it."""
    sources: dict[str, str] = {}
    for path in sorted(MOD.glob("*.cs")):
        src = path.read_text(encoding="utf-8")
        for name in TYPE_DECL.findall(src):
            sources[name] = sources.get(name, "") + src
    return sources


def members(src: str) -> set[str]:
    return set(MEMBER_DECL.findall(src))


def parameters(src: str, member: str) -> set[str] | None:
    """Parameter names of ``member`` in ``src``, or None if it is not a method."""
    for match in re.finditer(rf"\bpublic\b[^;{{}}]*?\b{re.escape(member)}\s*\(", src):
        depth = 0
        i = match.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        args = src[match.end() : i]
        depth = 0
        names: set[str] = set()
        current = ""
        for ch in args:
            if ch in "(<[":
                depth += 1
            elif ch in ")>]":
                depth -= 1
            if ch == "," and depth == 0:
                names.add(current)
                current = ""
            else:
                current += ch
        names.add(current)
        cleaned = set()
        for name in names:
            # Drop the default value: `float timeout = 8f` names `timeout`.
            tokens = re.findall(r"\w+", name.split("=")[0])
            if tokens:
                cleaned.add(tokens[-1])
        return cleaned
    return None


def argument_span(text: str, open_paren: int) -> str:
    depth = 0
    for i in range(open_paren, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : i]
    return ""


def check_snippets(snippets: list[tuple[str, str]], types: dict[str, str]) -> None:
    for origin, snippet in snippets:
        for call in TYPE_REF.finditer(snippet):
            type_name, member = call.group(1), call.group(2)
            assert type_name in types or type_name in NON_MOD_TYPES, (
                f"{origin}: {type_name}.{member} names a type that is neither "
                f"public in Source/PlayTestMod nor listed in NON_MOD_TYPES"
            )
            if type_name not in types:
                continue
            src = types[type_name]
            assert member in members(src), (
                f"{origin}: {type_name}.{member} is documented but not a public "
                f"member of {type_name} in Source/PlayTestMod"
            )
            params = parameters(src, member)
            if params is None:
                continue
            span = argument_span(snippet, call.end() - 1)
            depth = 0
            arg = ""
            args: list[str] = []
            for ch in span:
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                if ch == "," and depth == 0:
                    args.append(arg)
                    arg = ""
                else:
                    arg += ch
            args.append(arg)
            for value in args:
                # String literals carry colons of their own ("spawn_vehicle:x").
                named = NAMED_ARG.search(STRING_LITERAL.sub("", value))
                if not named:
                    continue
                assert named.group(1) in params, (
                    f"{origin}: {type_name}.{member} has no parameter "
                    f"'{named.group(1)}'"
                )


def check_object_initializers(snippets: list[tuple[str, str]], types: dict[str, str]) -> None:
    pattern = re.compile(r"new\s+(\w+)\s*\{(.*?)\}", re.DOTALL)
    for origin, snippet in snippets:
        for type_name, body in pattern.findall(snippet):
            assert type_name in types, (
                f"{origin}: object initializer for {type_name}, which is not a "
                f"public type in Source/PlayTestMod"
            )
            declared = members(types[type_name])
            for field in re.findall(r"(?:^|[{;\n])\s*(\w+)\s*=", body):
                assert field in declared, (
                    f"{origin}: {type_name} has no public field '{field}' set by "
                    f"this initializer"
                )


def non_mod_types_used(readme: str) -> set[str]:
    """The exempted types the README's snippets actually call into."""
    used: set[str] = set()
    for snippet in FENCE.findall(readme) + INLINE_CODE.findall(readme):
        used.update(name for name, _ in TYPE_REF.findall(snippet))
    return used & NON_MOD_TYPES


def main() -> int:
    readme = README.read_text(encoding="utf-8")
    types = public_types()
    assert types, "no public types found in Source/PlayTestMod"

    blocks = [(f"README.md block {i + 1}", block) for i, block in enumerate(FENCE.findall(readme))]
    assert blocks, "README has no csharp snippet to check"
    prose = [
        (f"README.md inline `{span}`", span)
        for span in INLINE_CODE.findall(readme)
        if re.search(r"\b[A-Z]\w*\.\w+\(", span)
    ]

    snippets = blocks + prose
    check_snippets(snippets, types)
    check_object_initializers(blocks, types)

    assert "IScenarioProvider" in types, (
        "the documented provider interface must be public in the mod assembly"
    )
    called = non_mod_types_used(readme)
    assert called == NON_MOD_TYPES, (
        f"NON_MOD_TYPES lists {sorted(NON_MOD_TYPES)} but the README calls "
        f"{sorted(called)}; keep the exemption list to what is still used"
    )
    print(f"OK every documented call resolves ({len(blocks)} snippets, {len(prose)} inline)")
    print("OK documented named arguments exist on the method they are passed to")
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
