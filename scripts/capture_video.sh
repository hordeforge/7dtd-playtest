#!/usr/bin/env bash
# capture_video.sh - run a suite and mux the clips its StagedClip cases capture.
#
# The in-game path (CaseDef.StagedClip) proves several moments of a hold with
# the same guarantee a single staged frame has: every frame is written by the
# client process's own ScreenCapture call. What it cannot do is mux - a host
# script has to wait for the frames and join them into a video a person can
# watch. This is that script. See "Visual confirmation" in README.md.
#
# It waits for the harness's own `clip complete` marker, which CaseDef.StagedClip
# emits once the hold ends with the real frame count. Do NOT key a loop on a
# case's result: those are flushed when the case reports, tens of seconds after
# the camera moved. Because the frame write is asynchronous (Unity flushes at
# the end of the requested frame), it then polls for the last expected frame
# file to exist before muxing.
#
# Usage:
#   ./scripts/capture_video.sh --suite <id> [--out DIR] [--runner CMD] [--clip-id ID]
#
# Options / env:
#   --suite <id>        suite to run (required; or PLAYTEST_SUITE)
#   --out <dir>         output directory (default under ./.local/capture)
#   --runner <cmd>      command that runs one suite. It is invoked as
#                       `<cmd> --suite <id>`, so a project with its own wrapper
#                       (deploys, .local.env, lock handling) passes that here.
#                       Default: this repo's own scripts/playtest_run.py.
#   --clip-id <id>      clip id to wait for (default: the first `clip complete`)
#   CAPTURE_CLIP_ID     same as --clip-id
#   CAPTURE_FPS         frame rate for the muxed video (default 4; must match
#                       the clipFps the case was generated with)
#   PLAYTEST_CLIENT_LOG the client log to watch
#
# The clip frames are material for a human verdict. Nothing here judges them.
#
# Exit codes: 0 the clip was muxed, 1 the run or the mux failed, 2 bad usage
# or a missing host tool.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

SUITE="${PLAYTEST_SUITE:-}"
OUT=""
RUNNER=""
CLIP_ID="${CAPTURE_CLIP_ID:-}"

while [[ $# -gt 0 ]]; do
	case "$1" in
		--suite|--out|--runner|--clip-id)
			[[ $# -ge 2 ]] || { echo "capture_video: $1 requires a value" >&2; exit 2; }
			case "$1" in
				--suite) SUITE="$2" ;;
				--out) OUT="$2" ;;
				--runner) RUNNER="$2" ;;
				--clip-id) CLIP_ID="$2" ;;
			esac
			shift 2
			;;
		-h|--help) sed -n '2,36p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "capture_video: unknown argument $1" >&2; exit 2 ;;
	esac
done

[[ -n "$SUITE" ]] || { echo "capture_video: --suite is required" >&2; exit 2; }
OUT="${OUT:-$ROOT/.local/capture/$SUITE-$STAMP}"
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv is not on PATH; host Python goes through it (see README: Requirements)" >&2; exit 2; }
PY=(uv run --locked --project "$ROOT" python)
RUNNER="${RUNNER:-${PY[*]} $HERE/playtest_run.py --suite}"

FPS="${CAPTURE_FPS:-4}"

command -v ffmpeg >/dev/null || {
	echo "ERROR: ffmpeg is required to mux the clip; install it, or use the raw" >&2
	echo "       frame directory as the evidence (the frames are all present)." >&2
	exit 2
}

# The log a run on this machine actually writes, resolved by the orchestrator
# (PLAYTEST_CLIENT_LOG, then COMPAT, then the discovered client install). A
# Steam root hardcoded here missed a library on a second disk, a Flatpak Steam,
# and a managed Safehouse client instance alike, and then muxed a clip
# against a log no run was writing.
CLIENT_LOG="$("${PY[@]}" "$HERE/playtest_run.py" --print-client-log)"

# Refuse to start on top of a live run: the previous run's client is still
# writing that log, so a "newer than start" check passes against ITS marker and
# the clip belongs to the wrong run. Same guard and reason as capture_frames.sh,
# including its scope: `live` reports the client only, not a dedicated or zdtd.
runtime_rc=0
"${PY[@]}" "$HERE/playtest_lock.py" live || runtime_rc=$?
case $runtime_rc in
	0) : ;;
	1)
		echo "ERROR: a 7 Days to Die client is already running." >&2
		echo "       Let it finish before capturing; overlapping runs photograph the wrong one." >&2
		exit 1
		;;
	*)
		echo "ERROR: could not verify that no 7 Days to Die runtime is live; refusing." >&2
		exit 2
		;;
esac

mkdir -p "$OUT"
RUN_LOG="$OUT/run.log"

# Where this run's log begins, as a byte offset into the client log as it is
# right now. "Written after this run started" is a fact about the file's
# contents, so it is answered with an offset rather than by comparing the log's
# mtime against `date`: an NTP correction, a manual clock change or a resumed
# host moves the wall clock under the run and inverts that comparison, which
# lets a previous run's marker trigger this one.
LOG_INODE="$(stat -c %i "$CLIENT_LOG" 2>/dev/null || echo 0)"
LOG_BASE="$(stat -c %s "$CLIENT_LOG" 2>/dev/null || echo 0)"

# NEW_LOG: the part of the client log this run has produced. A log the client
# recreated or truncated carries no baseline to skip, so the anchor resets to
# its new zero rather than skipping past everything this run wrote.
read_log_since_start() {
	local inode size
	inode="$(stat -c %i "$CLIENT_LOG" 2>/dev/null || echo 0)"
	size="$(stat -c %s "$CLIENT_LOG" 2>/dev/null || echo 0)"
	if [[ "$inode" != "$LOG_INODE" ]] || (( size < LOG_BASE )); then
		LOG_INODE="$inode"
		LOG_BASE=0
	fi
	NEW_LOG=""
	if (( size > LOG_BASE )); then
		NEW_LOG="$(tail -c "+$((LOG_BASE + 1))" -- "$CLIENT_LOG")"
	fi
	return 0
}

echo "CAPTURE VIDEO"
echo "  suite         $SUITE"
echo "  clip id       ${CLIP_ID:-<first clip complete>}"
echo "  output        $OUT"
echo "  client log    $CLIENT_LOG"
echo

# Every path out of this script before the `wait` below must stop the run it
# started. The suite holds the playtest exclusivity lock, and with it a live
# client and dedicated, so a capture that gives up (unparseable marker, missing
# frames, ffmpeg failure) or is interrupted leaves the machine's one shared
# client busy until the run's own timeout. setsid puts the run in its own
# process group, so the teardown signals the orchestrator and everything it
# spawned, not just the pid the shell happened to record.
RUN_PID=""
RUN_PGID=""
RUN_STOP_TIMEOUT_SEC="${RUN_STOP_TIMEOUT_SEC:-30}"
stop_run() {
	if [[ -z "$RUN_PID" ]] || ! kill -0 "$RUN_PID" 2>/dev/null; then
		return 0
	fi
	# TERM, not KILL: the orchestrator converts it into its own teardown
	# (stop the runtimes, release the lock) instead of being cut off mid-run.
	if [[ -n "$RUN_PGID" ]]; then
		kill -TERM -- "-$RUN_PGID" 2>/dev/null || true
	else
		kill -TERM "$RUN_PID" 2>/dev/null || true
	fi
	# Bounded: a run that has already wedged (or one that ignores TERM) must
	# not hold this script's exit open, which is the very path that exists to
	# let the machine go.
	local deadline=$((SECONDS + RUN_STOP_TIMEOUT_SEC))
	while kill -0 "$RUN_PID" 2>/dev/null && (( SECONDS < deadline )); do
		sleep 0.2
	done
	if kill -0 "$RUN_PID" 2>/dev/null; then
		echo "ERROR: the suite ignored SIGTERM for ${RUN_STOP_TIMEOUT_SEC}s; killing it" >&2
		if [[ -n "$RUN_PGID" ]]; then
			kill -KILL -- "-$RUN_PGID" 2>/dev/null || true
		else
			kill -KILL "$RUN_PID" 2>/dev/null || true
		fi
	fi
	wait "$RUN_PID" 2>/dev/null || true
}
trap stop_run EXIT INT TERM

# The suite in the background; the loop reads only the part of the client log
# that appeared after the baseline above, so a marker left by a previous run
# cannot trigger this one.
# RUNNER deliberately undergoes word splitting so its configured command and arguments execute.
# shellcheck disable=SC2086
if command -v setsid >/dev/null 2>&1; then
	setsid $RUNNER "$SUITE" >"$RUN_LOG" 2>&1 &
	RUN_PID=$!
	RUN_PGID="$RUN_PID"
else
	$RUNNER "$SUITE" >"$RUN_LOG" 2>&1 &
	RUN_PID=$!
fi

# Wait for the completion line of the wanted clip. Without --clip-id the first
# `clip complete` line wins, so a suite that captures one clip needs no flag.
CLIP_LINE=""
NEW_LOG=""
echo "waiting for a completed clip..."
while :; do
	if ! kill -0 "$RUN_PID" 2>/dev/null; then
		echo "ERROR: the run exited before any clip completed; see $RUN_LOG" >&2
		wait "$RUN_PID" || true
		exit 1
	fi
	read_log_since_start
	if [[ -n "$NEW_LOG" ]]; then
		if [[ -n "$CLIP_ID" ]]; then
			CLIP_LINE="$(grep -E "clip complete $CLIP_ID " <<<"$NEW_LOG" 2>/dev/null | tail -1 || true)"
		else
			CLIP_LINE="$(grep "clip complete " <<<"$NEW_LOG" 2>/dev/null | tail -1 || true)"
		fi
		if [[ -n "$CLIP_LINE" ]]; then
			break
		fi
	fi
	sleep 1
done
echo "$CLIP_LINE"

# clip complete <id> frames=N -> playtest-shots/clips/<id>
# The marker rides the client log's own line, which carries Unity's prefix
# (timestamp, level, the harness's "[7dtd-playtest]"), so the id is not a
# fixed whitespace field. The trailing "-> <dir>" is stable, and the id is
# that directory's basename; the line's CRLF newline is stripped first or it
# would ride into every parsed field.
CLIP_LINE="$(printf '%s' "$CLIP_LINE" | tr -d '\r')"
CLIP_DIR="$(echo "$CLIP_LINE" | awk -F'-> ' '{print $2}')"
CLIP_ID="$(basename "$CLIP_DIR")"
FRAME_COUNT="$(echo "$CLIP_LINE" | awk -F'frames=' '{print $2}' | awk '{print $1}')"
[[ -n "$CLIP_ID" && -n "$FRAME_COUNT" && -n "$CLIP_DIR" ]] || {
	echo "ERROR: could not parse the clip completion line: $CLIP_LINE" >&2
	exit 2
}
# The frame count is not just compared below, it is arithmetic: LAST_INDEX is
# FRAME_COUNT - 1 and the last-frame name is printf'd from it. A non-numeric or
# zero count would abort inside $(( )) under `set -u`, or ask for "frame--1"
# and report the frames as missing, which names the wrong cause.
[[ "$FRAME_COUNT" =~ ^[1-9][0-9]*$ ]] || {
	echo "ERROR: clip completion line has no positive frame count: $CLIP_LINE" >&2
	exit 2
}
# Resolve the frames directory the way the mod and launch_client.sh do:
# the Proton prefix's playtest-shots lives under COMPAT (env, set by the
# same run that launched the client). A hardcoded default library silently
# breaks on a Steam library on another disk.
# playtest-shots sits beside logs/ in the same Proton prefix the resolved
# client log came from, so it is read off that path instead of guessed again
# from a second Steam root. COMPAT still wins when the caller set it: the run
# just launched may have used a prefix the discovery did not see.
if [[ -n "${COMPAT:-}" ]]; then
    SHOTS_DIR="$COMPAT/pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/playtest-shots"
else
    SHOTS_DIR="$(dirname "$(dirname "$CLIENT_LOG")")/playtest-shots"
fi
if [[ ! -d "$SHOTS_DIR/clips/$CLIP_ID" ]]; then
    echo "ERROR: no frames found at $SHOTS_DIR/clips/$CLIP_ID; is COMPAT set to the Proton prefix this client ran in?" >&2
    exit 1
fi

# The write is asynchronous: Unity flushes the PNG at the end of the frame it
# was requested on, so the completion line can beat the last file to disk.
# Poll briefly for the last expected frame before muxing; a gap would mux as
# continuous motion and claim frames that never landed.
LAST_INDEX=$((FRAME_COUNT - 1))
LAST_FRAME="$(printf "frame-%04d.png" "$LAST_INDEX")"
FOUND=""
for _ in $(seq 1 30); do
	if [[ -f "$SHOTS_DIR/clips/$CLIP_ID/$LAST_FRAME" ]]; then
		FOUND=1
		break
	fi
	sleep 1
done
if [[ -z "$FOUND" ]]; then
	echo "ERROR: the last expected frame ($LAST_FRAME) never appeared after the" >&2
	echo "       completion line; the clip is short or the write failed. The frames" >&2
	echo "       that do exist are in $SHOTS_DIR/clips/$CLIP_ID" >&2
	exit 1
fi
# Counting only the clip's own frame-*.png output (fixed, safe names).
# ls is intentional here because only the count of the fixed frame glob is needed.
# shellcheck disable=SC2012
ACTUAL="$(ls "$SHOTS_DIR/clips/$CLIP_ID"/frame-*.png 2>/dev/null | wc -l)"
if [[ "$ACTUAL" != "$FRAME_COUNT" ]]; then
	echo "WARNING: expected $FRAME_COUNT frames, found $ACTUAL; muxing what exists." >&2
fi

# Mux this process's own frames; ffmpeg only ever reads files the client wrote.
ffmpeg -y -framerate "$FPS" -i "$SHOTS_DIR/clips/$CLIP_ID/frame-%04d.png" \
	-pix_fmt yuv420p "$OUT/$CLIP_ID.mp4" >/dev/null 2>&1 || {
	echo "ERROR: ffmpeg failed to mux $SHOTS_DIR/clips/$CLIP_ID; the raw frames" >&2
	echo "       remain the evidence." >&2
	exit 1
}

# Contact sheet from the same frames, so a reviewer who wants one image still
# gets one, exactly as capture_frames.sh does.
if command -v montage >/dev/null 2>&1; then
	montage "$SHOTS_DIR/clips/$CLIP_ID"/frame-*.png -tile 4x -geometry 420x324+3+3 \
		-background '#1b1b1b' -label '%f' "$OUT/$CLIP_ID-contact-sheet.png" 2>/dev/null || true
fi

# Keep the run's verdict visible: a clip from a crashed run means something
# different than one from a green run.
RUN_RC=0
wait "$RUN_PID" || RUN_RC=$?
# Reaped: the EXIT trap must not signal a pid the shell has already collected.
RUN_PID=""
trap - EXIT INT TERM

# Keep the client log with the run, the same self-containment capture_frames.sh
# establishes: it is the only place that says what was actually in the clip,
# and the client truncates it on its next launch.
if [[ -r "$CLIENT_LOG" ]]; then
	cp -f "$CLIENT_LOG" "$OUT/client.log" 2>/dev/null && CLIENT_LOG_SAVED="$OUT/client.log" || CLIENT_LOG_SAVED=""
else
	CLIENT_LOG_SAVED=""
fi

echo
echo "RESULT"
echo "  clip          $CLIP_ID"
echo "  frames        $ACTUAL (of $FRAME_COUNT reported)"
echo "  video         $OUT/$CLIP_ID.mp4"
[[ -f "$OUT/$CLIP_ID-contact-sheet.png" ]] && echo "  contact sheet $OUT/$CLIP_ID-contact-sheet.png"
echo "  source frames $SHOTS_DIR/clips/$CLIP_ID"
echo "  suite exit    $RUN_RC"
echo "  suite log     $RUN_LOG"
if [[ -n "$CLIENT_LOG_SAVED" ]]; then
	echo "  client log    $CLIENT_LOG_SAVED"
else
	echo "  client log    NOT SAVED - $CLIENT_LOG was unreadable; this clip cannot" >&2
	echo "                be explained after the next client launch overwrites it" >&2
fi
echo
echo "This clip is material for a human verdict. Nothing here judged it."
