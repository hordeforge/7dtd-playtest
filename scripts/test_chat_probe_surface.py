#!/usr/bin/env python3
"""Offline gate: captured chat text never leaves ChatProbe.

ChatProbe captures whatever a remote LAN player typed (EP8 in
docs/THREAT_MODEL.md). A case detail is flushed to the run log, the JSON
result event, the JUnit report and report-*.json, all of which leave the
machine for CI and for the third-party vision-review upload, so the text
itself must never reach one. The cases assert on a token match, which needs
no text; `chat_len=` is the whole diagnostic they get.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "Source" / "PlayTestMod"
CHAT_PROBE = MOD / "ChatProbe.cs"
CATALOG = MOD / "Catalog.cs"
SCENARIOS = ROOT / "SCENARIOS.md"


def main() -> int:
    probe = CHAT_PROBE.read_text(encoding="utf-8")
    catalog = CATALOG.read_text(encoding="utf-8")

    assert "public static string Last" not in probe, (
        "ChatProbe.Last must not be public: it is remote-player text and a "
        "detail string built from it is persisted in every report artifact"
    )
    assert "public static int LastLength" in probe, (
        "ChatProbe must expose the captured length so a case can still say how "
        "much chat arrived without saying what it said"
    )
    assert "string Last = \"\";" in probe, (
        "the captured text must be a private field of ChatProbe"
    )
    # Every reader of the captured text is a consumer inside the probe itself.
    assert not re.search(r"ChatProbe\.Last\b(?!ength)", catalog), (
        "Catalog must not read ChatProbe.Last; use ChatProbe.LastLength"
    )
    # The two chat cases still report what they saw, without its content.
    assert catalog.count('" chat_len=" + ChatProbe.LastLength') == 3, (
        "both chat cases (parachute announce, chat_roundtrip) must keep a "
        "length-only chat diagnostic"
    )
    for case in ("chat_roundtrip", "parachute_fall_announce"):
        assert f"`{case}`" in SCENARIOS.read_text(encoding="utf-8"), (
            f"SCENARIOS.md must still list {case}"
        )

    print("OK captured chat text stays inside ChatProbe")
    print("OK chat cases report a length, never the message")
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
