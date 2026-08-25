#!/usr/bin/env python3
"""Offline gate: scripts/video_review.py via the deadeye gateway boundary.

Every networked behaviour is exercised through a stubbed gateway runner; the
real provider is reachable only through the deadeye gateway with an opt-in
credential, so this suite never spends money and never sends bytes.
"""
from __future__ import annotations

import contextlib
import json
import random
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import video_review  # noqa: E402
from video_review import ReviewError, parse_intent, parse_intent_text, run_review  # noqa: E402

VALID_INTENT = {
    "schema_version": 1,
    "purpose": "show the garment survives a full turn without clipping",
    "subject": "thing (worn garment)",
    "camera_path": "turntable",
    "desired_qualities": "proportions read right from every side",
    "avoid": ["clipping", "popping"],
    "questions": ["does the grip read thin through the turn?"],
    "suite": "demo",
    "case": "motion_thing",
}


def _envelope() -> dict[str, object]:
    return {
        "kind": "deadeye-review",
        "schema_version": 1,
        "tool_version": "0.1.0",
        "created_utc": "2026-08-25T00:00:00+00:00",
        "review_id": "test",
        "advisory_only": True,
        "note": "Advisory only",
        "intent": {"sha256": "0" * 64, "schema_version": 1, "content": dict(VALID_INTENT)},
        "media": [
            {
                "path": "clip/frame-0000.png",
                "sha256": "0" * 64,
                "bytes": 4,
                "mime_type": "image/png",
                "kind": "frame",
            }
        ],
        "sampling": {
            "frames_available": 4,
            "frames_submitted": 4,
            "sampled": False,
            "note": "submitted the full frame sequence",
        },
        "provider": {"name": "fake", "endpoint_mode": "in-process-fake", "model_reported": "m"},
        "rubric_version": "1",
        "prompt_version": "1",
        "prompt": "p",
        "result": {
            "summary": "reads well in motion",
            "strengths": [],
            "issues": [{"description": "clips at the shoulder", "at_frame": [2, 3]}],
            "recommended_changes": [],
            "rubric_scores": {"semantic_fit": 4, "clipping_risk": 2},
            "confidence": 0.8,
            "limitations": [],
        },
        "error": None,
        "raw_provider_response": None,
        "usage": {"totalTokenCount": 3},
        "disclosure": {"network_consent": True, "third_party": "fake", "file_count": 1,
            "total_bytes": 4},
        "parameters": {},
    }


class _FakeGateway:
    def __init__(self, envelope: dict[str, object] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.envelope = envelope or _envelope()

    def __call__(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        # Emulate the real gateway's --output write: the evidence file lands
        # beside the clip only when the gateway is asked to write it.
        if "--output" in argv:
            output = Path(argv[argv.index("--output") + 1])
            output.write_text(json.dumps(self.envelope), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(self.envelope), stderr="")


def _clip(tmp_path: Path) -> Path:
    clip = tmp_path / "clip"
    clip.mkdir(exist_ok=True)
    for existing in clip.glob("frame-*.png"):
        existing.unlink()
    for index in range(4):
        (clip / f"frame-{index:04d}.png").write_bytes(bytes([index] * 4))
    return clip


def test_intent_requires_purpose() -> None:
    try:
        parse_intent({"camera_path": "turntable"}, "test")
    except ReviewError as exc:
        assert "missing required field 'purpose'" in str(exc)
    else:
        raise AssertionError("empty intent must be refused")
    try:
        parse_intent({**VALID_INTENT, "purpose": "  "}, "test")
    except ReviewError as exc:
        assert "never inferred" in str(exc)
    else:
        raise AssertionError("blank purpose must be refused")


def test_intent_text_round_trips() -> None:
    intent, raw = parse_intent_text(json.dumps(VALID_INTENT))
    assert intent.suite == "demo"
    assert intent.case == "motion_thing"
    assert raw.startswith(b"{")


def test_consent_is_demanded_before_the_gateway_is_consulted(tmp_path: Path) -> None:
    clip = _clip(tmp_path)
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps(VALID_INTENT), encoding="utf-8")
    gateway = _FakeGateway()
    try:
        run_review(clip, intent_path=intent, allow_network=False, runner=gateway)
    except ReviewError as exc:
        assert "--allow-network" in str(exc)
    else:
        raise AssertionError("review without consent must refuse")
    assert gateway.calls == [], "the gateway must not be consulted before consent"


def test_the_clip_and_intent_reach_the_gateway(tmp_path: Path) -> None:
    clip = _clip(tmp_path)
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps(VALID_INTENT), encoding="utf-8")
    gateway = _FakeGateway()
    with _gateway_available():
        run_review(clip, intent_path=intent, allow_network=True, runner=gateway)
    argv = gateway.calls[0]
    assert "review" in argv
    assert str(clip) in argv
    assert "--intent" in argv and str(intent) in argv
    assert "--allow-network" in argv and "--json" in argv


def test_a_missing_gateway_is_refused_with_the_install_route(tmp_path: Path) -> None:
    clip = _clip(tmp_path)
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps(VALID_INTENT), encoding="utf-8")
    with _gateway_available(False):
        try:
            run_review(clip, intent_path=intent, allow_network=True, runner=_FakeGateway())
        except ReviewError as exc:
            assert "deadeye" in str(exc) and "uv tool install" in str(exc)
        else:
            raise AssertionError("review without the gateway must refuse")


def test_the_envelope_result_is_validated_and_keeps_frame_moments(tmp_path: Path) -> None:
    clip = _clip(tmp_path)
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps(VALID_INTENT), encoding="utf-8")
    gateway = _FakeGateway()
    with _gateway_available():
        envelope = run_review(clip, intent_path=intent, allow_network=True, runner=gateway)
    result = envelope["result"]
    assert isinstance(result, dict)
    issues = result["issues"]
    assert isinstance(issues, list)
    first = issues[0]
    assert isinstance(first, dict)
    assert first.get("at_frame") == [2.0, 3.0]
    intent_summary = envelope["intent_summary"]
    assert isinstance(intent_summary, dict)
    assert intent_summary.get("case") == "motion_thing"


def test_an_invalid_result_from_the_gateway_fails_validation(tmp_path: Path) -> None:
    clip = _clip(tmp_path)
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps(VALID_INTENT), encoding="utf-8")
    envelope = _envelope()
    envelope["result"] = {"summary": "broken"}
    with _gateway_available():
        try:
            run_review(clip, intent_path=intent, allow_network=True, runner=_FakeGateway(envelope))
        except ReviewError as exc:
            assert "missing key" in str(exc)
        else:
            raise AssertionError("an invalid result must fail validation")


def test_credentials_never_reach_stdout_or_evidence(tmp_path: Path) -> None:
    clip = _clip(tmp_path)
    intent = tmp_path / "i.json"
    intent.write_text(json.dumps(VALID_INTENT), encoding="utf-8")
    output = clip / "review-gemini-test.json"
    gateway = _FakeGateway()
    with _gateway_available():
        run_review(
            clip,
            intent_path=intent,
            allow_network=True,
            output=output,
            runner=gateway,
        )
    assert "GEMINI_API_KEY" not in output.read_text(encoding="utf-8")
    argv_text = " ".join(" ".join(call) for call in gateway.calls)
    assert "GEMINI_API_KEY" not in argv_text, "credentials must never be passed as arguments"


@contextlib.contextmanager
def _gateway_available(present: bool = True) -> Iterator[None]:
    """Context manager stubbing the gateway-on-PATH probe."""
    from unittest import mock

    with mock.patch.object(video_review, "deadeye_available", return_value=present):
        yield


def test_scalar_and_null_moments_normalize() -> None:
    from typing import cast

    from video_review import validate_result

    result_data = cast("dict[str, object]", _envelope()["result"])
    result_data["issues"] = [
        {"description": "pops at frame 9", "at_frame": 9},
        {"description": "whole-clip read", "at_frame": None, "at_seconds": None},
    ]
    result = validate_result(result_data)
    issues = result["issues"]
    assert isinstance(issues, list)
    first = issues[0]
    assert isinstance(first, dict)
    assert first.get("at_frame") == [9.0, 9.0]
    assert issues[1] == {"description": "whole-clip read"}


# Seeded grammar for the gateway-result boundary: validate_result is the
# offline backstop run on whatever the deadeye gateway printed, so hostile
# shapes must fail closed (ReviewError) or normalize, never crash. The pool
# carries the crash classes found by review: unbounded JSON ints that
# overflow float(), Infinity/NaN tokens where numbers belong, booleans,
# wrong containers, unknown/missing keys. A failure prints its seed and doc
# so the exact input can be pasted as the next fixed regression case.
_RESULT_VALUE_FRAGMENTS: list[object] = [
    "ok",
    "",
    0,
    1,
    -3,
    True,
    False,
    None,
    [],
    {},
    [1, 2],
    4.2,
    1e999,
    -(10 ** 400),
    10 ** 400,
    float("nan"),
    float("inf"),
    {"at_frame": 9},
]
_ISSUE_SHAPES: list[Callable[[object], object]] = [
    lambda v: {"description": v},
    lambda v: {"description": v, "at_frame": v},
    lambda v: {"description": v, "at_seconds": v},
    lambda v: {"description": v, "at_frame": [v, v]},
    lambda v: {"description": v, "seconds": v},
    lambda v: {"description": v, "start_frame": v, "end_frame": v},
    lambda v: {"description": v, "unexpected": v},
    lambda v: v,
]
_TOP_KEYS = (
    "summary",
    "strengths",
    "issues",
    "recommended_changes",
    "rubric_scores",
    "confidence",
    "limitations",
)


def _hostile_result(rng: random.Random) -> dict[str, object]:
    def value() -> object:
        return rng.choice(_RESULT_VALUE_FRAGMENTS)

    issues = []
    if rng.random() < 0.8:
        issues = [
            rng.choice(_ISSUE_SHAPES)(value()) for _ in range(rng.randrange(0, 3))
        ]
    scores: dict[object, object] = {}
    if rng.random() < 0.8:
        scores = {rng.choice(["motion", "lighting", 7]): value() for _ in range(2)}
    doc: dict[str, object] = {
        key: rng.choice([value(), value()])
        if key in ("summary", "confidence")
        else ([value()] if key == "strengths" else
              (issues if key == "issues" else
               (scores if key == "rubric_scores" else [value()])))
        for key in _TOP_KEYS
        if rng.random() < 0.95 or rng.random() < 0.5
    }
    if rng.random() < 0.2:
        doc["surprise"] = value()
    return doc


def test_fuzz_validate_result_never_crashes_on_hostile_gateway_output() -> None:
    """Seeded fuzzer over the deadeye result validator.

    Invariants per generated document: validate_result either raises
    ReviewError (fail closed) or returns the exact normalized shape; a
    returned confidence is a float in 0..1, every rubric score a float in
    0..5 or None, and every issue moment a finite number pair. An
    OverflowError, KeyError or TypeError on hostile bytes is a bug."""
    import math

    from video_review import validate_result

    normalized_keys = set(video_review.RESULT_KEYS)
    for seed in range(60):
        rng = random.Random(3000 + seed)
        doc = _hostile_result(rng)
        try:
            result = validate_result(doc)
        except ReviewError:
            continue
        assert set(result) == normalized_keys, f"seed {seed}: keys {sorted(result)}"
        summary = result["summary"]
        assert isinstance(summary, str) and summary.strip(), f"seed {seed}: {summary!r}"
        confidence = result["confidence"]
        assert isinstance(confidence, float) and 0.0 <= confidence <= 1.0, (
            f"seed {seed}: confidence {confidence!r}"
        )
        for key in ("strengths", "recommended_changes", "limitations"):
            strings = result[key]
            assert isinstance(strings, list), f"seed {seed}: {key} {strings!r}"
            assert all(
                isinstance(item, str) and item.strip() for item in strings
            ), f"seed {seed}: {key} {strings!r}"
        scores = result["rubric_scores"]
        assert isinstance(scores, dict), f"seed {seed}: scores {scores!r}"
        for name, score in scores.items():
            assert isinstance(name, str), f"seed {seed}: score key {name!r}"
            assert score is None or (
                isinstance(score, float) and 0.0 <= score <= 5.0
            ), f"seed {seed}: score {name}={score!r}"
        issues = result["issues"]
        assert isinstance(issues, list), f"seed {seed}: issues {issues!r}"
        for issue in issues:
            assert isinstance(issue, dict), f"seed {seed}: issue {issue!r}"
            assert set(issue) <= {"description", "at_seconds", "at_frame"}, (
                f"seed {seed}: issue keys {sorted(issue)}"
            )
            description = issue.get("description")
            assert isinstance(description, str) and description.strip(), issue
            for moment_key in ("at_seconds", "at_frame"):
                moment = issue.get(moment_key)
                if moment is None:
                    continue
                assert (
                    isinstance(moment, list)
                    and len(moment) == 2
                    and all(isinstance(bound, float) and math.isfinite(bound) for bound in moment)
                    and moment[0] <= moment[1]
                ), f"seed {seed}: {moment_key} {moment!r}"
                if moment_key == "at_frame":
                    assert moment[0] >= 0.0, f"seed {seed}: negative frame {moment!r}"
    print("PASS result_fuzz 60 hostile gateway results fail closed or normalize")


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        test_scalar_and_null_moments_normalize()
        test_intent_requires_purpose()
        test_intent_text_round_trips()
        test_consent_is_demanded_before_the_gateway_is_consulted(root)
        test_the_clip_and_intent_reach_the_gateway(root)
        test_a_missing_gateway_is_refused_with_the_install_route(root)
        test_the_envelope_result_is_validated_and_keeps_frame_moments(root)
        test_an_invalid_result_from_the_gateway_fails_validation(root)
        test_credentials_never_reach_stdout_or_evidence(root)
        test_fuzz_validate_result_never_crashes_on_hostile_gateway_output()
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
