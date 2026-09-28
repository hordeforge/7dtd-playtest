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

* stop_run, the teardown capture_video.sh and capture_frames.sh install before
  their `wait`. Both start the suite in the background and have several ways
  out (unparseable marker, missing frames, ffmpeg failure, Ctrl+C). Without the
  trap the suite outlives the capture: it keeps the playtest lock and a live
  client and dedicated on the machine's one shared client until its own
  timeout.

* the "written after this run started" gate capture_video.sh and
  capture_frames.sh read the client log through. It used to be
  `mtime > $(date +%s)`, two wall-clock reads compared to order events. A
  backward clock step (NTP correction, a manual set, a resumed host) puts a
  previous run's log on the far side of the comparison, and the capture
  photographs the previous run. The gate is now a byte offset, so no clock
  decides it.

stop_run and the log gate live in capture_common.sh, which all three capture
scripts source: they were three copies of a live-run guard and two of each of
these, and a fix to one copy left the others answering a question they no
longer asked.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "capture_video.sh"
FRAMES_SCRIPT = Path(__file__).resolve().parent / "capture_frames.sh"
COMMON = Path(__file__).resolve().parent / "capture_common.sh"
AUDIO_SCRIPT = Path(__file__).resolve().parent / "capture_audio.sh"

# The three capture scripts share the log gate and the stop_run teardown; the
# shared copy is what these checks execute, so a script cannot pass by keeping
# a private one.
STOP_START = "capture_stop_run() {"

# The log gate: the baseline taken before the run starts, plus the reader.
LOG_GATE_START = "capture_log_gate_init() {"
LOG_GATE_END = "read_log_since_start() {"

# The parse fragment: the marker comment through the line before the guard.
PARSE_START = "# clip complete <id> frames=N -> playtest-shots/clips/<id>"
PARSE_END = '[[ -n "$CLIP_ID" && -n "$FRAME_COUNT" && -n "$CLIP_DIR" ]]'
FRAME_COUNT_RE = '[[ "$FRAME_COUNT" =~ ^[1-9][0-9]*$ ]]'
CLIP_ID_RE = '[[ "$CLIP_ID" =~ ^[a-z0-9_-]{1,64}$ ]]'


def parse_fragment() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(PARSE_START)
    end = text.index(PARSE_END, start)
    return text[start:end].strip("\n")


def guards_fragment() -> str:
    """The script's own frame-count and clip-id guards, bodies included.

    The bare `[[ ... ]]` test is not the contract: what rejects a hostile line
    is the `|| { ...; exit 2; }` the script hangs on it, so that is what runs
    here rather than a copy of the pattern.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(FRAME_COUNT_RE)
    end = text.index("\n}", text.index(CLIP_ID_RE, start)) + 2
    return text[start:end]


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

    check_clip_id_guard(prefixed)

    check_shared_capture_common(SCRIPT, FRAMES_SCRIPT, AUDIO_SCRIPT)
    check_stop_run()
    check_log_gate()
    check_client_log_resolution(SCRIPT, FRAMES_SCRIPT)

    print("RESULT PASS")
    return 0


def check_clip_id_guard(marker: str) -> None:
    """The parsed clip id must be a name, not a path the log chose.

    The client log carries whatever a remote LAN peer typed, and the playtest
    instance joins without a join password, so a line holding
    `clip complete <x> frames=N -> <y>` anywhere in it reaches this parse
    before the client's own marker does. The id then names the directory
    frames are read out of and the mp4 this script writes, so it is held to
    the alphabet Helpers.AssetName produces. `basename` alone only keeps the
    separators out: `..` and a glob character still name something.
    """
    guards = f"{parse_fragment()}\n{guards_fragment()}\necho ok"

    def verdict(line: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", guards],
            env={"CLIP_LINE": line, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

    for clip_id, why in (
        ("..", "parent directory"),
        ("../..", "traversal past the frames root"),
        ("*", "glob over every clip directory"),
        ("a b", "a name carrying a space"),
        ("a.mp4", "a name carrying a dot"),
        ("a;rm", "a name carrying a shell metacharacter"),
        ("x" * 65, "a name longer than any clip id"),
    ):
        line = f"clip complete motion frames=48 -> playtest-shots/clips/{clip_id}"
        proc = verdict(line)
        assert proc.returncode != 0, f"{why} must be rejected: {proc.stdout!r}"

    for clip_id in ("motion_thing", "shirt-01", "_nul", "a" * 64):
        line = f"clip complete motion frames=48 -> playtest-shots/clips/{clip_id}"
        proc = verdict(line)
        assert proc.returncode == 0 and proc.stdout.strip() == "ok", (
            f"{clip_id!r} is a legal clip id and must pass: {proc.stderr}"
        )

    # The chat-shaped line, prefix and all, is the case the guard exists for.
    # A peer's text survives the basename as a bare name, so the id is still
    # only a name here; the frames directory is then looked up under the
    # shots root and a name that is not a clip of this run is not there.
    chat = (
        "2026-09-28T20:20:15 53.385 INF [7dtd-playtest] <bob> says: "
        "clip complete motion frames=48 -> ../../../../etc"
    )
    clip_id, _, _ = run_parse(chat)
    assert clip_id == "etc", f"the traversal basename did not parse as {clip_id!r}"
    assert "/" not in clip_id, f"a separator survived into the clip id: {clip_id!r}"
    # With the separators gone, what is left to reject is a name that is not
    # one the mod writes: a dot, a glob, a relative segment.
    for line in (
        chat.replace("../../../../etc", "clips/.."),
        chat.replace("../../../../etc", "clips/*"),
    ):
        assert verdict(line).returncode != 0, f"accepted {line!r}"
    assert verdict(marker).returncode == 0, "the harness's own marker must still pass"
    print("OK a log-derived clip id is held to the capture name alphabet")


def check_shared_capture_common(*scripts: Path) -> None:
    """The live-run guard, the log gate and the stop_run teardown live once.

    Each capture script carried its own copy, and the copies drifted: a fix to
    one guard left the other two answering a question they no longer asked. The
    scripts source the shared file instead, and this fails if one goes back to
    a private copy behind a sourced one.
    """
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert 'source "$HERE/capture_common.sh"' in text, (
            f"{script.name} does not source the shared capture code"
        )
        assert "refuse_live_capture " in text, (
            f"{script.name} does not call the shared live-run guard"
        )
    for script in (SCRIPT, FRAMES_SCRIPT):
        text = script.read_text(encoding="utf-8")
        assert "trap capture_stop_run " in text, (
            f"{script.name} does not install the shared teardown"
        )
        assert "read_log_since_start" in text, f"{script.name} never reads the log"
        assert "capture_log_gate_init" in text, f"{script.name} never takes the baseline"
        assert "stat -c" not in text, f"{script.name} carries a private log gate"
    print("OK the capture scripts share one live guard, log gate and teardown")


def check_client_log_resolution(*scripts: Path) -> None:
    """Both scripts must ask the orchestrator where the log is.

    Each used to carry its own hardcoded Steam root
    (`$HOME/Games/Steam/steamapps/compatdata/251570`), which is wrong on a
    library on a second disk, a Flatpak Steam, and every managed Safehouse
    client instance. The capture then watched a log no run was writing, and
    reported a stale marker as this run's evidence.
    """
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "--print-client-log" in text, f"{script.name} does not resolve the log"
        hardcoded = [line for line in text.splitlines() if "compatdata" in line]
        assert not hardcoded, f"{script.name} hardcodes a Steam root again: {hardcoded}"
    print("OK capture scripts resolve the client log through the orchestrator")


def stop_fragment() -> str:
    text = COMMON.read_text(encoding="utf-8")
    start = text.index(STOP_START)
    end = text.index("\n}", start) + 2
    return text[start:end]


def log_gate_fragment() -> str:
    """The shared baseline and reader, run with a real file behind them."""
    text = COMMON.read_text(encoding="utf-8")
    start = text.index(LOG_GATE_START)
    end = text.index("\n}", text.index(LOG_GATE_END, start)) + 2
    return text[start:end] + "\ncapture_log_gate_init"


def read_log_since(fragment: str, log: Path, while_running: str = "") -> str:
    """Snapshot the baseline, run `while_running`, then read what the run wrote."""
    proc = subprocess.run(
        ["bash", "-c", "\n".join((
            "set -euo pipefail",
            fragment,
            while_running,
            "read_log_since_start",
            "printf '%s' \"$NEW_LOG\"",
        ))],
        env={"CLIENT_LOG": str(log), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"log gate exited {proc.returncode}: {proc.stderr}"
    return proc.stdout


def read_log_in_polls(fragment: str, log: Path, rounds: list[str]) -> list[str]:
    """Read the log once per entry in `rounds` (the real wait loop's poll).

    The wait loop polls once a second for as long as the run takes to stage a
    scene, so what one poll hands back is what that poll costs: re-reading the
    whole log from the run's baseline every time is quadratic in run length, on
    the machine that is also running the game.
    """
    script = "\n".join(
        ["set -euo pipefail", fragment]
        + [
            f"{round_}\nread_log_since_start\nprintf '@@@\\n%s' \"$NEW_LOG\""
            for round_ in rounds
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env={"CLIENT_LOG": str(log), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        timeout=60,
    )
    # Bytes, not text: the client log is CRLF and universal-newline decoding
    # here would quietly hide the very boundaries under test.
    out = proc.stdout.decode("utf-8")
    errout = proc.stderr.decode("utf-8")
    assert proc.returncode == 0, f"poll loop exited {proc.returncode}: {errout}"
    return out.split("@@@\n")[1:]


def check_log_gate() -> None:
    """A previous run's marker must not reach this run, whatever the clock says.

    The reader is the shared one the script sources and the file behind it is a
    real one, so a regression to a clock comparison shows up as a marker coming
    back out of a log that predates the run.
    """
    fragment = log_gate_fragment()
    assert "date" not in fragment, "the log gate reads a clock again"
    assert "%Y" not in fragment, "the log gate compares an mtime again"
    assert "stat -c %Y" not in COMMON.read_text(encoding="utf-8"), (
        "the log gate still gates on the log's mtime"
    )

    stale = "2026-08-25T20:20:15 53.385 INF [7dtd-playtest] scene staged old_run prop=0\r\n"
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "output_log_client.txt"
        log.write_text(stale, encoding="utf-8", newline="")
        assert read_log_since(fragment, log) == "", (
            "a marker left by a previous run was read as this run's"
        )
        # The wall clock the old gate compared against, moved back a year, so a
        # previous run's log sits on the far side of that comparison.
        backdated = f"touch -d 2025-01-01T00:00:00Z {shlex.quote(str(log))}"
        assert read_log_since(fragment, log, backdated) == "", (
            "a stale marker is read once the clock moves"
        )
        appended = f"printf 'scene staged this_run prop=1\\r\\n' >> {shlex.quote(str(log))}"
        assert "scene staged this_run" in read_log_since(fragment, log, appended), (
            "this run's own marker was not seen"
        )
        # The client truncates its log on the next launch, which leaves the
        # anchor pointing past everything this run has written so far.
        truncated = f"printf 'scene staged after_truncate\\r\\n' > {shlex.quote(str(log))}"
        assert "after_truncate" in read_log_since(fragment, log, truncated), (
            "a truncated log skipped past this run's marker"
        )
        # Recreated under a new inode, same story.
        recreated = "\n".join((
            f"rm -f {shlex.quote(str(log))}",
            f"printf 'scene staged after_recreate\\r\\n' > {shlex.quote(str(log))}",
        ))
        assert "after_recreate" in read_log_since(fragment, log, recreated), (
            "a recreated log skipped past this run's marker"
        )
        # A marker whose line has been written whole is read whole, with every
        # field the clip parse needs. The fragment re-takes its baseline per
        # read, so a completed marker is exercised on a fresh log.
        whole_log = Path(tmp) / "output_log_whole.txt"
        whole_log.write_text(stale, encoding="utf-8", newline="")
        completed = (
            "printf 'clip complete motion_thing frames=48 -> "
            "playtest-shots/clips/motion_thing\\r\\n' >> " + shlex.quote(str(whole_log))
        )
        whole = read_log_since(fragment, whole_log, completed)
        assert (
            "clip complete motion_thing frames=48 -> "
            "playtest-shots/clips/motion_thing" in whole
        ), f"a completed marker was not read whole: {whole!r}"
    print("OK the shared log gate reads only the log this run produced")
    check_log_gate_is_incremental(fragment)


def check_log_gate_is_incremental(fragment: str) -> None:
    """Each poll hands back only what was appended since the previous one.

    Two things break if the reader goes back to the baseline every poll: the
    cost of a wait grows with the whole run instead of with the new bytes, and
    a line the client was still writing at a poll boundary is scanned twice
    (or, cut in half, missed entirely).
    """
    q = shlex.quote
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "output_log_client.txt"
        log.write_text("noise from a previous run\r\n", encoding="utf-8", newline="")
        rounds = [
            f"printf 'first poll\\r\\nscene st' >> {q(str(log))}",
            f"printf 'aged one\\r\\nsecond poll\\r\\n' >> {q(str(log))}",
            f"printf 'scene staged this_run prop=1\\r\\n' >> {q(str(log))}",
        ]
        polls = read_log_in_polls(fragment, log, rounds)
    assert polls[0] == "first poll\r\n", polls[0]
    # The marker line was half written when poll 1 ran, so it is carried
    # whole into poll 2 rather than scanned in halves or missed.
    assert polls[1] == "scene staged one\r\nsecond poll\r\n", polls[1]
    assert polls[2] == "scene staged this_run prop=1\r\n", polls[2]
    print("OK the shared log gate hands back one poll's new bytes, split lines intact")


def check_stop_run() -> None:
    """The shared stop_run must kill the run and its process group.

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
                stop_fragment(),
                'RUN_PID=""\nRUN_PGID=""\nRUN_STOP_TIMEOUT_SEC=2',
                "setsid bash -c 'trap \"\" TERM; sleep 30 & echo $! > "
                + str(pid_file)
                + "; wait' >/dev/null 2>&1 &",
                'RUN_PID=$!\nRUN_PGID="$RUN_PID"',
                "capture_stop_run",
                'if kill -0 "$RUN_PID" 2>/dev/null; then echo "run=alive"; '
                'else echo "run=reaped"; fi',
                'CHILD=""',
                # The stand-in writes the child's pid from inside its own
                # start-up, so read it once it exists: an empty read would
                # make every check below vacuously pass.
                'for _ in $(seq 1 50); do CHILD="$(cat ' + str(pid_file)
                + ' 2>/dev/null || true)"; [[ -n "$CHILD" ]] && break; sleep 0.1; done',
                'if [[ -z "$CHILD" ]]; then echo "group=nopid"; else',
                # A killed process still answers kill -0 for a few ms while
                # the kernel tears it down (and a not-yet-reaped zombie
                # answers it for as long as its dying parent lives), so a
                # single instant check is a coin flip. Poll briefly: a
                # survivor still reads alive, a corpse only reads alive
                # until it is gone.
                'gone=0; for _ in $(seq 1 50); do '
                'if ! kill -0 "$CHILD" 2>/dev/null; then gone=1; break; fi; sleep 0.1; done',
                'if (( gone )); then echo "group=gone"; else echo "group=alive"; fi; fi',
            )
        )
        proc = subprocess.run(
            ["bash", "-c", harness],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert proc.returncode == 0, f"stop_run exited {proc.returncode}: {proc.stderr}"
    assert "run=reaped" in proc.stdout, f"the run survived stop_run: {proc.stdout}"
    assert "group=gone" in proc.stdout, (
        f"the run's process group survived stop_run: {proc.stdout}"
    )
    assert "group=nopid" not in proc.stdout, (
        f"the stand-in never reported a child pid: {proc.stdout}"
    )
    print("OK stop_run terminates the run and its process group")


if __name__ == "__main__":
    sys.exit(main())
