"""Vision-model review of a staged clip, via the deadeye gateway.

A staged clip (turntable, walk-cycle, timed VFX) takes real time for a person
to watch fully, and iteration compounds it. A vision-capable model can
prescreen a clip against explicit context and name concrete moments worth a
person's attention. This module submits an already-captured clip directory
plus its recorded intent to the deadeye gateway (the shared vision-model
review component in hordeforge/7dtd-vision-review) and returns the structured,
advisory result, with the same consent and credential boundaries the gateway
enforces.

A verdict here is evidence, never acceptance: the human-watch gate README
requires is untouched, and nothing in this module can mark a clip accepted.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

INTENT_SCHEMA_VERSION = 1

DEFAULT_PROVIDER = "gemini"
DEFAULT_TIMEOUT_SECONDS = 120.0

GATEWAY = "deadeye"
GATEWAY_INSTALL_HINT = (
    "install the deadeye gateway from hordeforge/7dtd-vision-review and put it "
    "on PATH, e.g. with: uv tool install --from git+https://github.com/hordeforge/7dtd-vision-review"
)

MAX_PRINTED_CHARS = 500

# Code points that continue the grapheme cluster before them, so cutting
# between one and its follower prints half a glyph: a bare accent mark, the
# right half of an emoji ZWJ sequence, a variation selector. U+200D is the
# joiner itself. A regional indicator (one half of a flag) needs its partner
# too, but by count rather than by class, so it is checked at the cut.
_COMBINING_CATEGORIES = frozenset({"Mn", "Me", "Mc"})
_ZWJ = "‍"
_VARIATION_SELECTOR_CODEPOINTS = frozenset(range(0xFE00, 0xFE10)) | frozenset(
    range(0xE0100, 0xE01F0)
)
_REGIONAL_INDICATOR_FIRST = 0x1F1E6
_REGIONAL_INDICATOR_LAST = 0x1F1FF


def _is_cluster_tail(character: str) -> bool:
    """Whether ``character`` can only appear joined to the code point before it."""
    return (
        character == _ZWJ
        or ord(character) in _VARIATION_SELECTOR_CODEPOINTS
        or unicodedata.category(character) in _COMBINING_CATEGORIES
    )


def _truncate_at_cluster(text: str, limit: int) -> str:
    """The first ``limit`` code points of ``text``, minus a half cluster.

    A model verdict is prose, and prose is where the emoji, the accents and
    the flags live; a cut straight at the limit can land inside one and leave
    a lone combining mark or an orphan ZWJ on the operator's terminal. Walk
    back over a trailing run of cluster tails, and off a flag left holding
    one regional indicator instead of two.

    This is not UAX #29 segmentation (nothing in the standard library is):
    it covers the clusters that actually occur in a review sentence, and the
    worst a cluster it does not model can do is lose its last code point,
    which the ellipsis marks as cut text either way.
    """
    head = text[:limit]
    while head and _is_cluster_tail(head[-1]):
        head = head[:-1]
    flags = sum(
        1
        for character in head
        if _REGIONAL_INDICATOR_FIRST <= ord(character) <= _REGIONAL_INDICATOR_LAST
    )
    if flags % 2:
        head = head[:-1]
    return head

# Intent caps. The intent is the one author-supplied text this repository
# hands the gateway, and the gateway puts it in the review prompt verbatim, so
# an unbounded field is an unbounded prompt: cost, prompt-flooding, and an
# author able to bury the actual question under a pasted log. The useful
# intent is a sentence or two per field, so these sit far above any real one.
MAX_INTENT_FILE_BYTES = 64 * 1024
MAX_INTENT_FIELD_CHARS = 4000
MAX_INTENT_ITEM_CHARS = 1000
MAX_INTENT_LIST_ITEMS = 50
MAX_INTENT_TOTAL_CHARS = 16000

_PROVIDER_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

RESULT_KEYS = (
    "summary",
    "strengths",
    "issues",
    "recommended_changes",
    "rubric_scores",
    "confidence",
    "limitations",
)


class ReviewError(Exception):
    """A refusal or fault carrying one message the caller can act on."""


# -- intent -------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewIntent:
    """The recorded intended use a reviewer needs besides the footage."""

    purpose: str
    subject: str
    camera_path: str
    desired_qualities: str
    avoid: tuple[str, ...]
    questions: tuple[str, ...]
    suite: str
    case: str


def _string_field(data: dict[str, object], key: str, origin: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReviewError(f"{origin}: field {key!r} must be a string, got {type(value).__name__}")
    return _bounded(value.strip(), key, origin, MAX_INTENT_FIELD_CHARS)


def _string_list(data: dict[str, object], key: str, origin: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ReviewError(f"{origin}: field {key!r} must be a list of strings")
    if len(value) > MAX_INTENT_LIST_ITEMS:
        raise ReviewError(
            f"{origin}: field {key!r} carries {len(value)} items, over the "
            f"{MAX_INTENT_LIST_ITEMS} this review accepts; a list that long is a "
            "pasted log, not a review concern"
        )
    return tuple(
        _bounded(item.strip(), key, origin, MAX_INTENT_ITEM_CHARS)
        for item in value
        if item.strip()
    )


def _bounded(text: str, key: str, origin: str, limit: int) -> str:
    if len(text) > limit:
        raise ReviewError(
            f"{origin}: field {key!r} is {len(text)} characters, over the {limit} "
            "this review accepts; everything here goes into the provider prompt "
            "verbatim, so an unbounded field is an unbounded request"
        )
    return text


def parse_intent(data: object, origin: str) -> ReviewIntent:
    """Validate one intent document, refusing with every missing requirement."""
    if not isinstance(data, dict):
        raise ReviewError(f"{origin}: the intent must be a JSON object")
    allowed = {
        "schema_version",
        "purpose",
        "subject",
        "camera_path",
        "desired_qualities",
        "avoid",
        "questions",
        "suite",
        "case",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ReviewError(
            f"{origin}: unknown intent field(s) {', '.join(unknown)}; expected: "
            + ", ".join(sorted(allowed))
        )
    version = data.get("schema_version", INTENT_SCHEMA_VERSION)
    if version != INTENT_SCHEMA_VERSION:
        raise ReviewError(
            f"{origin}: intent schema_version {version!r} is not supported by this "
            f"tool (it speaks version {INTENT_SCHEMA_VERSION})"
        )
    if "purpose" not in data:
        raise ReviewError(f"{origin}: intent is missing required field 'purpose'")
    intent = ReviewIntent(
        purpose=_string_field(data, "purpose", origin),
        subject=_string_field(data, "subject", origin),
        camera_path=_string_field(data, "camera_path", origin),
        desired_qualities=_string_field(data, "desired_qualities", origin),
        avoid=_string_list(data, "avoid", origin),
        questions=_string_list(data, "questions", origin),
        suite=_string_field(data, "suite", origin),
        case=_string_field(data, "case", origin),
    )
    if not intent.purpose:
        raise ReviewError(
            f"{origin}: 'purpose' must not be empty; context is never inferred from a filename"
        )
    total = sum(
        len(part)
        for part in (
            intent.purpose,
            intent.subject,
            intent.camera_path,
            intent.desired_qualities,
            intent.suite,
            intent.case,
            *intent.avoid,
            *intent.questions,
        )
    )
    if total > MAX_INTENT_TOTAL_CHARS:
        raise ReviewError(
            f"{origin}: the intent is {total} characters in total, over the "
            f"{MAX_INTENT_TOTAL_CHARS} this review accepts"
        )
    return intent


def load_intent_file(path: Path) -> tuple[ReviewIntent, bytes]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReviewError(f"cannot read intent file {path}: {exc}") from exc
    if size > MAX_INTENT_FILE_BYTES:
        raise ReviewError(
            f"intent file {path} is {size} bytes, over the {MAX_INTENT_FILE_BYTES} "
            "this review accepts"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReviewError(f"cannot read intent file {path}: {exc}") from exc
    return parse_intent(_decode_json(raw, f"intent file {path}"), f"intent file {path}"), raw


def parse_intent_text(text: str) -> tuple[ReviewIntent, bytes]:
    raw = text.encode("utf-8")
    if len(raw) > MAX_INTENT_FILE_BYTES:
        raise ReviewError(
            f"--intent-text is {len(raw)} bytes, over the {MAX_INTENT_FILE_BYTES} "
            "this review accepts"
        )
    return parse_intent(_decode_json(raw, "--intent-text"), "--intent-text"), raw


def _decode_json(raw: bytes, origin: str) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"{origin} is not valid JSON: {exc}") from exc


# -- result -------------------------------------------------------------------


def validate_result(
    data: dict[str, object], origin: str = "gateway response"
) -> dict[str, object]:
    """Normalize a review into the shared result shape (audio-review family).

    The canonical validator lives in the deadeye gateway; this is the offline
    backstop a caller runs on what the gateway returned. An issue may name its
    moment as `at_seconds` and/or `at_frame`. Every deviation is a hard
    failure naming what was wrong.
    """
    problems: list[str] = []
    missing = [key for key in RESULT_KEYS if key not in data]
    if missing:
        problems.append(f"missing key(s): {', '.join(missing)}")
    extra = sorted(set(data) - set(RESULT_KEYS))
    if extra:
        problems.append(f"unexpected key(s): {', '.join(extra)}")
    if problems:
        raise ReviewError(f"{origin} returned an invalid structure: {'; '.join(problems)}")

    def strings(key: str) -> list[str]:
        value = data[key]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            problems.append(f"{key} must be an array of strings")
            return []
        return [item for item in value if item.strip()]

    summary = data["summary"]
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary must be a non-empty string")

    issues: list[dict[str, object]] = []
    raw_issues = data["issues"]
    if not isinstance(raw_issues, list):
        problems.append("issues must be an array")
    else:
        for index, entry in enumerate(raw_issues):
            if not isinstance(entry, dict) or "description" not in entry:
                problems.append(f"issue #{index + 1} must be an object with 'description'")
                continue
            # Live models name a moment with the singular aliases `frame` /
            # `seconds` as often as `at_frame` / `at_seconds`; normalize them
            # before the shape check (canonical wins when both are present).
            if "frame" in entry:
                entry.setdefault("at_frame", entry.pop("frame"))
            if "seconds" in entry:
                entry.setdefault("at_seconds", entry.pop("seconds"))
            # Start/end pairs: {"start_frame": 9, "end_frame": 11} is the
            # same moment as {"at_frame": [9, 11]}.
            start, end = entry.pop("start_frame", None), entry.pop("end_frame", None)
            if "at_frame" not in entry and start is not None and end is not None:
                entry["at_frame"] = [start, end]
            elif (start is None) != (end is None):
                # Both are popped unconditionally, so a half pair used to
                # lose the moment with nothing to report: the key is gone
                # before the unexpected-key check can name it.
                problems.append(
                    f"issue #{index + 1} names only one of start_frame/end_frame"
                )
                continue
            start, end = entry.pop("start_seconds", None), entry.pop("end_seconds", None)
            if "at_seconds" not in entry and start is not None and end is not None:
                entry["at_seconds"] = [start, end]
            elif (start is None) != (end is None):
                problems.append(
                    f"issue #{index + 1} names only one of start_seconds/end_seconds"
                )
                continue
            unexpected = sorted(set(entry) - {"description", "at_seconds", "at_frame"})
            if unexpected:
                problems.append(
                    f"issue #{index + 1} has unexpected key(s): {', '.join(unexpected)}"
                )
                continue
            description = entry["description"]
            if not isinstance(description, str) or not description.strip():
                problems.append(f"issue #{index + 1} needs a non-empty description")
                continue
            issue: dict[str, object] = {"description": description.strip()}
            seconds = _moment(entry.get("at_seconds"), non_negative=False)
            if "at_seconds" in entry and entry["at_seconds"] is not None and seconds is None:
                problems.append(
                    f"issue #{index + 1} at_seconds must be [start, end] numbers "
                    "with start <= end, or a single second"
                )
                continue
            if seconds is not None:
                issue["at_seconds"] = seconds
            frame = _moment(entry.get("at_frame"), non_negative=True)
            if "at_frame" in entry and entry["at_frame"] is not None and frame is None:
                problems.append(
                    f"issue #{index + 1} at_frame must be [start, end] non-negative "
                    "numbers with start <= end, or a single frame index"
                )
                continue
            if frame is not None:
                issue["at_frame"] = frame
            issues.append(issue)

    scores: dict[str, float | None] = {}
    raw_scores = data["rubric_scores"]
    if not isinstance(raw_scores, dict):
        problems.append("rubric_scores must be an object keyed by rubric dimension")
    else:
        for key, value in raw_scores.items():
            if value is None:
                scores[key] = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"rubric_scores[{key!r}] must be a number or null")
            elif not 0 <= value <= 5:
                problems.append(f"rubric_scores[{key!r}] must be within 0-5")
            else:
                scores[key] = float(value)

    confidence = data["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        problems.append("confidence must be a number between 0 and 1")

    # Read the string-list keys while `problems` can still be looked at. They
    # were normalized inside the return dict, so a wrong type appended here
    # after the last read and was discarded, and the review was stamped
    # review_validated with three fields silently coerced to [].
    strengths = strings("strengths")
    recommended_changes = strings("recommended_changes")
    limitations = strings("limitations")

    if problems:
        raise ReviewError(
            f"{origin} returned an invalid structure (schema mismatch): " + "; ".join(problems)
        )
    assert isinstance(summary, str)  # checked above; narrows for the return
    assert isinstance(confidence, (int, float))  # checked above; narrows for the return
    return {
        "summary": summary.strip(),
        "strengths": strengths,
        "issues": issues,
        "recommended_changes": recommended_changes,
        "rubric_scores": scores,
        "confidence": round(float(confidence), 4),
        "limitations": limitations,
    }


def _moment_number(value: object) -> float | None:
    """Finite non-boolean number, else ``None``.

    JSON integers are unbounded (``float()`` overflows past an isinstance
    check) and Python's parser accepts Infinity/NaN tokens; neither names a
    frame or a second, so both fail closed here.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _moment(value: object, *, non_negative: bool) -> list[float] | None:
    """Normalize an issue moment: `[start, end]` or a single value -> `[n, n]`.

    Mirrors the deadeye gateway's canonical validator: models point at a
    moment with either shape, and a single frame index or second is the
    natural way to name one frame. Values that are not finite numbers are
    refused, never stored into evidence.
    """
    if isinstance(value, list):
        if len(value) != 2:
            return None
        start = _moment_number(value[0])
        end = _moment_number(value[1])
        if start is None or end is None or start > end or (non_negative and start < 0):
            return None
        return [start, end]
    number = _moment_number(value)
    if number is None or (non_negative and number < 0):
        return None
    return [number, number]


# -- rendering ----------------------------------------------------------------


def terminal_safe(text: str) -> str:
    """Model-authored text flattened for a terminal.

    The summary and issue descriptions a review returns are model output, not
    text this tool wrote: C0/C1 control characters (ESC sequences that repaint
    or hide the lines around them) and embedded newlines (a forged
    `PASS suite/case` line in the same stream as the playtest log) are
    flattened to single spaces, and an overlong answer is truncated, so a
    verbose or hostile model cannot own the reader's screen.
    """
    flattened = "".join(
        " " if character < " " or character == "\x7f" or "\x80" <= character <= "\x9f"
        else character
        for character in text
    )
    collapsed = " ".join(flattened.split())
    if len(collapsed) > MAX_PRINTED_CHARS:
        return _truncate_at_cluster(collapsed, MAX_PRINTED_CHARS) + "..."
    return collapsed


# -- the deadeye boundary -----------------------------------------------------


Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def deadeye_available() -> bool:
    """Whether the gateway CLI is on PATH. Presence only, never a network call."""
    return shutil.which(GATEWAY) is not None


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            # A verdict is prose: em dashes, accents, emoji. Decoding it with
            # the locale's encoding raises UnicodeDecodeError on the first
            # non-ASCII byte under a C locale, which is a lost review, not a
            # malformed one.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReviewError(
            f"the {GATEWAY} gateway did not answer within {timeout:g}s; no verdict was produced"
        ) from exc
    except OSError as exc:
        raise ReviewError(f"could not run the {GATEWAY} gateway: {exc}") from exc


def run_review(
    clip: Path,
    *,
    provider: str = DEFAULT_PROVIDER,
    intent_path: Path | None = None,
    intent_text: str | None = None,
    model: str | None = None,
    allow_network: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    keep_raw_response: bool = False,
    output: Path | None = None,
    force: bool = False,
    notify: Callable[[str], None] | None = None,
    runner: Runner | None = None,
) -> dict[str, object]:
    """Submit the clip plus recorded intent via deadeye, return the envelope.

    Order matters: consent gate, local intent validation, clip existence,
    gateway availability, disclosure, submission, structural validation. A
    failure at any step raises one message the caller can act on and preserves no
    partial verdict as a completed review.
    """
    if not allow_network:
        raise ReviewError(
            "review_video sends the whole clip directory to a third-party vision "
            "model, including the run log and the client log copied beside the "
            "frames; pass --allow-network to consent to that upload"
        )
    if intent_path is not None and intent_text is not None:
        raise ReviewError("takes exactly one of --intent PATH or --intent-text JSON, never both")
    provider = provider_token(provider)
    if intent_path is not None:
        intent, _ = load_intent_file(Path(intent_path))
    elif intent_text is not None:
        intent, _ = parse_intent_text(intent_text)
    else:
        raise ReviewError(
            "needs exactly one of --intent PATH (the reproducible route) or --intent-text JSON"
        )

    if not clip.is_dir():
        raise ReviewError(f"no such clip directory: {clip}")
    if not deadeye_available():
        raise ReviewError(
            f"the {GATEWAY} gateway CLI is not on PATH. {GATEWAY_INSTALL_HINT}"
        )

    if notify is not None:
        notify(f"gateway: {GATEWAY} (provider {provider})")
        notify(f"model: {model or 'default per provider'}")
        # The whole clip directory is submitted, not only the frames: the
        # capture scripts leave the run log and a copy of the client log
        # beside the mp4, and the client log carries whatever the game and
        # any remote LAN player put there. Say what actually leaves, not
        # just "the media".
        notify(
            f"reviewing {clip} against {provider}; this whole directory (frames, "
            "mp4, run log, client log) is uploaded to the provider and retention "
            "is governed by that provider's terms"
        )

    argv: list[str] = [GATEWAY, "review", str(clip), "--provider", provider]
    if intent_path is not None:
        argv += ["--intent", str(intent_path)]
    else:
        argv += ["--intent-text", intent_text or ""]
    if model:
        argv += ["--model", model]
    argv += ["--allow-network", "--json", "--timeout", f"{timeout_seconds:g}"]
    if keep_raw_response:
        argv += ["--keep-raw-response"]
    if output is not None:
        argv += ["--output", str(output)]
    if force:
        argv += ["--force"]

    execute = runner or _default_runner
    # The gateway writes --output itself, so an envelope reaches disk before
    # this tool has looked at it. An evidence path that did not exist before
    # this call is this call's to keep or remove; one that did is an earlier
    # review, which a refusal must never delete.
    output_existed = output is not None and output.exists()
    try:
        result = execute(argv, timeout_seconds)
        if result.returncode != 0:
            # Both streams and their tail: a gateway whose stderr is a
            # progress trace and whose reason (a provider 401, a prompt-size
            # refusal) is on stdout would otherwise report the trace's last
            # line as the cause.
            message = " | ".join(
                part
                for part in (
                    terminal_safe(result.stderr or "").strip(),
                    terminal_safe(result.stdout or "").strip(),
                )
                if part
            )
            raise ReviewError(
                f"the {GATEWAY} gateway refused the review (exit {result.returncode})"
                + (f": {message}" if message else "")
            )
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReviewError(
                f"the {GATEWAY} gateway returned a non-JSON envelope: {exc}"
            ) from exc
        if not isinstance(envelope, dict) or envelope.get("kind") != "deadeye-review":
            raise ReviewError(
                f"the {GATEWAY} gateway returned an unexpected envelope; is the installed "
                "version the hordeforge gateway?"
            )
        if not isinstance(envelope.get("result"), dict):
            raise ReviewError("the gateway returned no validated result")
        # Keep the normalized copy, not the provider's: the result a caller
        # reads and the result stored below are the one that passed.
        envelope["result"] = validate_result(envelope["result"])
    except ReviewError:
        _discard_unvalidated(output, output_existed)
        raise
    envelope["review_validated"] = True
    envelope["intent_summary"] = {
        "purpose": intent.purpose,
        "suite": intent.suite,
        "case": intent.case,
    }
    if output is not None:
        # Rewrite what the gateway wrote: the evidence on disk is the envelope
        # this tool validated, so a later reader cannot mistake an unchecked
        # provider response for a review. Replaced atomically, so a failed
        # write cannot truncate an earlier review at the same path.
        _write_evidence(output, envelope)
    return envelope


def _write_evidence(output: Path, envelope: dict[str, object]) -> None:
    staging = output.with_name(f".{output.name}.partial")
    try:
        staging.write_text(
            json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        staging.replace(output)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise ReviewError(f"cannot write the evidence file {output}: {exc}") from exc


def _discard_unvalidated(output: Path | None, output_existed: bool) -> None:
    """Remove the evidence this call left behind when the review did not validate."""
    if output is None or output_existed or not output.exists():
        return
    try:
        output.unlink()
    except OSError as exc:
        raise ReviewError(
            f"the {GATEWAY} gateway left an unvalidated envelope at {output} and it "
            f"cannot be removed ({exc}); delete it before trusting that folder"
        ) from exc


def default_output(clip: Path, provider: str) -> Path:
    """The default evidence path beside the clip, per the review docs."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return clip / f"review-{provider_token(provider)}-{stamp}.json"


def provider_token(provider: str) -> str:
    """The provider name as it is safe to put in a filename.

    The provider reaches this tool as a free-form string (a CLI flag, a
    Makefile variable) and lands in the evidence filename, which a failed
    review then deletes. `../` in that string writes the evidence, or takes an
    earlier file with it, outside the clip folder it is meant to describe, so
    anything that is not a plain name is refused before the review starts.
    """
    if not _PROVIDER_TOKEN.fullmatch(provider):
        raise ReviewError(
            f"provider {provider!r} is not a plain name; it becomes part of the "
            "evidence filename, so it may hold only letters, digits, dot, dash "
            "and underscore"
        )
    return provider

