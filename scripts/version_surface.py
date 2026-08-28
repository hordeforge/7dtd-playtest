"""Shared version-surface checks used by the offline gate and its units."""
from __future__ import annotations

import re
from pathlib import Path

TAG_RE = re.compile(r"v(\d+\.\d+\.\d+)")


def _git_dir(root: Path) -> Path | None:
    """Resolve root/.git to a directory, following a worktree pointer file."""
    dot_git = root / ".git"
    if dot_git.is_file():
        text = dot_git.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return None
        target = Path(text.removeprefix("gitdir:").strip())
        return target if target.is_absolute() else root / target
    if dot_git.is_dir():
        return dot_git
    return None


def discover_tag_versions(root: Path) -> list[str]:
    """X.Y.Z versions of the local ``vX.Y.Z`` tags, oldest first.

    Reads refs straight off disk so the gate stays offline and dependency-
    free; returns [] where no git metadata is reachable (tarball download,
    shallow CI checkout that did not fetch tags), which makes the tag-coverage
    check vacuous there rather than wrong.
    """
    git_dir = _git_dir(root)
    if git_dir is None:
        return []
    names: set[str] = set()
    tags_dir = git_dir / "refs" / "tags"
    if tags_dir.is_dir():
        names.update(
            path.relative_to(tags_dir).as_posix()
            for path in tags_dir.rglob("*")
            if path.is_file()
        )
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            fields = line.split(maxsplit=1)  # "<sha> <ref>"; peel lines "^<sha>"
            if len(fields) == 2 and fields[1].startswith("refs/tags/"):
                names.add(fields[1].removeprefix("refs/tags/").strip())
    versions = [match.group(1) for name in names if (match := TAG_RE.fullmatch(name))]
    return sorted(versions)


def uncovered_tag_versions(tag_versions: list[str], headings: list[str]) -> list[str]:
    """Tagged versions without a ``## [<version>]`` changelog entry."""
    known = set(headings)
    return [version for version in tag_versions if version not in known]
