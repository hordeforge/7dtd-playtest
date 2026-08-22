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
#      LOGDIR (default ~/.cache/7dtd-playtest; also passed to the orchestrator
#      as --logdir so laps write and aggregate in the same directory).
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
    --laps) LAPS="$2"; shift 2 ;;
    --suite) SUITE="$2"; shift 2 ;;
    --logdir) REPORT_DIR="$2"; shift 2 ;;
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

# Newest report for a lap (report-<epoch>.json). Pure bash: no ls -t parsing,
# paths with spaces survive.
latest_report() {
  local f newest=""
  for f in "$REPORT_DIR"/report-*.json; do
    [[ -f "$f" ]] || continue
    if [[ -z "$newest" || "$f" -nt "$newest" ]]; then
      newest="$f"
    fi
  done
  printf '%s' "$newest"
}

# "pass fail skip" from one report, read via argv (no path interpolation into code).
summary_counts() {
  python3 -c '
import json, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))["summary"]
print(int(s.get("pass", 0)), int(s.get("fail", 0)), int(s.get("skip", 0)))
' "$1" 2>/dev/null
}

for lap in $(seq 1 "$LAPS"); do
  echo "=== lap $lap/$LAPS ==="
  if ! uv run --project "$ROOT" python "$ORCH" --suite "$SUITE" --logdir "$REPORT_DIR" "${ORCH_ARGS[@]}"; then
    echo "playtest_repeat: lap $lap failed (orchestrator exit != 0)"
    continue
  fi
  latest="$(latest_report)"
  if [[ -z "$latest" ]]; then
    echo "playtest_repeat: lap $lap produced no report under $REPORT_DIR" >&2
    continue
  fi
  reports+=("$latest")
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
  echo "playtest_repeat: FAIL (not all laps clean)"
  exit 1
fi
echo "playtest_repeat: PASS"
exit 0
