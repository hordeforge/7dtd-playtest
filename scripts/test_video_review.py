#!/usr/bin/env python3
"""Offline gate: scripts/video_review.py via the deadeye gateway boundary.

Every networked behaviour is exercised through a stubbed gateway runner; the
real provider is reachable only through the deadeye gateway with an opt-in
credential, so this suite never spends money and never sends bytes.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterator
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
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
