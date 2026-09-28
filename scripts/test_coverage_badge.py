#!/usr/bin/env python3
"""Offline gate: the coverage badge renders, fails closed, and leaves no litter.

`make coverage` measures the gates and CI renders the result into the README
badge through this script, so it is the only place a coverage number becomes
something a reader believes. Three things therefore have to hold: the
percentage really is the one the measured run produced, every exit code in
its own usage line is the one it returns, and the scratch `.coverage.json`
never survives the call (a `json.loads` or `coverage json` failure would
otherwise leave one in the project root, where the next run picks up a stale
report and publishes last month's number).

The gates drive the shipped functions, not a reimplementation of them, and
substitute only the one thing a test cannot have: a measured `.coverage` file.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import cast
from unittest import mock

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import coverage_badge as cb  # noqa: E402

SCRATCH = ".coverage.json"


def cli(*args: str) -> int:
    """``main`` with the argv shape the script is actually invoked with.

    main() reads argv[1:], so calling it with the arguments alone would test
    the wrong slice and every well-formed invocation would read as bad usage.
    """
    return cb.main(["coverage_badge.py", *args])


def _fake_coverage_json(percent: float) -> AbstractContextManager[None]:
    """A `coverage json` that renders ``percent`` the way the real one does.

    Only the measured report is substituted; the call that reads it, rounds
    it and unlinks it is the shipped one.
    """
    rendered: dict[str, object] = {"totals": {"percent_covered": percent}}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(json.dumps(rendered), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    return cast(
        "AbstractContextManager[None]", mock.patch.object(subprocess, "run", run)
    )


def test_colour_bands_cover_every_percentage() -> None:
    """Each band's lower bound has its own colour, and the top is green.

    Boundaries are inclusive from below, so 89 is not the 90 band and 39 is
    not the 40 band; a reader watching a drop from 90 to 89 should see it.
    """
    for pct, want in (
        (100, "#4c1"),
        (90, "#4c1"),
        (89, "#97ca00"),
        (75, "#97ca00"),
        (74, "#dfb317"),
        (60, "#dfb317"),
        (59, "#fe7d37"),
        (40, "#fe7d37"),
        (39, "#e05d44"),
        (0, "#e05d44"),
    ):
        got = cb.colour(pct)
        assert got == want, f"{pct}% must render {want}, got {got}"


def test_badge_names_the_percentage_it_was_given() -> None:
    """The rendered SVG carries the number in its title, label and text.

    The accessible name is the only thing a screen reader gets, so a badge
    whose visible number and label disagree is a wrong claim rather than a
    cosmetic one.
    """
    svg = cb.badge(87, cb.colour(87))
    assert svg.count("<title>coverage: 87%</title>") == 1, svg
    assert 'aria-label="coverage: 87%"' in svg, svg
    assert ">87%</text>" in svg, svg
    assert "#97ca00" in svg, "the band colour must reach the rendered badge"
    assert svg.startswith("<svg ") and svg.rstrip().endswith("</svg>"), svg


def test_percentage_rounds_the_measured_total_and_removes_the_scratch() -> None:
    """The number comes from the measured file, and the scratch does not stay."""
    for reported, want in ((0.0, 0), (49.6, 50), (87.4, 87), (99.5, 100)):
        with tempfile.TemporaryDirectory(prefix="playtest-badge-") as td:
            with contextlib.chdir(td), _fake_coverage_json(reported):
                got = cb.percentage()
            assert got == want, f"{reported}% must report {want}, got {got}"
            assert not Path(td, SCRATCH).exists(), (
                f"{SCRATCH} survived a successful read in {td}"
            )


def test_scratch_is_removed_even_when_the_read_fails() -> None:
    """A failed `coverage json` must not leave a stale report in the root.

    Without the cleanup, a crashed run leaves a `.coverage.json` the next run
    overwrites but a partially-read one could survive, and the badge then
    publishes a number from an earlier measurement.
    """
    with tempfile.TemporaryDirectory(prefix="playtest-badge-") as td:
        stale = Path(td, SCRATCH)
        stale.write_text("{ not json", encoding="utf-8")
        with (
            contextlib.chdir(td),
            _fake_coverage_json(0.0),
            mock.patch.object(json, "loads", side_effect=ValueError("bad json")),
        ):
            try:
                cb.percentage()
            except ValueError:
                pass
            else:
                raise AssertionError("a malformed .coverage.json was accepted")
        assert not stale.exists(), f"{SCRATCH} survived a failed read in {td}"


def test_main_exit_codes_match_the_usage_line() -> None:
    """0 written, 1 no coverage data, 2 bad usage, per the script's own help."""
    with tempfile.TemporaryDirectory(prefix="playtest-badge-") as td:
        out = Path(td) / "badge.svg"
        with contextlib.chdir(td), _fake_coverage_json(91.2):
            assert cli(str(out)) == 0, "a measured run must write the badge"
        assert out.is_file(), "the badge was not written"
        assert "91%" in out.read_text(encoding="utf-8"), "the measured value is not shown"

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            assert cli("-h") == 0, "--help is not a failure"
        assert cb.USAGE.splitlines()[0] in stdout.getvalue(), stdout.getvalue()

        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            assert cli() == 2, "no output path is bad usage"
        assert cb.USAGE.strip() in stderr.getvalue(), stderr.getvalue()
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            assert cli(str(out), "extra.svg") == 2, "two output paths is bad usage"
        assert cb.USAGE.strip() in stderr.getvalue(), stderr.getvalue()


def test_main_reports_a_failed_measurement_distinctly_from_bad_usage() -> None:
    """No coverage data is exit 1 with the reason, not a traceback.

    The two failure shapes need different fixes (run `make coverage`, fix the
    command line), so they are told apart by exit code and by message.
    """
    with tempfile.TemporaryDirectory(prefix="playtest-badge-") as td:
        out = Path(td) / "badge.svg"
        with contextlib.chdir(td), mock.patch.object(
            cb, "percentage", side_effect=OSError("no such file: .coverage")
        ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            assert cli(str(out)) == 1, "missing coverage data must be exit 1"
        assert "no such file" in stderr.getvalue(), stderr.getvalue()
        assert not out.exists(), "a failed measurement must write no badge"

        failed = subprocess.CalledProcessError(
            returncode=1, cmd=["coverage", "json"]
        )
        with contextlib.chdir(td), mock.patch.object(
            cb, "percentage", side_effect=failed
        ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            assert cli(str(out)) == 1, "a failed `coverage json` must be exit 1"
        assert "make coverage" in stderr.getvalue(), stderr.getvalue()
        assert not out.exists(), "a failed measurement must write no badge"


CASES: tuple[tuple[str, Callable[[], None]], ...] = (
    ("badge_colour_bands", test_colour_bands_cover_every_percentage),
    ("badge_svg_names_its_percentage", test_badge_names_the_percentage_it_was_given),
    (
        "badge_percentage_rounds_and_cleans",
        test_percentage_rounds_the_measured_total_and_removes_the_scratch,
    ),
    ("badge_scratch_removed_on_failure", test_scratch_is_removed_even_when_the_read_fails),
    ("badge_main_exit_codes", test_main_exit_codes_match_the_usage_line),
    (
        "badge_main_reports_failed_measurement",
        test_main_reports_a_failed_measurement_distinctly_from_bad_usage,
    ),
)


def main() -> int:
    failures = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as ex:
            failures += 1
            print(f"FAIL {name}: {ex}", file=sys.stderr)
    if failures:
        print(f"RESULT FAIL ({failures})", file=sys.stderr)
        return 1
    for name, _ in CASES:
        print(f"PASS {name}")
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
