#!/usr/bin/env bash
# capture_common.sh - the parts every capture_* script shares.
#
# Sourced, never executed. Each capture script resolves CLIENT_LOG through the
# orchestrator, then calls the functions below. capture_frames.sh and
# capture_video.sh read the client log for a marker; all three refuse to start
# on top of a live run and, in the two that background the suite, have to stop
# it on the way out.
#
# The caller owns: PY, CLIENT_LOG, RUN_PID, RUN_PGID, RUN_STOP_TIMEOUT_SEC.

# The variables above are set by the sourcing script, not here. NEW_LOG is read
# by that script's wait loop.
# shellcheck disable=SC2154,SC2034

# Refuse to start on top of a live run: the previous run's client is still
# writing that log, so a "newer than start" check passes against ITS marker
# and the capture records the wrong run.
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
#
# $1: what an overlapping run would spoil, e.g. "photograph the wrong one".
refuse_live_capture() {
	local runtime_rc=0
	"${PY[@]}" "$HERE/playtest_lock.py" live || runtime_rc=$?
	case $runtime_rc in
		0) : ;;
		1)
			echo "ERROR: a 7 Days to Die client is already running." >&2
			echo "       Let it finish before capturing; overlapping runs $1." >&2
			exit 1
			;;
		*)
			echo "ERROR: could not verify that no 7 Days to Die runtime is live; refusing." >&2
			exit 2
			;;
	esac
}

# Where this run's log begins, as a byte offset into the client log as it is
# right now. "Written after this run started" is a fact about the file's
# contents, so it is answered with an offset rather than by comparing the log's
# mtime against `date`: an NTP correction, a manual clock change or a resumed
# host moves the wall clock under the run and inverts that comparison, which
# lets a previous run's marker trigger this one.
capture_log_gate_init() {
	LOG_INODE="$(stat -c %i "$CLIENT_LOG" 2>/dev/null || echo 0)"
	LOG_BASE="$(stat -c %s "$CLIENT_LOG" 2>/dev/null || echo 0)"
}

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

# Every path out of a capture script before its `wait` must stop the run it
# started. The suite holds the playtest exclusivity lock, and with it a live
# client and dedicated, so a capture that gives up or is interrupted leaves the
# machine's one shared client busy until the run's own timeout. setsid puts
# the run in its own process group, so the teardown signals the orchestrator
# and everything it spawned, not just the pid the shell happened to record.
capture_stop_run() {
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
