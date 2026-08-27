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
import shutil
import subprocess
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

    def as_dict(self) -> dict[str, object]:
        return {
            "purpose": self.purpose,
            "subject": self.subject,
            "camera_path": self.camera_path,
            "desired_qualities": self.desired_qualities,
            "avoid": list(self.avoid),
            "questions": list(self.questions),
            "suite": self.suite,
            "case": self.case,
        }


def _string_field(data: dict[str, object], key: str, origin: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReviewError(f"{origin}: field {key!r} must be a string, got {type(value).__name__}")
    return value.strip()


def _string_list(data: dict[str, object], key: str, origin: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ReviewError(f"{origin}: field {key!r} must be a list of strings")
    return tuple(item.strip() for item in value if item.strip())


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
    purpose = _string_field(data, "purpose", origin)
    if not purpose:
        raise ReviewError(
            f"{origin}: 'purpose' must not be empty; context is never inferred from a filename"
        )
    return ReviewIntent(
        purpose=purpose,
        subject=_string_field(data, "subject", origin),
        camera_path=_string_field(data, "camera_path", origin),
        desired_qualities=_string_field(data, "desired_qualities", origin),
        avoid=_string_list(data, "avoid", origin),
        questions=_string_list(data, "questions", origin),
        suite=_string_field(data, "suite", origin),
        case=_string_field(data, "case", origin),
    )


def load_intent_file(path: Path) -> tuple[ReviewIntent, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReviewError(f"cannot read intent file {path}: {exc}") from exc
    return parse_intent(_decode_json(raw, f"intent file {path}"), f"intent file {path}"), raw


def parse_intent_text(text: str) -> tuple[ReviewIntent, bytes]:
    raw = text.encode("utf-8")
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
            start, end = entry.pop("start_seconds", None), entry.pop("end_seconds", None)
            if "at_seconds" not in entry and start is not None and end is not None:
                entry["at_seconds"] = [start, end]
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

    if problems:
        raise ReviewError(
            f"{origin} returned an invalid structure (schema mismatch): " + "; ".join(problems)
        )
    assert isinstance(summary, str)  # checked above; narrows for the return
    assert isinstance(confidence, (int, float))  # checked above; narrows for the return
    return {
        "summary": summary.strip(),
        "strengths": strings("strengths"),
        "issues": issues,
        "recommended_changes": strings("recommended_changes"),
        "rubric_scores": scores,
        "confidence": round(float(confidence), 4),
        "limitations": strings("limitations"),
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


# -- the deadeye boundary -----------------------------------------------------


Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def deadeye_available() -> bool:
    """Whether the gateway CLI is on PATH. Presence only, never a network call."""
    return shutil.which(GATEWAY) is not None


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
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
            "review_video sends the authored clip to a third-party vision model; pass "
            "--allow-network to consent to that upload"
        )
    if intent_path is not None and intent_text is not None:
        raise ReviewError("takes exactly one of --intent PATH or --intent-text JSON, never both")
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
        notify(
            f"reviewing {clip} against {provider}; the media leaves this machine and "
            "retention is governed by that provider's terms"
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
    result = execute(argv, timeout_seconds)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        raise ReviewError(
            f"the {GATEWAY} gateway refused the review"
            + (f": {message[-1]}" if message else "")
        )
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReviewError(f"the {GATEWAY} gateway returned a non-JSON envelope: {exc}") from exc
    if not isinstance(envelope, dict) or envelope.get("kind") != "deadeye-review":
        raise ReviewError(
            f"the {GATEWAY} gateway returned an unexpected envelope; is the installed "
            "version the hordeforge gateway?"
        )
    if not isinstance(envelope.get("result"), dict):
        raise ReviewError("the gateway returned no validated result")
    validate_result(envelope["result"])
    envelope["review_validated"] = True
    envelope["intent_summary"] = {
        "purpose": intent.purpose,
        "suite": intent.suite,
        "case": intent.case,
    }
    return envelope


def default_output(clip: Path, provider: str) -> Path:
    """The default evidence path beside the clip, per the review docs."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return clip / f"review-{provider}-{stamp}.json"
