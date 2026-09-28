#!/usr/bin/env python3
"""Stock-vs-zdtd playtest comparison.

Runs nothing itself: it diffs the per-case results of two playtest_run.py runs
(same suite, one against the stock dedicated server, one against zdtd) into a
machine-readable report. A per-case delta (PASS on one side, FAIL/SKIP on the
other, or a case present on only one side) is a FINDING to triage (zdtd bug vs
harness artifact vs known divergence), never a pass to fake.

Inputs are either the orchestrator's report JSONs (report-*.json, globbed from
a --*-dir) or raw client logs with [7dtd-playtest] result lines.

Usage:
  playtest_compare.py --stock-dir <dir> --zdtd-dir <dir> [--out <dir>]
  playtest_compare.py --stock <file> --zdtd <file> [--out <dir>]

Exit codes:
  0  comparison written
  1  neither side had a playtest result line (one empty side still diffs)
  2  a side has no input (side never ran, logs wiped, or a bad path)
  3  inputs older than --require-fresh-minutes
  4  comparison outputs could not be written
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from playtest_log import parse_client_log  # noqa: E402

# Roughly 1900 years. No run report is ever that old, and the value is
# scaled into float seconds, so a larger one is a typo rather than a window.
MAX_FRESHNESS_MINUTES = 10**9


class CompareError(ValueError):
    """A comparison input the tool refuses to diff."""


def write_text_atomic(path: Path, text: str) -> None:
    """Publish ``text`` at ``path`` so no reader sees a partial document.

    The orchestrator has its own copy (playtest_run.write_text_atomic); this
    CLI runs standalone, and its outputs are the committed baselines, so a
    crash must leave the previous generation intact rather than a truncated
    one that still parses as a diff.
    """
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _count(value: object) -> int:
    """A summary count as an int; anything else (bool included) is 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _summary_counts(value: object) -> dict[str, int]:
    """``{pass, fail, skip}`` counts out of whatever a side reported.

    Absent or malformed counts read as 0, which is what an unrun case counts
    as; they are a cost axis in the report, never a verdict.
    """
    if not isinstance(value, dict):
        return {"pass": 0, "fail": 0, "skip": 0}
    return {k: _count(value.get(k, 0)) for k in ("pass", "fail", "skip")}


def _text(value: object) -> str:
    """Any JSON value as the text a log-derived field renders as."""
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _wall_seconds(value: object) -> float | None:
    """``wall_sec`` as a finite float, or None when absent or unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _result_rows(value: object) -> list[dict[str, str]]:
    """Result rows as ``{case, status, detail}`` string dicts.

    This is the untrusted boundary of the tool: a report JSON is a file on
    disk, not a value this repo produced in this process, so a truncated run,
    a hand-built fixture, or another tool's output can put any JSON value
    where a row, a case id, a status or a detail belongs. Coercing once here
    is what lets the diff below read strings; a row that is not an object at
    all keeps its place as an unnamed case (``?``) instead of ending the run.
    """
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    for raw in value:
        row = raw if isinstance(raw, dict) else {}
        case = row.get("case")
        if not isinstance(case, str) or not case:
            case = "?"
        status = row.get("status", "MISSING")
        detail = row.get("detail")
        rows.append(
            {
                "case": case,
                "status": _text(status),
                "detail": _text(detail),
            }
        )
    return rows


def load_results(path: Path) -> dict:
    """Return {"results": [...], "summary": {...}, "wall": s|None, "server": str|None,
    "ran_epoch": s|None} from a report JSON or a log. wall is the orchestrator's
    wall_sec (server session wall time), reported as a cost axis, never a
    per-case finding. A log input adds "nre_like" and leaves wall/server/
    ran_epoch None. Results, summary and wall are coerced to the shapes main()
    reads (see :func:`_result_rows`): both inputs are files."""
    # One read feeds both decoders: a second read after the JSON attempt can
    # fail (file replaced/removed between reads) and crash the diff on input
    # the first read already saw.
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as ex:
        print(f"ERROR: cannot read {path}: {ex}", file=sys.stderr)
        text = ""
    payload = None
    if text:
        try:
            loaded = json.loads(text)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict) and "results" in loaded:
            payload = loaded
    if payload is not None:
        return {"results": _result_rows(payload["results"]),
                "summary": _summary_counts(payload.get("summary")),
                "wall": _wall_seconds(payload.get("wall_sec")),
                "server": payload.get("server"),
                "ran_epoch": payload.get("ran_epoch")}
    parsed = parse_client_log(text)
    return {"results": _result_rows(parsed["results"]),
            "summary": _summary_counts(parsed["summary"]),
            "nre_like": parsed["nre_like"], "wall": None, "server": None,
            "ran_epoch": None}


# Characters that cannot appear inside a markdown table cell or list item:
# C0 controls including CR/LF/TAB (a newline would break the row into
# arbitrary markdown), DEL, and C1 controls. They are dropped before the
# structural escapes in md_cell. Result rows are parsed back out of client
# log bytes, so this is the same boundary playtest_run.xml_attr guards for
# the JUnit artifact; the markdown writer was the one report surface left
# without it.
_MD_ILLEGAL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")


def md_cell(text: str) -> str:
    """Make log-derived text safe inside one markdown cell or list entry.

    Case ids come from client-log result lines (the JSON event path carries
    any string, including real newlines via \\n escapes), so structural
    characters must not reach playtest-compare.md raw: an unescaped pipe
    breaks out of the table cell and a newline lets a crafted row author
    arbitrary markdown below it, which renders when the report is viewed.
    """
    return (
        _MD_ILLEGAL_RE.sub("", str(text))
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "'")
    )


def ran_epoch_of(path: Path, res: dict) -> float | None:
    """Best-known run epoch for a side: payload field, report-<epoch>.json
    filename, or file mtime as a last resort. None when unknown."""
    reported = res.get("ran_epoch")
    if reported is not None and not isinstance(reported, bool):
        try:
            epoch = float(reported)
        except (TypeError, ValueError, OverflowError):
            epoch = None
        if epoch is not None and math.isfinite(epoch):
            return epoch
    m = re.match(r"report-(\d+)\.json$", path.name)
    if m:
        epoch = float(m.group(1))
        return epoch if math.isfinite(epoch) else None
    try:
        epoch = path.stat().st_mtime
        return epoch if math.isfinite(epoch) else None
    except OSError:
        return None


def fmt_utc(epoch: float | None) -> str:
    if epoch is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%MZ")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def non_negative_int(text: str) -> int:
    """argparse type for a count where zero explicitly disables the feature."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be non-negative, got {text!r}")
    return value


def freshness_minutes(text: str) -> int:
    """argparse type for ``--require-fresh-minutes``.

    The value is scaled into seconds with ``* 60.0``, and a Python int is
    unbounded, so a value past the float range raised OverflowError out of
    the comparison instead of the usage error every other bad value gets.
    """
    value = non_negative_int(text)
    if value > MAX_FRESHNESS_MINUTES:
        raise argparse.ArgumentTypeError(
            f"must be at most {MAX_FRESHNESS_MINUTES} minutes, got {text!r}"
        )
    return value


def newest_report(d: Path) -> Path | None:
    if d.is_file():
        return d
    # Name as tie-break: equal mtimes must not let readdir order decide
    # which run's evidence gets diffed. A report removed between glob and
    # stat sorts as oldest instead of crashing the whole comparison.
    def mtime(p: Path) -> tuple[float, str]:
        try:
            return (p.stat().st_mtime, p.name)
        except OSError:
            return (float("-inf"), p.name)

    cands = sorted(d.glob("report-*.json"), key=mtime)
    return cands[-1] if cands else None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--stock", type=Path, default=None,
                    help="stock side input: report JSON or client log file")
    ap.add_argument("--zdtd", type=Path, default=None,
                    help="zdtd side input: report JSON or client log file")
    ap.add_argument("--stock-dir", type=Path, default=None,
                    help="diff the newest report-*.json under this stock dir")
    ap.add_argument("--zdtd-dir", type=Path, default=None,
                    help="diff the newest report-*.json under this zdtd dir")
    ap.add_argument("--out", type=Path, default=Path("."),
                    help="directory for playtest-compare.{md,json} (default .)")
    ap.add_argument("--require-fresh-minutes", type=freshness_minutes, default=0,
                    help="refuse to diff a side whose run is older than this "
                         "many minutes (0 disables the check)")
    args = ap.parse_args()

    stock_path, stock_flag = (
        (args.stock, "--stock")
        if args.stock is not None
        else (
            (newest_report(args.stock_dir), "--stock-dir")
            if args.stock_dir
            else (None, "--stock")
        )
    )
    zdtd_path, zdtd_flag = (
        (args.zdtd, "--zdtd") if args.zdtd is not None
        else ((newest_report(args.zdtd_dir), "--zdtd-dir") if args.zdtd_dir else (None, "--zdtd"))
    )
    if stock_path is None or zdtd_path is None:
        missing = [s for s, p in (("stock", stock_path), ("zdtd", zdtd_path)) if p is None]
        print(f"ERROR: no report found on the {', '.join(missing)} side; the side "
              "either failed to start or its logs were wiped before the run. "
              "Refusing to diff missing or stale evidence.",
              file=sys.stderr)
        return 2
    # A path that does not name a readable file must fail like every other
    # unusable input (exit 2 with the offending flag named), never as a
    # FileNotFoundError traceback from load_results.
    unreadable = [
        f"{flag} {path}: not a readable file"
        for flag, path in (
            (stock_flag, stock_path),
            (zdtd_flag, zdtd_path),
        )
        if not path.is_file()
    ]
    if unreadable:
        print("ERROR: " + "; ".join(unreadable) + "; refusing to diff.",
              file=sys.stderr)
        return 2

    try:
        stock = load_results(stock_path)
        zdtd = load_results(zdtd_path)
    except CompareError as ex:
        print(f"ERROR: {ex}; refusing to diff.", file=sys.stderr)
        return 2
    if not stock["results"] and not zdtd["results"]:
        print("ERROR: neither side had a playtest result line", file=sys.stderr)
        return 1

    if args.require_fresh_minutes:
        now = time.time()
        limit = args.require_fresh_minutes * 60.0
        stale = []
        for side, path, res in (("stock", stock_path, stock), ("zdtd", zdtd_path, zdtd)):
            epoch = ran_epoch_of(path, res)
            # A far-future ran_epoch (year 2099, 1e18) makes now-epoch negative,
            # so the old `now - epoch > limit` test treated it as fresh.
            age = None if epoch is None else now - epoch
            if (
                age is None
                or not math.isfinite(age)
                or abs(age) > limit
            ):
                age_s = "unknown" if age is None or not math.isfinite(age) else f"{int(age)}s"
                stale.append(f"{side} ({path.name}, age {age_s})")
        if stale:
            print(f"ERROR: comparison inputs are stale (--require-fresh-minutes "
                  f"{args.require_fresh_minutes}): " + "; ".join(stale),
                  file=sys.stderr)
            return 3

    def by_case(res: dict[str, Any]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in res["results"]:
            # load_results already coerced every row to a non-empty string
            # case id, so the sort below never sees a str/int key mix.
            out.setdefault(r["case"], []).append(r)
        return out

    scases, zcases = by_case(stock), by_case(zdtd)
    rows: list[dict[str, Any]] = []
    findings = []
    for case in sorted(set(scases) | set(zcases)):
        s = scases.get(case, [{}])[-1]
        z = zcases.get(case, [{}])[-1]
        s_st, z_st = s.get("status", "MISSING"), z.get("status", "MISSING")
        rows.append({
            "case": case,
            "stock": {"status": s_st, "detail": (s.get("detail") or "")[:120]},
            "zdtd": {"status": z_st, "detail": (z.get("detail") or "")[:120]},
        })
        if s_st != z_st:
            # Findings are display prose shared by the md and JSON payloads;
            # md_cell keeps a log-derived id or status from authoring markdown
            # there, since both come out of the client's own bytes.
            shown = md_cell(case)
            s_shown, z_shown = md_cell(s_st), md_cell(z_st)
            if "MISSING" in (s_st, z_st):
                findings.append(f"{shown}: ran only on "
                                f"{'stock' if z_st == 'MISSING' else 'zdtd'} "
                                f"({s_shown} vs {z_shown})")
            else:
                findings.append(f"{shown}: status differs ({s_shown} vs {z_shown})")

    ss, zs = stock["summary"], zdtd["summary"]
    wall = {"stock": stock.get("wall"), "zdtd": zdtd.get("wall")}
    ran_at = {"stock": ran_epoch_of(stock_path, stock),
              "zdtd": ran_epoch_of(zdtd_path, zdtd)}
    payload = {
        "compared": bool(stock["results"] and zdtd["results"]),
        "stock": {"summary": ss, "nreLike": len(stock.get("nre_like", [])),
                  "wall": wall["stock"], "server": stock.get("server"),
                  "ranAtUtc": fmt_utc(ran_at["stock"])},
        "zdtd": {"summary": zs, "nreLike": len(zdtd.get("nre_like", [])),
                 "wall": wall["zdtd"], "server": zdtd.get("server"),
                 "ranAtUtc": fmt_utc(ran_at["zdtd"])},
        "findings": findings,
        "cases": rows,
    }

    lines = ["# Stock-vs-zdtd playtest comparison\n"]
    lines.append("| axis | stock | zdtd |")
    lines.append("|---|---|---|")
    lines.append(f"| ran (UTC) | {fmt_utc(ran_at['stock'])} | {fmt_utc(ran_at['zdtd'])} |")
    lines.append(f"| cases PASS | {ss['pass']} | {zs['pass']} |")
    lines.append(f"| cases FAIL | {ss['fail']} | {zs['fail']} |")
    lines.append(f"| cases SKIP | {ss['skip']} | {zs['skip']} |")
    if wall["stock"] is not None or wall["zdtd"] is not None:

        def wf(v: float | None) -> str:
            return f"{v:.1f}" if v is not None else "n/a"

        lines.append(f"| wall time (s) | {wf(wall['stock'])} | {wf(wall['zdtd'])} |")
    if stock.get("nre_like") or zdtd.get("nre_like"):
        lines.append(f"| client NRE-like hits | {len(stock.get('nre_like', []))} | "
                     f"{len(zdtd.get('nre_like', []))} |")
    lines.append("\n## Per-case\n")
    lines.append("| case | stock | zdtd |")
    lines.append("|---|---|---|")
    for r in rows:
        lines.append(
            f"| `{md_cell(r['case'])}` | {md_cell(r['stock']['status'])} "
            f"| {md_cell(r['zdtd']['status'])} |"
        )
    lines.append("\n## Findings\n")
    if findings:
        for f in findings:
            lines.append(f"- {f}")
    else:
        lines.append("- no per-case status differences")
    lines.append("\n*Triage each finding: zdtd bug vs harness artifact vs known "
                 "divergence. Known divergences are recorded in "
                 "zdtd-server/docs/PROVENANCE.md (divergence register).*")

    try:
        args.out.mkdir(parents=True, exist_ok=True)
        # The default --out is the committed workspace/ baseline dir, so these
        # two files are the only durable record of a comparison once the
        # gitignored report-*.json inputs are pruned. A plain write_text
        # truncates first: a kill mid-write leaves a half document that git
        # records as a valid baseline, with no other copy to compare against.
        write_text_atomic(args.out / "playtest-compare.md",
                          "\n".join(lines) + "\n")
        write_text_atomic(args.out / "playtest-compare.json",
                          json.dumps(payload, indent=1, sort_keys=True))
    except OSError as ex:
        # An unwritable --out must not fall through to a traceback with
        # Python's default exit 1, which this CLI documents as "no playtest
        # result lines found". Name the destination and exit 4 instead.
        print(f"ERROR: cannot write comparison outputs under {args.out}: {ex}",
              file=sys.stderr)
        return 4
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
