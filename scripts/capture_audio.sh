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
# shellcheck source=scripts/capture_common.sh
source "$HERE/capture_common.sh"

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
if [[ -z "$OUT" ]]; then
	# A unique directory, not "<suite>-audio-<second-stamp>": two captures of
	# the same suite starting in the same second resolved to one path and
	# overwrote each other's recording. An explicit --out is the caller
	# naming a directory to reuse.
	mkdir -p "$ROOT/.local/capture"
	OUT="$(mktemp -d "$ROOT/.local/capture/$SUITE-audio-$STAMP-XXXXXX")"
fi
command -v uv >/dev/null 2>&1 || { echo "ERROR: uv is not on PATH; host Python goes through it (see README: Requirements)" >&2; exit 2; }
PY=(uv run --locked --project "$ROOT" python)
RUNNER="${RUNNER:-${PY[*]} $HERE/playtest_run.py --suite}"

command -v parec >/dev/null || { echo "ERROR: parec (PulseAudio/PipeWire) is required" >&2; exit 2; }
command -v pactl >/dev/null || { echo "ERROR: pactl is required" >&2; exit 2; }

# The monitor records the whole sink, so an overlapping run's audio lands in
# this recording and nobody can tell whose blast was heard.
refuse_live_capture "record each other"

SINK="$(pactl get-default-sink 2>/dev/null)"
[[ -n "$SINK" ]] || { echo "ERROR: no default sink" >&2; exit 2; }
MONITOR="${SINK}.monitor"

mkdir -p "$OUT"
WAV="$OUT/audio.wav"
WAV_PART="$OUT/.audio.part"
RUN_LOG="$OUT/run.log"
EMPTY_WAV=0

RUN_PID=""
RUN_PGID=""
REC_PID=""
capture_init_run_timeout

# The recorder writes a WAV's RIFF length header when it closes the stream,
# so a file named by the recorder is only a recording once the recorder has
# exited. Record to a sibling part file and publish by rename: a SIGTERM
# landing mid-frame otherwise leaves a header-only file that passes a size
# test and is handed over as evidence.
stop_recorder() {
	[[ -n "$REC_PID" ]] || return 0
	kill "$REC_PID" 2>/dev/null || true
	wait "$REC_PID" 2>/dev/null || true
	REC_PID=""
	if [[ -f "$WAV_PART" ]]; then
		mv -f "$WAV_PART" "$WAV"
	fi
}

# Every path out of this script before its `wait` must stop both halves. The
# suite holds the playtest exclusivity lock, and with it a live client and
# dedicated, so a capture that gives up leaves the machine's one shared client
# busy. A SIGTERM to a shell blocked on a foreground child is not delivered
# until that child finishes, which is why the suite runs backgrounded and is
# torn down from here.
# shellcheck disable=SC2329  # the trap below is its only caller
stop_all() {
	stop_recorder
	capture_stop_run
}

echo "CAPTURE AUDIO"
echo "  suite         $SUITE"
echo "  monitor       $MONITOR"
echo "  output        $WAV"
echo

trap stop_all EXIT INT TERM

# shellcheck disable=SC2086
parec --device="$MONITOR" --file-format=wav --rate=48000 --channels=2 "$WAV_PART" &
REC_PID=$!

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

RUN_RC=0
wait "$RUN_PID" || RUN_RC=$?
# Reaped: no path out of here may signal a pid the shell has already collected.
RUN_PID=""
RUN_PGID=""

stop_recorder
trap - EXIT INT TERM

echo "RESULT"
echo "  suite exit    $RUN_RC"
if [[ -s "$WAV" ]]; then
	echo "  recording     $WAV ($(du -h "$WAV" | cut -f1))"
	# A file full of digital silence means the client was muted after all, or
	# the wrong monitor was recorded. Say so rather than shipping silence.
	if command -v sox >/dev/null 2>&1; then
		# sox parses the RIFF header, so a stream the recorder was killed
		# short of finalising fails here rather than shipping as evidence.
		# A size test cannot see that: a header-only file is not empty.
		if peak="$(sox "$WAV" -n stat 2>&1 | awk '/Maximum amplitude/ {print $3}')"; then
			echo "  peak amplitude ${peak:-unknown}"
		else
			echo "  recording     UNREADABLE: $WAV is not a complete WAV" >&2
			EMPTY_WAV=1
		fi
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
