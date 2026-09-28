#!/usr/bin/env python3
"""Structural regression checks for passive stock-peer orchestration.

Verifies that the orchestrator and README maintain the stock-peer contract:
paired arguments, distinct identity, connection-rate spacing, setup-suite
gating, shared-fixture teleport routing, environment isolation, lifecycle
cleanup, suite-done verdicting, and documentation.

The orchestrator is matched with its comments blanked out. A plain substring
search over the file also finds a contract that was written up, commented out,
or left behind by a refactor, so the check would pass against code that no
longer runs. Message strings the search needs are wrapped across lines in the
source and cannot be matched on the stripped text; those two are matched
against the raw file and say so where they are used.
"""
from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "scripts" / "playtest_run.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def without_comments(src: str) -> str:
    """Blank out every comment, preserving line numbers and column offsets.

    Blanking rather than deleting keeps ``.index`` and ``.splitlines`` results
    pointing at the same place in either text, so a failure names the line the
    live code is on.
    """
    lines = src.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.COMMENT:
            continue
        (start_row, start_col), (end_row, end_col) = tok.start, tok.end
        for row in range(start_row, end_row + 1):
            line = lines[row - 1]
            if row == start_row:
                line = line[:start_col] + (line[end_col:] if row == end_row else "")
            elif row == end_row:
                line = " " * end_col + line[end_col:]
            else:
                line = " " * len(line)
            lines[row - 1] = line
    return "".join(lines)


RUNNER_CODE = without_comments(RUNNER)


def test_peer_arguments_require_isolated_profile() -> None:
    """Paired --peer-client-name and --peer-client-compat must require both."""
    assert '"--peer-client-name"' in RUNNER_CODE
    assert '"--peer-client-compat"' in RUNNER_CODE
    # argparse wraps this message across lines in the source, so it only
    # matches the raw text.
    assert "must be provided together" in RUNNER


def test_peer_gets_distinct_stock_local_identity() -> None:
    """The peer receives its own player-name via the env."""
    assert '"7DTD_PLAYER_NAME": peer_client_name' in RUNNER_CODE


def test_peer_spacing_past_engine_rate_limit() -> None:
    """Same-IP stock clients must be spaced past the engine's rate limit.

    Staggering the launches is not enough: the engine rejects same-IP connects
    less than 500 ms apart, and two clients booting from identical instances
    reach the menu together however the launches were spaced. A one-second head
    start evaporated and the peer was rejected with ConnectionRejected, while
    the suite still passed on the primary alone. The peer therefore waits for
    the primary to be in the world, with the sleep kept as a floor for the case
    where that marker never arrives.
    """
    # The rationale the spacing rests on is prose in a comment by design; only
    # the two mechanical facts below are pinned against live code.
    assert "rejects same-IP connection attempts" in RUNNER
    assert "PEER_STAGGER_MARKER" in RUNNER_CODE
    # Client-side: this is read out of the primary's own log, and the server's
    # word for the same moment (PlayerSpawnedInWorld) never appears there, so
    # waiting on it timed out every run.
    assert 'PEER_STAGGER_MARKER = "Respawning: EnterMultiplayer"' in RUNNER
    assert "wait_file_contains(" in RUNNER
    # The stagger floor goes through the injected clock, so a simulation can
    # run this wait without spending the second. What is pinned here is the
    # ordering, not which clock API the file happens to call.
    assert "pause(1.0)" in RUNNER
    # Both the wait and the floor precede the peer launch.
    peer_launch = RUNNER.index('"7DTD_PLAYER_NAME": peer_client_name')
    assert RUNNER.index("PEER_STAGGER_TIMEOUT_SEC\n") < peer_launch
    assert RUNNER.index("pause(1.0)") < peer_launch


def test_peer_can_run_explicit_provider_setup_suite() -> None:
    """--peer-client-suite gates a setup suite on the peer side."""
    assert '"--peer-client-suite"' in RUNNER_CODE
    assert "run_suite=bool(peer_client_suite)" in RUNNER_CODE


def test_peer_ready_state_gates_shared_fixture_teleport() -> None:
    """Peer ready state gates a shared fixture teleport through the common
    teleport helper, not a separate code path."""
    assert '"--peer-client-teleport"' in RUNNER_CODE
    assert "peer_ready_seen" in RUNNER_CODE
    assert "moved, connected = teleport_all_players_via_telnet(" in RUNNER_CODE
    assert "args.peer_client_teleport" in RUNNER_CODE
    assert "tn.teleport_players_to(*coords) if connected else 0" in RUNNER_CODE


def test_peer_clears_inherited_scenario_environment() -> None:
    """The peer must pop inherited scenario env keys to avoid cross-contamination."""
    assert "run_suite: bool = True" in RUNNER_CODE
    assert 'env.pop(key, None)' in RUNNER_CODE
    assert "run_suite=bool(peer_client_suite)" in RUNNER_CODE


def test_peer_lifecycle_cleaned_with_primary_client() -> None:
    """The peer process must be stopped during primary-client cleanup."""
    assert "stop_proc(peer_client_proc)" in RUNNER_CODE


def test_peer_scenario_suite_must_finish_before_green() -> None:
    """A peer scenario suite must produce DONE before the run is green."""
    assert "peer_suite_done()" in RUNNER_CODE
    assert "saw DONE in every scenario client log" in RUNNER_CODE
    assert '"peer_done": peer_done' in RUNNER_CODE


def test_peer_scenario_failures_affect_exit_status() -> None:
    """Peer scenario failures must propagate to the orchestrator exit status."""
    assert "peer_summary" in RUNNER_CODE
    assert "peer_results" in RUNNER_CODE
    assert 'int(peer_summary["fail"])' in RUNNER_CODE


def test_readme_distinguishes_stock_peer_from_loadgen() -> None:
    """The README must clearly distinguish the stock peer from loadgen."""
    assert "passive **stock** peer" in README
    assert "not a loadgen bot" in README
    assert "--peer-client-suite" in README
    assert "500 ms same-IP connection limiter" in README


def main() -> int:
    tests = [
        test_peer_arguments_require_isolated_profile,
        test_peer_gets_distinct_stock_local_identity,
        test_peer_spacing_past_engine_rate_limit,
        test_peer_can_run_explicit_provider_setup_suite,
        test_peer_ready_state_gates_shared_fixture_teleport,
        test_peer_clears_inherited_scenario_environment,
        test_peer_lifecycle_cleaned_with_primary_client,
        test_peer_scenario_suite_must_finish_before_green,
        test_peer_scenario_failures_affect_exit_status,
        test_readme_distinguishes_stock_peer_from_loadgen,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as ex:
            failures += 1
            print(f"FAIL {fn.__name__}: {ex}", file=sys.stderr)
    if failures:
        print(f"RESULT FAIL ({failures})", file=sys.stderr)
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
