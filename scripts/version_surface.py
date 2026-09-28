"""Shared version-surface checks used by the offline gate and its units."""
from __future__ import annotations

import re
from pathlib import Path

TAG_RE = re.compile(r"v(\d+\.\d+\.\d+)")
SECTION_RE = re.compile(r"^##\s+\[([^\]]+)\]", re.MULTILINE)
REMOVED_HEADING_RE = re.compile(r"^###\s+Removed\b", re.MULTILINE)
BREAKING_MARKER = "**Breaking.**"

# The uv that wrote uv.lock is named in two places: the floor in
# pyproject.toml ([tool.uv] required-version) and the version every workflow
# installs. A bump that moves one and not the other is quiet in one direction
# (CI keeps running the old uv against a lock it did not write) and fatal in
# the other (a contributor's newer uv is refused), so the pair is checked.
UV_REQUIRED_VERSION_RE = re.compile(r'required-version\s*=\s*">=([0-9]+\.[0-9]+\.[0-9]+)')
UV_VERSION_ENV_RE = re.compile(
    r'^\s*UV_VERSION:\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$', re.MULTILINE
)
SETUP_UV_USE_RE = re.compile(r"uses:\s*astral-sh/setup-uv@")
UV_VERSION_REF = "${{ env.UV_VERSION }}"
# How many lines after a `uses: astral-sh/setup-uv@` line still belong to that
# step: the pinned action line, `with:`, and the version pin under it.
SETUP_UV_STEP_LINES = 6


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
    # Oldest first, by component: sorted() on the strings puts "1.10.0"
    # before "1.9.0", so the tenth minor of a release sorts as older than
    # the ninth. TAG_RE admits only digits and dots, so the key is total.
    return sorted(versions, key=lambda v: tuple(int(part) for part in v.split(".")))


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


def required_uv_floor(pyproject: str) -> str:
    """The uv version [tool.uv] required-version demands, as X.Y.Z."""
    match = UV_REQUIRED_VERSION_RE.search(pyproject)
    assert match, (
        'pyproject.toml has no [tool.uv] required-version = ">=X.Y.Z,<..."; '
        "the workflows' uv pin has no floor to match against"
    )
    return match.group(1)


def uv_pin_problems(workflow: str, floor: str) -> list[str]:
    """Why a workflow's uv pin does not match the floor pyproject declares.

    Empty for a workflow that never sets uv up, and empty when every
    setup-uv step reads the workflow-level ``UV_VERSION`` and that value is
    the floor. Each returned string names one way the pair can drift.
    """
    if SETUP_UV_USE_RE.search(workflow) is None:
        return []

    problems: list[str] = []
    declared = UV_VERSION_ENV_RE.search(workflow)
    if declared is None:
        problems.append("sets uv up but declares no workflow-level UV_VERSION")
    elif declared.group(1) != floor:
        problems.append(
            f"pins UV_VERSION {declared.group(1)}, pyproject requires >= {floor}"
        )

    lines = workflow.splitlines()
    for index, line in enumerate(lines):
        if SETUP_UV_USE_RE.search(line) is None:
            continue
        step = "\n".join(lines[index + 1 : index + 1 + SETUP_UV_STEP_LINES])
        pin = re.search(r"^\s*version:\s*(.+?)\s*$", step, re.MULTILINE)
        if pin is None:
            problems.append("runs setup-uv with no version pin")
        elif pin.group(1) != UV_VERSION_REF:
            problems.append(f"pins setup-uv to {pin.group(1)}, not {UV_VERSION_REF}")
    return problems
