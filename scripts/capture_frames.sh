#!/usr/bin/env bash
# capture_frames.sh - run a suite and photograph the scenes it stages.
#
# A suite proves data. Nothing in this harness looks at the screen, so anything
# a person has to judge by eye (a model, an icon, a UI row, an effect) needs a
# frame, and a frame has to be taken while the scene is actually up. This is the
# supported way to get one. See "Visual confirmation" in README.md.
#
# It waits for the harness's own `scene staged` marker (Report.Staged), which is
# emitted the moment a scene is on screen. Do NOT key a loop on a case's result
# or Detail text: those are flushed when the case reports, tens of seconds after
# the camera moved, so the loop photographs whatever came next, in practice the
# disconnect dialog.
#
# Usage:
#   ./scripts/capture_frames.sh --suite <id> [--out DIR] [--runner CMD]
#
# Options / env:
#   --suite <id>        suite to run (required; or PLAYTEST_SUITE)
#   --out <dir>         frame output directory (default under ./.local/capture)
#   --runner <cmd>      command that runs one suite. It is invoked as
#                       `<cmd> --suite <id>`, so a project with its own wrapper
#                       (deploys, .local.env, lock handling) passes that here.
#                       Default: this repo's own scripts/playtest_run.py.
#   --marker <text>     log text to wait for (default: `scene staged`)
#   CAPTURE_FRAMES      how many frames (default 18)
#   CAPTURE_INTERVAL    seconds between frames (default 0.4)
#   CAPTURE_CROP        ImageMagick geometry for the client window
#                       (default 1286x992+0+0). Whole-desktop shots also catch
#                       whatever else is on screen, which is nobody's business
#                       in a review artefact.
#   PLAYTEST_CLIENT_LOG the client log to watch
#
# The frames are material for a human verdict. Nothing here judges them.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

SUITE="${PLAYTEST_SUITE:-}"
OUT=""
RUNNER=""
MARKER="scene staged"

while [[ $# -gt 0 ]]; do
	case "$1" in
		--suite|--out|--runner|--marker)
			[[ $# -ge 2 ]] || { echo "capture_frames: $1 requires a value" >&2; exit 2; }
			case "$1" in
				--suite) SUITE="$2" ;;
				--out) OUT="$2" ;;
				--runner) RUNNER="$2" ;;
				--marker) MARKER="$2" ;;
			esac
			shift 2
			;;
		-h|--help) sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "capture_frames: unknown argument $1" >&2; exit 2 ;;
	esac
done

[[ -n "$SUITE" ]] || { echo "capture_frames: --suite is required" >&2; exit 2; }
OUT="${OUT:-$ROOT/.local/capture/$SUITE-$STAMP}"
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv is not on PATH; host Python goes through it (see README: Requirements)" >&2; exit 2; }
PY=(uv run --locked --project "$ROOT" python)
RUNNER="${RUNNER:-${PY[*]} $HERE/playtest_run.py --suite}"

FRAMES="${CAPTURE_FRAMES:-18}"
INTERVAL="${CAPTURE_INTERVAL:-0.4}"
CROP="${CAPTURE_CROP:-1286x992+0+0}"

command -v spectacle >/dev/null || { echo "ERROR: spectacle is required" >&2; exit 2; }
command -v magick >/dev/null || { echo "ERROR: ImageMagick (magick) is required" >&2; exit 2; }

COMPAT_DEFAULT="$HOME/Games/Steam/steamapps/compatdata/251570"
CLIENT_LOG="${PLAYTEST_CLIENT_LOG:-$COMPAT_DEFAULT/pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/logs/output_log_client_7dtd_connect.txt}"

# Refuse to start on top of a live run: the previous run's client is still
# writing that log, so a "newer than start" check passes against ITS marker and
# the frames belong to the wrong run.
#
# pgrep -f on the command line would match any process whose cmdline merely
# contains the game's name, which includes the monitoring commands a session
# runs while watching a run (a `tail -f` of the client log, a `pgrep` in a
# wait loop). That false positive is not theoretical. Instead reuse the
# orchestrator's own runtime probe (playtest_lock): it inspects each
# process's executable, so the stock/Proton client (including the Wine
# preloader phase) is detected with no drift between this guard and the
# lock the runner itself enforces. `live` reports the client only: a stock
# dedicated or a zdtd belongs to its own instance and ports, so neither
# blocks a capture.
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
# A reused --out must not mix takes: every artifact below has a name only this
# script writes, and stale raw/cropped frames from a previous run would
# otherwise be re-cropped, counted, and montaged into this run's evidence.
rm -f "$OUT"/raw-*.png "$OUT"/contact-sheet.png
mkdir -p "$OUT/cropped"
rm -f "$OUT"/cropped/frame-*.png
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

echo "CAPTURE FRAMES"
echo "  suite         $SUITE"
echo "  frames        $FRAMES every ${INTERVAL}s, cropped to $CROP"
echo "  output        $OUT"
echo "  client log    $CLIENT_LOG"
echo "  marker        $MARKER"
echo

# Every path out of this script before the `wait` below must stop the run it
# started. The suite holds the playtest exclusivity lock, and with it a live
# client and dedicated, so a capture that gives up or is interrupted leaves the
# machine's one shared client busy until the run's own timeout. setsid puts
# the run in its own process group, so the teardown signals the orchestrator
# and everything it spawned, not just the pid the shell happened to record.
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

echo "waiting for the first staged scene..."
while :; do
	if ! kill -0 "$RUN_PID" 2>/dev/null; then
		echo "ERROR: the run exited before any scene was staged; see $RUN_LOG" >&2
		wait "$RUN_PID" || true
		exit 1
	fi
	read_log_since_start
	if [[ -n "$NEW_LOG" ]] && grep -q "$MARKER" <<<"$NEW_LOG"; then
		break
	fi
	sleep 1
done
grep "$MARKER" <<<"$NEW_LOG" | tail -1

for i in $(seq -w 1 "$FRAMES"); do
	spectacle -b -n -f -o "$OUT/raw-$i.png" >/dev/null 2>&1 || true
	sleep "$INTERVAL"
done

# Reap the suite and carry its exit into RESULT below: a frame set from a
# crashed run means something different than one from a green run.
RUN_RC=0
wait "$RUN_PID" || RUN_RC=$?
# Reaped: the EXIT trap must not signal a pid the shell has already collected.
RUN_PID=""
trap - EXIT INT TERM

for f in "$OUT"/raw-*.png; do
	[[ -e "$f" ]] || continue
	# Drop a raw only once its crop exists: magick failing here must not delete
	# the only copy of the evidence with it.
	if magick "$f" -crop "$CROP" +repage "$OUT/cropped/$(basename "${f/raw-/frame-}")" 2>/dev/null; then
		rm -f "$f"
	fi
done
montage "$OUT/cropped"/frame-*.png -tile 4x -geometry 420x324+3+3 \
	-background '#1b1b1b' -label '%f' "$OUT/contact-sheet.png" 2>/dev/null \
	|| echo "  contact sheet NOT BUILT: montage failed or is not installed" >&2

# Counting only this script's own frame-*.png output (fixed, safe names).
# ls is intentional here because only the count of the fixed frame glob is needed.
# shellcheck disable=SC2012
FRAME_COUNT="$(ls "$OUT/cropped"/frame-*.png 2>/dev/null | wc -l)"
if (( FRAME_COUNT == 0 )); then
	echo
	echo "ERROR: no frames were captured; spectacle produced nothing for this run." >&2
	echo "       The suite log may still say why the run itself failed: $RUN_LOG" >&2
	exit 1
fi

# Keep the client log with the run.
#
# The frames are the *subject*; the client log is the only place that says what
# was actually in them: which prefabs loaded, which meshes grafted, what the
# engine warned about. And it is the one file here that does not survive: the
# client truncates it on its next launch, so the evidence for a run is gone the
# moment anybody starts the game again, including the person opening the frames
# to look at what the run produced.
#
# That is not a corner case. On 2026-08-24 four runs of a garment suite were
# judged from frames alone, over an afternoon, because every client log had
# already been overwritten by the next launch by the time anyone asked what the
# frames contained. Copying it costs a few hundred kilobytes and makes the run
# self-contained.
if [[ -r "$CLIENT_LOG" ]]; then
	cp -f "$CLIENT_LOG" "$OUT/client.log" 2>/dev/null \
		&& CLIENT_LOG_SAVED="$OUT/client.log" \
		|| CLIENT_LOG_SAVED=""
else
	CLIENT_LOG_SAVED=""
fi

echo
echo "RESULT"
echo "  frames        $FRAME_COUNT"
# Only name the contact sheet when it is on disk. Printing the path
# unconditionally turns a montage that failed or is not installed into a
# reviewer opening a file that was never written.
[[ -f "$OUT/contact-sheet.png" ]] && echo "  contact sheet $OUT/contact-sheet.png"
echo "  suite exit    $RUN_RC"
echo "  suite log     $RUN_LOG"
if [[ -n "$CLIENT_LOG_SAVED" ]]; then
	echo "  client log    $CLIENT_LOG_SAVED"
else
	# Say so rather than leaving its absence to be discovered later by
	# somebody trying to explain a frame.
	echo "  client log    NOT SAVED: $CLIENT_LOG was unreadable; this run cannot" >&2
	echo "                be explained after the next client launch overwrites it" >&2
fi
echo "  staged scenes"
grep -oE 'scene staged [^ ]+' "$RUN_LOG" "$CLIENT_LOG" 2>/dev/null | sed 's/^/    /' | sort -u || true
echo
echo "These frames are material for a human verdict. Nothing here judged them."
