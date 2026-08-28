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
RUNNER="${RUNNER:-$HERE/playtest_run.py --suite}"

FPS="${CAPTURE_FPS:-4}"

command -v ffmpeg >/dev/null || {
	echo "ERROR: ffmpeg is required to mux the clip; install it, or use the raw" >&2
	echo "       frame directory as the evidence (the frames are all present)." >&2
	exit 2
}

COMPAT_DEFAULT="$HOME/Games/Steam/steamapps/compatdata/251570"
CLIENT_LOG="${PLAYTEST_CLIENT_LOG:-$COMPAT_DEFAULT/pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/logs/output_log_client_7dtd_connect.txt}"

# Refuse to start on top of a live run: the previous run's client is still
# writing that log, so a "newer than start" check passes against ITS marker and
# the clip belongs to the wrong run. Same guard and reason as capture_frames.sh.
runtime_rc=0
python3 "$HERE/playtest_lock.py" live || runtime_rc=$?
case $runtime_rc in
	0) : ;;
	1)
		echo "ERROR: a 7 Days to Die client or dedicated server is already running." >&2
		echo "       Let it finish before capturing; overlapping runs photograph the wrong one." >&2
		exit 1
		;;
	*)
		echo "ERROR: could not verify that no 7 Days to Die runtime is live; refusing." >&2
		exit 2
		;;
esac

mkdir -p "$OUT"
START="$(date +%s)"
RUN_LOG="$OUT/run.log"

echo "CAPTURE VIDEO"
echo "  suite         $SUITE"
echo "  clip id       ${CLIP_ID:-<first clip complete>}"
echo "  output        $OUT"
echo "  client log    $CLIENT_LOG"
echo

# The suite in the background; the loop waits for the marker in a log written
# after this run started, so one left by a previous run cannot trigger it early.
# RUNNER deliberately undergoes word splitting so its configured command and arguments execute.
# shellcheck disable=SC2086
$RUNNER "$SUITE" >"$RUN_LOG" 2>&1 &
RUN_PID=$!

# Wait for the completion line of the wanted clip. Without --clip-id the first
# `clip complete` line wins, so a suite that captures one clip needs no flag.
CLIP_LINE=""
echo "waiting for a completed clip..."
while :; do
	if ! kill -0 "$RUN_PID" 2>/dev/null; then
		echo "ERROR: the run exited before any clip completed; see $RUN_LOG" >&2
		wait "$RUN_PID" || true
		exit 1
	fi
	mtime="$(stat -c %Y "$CLIENT_LOG" 2>/dev/null || echo 0)"
	if [[ "$mtime" -gt "$START" ]]; then
		if [[ -n "$CLIP_ID" ]]; then
			CLIP_LINE="$(grep -E "clip complete $CLIP_ID " "$CLIENT_LOG" 2>/dev/null | tail -1 || true)"
		else
			CLIP_LINE="$(grep "clip complete " "$CLIENT_LOG" 2>/dev/null | tail -1 || true)"
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
# Resolve the frames directory the way the mod and launch_client.sh do:
# the Proton prefix's playtest-shots lives under COMPAT (env, set by the
# same run that launched the client). A hardcoded default library silently
# breaks on a Steam library on another disk.
if [[ -n "${COMPAT:-}" ]]; then
    SHOTS_DIR="$COMPAT/pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/playtest-shots"
else
    SHOTS_DIR="$HOME/AppData/Roaming/7DaysToDie/playtest-shots"
    [[ -d "$SHOTS_DIR" ]] || SHOTS_DIR="$HOME/.steam/steam/steamapps/compatdata/251570/pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/playtest-shots"
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
