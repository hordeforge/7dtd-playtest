#!/usr/bin/env bash
# capture_frames.sh - run a suite and photograph the scenes it stages.
#
# A suite proves data. Nothing in this harness looks at the screen, so anything
# a person has to judge by eye — a model, an icon, a UI row, an effect — needs a
# frame, and a frame has to be taken while the scene is actually up. This is the
# supported way to get one. See "Visual confirmation" in README.md.
#
# It waits for the harness's own `scene staged` marker (Report.Staged), which is
# emitted the moment a scene is on screen. Do NOT key a loop on a case's result
# or Detail text: those are flushed when the case reports, tens of seconds after
# the camera moved, so the loop photographs whatever came next — in practice the
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
RUNNER="${RUNNER:-$HERE/playtest_run.py --suite}"

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
# process's executable, so stock/Proton clients (including the Wine preloader
# phase), the stock dedicated, and zdtd are all covered with no drift between
# this guard and the lock the runner itself enforces.
runtime_rc=0
python3 - "$HERE" <<'PYEOF' || runtime_rc=$?
import sys

sys.path.insert(0, sys.argv[1])
try:
    import playtest_lock
    live = bool(playtest_lock.default_live_runtime_running())
except Exception as ex:  # noqa: BLE001 - guard must never fail open silently
    print(f"capture_frames: cannot inspect live runtimes: {ex}", file=sys.stderr)
    sys.exit(2)
sys.exit(1 if live else 0)
PYEOF
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

echo "CAPTURE FRAMES"
echo "  suite         $SUITE"
echo "  frames        $FRAMES every ${INTERVAL}s, cropped to $CROP"
echo "  output        $OUT"
echo "  client log    $CLIENT_LOG"
echo "  marker        $MARKER"
echo

# The suite in the background; the loop waits for the marker in a log written
# after this run started, so one left by a previous run cannot trigger it early.
# shellcheck disable=SC2086
$RUNNER "$SUITE" >"$RUN_LOG" 2>&1 &
RUN_PID=$!

echo "waiting for the first staged scene..."
while :; do
	if ! kill -0 "$RUN_PID" 2>/dev/null; then
		echo "ERROR: the run exited before any scene was staged; see $RUN_LOG" >&2
		wait "$RUN_PID" || true
		exit 1
	fi
	mtime="$(stat -c %Y "$CLIENT_LOG" 2>/dev/null || echo 0)"
	if [[ "$mtime" -gt "$START" ]] && grep -q "$MARKER" "$CLIENT_LOG" 2>/dev/null; then
		break
	fi
	sleep 1
done
grep "$MARKER" "$CLIENT_LOG" | tail -1

for i in $(seq -w 1 "$FRAMES"); do
	spectacle -b -n -f -o "$OUT/raw-$i.png" >/dev/null 2>&1 || true
	sleep "$INTERVAL"
done

wait "$RUN_PID" || true

mkdir -p "$OUT/cropped"
for f in "$OUT"/raw-*.png; do
	[[ -e "$f" ]] || continue
	# Drop a raw only once its crop exists: magick failing here must not delete
	# the only copy of the evidence with it.
	if magick "$f" -crop "$CROP" +repage "$OUT/cropped/$(basename "${f/raw-/frame-}")" 2>/dev/null; then
		rm -f "$f"
	fi
done
montage "$OUT/cropped"/frame-*.png -tile 4x -geometry 420x324+3+3 \
	-background '#1b1b1b' -label '%f' "$OUT/contact-sheet.png" 2>/dev/null || true

# Counting only this script's own frame-*.png output (fixed, safe names).
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
# was actually in them — which prefabs loaded, which meshes grafted, what the
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
echo "  contact sheet $OUT/contact-sheet.png"
echo "  suite log     $RUN_LOG"
if [[ -n "$CLIENT_LOG_SAVED" ]]; then
	echo "  client log    $CLIENT_LOG_SAVED"
else
	# Say so rather than leaving its absence to be discovered later by
	# somebody trying to explain a frame.
	echo "  client log    NOT SAVED — $CLIENT_LOG was unreadable; this run cannot" >&2
	echo "                be explained after the next client launch overwrites it" >&2
fi
echo "  staged scenes"
grep -oE 'scene staged [^ ]+' "$RUN_LOG" "$CLIENT_LOG" 2>/dev/null | sed 's/^/    /' | sort -u || true
echo
echo "These frames are material for a human verdict. Nothing here judged them."
