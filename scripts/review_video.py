#!/usr/bin/env python3
"""review_video.py - ask a vision model to critique a staged clip.

Submits an already-captured clip directory (the frame sequence, the muxed mp4
if ffmpeg was available, and the client.log that capture_video.sh produced)
plus the author's recorded intent to the deadeye gateway and prints the
structured, advisory result. The verdict is evidence for the human-watch
gate; it can never satisfy it.

Usage:
  uv run scripts/review_video.py <clip-dir> \
      --intent <path> --provider PROVIDER [--model MODEL] --allow-network [--json]

Run `uv run scripts/review_video.py --help` for the full surface.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from video_review import (
    DEFAULT_PROVIDER,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    ReviewError,
    default_output,
    run_review,
    terminal_safe,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="review_video.py",
        description=(
            "vision-model review of a staged clip via the deadeye gateway; "
            "uploads the whole clip directory (frames, mp4, run log, client "
            "log) to a third party, so it refuses without --allow-network"
        ),
        epilog=(
            "examples:\n"
            "  review_video.py <clip-dir> --intent intent.json --allow-network\n"
            "  review_video.py <clip-dir> --intent-text '{\"purpose\":\"...\"}' \\\n"
            "      --allow-network --json\n"
            "  make playtest-review-video SUITE=<id> INTENT=<path>\n"
            "exit codes: 0 evidence written, 1 review failed or was refused, "
            "2 bad usage"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("clip", type=Path, help="the clip directory to review")
    parser.add_argument(
        "--intent",
        type=Path,
        default=None,
        help="intent JSON file committed beside the suite definition; requires purpose",
    )
    parser.add_argument(
        "--intent-text", default=None, help="inline intent JSON instead of --intent"
    )
    parser.add_argument(
        "--provider", default=DEFAULT_PROVIDER, help=f"(default {DEFAULT_PROVIDER})"
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "provider model identifier; omit it and the provider's current "
            "default answers, which is recorded in the evidence as unpinned"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="evidence path (default: <clip-dir>/review-<provider>-<timestamp>.json)",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help=(
            "consent to uploading the clip directory (frames, mp4, run log, "
            "client log) to the provider"
        ),
    )
    parser.add_argument(
        "--keep-raw-response",
        action="store_true",
        help="pass --keep-raw-response to the gateway CLI",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="pass --force to the gateway CLI",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=(
            f"seconds to wait ({MIN_TIMEOUT_SECONDS:g}-{MAX_TIMEOUT_SECONDS:g}, "
            "finite); a value outside that range is refused"
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the full evidence envelope")
    args = parser.parse_args(argv)

    try:
        output = args.output or default_output(args.clip, args.provider)
        envelope = run_review(
            args.clip,
            provider=args.provider,
            intent_path=args.intent,
            intent_text=args.intent_text,
            model=args.model,
            allow_network=args.allow_network,
            timeout_seconds=args.timeout,
            keep_raw_response=args.keep_raw_response,
            output=output,
            force=args.force,
            notify=lambda line: print(line, file=sys.stderr),
        )
    except ReviewError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(envelope, indent=2, sort_keys=True))
    else:
        return _print_result(envelope, output)
    return 0


def _print_result(envelope: dict[str, object], output: Path) -> int:
    """Print the human-readable review. The gateway, not the model, fixed the
    envelope's shape; the summary and issue descriptions are model-authored and
    go through `terminal_safe` before they reach the operator's screen."""
    result = envelope["result"]
    assert isinstance(result, dict)
    summary = result["summary"]
    issues = result["issues"]
    confidence = result["confidence"]
    assert isinstance(summary, str)
    assert isinstance(issues, list)
    assert isinstance(confidence, float)
    print(f"summary: {terminal_safe(summary)}")
    for issue in issues:
        if isinstance(issue, dict):
            description = issue.get("description")
            text = description if isinstance(description, str) else str(issue)
            print(f"issue: {terminal_safe(text)}")
    request = envelope.get("review_request")
    if isinstance(request, dict) and request.get("model"):
        print(f"model: {terminal_safe(str(request['model']))}")
    print(f"confidence: {confidence:g} (advisory only; a human accepts the clip)")
    print(f"tokens: {_token_count(envelope)}")
    print(f"evidence: {output}")
    return 0


def _token_count(envelope: dict[str, object]) -> str:
    """Reported token usage, or `unavailable` when the provider sent none."""
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        for key in ("totalTokenCount", "total_token_count", "input_tokens", "prompt_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return str(value)
    return "unavailable"


if __name__ == "__main__":
    raise SystemExit(main())
