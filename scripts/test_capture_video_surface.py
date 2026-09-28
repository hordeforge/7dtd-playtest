#!/usr/bin/env python3
"""Regression guards for the capture scripts.

Two contracts, both executed out of the real script text rather than a copy,
so the script and the pinned contract cannot drift:

* capture_video.sh's clip-completion-line parse. The marker the harness writes
  arrives on the client log's own line, which carries Unity's prefix
  (timestamp, level, the "[7dtd-playtest]" tag), and the file is CRLF. The
  first implementation read the clip id as a fixed whitespace field, so on a
  real prefixed line it parsed the log level ("INF") as the id and looked for
  frames under clips/INF, while the frames sat under clips/<id>. This happened
  on the first real in-game run of the vendored 7dtd-vision-review
  end-to-end test.

* stop_run in capture_video.sh and capture_frames.sh. Both start the suite in
  the background and have several ways out before the `wait` (unparseable
  marker, missing frames, ffmpeg failure, Ctrl+C). Without the trap the suite
  outlives the capture: it keeps the playtest lock and a live client and
  dedicated on the machine's one shared client until its own timeout.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "capture_video.sh"
FRAMES_SCRIPT = Path(__file__).resolve().parent / "capture_frames.sh"

STOP_START = "stop_run() {"

# The parse fragment: the marker comment through the line before the guard.
PARSE_START = "# clip complete <id> frames=N -> playtest-shots/clips/<id>"
PARSE_END = '[[ -n "$CLIP_ID" && -n "$FRAME_COUNT" && -n "$CLIP_DIR" ]]'
FRAME_COUNT_RE = '[[ "$FRAME_COUNT" =~ ^[1-9][0-9]*$ ]]'


def parse_fragment() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(PARSE_START)
    end = text.index(PARSE_END, start)
    return text[start:end].strip("\n")


def run_parse(line: str) -> tuple[str, str, str]:
    """Run the real parse fragment with CLIP_LINE set; return (id, frames, dir)."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'{parse_fragment()}\nprintf "%s|%s|%s" "$CLIP_ID" "$FRAME_COUNT" "$CLIP_DIR"',
        ],
        env={"CLIP_LINE": line, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    clip_id, frames, clip_dir = proc.stdout.split("|", 2)
    return clip_id, frames, clip_dir


def main() -> int:
    text = SCRIPT.read_text(encoding="utf-8")
    assert PARSE_START in text, "capture_video.sh lost its marker-comment anchor"
    assert PARSE_END in text, "capture_video.sh lost its parse guard"

    prefixed = (
        "2026-08-25T20:20:15 53.385 INF [7dtd-playtest] "
        "clip complete motion_thing frames=48 -> playtest-shots/clips/motion_thing"
    )
    clip_id, frames, clip_dir = run_parse(prefixed)
    assert clip_id == "motion_thing", f"id parsed as {clip_id!r}, expected motion_thing"
    assert frames == "48", f"frames parsed as {frames!r}, expected 48"
    assert clip_dir == "playtest-shots/clips/motion_thing", f"dir parsed as {clip_dir!r}"
    print("OK prefixed marker line parses id from the trailing directory")

    crlf = prefixed + "\r\n"
    clip_id, frames, clip_dir = run_parse(crlf)
    assert clip_id == "motion_thing", f"CRLF id parsed as {clip_id!r}"
    assert "\r" not in clip_dir, f"CR leaked into the clip dir: {clip_dir!r}"
    print("OK CRLF newline is stripped before parsing")

    bare = "clip complete motion_thing frames=48 -> playtest-shots/clips/motion_thing"
    clip_id, frames, clip_dir = run_parse(bare)
    assert clip_id == "motion_thing" and frames == "48"
    assert clip_dir == "playtest-shots/clips/motion_thing"
    print("OK a bare marker (no client prefix) still parses")

    # The frame count feeds arithmetic (LAST_INDEX = count - 1), so a count
    # that is not a positive integer has to be rejected by name rather than
    # aborting inside $(( )) or asking for "frame--1".
    assert FRAME_COUNT_RE in text, "capture_video.sh lost its frame-count guard"
    assert text.index(FRAME_COUNT_RE) > text.index(PARSE_END), (
        "the frame-count guard must follow the parse"
    )
    for line, why in (
        (prefixed.replace("frames=48", "frames=0"), "zero count"),
        (prefixed.replace("frames=48", "frames=N/A"), "non-numeric count"),
        (prefixed.replace("frames=48", "frames="), "empty count"),
    ):
        proc = subprocess.run(
            ["bash", "-c", f'set -u\n{parse_fragment()}\n{FRAME_COUNT_RE}'],
            env={"CLIP_LINE": line, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, f"{why} must be rejected: {proc.stdout!r}"

    proc = subprocess.run(
        ["bash", "-c", f'set -u\n{parse_fragment()}\n{FRAME_COUNT_RE}\necho ok'],
        env={"CLIP_LINE": prefixed, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr
    print("OK a non-positive or non-numeric frame count is rejected by name")

    check_stop_run(SCRIPT)
    check_stop_run(FRAMES_SCRIPT)

    print("RESULT PASS")
    return 0


def stop_fragment(script: Path) -> str:
    text = script.read_text(encoding="utf-8")
    start = text.index(STOP_START)
    end = text.index("\n}", start) + 2
    return text[start:end]


def check_stop_run(script: Path) -> None:
    """The script's own stop_run must kill the run and its process group.

    The inner child stands in for what a real run is: a supervisor (`uv run`)
    with the playtest itself underneath it. Signalling only the recorded pid
    would leave the playtest holding the lock. The stand-in ignores SIGTERM
    (`trap '' TERM`), which is the case the escalation exists for, and the
    harness shortens the grace so the check stays fast.
    """
    with tempfile.TemporaryDirectory() as tmp:
        pid_file = Path(tmp) / "child.pid"
        harness = "\n".join(
            (
                "set -euo pipefail",
                stop_fragment(script),
                'RUN_PID=""\nRUN_PGID=""\nRUN_STOP_TIMEOUT_SEC=2',
                "setsid bash -c 'trap \"\" TERM; sleep 30 & echo $! > "
                + str(pid_file)
                + "; wait' >/dev/null 2>&1 &",
                'RUN_PID=$!\nRUN_PGID="$RUN_PID"',
                "stop_run",
                'if kill -0 "$RUN_PID" 2>/dev/null; then echo "run=alive"; '
                'else echo "run=reaped"; fi',
                'CHILD="$(cat ' + str(pid_file) + ' 2>/dev/null || true)"',
                'if [[ -n "$CHILD" ]] && kill -0 "$CHILD" 2>/dev/null; '
                'then echo "group=alive"; else echo "group=gone"; fi',
            )
        )
        proc = subprocess.run(
            ["bash", "-c", harness],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert proc.returncode == 0, f"{script.name}: stop_run exited {proc.returncode}: {proc.stderr}"
    assert "run=reaped" in proc.stdout, f"{script.name}: the run survived stop_run: {proc.stdout}"
    assert "group=gone" in proc.stdout, (
        f"{script.name}: the run's process group survived stop_run: {proc.stdout}"
    )
    print(f"OK {script.name} stop_run terminates the run and its process group")


if __name__ == "__main__":
    sys.exit(main())
