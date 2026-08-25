#!/usr/bin/env python3
"""Units for the version-surface gate's tag discovery and coverage check.

CHANGELOG.md's release model promises that test_version_surface.py fails when
a visible vX.Y.Z tag has no changelog entry. That promise shipped unenforced
once already: the v0.7.2 tag sat on a tree still declaring 0.7.1 with no
[0.7.2] notes, and nothing noticed. These units pin the ref reading (loose,
packed, peel lines, worktree pointers) and the full gate verdict on a
synthetic tree, so the promise cannot quietly rot again.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_version_surface import discover_tag_versions, uncovered_tag_versions

GATE = Path(__file__).resolve().parent / "test_version_surface.py"


def make_git_dir(git_dir: Path, loose: dict[str, str], packed: list[tuple[str, str]]) -> None:
    tags = git_dir / "refs" / "tags"
    tags.mkdir(parents=True)
    for name, sha in loose.items():
        (tags / name).write_text(sha + "\n", encoding="utf-8")
    lines = [f"{sha} refs/tags/{name}" for name, sha in packed]
    lines.append(f"{'a' * 40} refs/heads/main")
    if packed:
        # Annotated-tag peel line: must not be read as a ref name.
        lines.append("^" + "b" * 40)
    (git_dir / "packed-refs").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_root(
    root: Path,
    *,
    version: str,
    headings: list[str],
    git: bool = True,
) -> None:
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(GATE, root / "scripts" / "test_version_surface.py")
    (root / "ModInfo.xml").write_text(
        '<xml>\n  <Version value="' + version + '" />\n</xml>\n', encoding="utf-8"
    )
    api = root / "Source" / "PlayTestMod"
    api.mkdir(parents=True)
    (api / "ModIdentity.cs").write_text(
        f'public class ModIdentity {{ public const string Version = "{version}"; }}\n',
        encoding="utf-8",
    )
    entries = "\n".join(f"## [{h}]\n\n- note\n" for h in headings)
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- note\n\n" + entries, encoding="utf-8"
    )
    if git:
        make_git_dir(root / ".git", {"v9.9.9": "c" * 40}, [("v0.8.0", "d" * 40)])


def run_gate(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "test_version_surface.py")],
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="version-surface-units-") as td:
        base = Path(td)

        loose = base / "loose"
        make_git_dir(loose / ".git", {"v1.2.3": "a" * 40, "not-semver": "e" * 40}, [])
        assert discover_tag_versions(loose) == ["1.2.3"], discover_tag_versions(loose)
        print("OK loose refs are discovered and non vX.Y.Z names ignored")

        mixed = base / "mixed"
        make_git_dir(mixed / ".git", {"v1.2.3": "a" * 40}, [("v0.8.0", "d" * 40)])
        assert discover_tag_versions(mixed) == ["0.8.0", "1.2.3"], discover_tag_versions(mixed)
        print("OK packed refs merge with loose refs, sorted oldest first")

        pointer = base / "pointer"
        target = base / "elsewhere" / ".git"
        make_git_dir(target, {"v2.0.0": "f" * 40}, [])
        pointer.mkdir()
        pointer.joinpath(".git").write_text(
            f"gitdir: {target}\n", encoding="utf-8"
        )
        assert discover_tag_versions(pointer) == ["2.0.0"], discover_tag_versions(pointer)
        print("OK a .git worktree pointer file resolves to the real git dir")

        bare = base / "bare"
        bare.mkdir()
        assert discover_tag_versions(bare) == []
        print("OK no git metadata means the tag check is vacuous")

        assert uncovered_tag_versions(["0.7.1", "0.7.2"], ["Unreleased", "0.7.1"]) == ["0.7.2"]
        assert uncovered_tag_versions([], ["Unreleased"]) == []
        assert uncovered_tag_versions(["0.8.0"], ["Unreleased", "0.8.0"]) == []
        print("OK coverage diff reports only tagged versions without notes")

        missing = base / "missing-notes"
        make_root(missing, version="0.8.0", headings=["0.8.0"])
        proc = run_gate(missing)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "9.9.9" in proc.stderr, proc.stderr
        print("OK the gate fails when a visible tag has no changelog entry")

        covered = base / "covered"
        make_root(covered, version="0.8.0", headings=["0.8.0", "9.9.9"])
        proc = run_gate(covered)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "all 2 vX.Y.Z tags have changelog entries" in proc.stdout, proc.stdout
        print("OK the gate passes once every tagged version has notes")

    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
