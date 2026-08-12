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
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playtest_run import parse_client_log


def load_results(path: Path) -> dict:
    """Return {"results": [...], "summary": {...}} from a report JSON or a log."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(payload, dict) and "results" in payload:
            return {"results": payload["results"], "summary": payload.get("summary")}
    except (ValueError, OSError):
        pass
    parsed = parse_client_log(path.read_text(encoding="utf-8", errors="replace"))
    return {"results": parsed["results"], "summary": parsed["summary"],
            "nre_like": parsed["nre_like"]}


def newest_report(d: Path) -> Path | None:
    if d.is_file():
        return d
    cands = sorted(d.glob("report-*.json"), key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stock", type=Path, default=None)
    ap.add_argument("--zdtd", type=Path, default=None)
    ap.add_argument("--stock-dir", type=Path, default=None)
    ap.add_argument("--zdtd-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=Path("."))
    args = ap.parse_args()

    stock_path = args.stock or (newest_report(args.stock_dir) if args.stock_dir else None)
    zdtd_path = args.zdtd or (newest_report(args.zdtd_dir) if args.zdtd_dir else None)
    if stock_path is None or zdtd_path is None:
        print("ERROR: need both sides (--stock/--stock-dir and --zdtd/--zdtd-dir)",
              file=sys.stderr)
        return 2

    stock = load_results(stock_path)
    zdtd = load_results(zdtd_path)
    if not stock["results"] and not zdtd["results"]:
        print("ERROR: no playtest result lines on either side", file=sys.stderr)
        return 1

    def by_case(res):
        out = {}
        for r in res["results"]:
            case = r.get("case") or "?"
            out.setdefault(case, []).append(r)
        return out

    scases, zcases = by_case(stock), by_case(zdtd)
    rows = []
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
            if "MISSING" in (s_st, z_st):
                findings.append(f"{case}: ran only on "
                                f"{'stock' if z_st == 'MISSING' else 'zdtd'} "
                                f"({s_st} vs {z_st})")
            else:
                findings.append(f"{case}: status differs ({s_st} vs {z_st})")

    def summary(res):
        s = res.get("summary") or {}
        return {"pass": s.get("pass", 0), "fail": s.get("fail", 0),
                "skip": s.get("skip", 0)}

    ss, zs = summary(stock), summary(zdtd)
    payload = {
        "compared": bool(stock["results"] and zdtd["results"]),
        "stock": {"summary": ss, "nreLike": len(stock.get("nre_like", []))},
        "zdtd": {"summary": zs, "nreLike": len(zdtd.get("nre_like", []))},
        "findings": findings,
        "cases": rows,
    }

    lines = ["# Stock-vs-zdtd playtest comparison\n"]
    lines.append("| axis | stock | zdtd |")
    lines.append("|---|---|---|")
    lines.append(f"| cases PASS | {ss['pass']} | {zs['pass']} |")
    lines.append(f"| cases FAIL | {ss['fail']} | {zs['fail']} |")
    lines.append(f"| cases SKIP | {ss['skip']} | {zs['skip']} |")
    if stock.get("nre_like") or zdtd.get("nre_like"):
        lines.append(f"| client NRE-like hits | {len(stock.get('nre_like', []))} | "
                     f"{len(zdtd.get('nre_like', []))} |")
    lines.append("\n## Per-case\n")
    lines.append("| case | stock | zdtd |")
    lines.append("|---|---|---|")
    for r in rows:
        lines.append(f"| `{r['case']}` | {r['stock']['status']} | {r['zdtd']['status']} |")
    lines.append("\n## Findings\n")
    if findings:
        for f in findings:
            lines.append(f"- {f}")
    else:
        lines.append("- no per-case status differences")
    lines.append("\n*Triage each finding: zdtd bug vs harness artifact vs known "
                 "divergence. Known divergences are recorded in "
                 "zdtd/docs/PROVENANCE.md (divergence register).*")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "playtest-compare.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (args.out / "playtest-compare.json").write_text(
        json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
