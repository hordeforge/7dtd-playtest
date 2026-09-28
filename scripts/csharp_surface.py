#!/usr/bin/env python3
"""Structural readers shared by the C# surface gates.

The provider, mining-probe and survivability gates all pin behaviour that
lives inside one method body of a shipped .cs file, and all three need the
same answer to "where does this signature's body start and end". The reader
lives here so a gate imports a parser instead of importing another gate.
"""

from __future__ import annotations

import re


def without_comments(src: str) -> str:
    """Blank out every comment, preserving offsets.

    Handles C# (``//``, ``/* */``, ``$@"..."``) and Python (``#``) so one
    reader serves both the C# surface gates and the Python orchestrator gates.
    A hand-rolled scanner rather than ``tokenize``: an apostrophe in a comment
    ("the caller's view") opens a Python string literal and aborts that scan,
    and a ``$@"..."`` verbatim string is not a Python literal at all.

    Blanking rather than deleting keeps ``.index`` and ``splitlines`` results
    pointing at the same place in either text, so a failure names the line the
    live code is on. A gate that substring-matches a body would otherwise be
    satisfied by prose explaining what the body does.

    ``#`` only starts a comment when a space or the line end follows it, so a
    C# preprocessor directive (``#if``, ``#region``) stays visible.
    """
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if src[i : i + 2] == "//":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if src[i : i + 2] == "/*":
            while i < n and src[i : i + 2] != "*/":
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        if ch == "#" and (
            src[i : i + 2] in ("#!", "# ") or i + 1 == n or src[i + 1] == "\n"
        ):
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if src[i : i + 3] == '"""':
            out.append('"""')
            i += 3
            while i < n:
                out.append(src[i : i + 3] if src[i : i + 3] == '"""' else src[i])
                if src[i : i + 3] == '"""':
                    i += 3
                    break
                i += 1
            continue
        if src[i : i + 2] in ('@"', '$@"'):
            out.append(src[i : i + 2])
            i += 2
            while i < n:
                if src[i] == '"':
                    out.append('"')
                    i += 1
                    break
                out.append(src[i])
                i += 1
            continue
        if ch in '"\'':
            out.append(ch)
            i += 1
            while i < n and src[i] != ch:
                out.append(src[i : i + 2] if src[i] == "\\" else src[i])
                if src[i] == "\\":
                    i += 2
                else:
                    i += 1
                if i > n:
                    i = n
            if i < n and src[i] == ch:
                out.append(ch)
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def method_body(src: str, signature_re: str) -> str:
    """The brace-delimited body of the first method matching ``signature_re``."""
    m = re.search(signature_re, src)
    assert m, f"method not found: {signature_re}"
    i = m.end()
    while i < len(src) and src[i] in " \t\r\n":
        i += 1
    assert i < len(src) and src[i] == "{", f"expected '{{' after {signature_re}"
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"unclosed body for {signature_re}")
