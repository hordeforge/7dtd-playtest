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
#
# Exit codes: 0 the frames were written, 1 the run or the capture failed,
# 2 bad usage or a missing host tool.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
# shellcheck source=scripts/capture_common.sh
source "$HERE/capture_common.sh"

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
		-h|--help) sed -n '2,37p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "capture_frames: unknown argument $1" >&2; exit 2 ;;
	esac
done

[[ -n "$SUITE" ]] || { echo "capture_frames: --suite is required" >&2; exit 2; }
if [[ -z "$OUT" ]]; then
	# A unique directory, not "<suite>-<second-stamp>": two captures of the
	# same suite starting in the same second resolved to one path, and the
	# reuse sweep below then deleted frames the other capture was still
	# writing. An explicit --out is the caller naming a directory to reuse.
	mkdir -p "$ROOT/.local/capture"
	OUT="$(mktemp -d "$ROOT/.local/capture/$SUITE-$STAMP-XXXXXX")"
fi
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv is not on PATH; host Python goes through it (see README: Requirements)" >&2; exit 2; }
PY=(uv run --locked --project "$ROOT" python)
RUNNER="${RUNNER:-${PY[*]} $HERE/playtest_run.py --suite}"

FRAMES="${CAPTURE_FRAMES:-18}"
INTERVAL="${CAPTURE_INTERVAL:-0.4}"
CROP="${CAPTURE_CROP:-1286x992+0+0}"

command -v spectacle >/dev/null || { echo "ERROR: spectacle is required" >&2; exit 2; }
command -v magick >/dev/null || { echo "ERROR: ImageMagick (magick) is required" >&2; exit 2; }

# The log a run on this machine actually writes, resolved by the orchestrator
# (PLAYTEST_CLIENT_LOG, then COMPAT, then the discovered client install). A
# Steam root hardcoded here missed a library on a second disk, a Flatpak Steam,
# and a managed Safehouse client instance alike, and then photographed a log
# no run was writing.
CLIENT_LOG="$("${PY[@]}" "$HERE/playtest_run.py" --print-client-log)"

refuse_live_capture "photograph the wrong one"

mkdir -p "$OUT"
# A reused --out must not mix takes: every artifact below has a name only this
# script writes, and stale raw/cropped frames from a previous run would
# otherwise be re-cropped, counted, and montaged into this run's evidence.
rm -f "$OUT"/raw-*.png "$OUT"/contact-sheet.png
mkdir -p "$OUT/cropped"
rm -f "$OUT"/cropped/frame-*.png
RUN_LOG="$OUT/run.log"

capture_log_gate_init

echo "CAPTURE FRAMES"
echo "  suite         $SUITE"
echo "  frames        $FRAMES every ${INTERVAL}s, cropped to $CROP"
echo "  output        $OUT"
echo "  client log    $CLIENT_LOG"
echo "  marker        $MARKER"
echo

RUN_PID=""
RUN_PGID=""
RUN_STOP_TIMEOUT_SEC="${RUN_STOP_TIMEOUT_SEC:-30}"
trap capture_stop_run EXIT INT TERM

# The suite in the background; the loop reads only the part of the client log
# that appeared after the baseline above, so a marker left by a previous run
# cannot trigger this one.
capture_start_run

echo "waiting for the first staged scene..."
while :; do
	# Drain the log before asking whether the run is still alive. A suite can
	# stage its scene and exit between two polls; a liveness check first
	# reports "the run exited before any scene was staged" with the marker
	# sitting unread in the log it already wrote.
	read_log_since_start
	if [[ -n "$NEW_LOG" ]]; then
		LAST_MARK="$(grep -- "$MARKER" <<<"$NEW_LOG" | tail -1 || true)"
		if [[ -n "$LAST_MARK" ]]; then
			break
		fi
	fi
	if ! kill -0 "$RUN_PID" 2>/dev/null; then
		echo "ERROR: the run exited before any scene was staged; see $RUN_LOG" >&2
		wait "$RUN_PID" || true
		# Reaped: the EXIT trap must not signal a pid the shell has already
		# collected, which by then the kernel may have handed to an unrelated
		# process, nor its process group.
		RUN_PID=""
		RUN_PGID=""
		exit 1
	fi
	sleep 1
done
printf '%s\n' "$LAST_MARK"

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
	# The rename is applied to the basename only: substituting over the whole
	# path also rewrites an `raw-` in $OUT, so `--out ./raw-frames` wrote
	# crops named raw-NN.png, missed the frame-*.png glob, and reported that
	# spectacle captured nothing.
	raw_base="${f##*/}"
	if magick "$f" -crop "$CROP" +repage "$OUT/cropped/${raw_base/raw-/frame-}" 2>/dev/null; then
		rm -f "$f"
	fi
done
montage "$OUT/cropped"/frame-*.png -tile 4x -geometry 420x324+3+3 \
	-background '#1b1b1b' -label '%f' "$OUT/contact-sheet.png" 2>/dev/null \
	|| echo "  contact sheet NOT BUILT: montage failed or is not installed" >&2

# Counting only this script's own frame-*.png output (fixed, safe names), by
# glob rather than `ls | wc -l`, so a directory that vanished reads as no
# frames instead of silently counting as zero.
shopt -s nullglob
frames=("$OUT"/cropped/frame-*.png)
shopt -u nullglob
FRAME_COUNT=${#frames[@]}
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
# Only this run's own log. The client log is append-only across runs, so a
# whole-file grep lists the previous run's scenes under this run's result, and
# grep's two-file `file:` prefix makes sort -u list this run's own scene twice.
grep -oE 'scene staged [^ ]+' "$RUN_LOG" 2>/dev/null | sed 's/^/    /' | sort -u || true
echo
echo "These frames are material for a human verdict. Nothing here judged them."
