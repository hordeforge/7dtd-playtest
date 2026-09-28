#!/usr/bin/env bash
# capture_audio.sh - run a suite and record what it actually sounds like.
#
# A suite proves data. Nothing in this harness listens, so anything a person
# has to judge by ear (a blast, an ambience, a UI cue) needs a recording,
# and the recording has to cover the run that played it. This is the supported
# way to get one: it records the default sink's monitor for the length of one
# suite run, so the listening can happen later, by whoever does the sign-off.
#
# The recording is evidence to listen to, not a verdict. Nothing here decides
# whether anything sounds right. It also does not unmute anything: the runner
# owns mute policy, and a recording of a muted client is reported as such by
# the peak-amplitude line rather than silently shipped.
#
# Usage:
#   ./scripts/capture_audio.sh --suite <id> [--out DIR] [--runner CMD]
#
# Options / env:
#   --suite <id>   suite to run (required; or PLAYTEST_SUITE)
#   --out <dir>    output directory (default under ./.local/capture)
#   --runner <cmd> command that runs one suite; invoked as
#                  `<cmd> --suite <id>`, so a project with its own wrapper
#                  (deploys, .local.env, lock handling) passes that here.
#                  Default: this repo's own scripts/playtest_run.py.
#
# Exit codes: 0 the recording was written, 1 the run or the recording failed,
# 2 bad usage or a missing host tool.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

SUITE="${PLAYTEST_SUITE:-}"
OUT=""
RUNNER=""

while [[ $# -gt 0 ]]; do
	case "$1" in
		--suite|--out|--runner)
			[[ $# -ge 2 ]] || { echo "capture_audio: $1 requires a value" >&2; exit 2; }
			case "$1" in
				--suite) SUITE="$2" ;;
				--out) OUT="$2" ;;
				--runner) RUNNER="$2" ;;
			esac
			shift 2
			;;
		-h|--help) sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "capture_audio: unknown argument $1" >&2; exit 2 ;;
	esac
done

[[ -n "$SUITE" ]] || { echo "capture_audio: --suite is required" >&2; exit 2; }
OUT="${OUT:-$ROOT/.local/capture/$SUITE-audio-$STAMP}"
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv is not on PATH; host Python goes through it (see README: Requirements)" >&2; exit 2; }
PY=(uv run --locked --project "$ROOT" python)
RUNNER="${RUNNER:-${PY[*]} $HERE/playtest_run.py --suite}"

command -v parec >/dev/null || { echo "ERROR: parec (PulseAudio/PipeWire) is required" >&2; exit 2; }
command -v pactl >/dev/null || { echo "ERROR: pactl is required" >&2; exit 2; }

# Refuse to start on top of a live run: the monitor records the whole sink, so
# an overlapping run's audio lands in this recording and nobody can tell whose
# blast was heard.
#
# pgrep -f on the command line would match any process whose cmdline merely
# contains the game's name, which includes the monitoring commands a session
# runs while watching a run. Instead reuse the orchestrator's own runtime
# probe (playtest_lock): it inspects each process's executable, so the
# stock/Proton client (including the Wine preloader phase) is detected with no
# drift between this guard and the runner's lock. `live` reports the client
# only: a stock dedicated or a zdtd owns its own instance and ports, so
# neither blocks a capture.
runtime_rc=0
"${PY[@]}" "$HERE/playtest_lock.py" live || runtime_rc=$?
case $runtime_rc in
	0) : ;;
	1)
		echo "ERROR: a 7 Days to Die client is already running." >&2
		echo "       Let it finish before capturing; overlapping runs record each other." >&2
		exit 1
		;;
	*)
		echo "ERROR: could not verify that no 7 Days to Die runtime is live; refusing." >&2
		exit 2
		;;
esac

SINK="$(pactl get-default-sink 2>/dev/null)"
[[ -n "$SINK" ]] || { echo "ERROR: no default sink" >&2; exit 2; }
MONITOR="${SINK}.monitor"

mkdir -p "$OUT"
WAV="$OUT/audio.wav"
RUN_LOG="$OUT/run.log"
EMPTY_WAV=0

echo "CAPTURE AUDIO"
echo "  suite         $SUITE"
echo "  monitor       $MONITOR"
echo "  output        $WAV"
echo

parec --device="$MONITOR" --file-format=wav --rate=48000 --channels=2 "$WAV" &
REC_PID=$!
trap 'kill "$REC_PID" 2>/dev/null || true' EXIT

set +e
# RUNNER deliberately undergoes word splitting so its configured command and arguments execute.
# shellcheck disable=SC2086
$RUNNER "$SUITE" >"$RUN_LOG" 2>&1
RUN_RC=$?
set -e

kill "$REC_PID" 2>/dev/null || true
wait "$REC_PID" 2>/dev/null || true
trap - EXIT

echo "RESULT"
echo "  suite exit    $RUN_RC"
if [[ -s "$WAV" ]]; then
	echo "  recording     $WAV ($(du -h "$WAV" | cut -f1))"
	# A file full of digital silence means the client was muted after all, or
	# the wrong monitor was recorded. Say so rather than shipping silence.
	if command -v sox >/dev/null 2>&1; then
		peak="$(sox "$WAV" -n stat 2>&1 | awk '/Maximum amplitude/ {print $3}')"
		echo "  peak amplitude ${peak:-unknown}"
	fi
else
	echo "  recording     EMPTY -- nothing was captured" >&2
	# The recorder dying on a busy or vanished monitor is a failed capture, not
	# a passing one. Exiting 0 here ships an audio run with no audio, and the
	# suite's own 0 is the only status a caller reads.
	EMPTY_WAV=1
fi
grep -E "\[7dtd-playtest\] (PASS|FAIL) $SUITE" "$RUN_LOG" | tail -3 || true
echo "  suite log     $RUN_LOG"
if (( EMPTY_WAV )); then
	exit 1
fi
exit "$RUN_RC"
