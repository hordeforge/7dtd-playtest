#!/usr/bin/env bash
# playtest_repeat.sh - run a playtest suite N times and aggregate the reports.
#
# Flake detection: a suite that passes 1/1 can still be flaky. This wrapper
# runs the orchestrator LAPS times (fresh server each lap) and aggregates the
# per-lap report JSON, so a PR gate can require N clean laps.
#
# Usage:
#   ./scripts/playtest_repeat.sh [--laps N] [--suite demo] [orchestrator args...]
#
# Options:
#   --laps N      laps to run (env PLAYTEST_LAPS; default 3)
#   --suite ID    suite id (env PLAYTEST_SUITE; default demo)
#   --logdir DIR  report directory (env LOGDIR; default ~/.cache/7dtd-playtest),
#                 also passed to the orchestrator as --logdir
#   -h, --help    print this text
#
# Env:
#   PLAYTEST_LAPS               same as --laps
#   PLAYTEST_SUITE              same as --suite
#   LOGDIR                      same as --logdir
#   PLAYTEST_LAP_MARK_STALE_SEC age at which an abandoned lap mark is swept
#                               (positive integer seconds; default 86400)
#
# Anything else is passed through to playtest_run.py unchanged.
#
# Exit codes: 0 every lap clean, 1 a lap failed or the aggregate was not all
# clean, 2 usage or a missing orchestrator.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCH="$HERE/playtest_run.py"
ROOT="$(cd "$HERE/.." && pwd)"
SUITE="${PLAYTEST_SUITE:-demo}"
LAPS="${PLAYTEST_LAPS:-3}"
REPORT_DIR="${LOGDIR:-$HOME/.cache/7dtd-playtest}"
ORCH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --laps|--suite|--logdir)
      [[ $# -ge 2 ]] || { echo "playtest_repeat: $1 requires a value" >&2; exit 2; }
      case "$1" in
        --laps) LAPS="$2" ;;
        --suite) SUITE="$2" ;;
        --logdir) REPORT_DIR="$2" ;;
      esac
      shift 2
      ;;
    -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) ORCH_ARGS+=("$1"); shift ;;
  esac
done

if [[ ! -x "$ORCH" ]]; then
  echo "playtest_repeat: orchestrator not found at $ORCH" >&2
  exit 2
fi

if [[ ! "$LAPS" =~ ^[1-9][0-9]*$ ]]; then
  echo "playtest_repeat: laps must be a positive integer, got '$LAPS' (--laps N or PLAYTEST_LAPS)" >&2
  exit 2
fi

echo "playtest_repeat: suite=$SUITE laps=$LAPS report_dir=$REPORT_DIR"
declare -i laps_passed=0 laps_total=0
declare -i sum_pass=0 sum_fail=0 sum_skip=0

# A lap mark is this script's own scratch, and a run killed between mktemp and
# its own rm -f leaves one behind in the report dir. Nothing else removes them,
# so repeated runs pile up an empty file per killed lap for good. Sweep only
# the marks old enough that no live lap can still be holding one: a concurrent
# session's fresh mark must survive its own release_lap_mark.
#
# The mark is scratch, not the filter. Which report belongs to a lap is decided
# by snapshot_reports (what was already on disk before the lap), because a
# concurrent session publishes into this same directory and a time bound alone
# cannot tell the two apart.
MARK_STALE_SEC="${PLAYTEST_LAP_MARK_STALE_SEC:-86400}"
# Same check as LAPS above, and for the same reason: the value is arithmetic,
# not text. `PLAYTEST_LAP_MARK_STALE_SEC=24h` aborts the sweep on a bash
# arithmetic error (this script runs without `set -e`, so the failure is a
# silent no-op, not a message), and a value under a minute reads as
# "sweep disabled" rather than the near-instant sweep it asks for.
if [[ ! "$MARK_STALE_SEC" =~ ^[1-9][0-9]*$ ]]; then
  echo "playtest_repeat: lap mark stale window must be a positive integer number" \
       "of seconds, got '$MARK_STALE_SEC' (PLAYTEST_LAP_MARK_STALE_SEC)" >&2
  exit 2
fi
sweep_stale_lap_marks() {
  local older_than=$(( MARK_STALE_SEC / 60 )) mark
  (( older_than > 0 )) || return 0
  while IFS= read -r mark; do
    rm -f "$mark"
  done < <(find "$REPORT_DIR" -maxdepth 1 -name '.lap-mark.*' -type f \
             -mmin "+$older_than" 2>/dev/null)
}

if [[ ! -d "$REPORT_DIR" ]]; then
  echo "playtest_repeat: report dir does not exist: $REPORT_DIR" >&2
  echo "  pass --logdir, or let the orchestrator create it" >&2
  exit 2
fi
sweep_stale_lap_marks

# An interrupted run drops the mark it is holding, so the common exit paths
# leave nothing behind. A SIGKILLed run still leaves one; the sweep above is
# what bounds those.
LAP_MARK=""
# shellcheck disable=SC2329  # the trap below is its only caller
release_lap_mark() {
  if [[ -n "$LAP_MARK" ]]; then
    rm -f "$LAP_MARK"
    LAP_MARK=""
  fi
}
trap release_lap_mark EXIT INT TERM

# The reports already on disk, by name. The report dir is the shared default:
# a concurrent session on another client instance publishes
# report-<epoch>.json into the same directory, and a mark is only a time
# bound, so "newer than the mark" alone selects another session's report and
# grades this lap with its counts. A name that was already here before the
# lap started is never this lap's, whatever its mtime.
declare -A prior_reports=()
snapshot_reports() {
  local f
  prior_reports=()
  for f in "$REPORT_DIR"/report-*.json; do
    [[ -f "$f" ]] || continue
    prior_reports["${f##*/}"]=1
  done
}

# Newest report a lap produced (report-<epoch>.json). Pure bash: no ls -t
# parsing, paths with spaces survive. Only reports this lap created count: the
# report dir is shared, and a previous lap or a concurrent session wrote the
# rest.
latest_report() {
  local f name newest=""
  for f in "$REPORT_DIR"/report-*.json; do
    [[ -f "$f" ]] || continue
    name="${f##*/}"
    [[ -z "${prior_reports[$name]:-}" ]] || continue
    if [[ -z "$newest" || "$f" -nt "$newest" ]]; then
      newest="$f"
    fi
  done
  printf '%s' "$newest"
}

# "pass fail skip" from one report, read via argv (no path interpolation into code).
# Through uv like every host script: one interpreter, honoring uv.lock.
summary_counts() {
  uv run --locked --project "$ROOT" python "$HERE/report_summary.py" "$1" 2>/dev/null
}

for lap in $(seq 1 "$LAPS"); do
  echo "=== lap $lap/$LAPS ==="
  # Stamped before the lap runs, and the reports already present are recorded
  # before it starts, so nothing this lap did not write is graded as its
  # verdict.
  lap_mark="$(mktemp "$REPORT_DIR/.lap-mark.XXXXXX")" || {
    echo "playtest_repeat: cannot create a lap mark in $REPORT_DIR" >&2
    exit 2
  }
  LAP_MARK="$lap_mark"
  snapshot_reports
  if ! uv run --locked --project "$ROOT" python "$ORCH" --suite "$SUITE" --logdir "$REPORT_DIR" "${ORCH_ARGS[@]}"; then
    rm -f "$lap_mark"
    LAP_MARK=""
    echo "playtest_repeat: lap $lap failed (orchestrator exit != 0)" >&2
    continue
  fi
  latest="$(latest_report)"
  rm -f "$lap_mark"
  LAP_MARK=""
  if [[ -z "$latest" ]]; then
    echo "playtest_repeat: lap $lap produced no report under $REPORT_DIR" >&2
    continue
  fi
  laps_total+=1
  if counts="$(summary_counts "$latest")"; then
    read -r p f sk <<<"$counts"
  else
    echo "playtest_repeat: lap $lap summary unreadable in $latest" >&2
    p=0 f=1 sk=0
  fi
  sum_pass+=p; sum_fail+=f; sum_skip+=sk
  if [[ "$f" -eq 0 ]]; then laps_passed+=1; fi
  echo "lap $lap: pass=$p fail=$f skip=$sk report=$latest"
done

echo "=== aggregate ==="
echo "laps passed $laps_passed/$LAPS (with a report: $laps_total)"
echo "cases pass=$sum_pass fail=$sum_fail skip=$sum_skip"
if [[ "$laps_passed" -lt "$LAPS" ]]; then
  echo "playtest_repeat: FAIL (not all laps clean)" >&2
  exit 1
fi
echo "playtest_repeat: PASS"
exit 0
