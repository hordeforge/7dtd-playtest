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
# Env: PLAYTEST_LAPS (default 3), PLAYTEST_SUITE (default demo),
#      LOGDIR (default ~/.cache/7dtd-playtest).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCH="$HERE/playtest_run.py"
SUITE="${PLAYTEST_SUITE:-demo}"
LAPS="${PLAYTEST_LAPS:-3}"
REPORT_DIR="${LOGDIR:-$HOME/.cache/7dtd-playtest}"
ORCH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --laps) LAPS="$2"; shift 2 ;;
    --suite) SUITE="$2"; shift 2 ;;
    *) ORCH_ARGS+=("$1"); shift ;;
  esac
done

if [[ ! -x "$ORCH" ]]; then
  echo "playtest_repeat: orchestrator not found at $ORCH" >&2
  exit 2
fi

echo "playtest_repeat: suite=$SUITE laps=$LAPS report_dir=$REPORT_DIR"
declare -i laps_passed=0 laps_total=0
declare -i sum_pass=0 sum_fail=0 sum_skip=0
declare -a reports=()

for lap in $(seq 1 "$LAPS"); do
  echo "=== lap $lap/$LAPS ==="
  # Only reports written during this lap count; a lap that dies before scoring
  # (lock refused, missing binary) must not inherit an older run's report.
  lap_start="$(date +%s)"
  if ! python3 "$ORCH" --suite "$SUITE" "${ORCH_ARGS[@]}"; then
    echo "playtest_repeat: lap $lap failed (orchestrator exit != 0)"
    continue
  fi
  # Newest report for this lap (report-<epoch>.json, mtime >= lap start).
  # NUL-delimited so paths with spaces/newlines stay one argument.
  latest="$(
    find "$REPORT_DIR" -maxdepth 1 -name 'report-*.json' -newermt "@$lap_start" \
      -print0 2>/dev/null | xargs -0 -r ls -t 2>/dev/null | head -n 1 || true
  )"
  if [[ -z "$latest" ]]; then
    echo "playtest_repeat: lap $lap produced no report under $REPORT_DIR" >&2
    continue
  fi
  reports+=("$latest")
  laps_total+=1
  # One python launch reads pass/fail/skip together; path is passed as argv[1],
  # never interpolated into the Python source (a quote in $latest must not
  # become code execution).
  summary_line="$(
    python3 -c 'import json,sys; s=json.load(open(sys.argv[1]))["summary"]; print(s.get("pass",0), s.get("fail",0), s.get("skip",0))' "$latest" 2>/dev/null \
      || echo "0 0 0"
  )"
  read -r p f sk <<<"$summary_line"
  sum_pass+=p; sum_fail+=f; sum_skip+=sk
  if [[ "$f" -eq 0 ]]; then laps_passed+=1; fi
  echo "lap $lap: pass=$p fail=$f skip=$sk report=$latest"
done

echo "=== aggregate ==="
echo "laps passed $laps_passed/$LAPS (with a report: $laps_total)"
echo "cases pass=$sum_pass fail=$sum_fail skip=$sum_skip"
if [[ "$laps_passed" -lt "$LAPS" ]]; then
  echo "playtest_repeat: FAIL (not all laps clean)"
  exit 1
fi
echo "playtest_repeat: PASS"
exit 0
