#!/usr/bin/env python3
"""Windows filesystem rules the client-side file writes depend on.

The host is Linux-only by documentation, but the mod runs on the client, which
is a Windows process (Proton runs it as one). Two Windows-only rules decide
whether a staged frame or a clip directory exists at all, and neither shows up
in a Linux-side review:

* A reserved device name is the device, not a file. `AUX` and `aux` name the
  auxiliary device, so `CreateDirectory` on one fails and the case photographs
  nothing. `COM1`-`COM9` and `LPT1`-`LPT9` are the numbered members; `COM0` is
  not one. The extension cannot carry a device name past the character
  mapping, because `.` is one of the characters it rewrites (`aux.png`
  becomes `aux_png`), so the check runs on the already-mapped name.
* A name that sanitizes to nothing collapses onto its parent directory, so the
  frame lands in the shots root under a name the collector never looks for.

Both rules are judged against the real C# the client runs, not a copy of it, so
the sanitizer and the contract cannot drift. They are judged on
`Helpers.AssetName`, the single name a clip marker, a staged scene and the
frames directory all share: a second sanitizer beside it would be free to
disagree with the marker a collector reads, which is the drift this one
already replaced once. The mod cannot be compiled offline (it references game
assemblies), which is why this gate reads the source.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from csharp_surface import method_body

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "Source" / "PlayTestMod" / "Helpers.Ui.cs"

# Windows device names, as the sanitizer must know them.
RESERVED = ["con", "prn", "aux", "nul"] + [f"com{i}" for i in range(1, 10)] + [
    f"lpt{i}" for i in range(1, 10)
]


def _list_body(src: str, field: str) -> str:
    m = re.search(rf"static readonly string\[\] {field}\s*=\s*\{{(.*?)\}};", src, re.DOTALL)
    assert m, f"{UI.name} has no `static readonly string[] {field}` initializer"
    return m.group(1)


def test_reserved_device_names_are_listed() -> None:
    src = UI.read_text(encoding="utf-8")
    listed = re.findall(r'"([a-z0-9]+)"', _list_body(src, "ReservedDeviceNames"))
    missing = [name for name in RESERVED if name not in listed]
    assert not missing, f"ReservedDeviceNames does not cover the Windows devices: {missing}"
    extra = [name for name in listed if name not in RESERVED]
    assert not extra, (
        f"ReservedDeviceNames names something Windows does not reserve: {extra} "
        "(COM0/LPT0 are ordinary file names)"
    )
    print(f"OK ReservedDeviceNames covers all {len(RESERVED)} Windows device names")


def test_sanitizer_refuses_device_names_and_empty() -> None:
    src = UI.read_text(encoding="utf-8")
    body = method_body(src, r"public\s+static\s+string\s+AssetName\s*\([^)]*\)")
    assert "ToLowerInvariant" in body, (
        "AssetName must compare case-insensitively: Windows device names "
        "match any casing"
    )
    assert "ReservedDeviceNames" in body, "AssetName never consults ReservedDeviceNames"
    assert '"_" +' in body, (
        "AssetName must prefix a device name, otherwise `aux` and `aux.png` "
        "still name the device and CreateDirectory fails"
    )
    assert re.search(r"if \(string\.IsNullOrEmpty\(name\)\)\s*return", body), (
        "AssetName must refuse an empty id before sanitizing: an empty name "
        "collapses onto the parent directory and the collector never finds "
        "the frame"
    )
    assert re.search(r"if \(safe\.Length == 0\)\s*return", body), (
        "AssetName must not return an empty name: it collapses onto the "
        "parent directory and the collector never finds the frame"
    )
    print("OK AssetName refuses empty names and Windows device names")


def test_asset_name_is_lowercase_only() -> None:
    src = UI.read_text(encoding="utf-8")
    body = method_body(src, r"public\s+static\s+string\s+AssetName\s*\([^)]*\)")
    assert re.search(r"c >= 'a' && c <= 'z'", body), (
        "AssetName must not let an uppercase letter survive: the reserved "
        "device names match any casing, and the exact lowercase set below is "
        "the only thing keeping `AUX` (which would become `aux`) off a path"
    )
    print("OK AssetName survives lowercase letters only")


def test_paths_are_built_by_the_path_api() -> None:
    src = UI.read_text(encoding="utf-8")
    lines = src.splitlines()
    hardcoded = [
        (i, line.strip())
        for i, line in enumerate(lines, 1)
        if re.search(r'["\'][^"\']*[/\\][^"\']*["\']', line)
        and ("Path.Combine" in line or "String.Format" in line)
    ]
    assert not hardcoded, (
        "a path built with a hardcoded separator instead of Path.Combine "
        f"segments: {hardcoded}"
    )
    print("OK Helpers.Ui.cs builds every path from Path.Combine segments")


def main() -> int:
    test_reserved_device_names_are_listed()
    test_sanitizer_refuses_device_names_and_empty()
    test_asset_name_is_lowercase_only()
    test_paths_are_built_by_the_path_api()
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
