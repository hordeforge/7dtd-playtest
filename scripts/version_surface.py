"""Shared version-surface checks used by the offline gate and its units."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

TAG_RE = re.compile(r"v(\d+\.\d+\.\d+)")
SECTION_RE = re.compile(r"^##\s+\[([^\]]+)\]", re.MULTILINE)
REMOVED_HEADING_RE = re.compile(r"^###\s+Removed\b", re.MULTILINE)
IMPACT_HEADING_RE = re.compile(r"^###\s+([A-Z][A-Za-z ]*?)\s*$", re.MULTILINE)
# A markdown table's delimiter row: pipes, dashes, colons.
TABLE_ROW_RE = re.compile(r"^\|[\s:|-]+\|\s*$", re.MULTILINE)
BREAKING_MARKER = "**Breaking.**"

# The uv that wrote uv.lock is named in two places: the floor in
# pyproject.toml ([tool.uv] required-version) and the version every workflow
# installs. A bump that moves one and not the other is quiet in one direction
# (CI keeps running the old uv against a lock it did not write) and fatal in
# the other (a contributor's newer uv is refused), so the pair is checked.
UV_REQUIRED_VERSION_RE = re.compile(r'required-version\s*=\s*">=([0-9]+\.[0-9]+\.[0-9]+)')
UV_VERSION_ENV_RE = re.compile(r'^\s*UV_VERSION:\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$', re.MULTILINE)
SETUP_UV_USE_RE = re.compile(r"uses:\s*astral-sh/setup-uv@")
UV_VERSION_REF = "${{ env.UV_VERSION }}"
# How many lines after a `uses: astral-sh/setup-uv@` line still belong to that
# step: `with:`, the version pin, and the slack a reindented step needs. The
# window has to stop before the next step's `uses:` or its lines are read as
# this step's pin.
SETUP_UV_STEP_LINES = 6


def _git_dir(root: Path) -> Path | None:
    """Resolve root/.git to the directory holding refs and packed-refs.

    A linked worktree's pointer file names the per-worktree dir, whose
    refs/ is empty and which has no packed-refs; the shared refs live in the
    common dir that ``commondir`` points at, so follow it when present.
    Without that hop the tag scan returns [] in every worktree and the
    tag-coverage check passes vacuously.
    """
    dot_git = root / ".git"
    if dot_git.is_file():
        text = dot_git.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return None
        target = Path(text.removeprefix("gitdir:").strip())
        git_dir = target if target.is_absolute() else root / target
    elif dot_git.is_dir():
        git_dir = dot_git
    else:
        return None
    commondir = git_dir / "commondir"
    if not commondir.is_file():
        return git_dir
    common = Path(commondir.read_text(encoding="utf-8").strip())
    return common if common.is_absolute() else (git_dir / common).resolve()


def _common_git_dir(git_dir: Path) -> Path:
    """The shared object store a worktree's private git dir points at.

    A linked worktree's ``.git`` pointer names ``<common>/worktrees/<name>``,
    which holds no ``refs/tags`` and no ``packed-refs``; the tags live in the
    common dir its ``commondir`` file names. Without this the tag-coverage
    check reports no tags in every worktree and passes vacuously.
    """
    commondir = git_dir / "commondir"
    if not commondir.is_file():
        return git_dir
    text = commondir.read_text(encoding="utf-8").strip()
    if not text:
        return git_dir
    target = Path(text)
    return target if target.is_absolute() else (git_dir / target).resolve()


def _tag_object_types(root: Path) -> dict[str, str] | None:
    """Tag name -> the object its ref names, read from git itself.

    ``%(objecttype)`` is the only honest source for this: an annotated tag's
    ref resolves to a tag object, and a loose ref file holding a raw sha
    says nothing about which kind of object that sha names, because a ref
    written to name a tag object directly looks the same as one naming a
    commit. None when git cannot be run here (tarball download, a synthetic
    tree in the units), where the caller falls back to reading the refs.
    """
    git_dir = _git_dir(root)
    if git_dir is None:
        return None
    try:
        done = subprocess.run(
            [
                "git",
                "--git-dir",
                str(git_dir.resolve()),
                "for-each-ref",
                "--format=%(refname:short) %(objecttype)",
                "refs/tags",
            ],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if done.returncode != 0:
        return None
    types: dict[str, str] = {}
    for line in done.stdout.splitlines():
        name, _, object_type = line.strip().partition(" ")
        if name:
            types[name] = object_type
    return types or None


def _tag_refs_from_disk(root: Path) -> dict[str, bool]:
    """Local tag name -> whether it is a lightweight ref, read off disk.

    An annotated tag's loose ref is a ``ref: refs/tags/...`` indirection to
    the tag object, and its packed line is followed by the ``^<sha>`` peel
    of the commit it names. A lightweight ref has neither. A loose ref
    holding a raw sha is a lightweight ref only when the sha turns out to
    name a commit, which the object database has to answer; where git cannot
    be asked, the ref is read as lightweight so the caller checks the
    changelog for it rather than passing it silently.
    """
    private = _git_dir(root)
    if private is None:
        return {}
    git_dir = _common_git_dir(private)
    lightweight: dict[str, bool] = {}
    tags_dir = git_dir / "refs" / "tags"
    if tags_dir.is_dir():
        for path in tags_dir.rglob("*"):
            if not path.is_file():
                continue
            name = path.relative_to(tags_dir).as_posix()
            value = path.read_text(encoding="utf-8").strip()
            lightweight[name] = not value.startswith("ref:")
    packed = git_dir / "packed-refs"
    if packed.is_file():
        lines = packed.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if line.startswith("^"):
                continue
            fields = line.split(maxsplit=1)  # "<sha> <ref>"
            if len(fields) != 2 or not fields[1].startswith("refs/tags/"):
                continue
            name = fields[1].removeprefix("refs/tags/").strip()
            following = lines[index + 1] if index + 1 < len(lines) else ""
            lightweight[name] = not following.startswith("^")
    return lightweight


def _tag_refs(root: Path) -> dict[str, bool]:
    """Local tag name -> whether it is a lightweight ref, {} without git."""
    types = _tag_object_types(root)
    if types is None:
        return _tag_refs_from_disk(root)
    return {name: object_type != "tag" for name, object_type in types.items()}


def _ordered_versions(names: set[str]) -> list[str]:
    versions = [match.group(1) for name in names if (match := TAG_RE.fullmatch(name))]
    # Oldest first, by component: sorted() on the strings puts "1.10.0"
    # before "1.9.0", so the tenth minor of a release sorts as older than
    # the ninth. TAG_RE admits only digits and dots, so the key is total.
    return sorted(versions, key=lambda v: tuple(int(part) for part in v.split(".")))


def discover_tag_versions(root: Path) -> list[str]:
    """X.Y.Z versions of the local ``vX.Y.Z`` tags, oldest first.

    Returns [] where no git metadata is reachable.
    """
    return _ordered_versions(set(_tag_refs(root)))


def discover_lightweight_tag_versions(root: Path) -> list[str]:
    """X.Y.Z versions whose local ``vX.Y.Z`` tag is a lightweight ref."""
    return _ordered_versions({name for name, light in _tag_refs(root).items() if light})


def uncovered_tag_versions(tag_versions: list[str], headings: list[str]) -> list[str]:
    """Tagged versions without a ``## [<version>]`` changelog entry."""
    known = set(headings)
    return [version for version in tag_versions if version not in known]


LINK_DEF_RE = re.compile(r"^\[([^\]]+)\]:\s*(\S+)\s*$", re.MULTILINE)
COMPARE_BASE_RE = re.compile(r"/compare/v(\d+\.\d+\.\d+)\.\.\.")


def unlinked_release_headings(changelog: str) -> list[str]:
    """Released ``## [x]`` entries with no ``[x]:`` link definition.

    A bracket heading is a reference-style link: without the definition
    GitHub renders the literal text ``[0.13.0]`` instead of a link, so the
    newest releases read as unlinked while the oldest, defined back when
    they were cut, still work. A reader following the changelog to find out
    what changed in the release they just installed finds nothing.
    """
    defined = {match.group(1) for match in LINK_DEF_RE.finditer(changelog)}
    return [
        name
        for name in re.findall(r"^##\s+\[([^\]]+)\]", changelog, flags=re.MULTILINE)
        if name != "Unreleased" and name not in defined
    ]


def unreleased_compare_base(changelog: str) -> str | None:
    """The tag the ``[Unreleased]`` compare link starts from, if it has one.

    None when the link is absent, which is a different defect from a stale
    one: the reader gets no "what changed since" range at all.
    """
    for match in LINK_DEF_RE.finditer(changelog):
        if match.group(1) == "Unreleased":
            found = COMPARE_BASE_RE.search(match.group(2))
            return found.group(1) if found else None
    return None


def undocumented_lightweight_tags(changelog: str, lightweight: list[str]) -> list[str]:
    """Lightweight tags the changelog does not name as ``vX.Y.Z``.

    The release model promises annotated tags. A lightweight ref carries no
    tagger or date, so the notes have to say which ones are strays and
    whether the ref is a distinct release; otherwise the promise reads as
    true of the whole history. The link definitions are stripped first: a
    ``[0.7.2]: .../tag/v0.7.2`` URL says the tag exists, not that the notes
    mention what kind of ref it is.
    """
    prose = "\n".join(
        line for line in changelog.splitlines() if LINK_DEF_RE.fullmatch(line) is None
    )
    return [version for version in lightweight if f"v{version}" not in prose]


def _release_spans(changelog: str) -> list[tuple[int, int, str]]:
    """(body start, body end, release name) for each ``## [name]`` entry."""
    starts = [
        (match.start(), match.end(), match.group(1)) for match in SECTION_RE.finditer(changelog)
    ]
    spans: list[tuple[int, int, str]] = []
    for index, (_start, end, name) in enumerate(starts):
        stop = starts[index + 1][0] if index + 1 < len(starts) else len(changelog)
        spans.append((end, stop, name))
    return spans


def undeclared_breaking_sections(changelog: str) -> list[str]:
    """Version entries that remove something without the breaking marker.

    The pre-1.0 policy stated at the top of CHANGELOG.md: a minor may remove a
    public symbol, and the entry has to say so. 0.13.0 removed three
    ``Helpers`` methods without the marker, so a consumer reading the notes had
    no way to tell the removal from a dead-code tidy-up.

    The marker counts anywhere in the version entry, not only under the
    ``### Removed`` heading: the entry is the unit a reader reads.
    """
    return [
        name
        for start, stop, name in _release_spans(changelog)
        if REMOVED_HEADING_RE.search(changelog[start:stop])
        and BREAKING_MARKER not in changelog[start:stop]
    ]


def _removed_sections(changelog: str) -> list[tuple[str, str]]:
    """(release name, ``### Removed`` body) for every release that has one."""
    found: list[tuple[str, str]] = []
    for start, stop, name in _release_spans(changelog):
        body = changelog[start:stop]
        for match in REMOVED_HEADING_RE.finditer(body):
            following = re.search(r"^###\s", body[match.end() :], flags=re.MULTILINE)
            section_end = match.end() + following.start() if following else len(body)
            found.append((name, body[match.end() : section_end]))
    return found


def unnamed_replacement_rows(changelog: str) -> list[str]:
    """Releases whose ``### Removed`` section names no replacement.

    The release model promises a table naming the replacement for each
    removed symbol. A declared break without that table leaves a provider
    author reading "this symbol is gone" and nothing else, which is the same
    gap 0.13.0 had: the prose said the symbols were dead code and named
    nothing.
    """
    return [
        name
        for name, section in _removed_sections(changelog)
        if BREAKING_MARKER in section and not TABLE_ROW_RE.search(section)
    ]


def duplicate_impact_headings(changelog: str) -> list[str]:
    """Impact headings a release repeats, as ``<release> (Added)``.

    Keep a Changelog gives a release one section per impact class. A second
    ``### Changed`` reads as a second group of consumer-facing notes and
    renders as a repeated heading, and a ``### Removed`` filed under the
    second one is easy to skim past, which is how the ``ChatProbe.Last``
    removal shipped under ``### Fixed`` with no ``**Breaking.**`` marker.
    """
    duplicates: list[str] = []
    for start, stop, name in _release_spans(changelog):
        body = changelog[start:stop]
        seen: set[str] = set()
        for match in IMPACT_HEADING_RE.finditer(body):
            heading = match.group(1)
            if heading in seen:
                duplicates.append(f"{name} ({heading})")
            seen.add(heading)
    return duplicates


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
        problems.append(f"pins UV_VERSION {declared.group(1)}, pyproject requires >= {floor}")

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
