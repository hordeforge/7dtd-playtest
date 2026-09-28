"""Shared version-surface checks used by the offline gate and its units."""
from __future__ import annotations

import re
from pathlib import Path

TAG_RE = re.compile(r"v(\d+\.\d+\.\d+)")
SECTION_RE = re.compile(r"^##\s+\[([^\]]+)\]", re.MULTILINE)
REMOVED_HEADING_RE = re.compile(r"^###\s+Removed\b", re.MULTILINE)
BREAKING_MARKER = "**Breaking.**"


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


def undeclared_breaking_sections(changelog: str) -> list[str]:
    """Version entries whose ``### Removed`` section omits the breaking marker.

    The pre-1.0 policy stated at the top of CHANGELOG.md: a minor may remove a
    public symbol, and the entry has to say so. 0.13.0 removed three
    ``Helpers`` methods without the marker, so a consumer reading the notes had
    no way to tell the removal from a dead-code tidy-up.
    """
    starts = [
        (match.start(), match.end(), match.group(1))
        for match in SECTION_RE.finditer(changelog)
    ]
    undeclared: list[str] = []
    for index, (_start, end, heading) in enumerate(starts):
        stop = starts[index + 1][0] if index + 1 < len(starts) else len(changelog)
        body = changelog[end:stop]
        if REMOVED_HEADING_RE.search(body) and BREAKING_MARKER not in body:
            undeclared.append(heading)
    return undeclared
