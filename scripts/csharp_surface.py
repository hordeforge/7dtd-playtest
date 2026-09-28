#!/usr/bin/env python3
"""Structural readers shared by the C# surface gates.

The provider, mining-probe and survivability gates all pin behaviour that
lives inside one method body of a shipped .cs file, and all three need the
same answer to "where does this signature's body start and end". The reader
lives here so a gate imports a parser instead of importing another gate.
"""

from __future__ import annotations

import re


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
