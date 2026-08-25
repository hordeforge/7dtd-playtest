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
    ReviewError,
    default_output,
    run_review,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="review_video.py",
        description=(
            "vision-model review of a staged clip via the deadeye gateway; "
            "uploads the clip to a third party, so it refuses without "
            "--allow-network"
        ),
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
    parser.add_argument("--model", default=None, help="provider model identifier")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="evidence path (default: <clip-dir>/review-<provider>-<timestamp>.json)",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="consent to uploading the clip to the provider",
    )
    parser.add_argument("--keep-raw-response", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="seconds to wait"
    )
    parser.add_argument("--json", action="store_true", help="print the full evidence envelope")
    args = parser.parse_args(argv)

    output = args.output or default_output(args.clip, args.provider)
    try:
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
        result = envelope["result"]
        assert isinstance(result, dict)
        summary = result["summary"]
        issues = result["issues"]
        assert isinstance(summary, str)
        assert isinstance(issues, list)
        print(f"summary: {summary}")
        for issue in issues:
            if isinstance(issue, dict):
                description = issue.get("description")
                print(f"issue: {description if isinstance(description, str) else issue}")
        print(f"evidence: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
