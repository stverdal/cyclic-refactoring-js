#!/usr/bin/env python3
"""
extract_summary.py — Screenshot-friendly extraction of experiment results.

Designed to be run on the VM via RDP, producing compact terminal output
that can be captured in 2–3 screenshots. No source code is printed — only
aggregate metrics, statistical tests, and diff statistics.

Usage (from host/WSL or inside Docker):
    # Step 1: run the RQ table makers (if not already done)
    ./run_make_rq_tables.sh \
        --results-roots results --exp-ids expA \
        --repos-file repos.txt --cycles-file cycles_to_analyze.txt \
        --outdir analysis_out

    # Step 2: print the summary
    python3 scripts/extract_summary.py --outdir analysis_out

    # Optionally include diff statistics (patch file summaries):
    python3 scripts/extract_summary.py --outdir analysis_out \
        --results-roots results --exp-ids expA \
        --repos-file repos.txt --cycles-file cycles_to_analyze.txt

    # Show the N most interesting diffs (by lines changed):
    python3 scripts/extract_summary.py --outdir analysis_out \
        --results-roots results --exp-ids expA \
        --repos-file repos.txt --cycles-file cycles_to_analyze.txt \
        --top-diffs 5

    # Widen output for larger monitors:
    python3 scripts/extract_summary.py --outdir analysis_out --width 120
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── constants ───────────────────────────────────────────────────────────────
SEPARATOR_CHAR = "═"
THIN_SEP_CHAR = "─"


# ─── helpers ─────────────────────────────────────────────────────────────────
def load_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fv(val: Optional[str], decimals: int = 2) -> str:
    """Format a CSV value as a number, or '—' if missing."""
    if val is None or val == "" or val == "None":
        return "—"
    try:
        v = float(val)
        if math.isnan(v) or math.isinf(v):
            return "—"
        if v == int(v) and decimals == 0:
            return str(int(v))
        return f"{v:.{decimals}f}"
    except (ValueError, TypeError):
        return str(val)


def sig(val: Optional[str]) -> str:
    """Format a p-value with significance stars."""
    if val is None or val == "" or val == "None":
        return "—"
    try:
        p = float(val)
        if math.isnan(p):
            return "—"
        stars = ""
        if p < 0.001:
            stars = " ***"
        elif p < 0.01:
            stars = " **"
        elif p < 0.05:
            stars = " *"
        return f"{p:.4f}{stars}"
    except (ValueError, TypeError):
        return str(val)


def hline(width: int, char: str = SEPARATOR_CHAR) -> str:
    return char * width


def centered(text: str, width: int) -> str:
    return text.center(width)


def row_get(rows: List[Dict[str, str]], key: str, value: str) -> Optional[Dict[str, str]]:
    """Find the first row where row[key] == value."""
    for r in rows:
        if r.get(key, "").strip() == value:
            return r
    return None


# ─── diff statistics ─────────────────────────────────────────────────────────
def parse_diffstat(patch_path: Path) -> Optional[Dict[str, Any]]:
    """Parse a unified diff patch and return summary statistics."""
    if not patch_path.exists():
        return None
    try:
        text = patch_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if not text.strip():
        return None

    files_changed = set()
    lines_added = 0
    lines_removed = 0

    for line in text.splitlines():
        if line.startswith("diff --git"):
            # Extract b/path
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files_changed.add(parts[1].strip())
        elif line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1

    if not files_changed and lines_added == 0 and lines_removed == 0:
        return None

    return {
        "files": len(files_changed),
        "added": lines_added,
        "removed": lines_removed,
        "total_changed": lines_added + lines_removed,
        "file_list": sorted(files_changed),
    }


def collect_diff_stats(
    results_roots: List[str],
    exp_ids: List[str],
    repos_file: Path,
    cycles_file: Path,
) -> List[Dict[str, Any]]:
    """Collect diff statistics for all experiment units."""
    # Import branch_for from rq_utils (add table_makers to path)
    script_dir = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(script_dir / "table_makers"))
    from rq_utils import read_repos_file, parse_cycles, branch_for, map_roots_exps

    cfgs = map_roots_exps(results_roots, exp_ids)
    repos = read_repos_file(repos_file)
    cycles_map = parse_cycles(cycles_file)

    stats: List[Dict[str, Any]] = []

    for results_root, with_id, wo_id in cfgs:
        root = Path(results_root)
        for repo, baseline_branch, _src in repos:
            cids = cycles_map.get((repo, baseline_branch), [])
            for cid in cids:
                for cond_label, exp_label in [("with", with_id), ("without", wo_id)]:
                    branch = branch_for(exp_label, cid)
                    # Try pipeline layout (branches/) first, then flat layout
                    candidates = [
                        root / repo / "branches" / branch / "openhands" / "git_diff.patch",
                        root / repo / branch / "openhands" / "git_diff.patch",
                        root / repo / branch / "openhands" / "diff.patch",
                    ]
                    ds = None
                    for p in candidates:
                        ds = parse_diffstat(p)
                        if ds:
                            break

                    if ds:
                        ds["repo"] = repo
                        ds["cycle_id"] = cid
                        ds["condition"] = cond_label
                        ds["branch"] = branch
                        stats.append(ds)

    return stats


# ─── printers ────────────────────────────────────────────────────────────────
def print_rq1(outdir: Path, width: int) -> None:
    wv = load_csv(outdir / "rq1_with_vs_without.csv")
    pp = load_csv(outdir / "rq1_per_project.csv")

    if not wv:
        print("  [No RQ1 data found]\n")
        return

    r_with = row_get(wv, "Condition", "with")
    r_wo = row_get(wv, "Condition", "without")
    r_stats = row_get(wv, "Condition", "stats")

    print(hline(width))
    print(centered("RQ1: Structural Improvement  (WITH vs WITHOUT explanation)", width))
    print(hline(width))
    print()

    # ── Pooled summary ──
    if r_with and r_wo:
        print("  Pooled Results:")
        print(f"    {'':30s}  {'WITH':>12s}  {'WITHOUT':>12s}")
        print(f"    {THIN_SEP_CHAR * 58}")

        for label, key, dec in [
            ("Cycles attempted",   "n_total",  0),
            ("Cycles succeeded",   "n_success", 0),
            ("Success %",          "Success%",  1),
        ]:
            vw = fv(r_with.get(key), dec)
            vo = fv(r_wo.get(key), dec)
            print(f"    {label:30s}  {vw:>12s}  {vo:>12s}")

        print()
        print("  Δ Metrics (successful runs only):")
        print(f"    {'':30s}  {'WITH':>12s}  {'WITHOUT':>12s}")
        print(f"    {THIN_SEP_CHAR * 58}")
        for label, key_mean, key_std in [
            ("ΔEdges  mean±std",  "ΔEdges_success_mean",  "ΔEdges_success_std"),
            ("ΔNodes  mean±std",  "ΔNodes_success_mean",  "ΔNodes_success_std"),
            ("ΔLOC    mean±std",  "ΔLOC_success_mean",    "ΔLOC_success_std"),
        ]:
            mw = fv(r_with.get(key_mean)); sw = fv(r_with.get(key_std))
            mo = fv(r_wo.get(key_mean));   so = fv(r_wo.get(key_std))
            print(f"    {label:30s}  {mw:>5s}±{sw:<5s}  {mo:>5s}±{so:<5s}")

        print()
        print("  Safety:")
        ntr_w = fv(r_with.get("NoTestRegression%"), 1)
        ntr_o = fv(r_wo.get("NoTestRegression%"), 1)
        print(f"    {'NoTestRegression %':30s}  {ntr_w:>12s}  {ntr_o:>12s}")

        # Outcome breakdown
        print()
        print("  Outcome Breakdown (% of all runs):")
        print(f"    {'':30s}  {'WITH':>12s}  {'WITHOUT':>12s}")
        print(f"    {THIN_SEP_CHAR * 58}")
        for label, key in [
            ("Success",                "Success%"),
            ("Behavior regressed",     "BehaviorRegressed%"),
            ("Structure not improved", "StructureNotImproved%"),
            ("Both failed",            "BothFailed%"),
            ("Other error",            "OtherError%"),
        ]:
            vw = fv(r_with.get(key), 1)
            vo = fv(r_wo.get(key), 1)
            print(f"    {label:30s}  {vw:>11s}%  {vo:>11s}%")

    # ── Statistical tests ──
    if r_stats:
        print()
        print("  Statistical Tests:")
        print(f"    McNemar (two-sided)  p = {sig(r_stats.get('Success_p_McNemar_two_sided'))}")
        print(f"    McNemar (one-sided)  p = {sig(r_stats.get('Success_p_McNemar_one_sided'))}")
        print(f"    Wilcoxon ΔEdges      p = {sig(r_stats.get('ΔEdges_success_wilcoxon_p'))}")
        print()
        print(f"    Discordant pairs: b(with>without)={fv(r_stats.get('b_with_better'),0)}"
              f"  c(without>with)={fv(r_stats.get('c_without_better'),0)}")
        ws = fv(r_stats.get("with_win_share"), 3)
        ci_lo = fv(r_stats.get("with_win_share_ci_lo"), 3)
        ci_hi = fv(r_stats.get("with_win_share_ci_hi"), 3)
        print(f"    With-win share: {ws}  (95% CI: [{ci_lo}, {ci_hi}])")
        print(f"    Matched pairs: {fv(r_stats.get('pairs_matched'),0)}"
              f"  Both-success: {fv(r_stats.get('both_success'),0)}"
              f"  Both-fail: {fv(r_stats.get('both_fail'),0)}")

    # ── Per-project table ──
    if pp:
        print()
        print("  Per-Project Breakdown:")
        print(f"    {'Repo':25s} {'Cond':8s} {'n':>4s} {'Succ':>5s} {'S%':>7s}"
              f" {'ΔEdge':>7s} {'ΔNode':>7s} {'ΔLOC':>8s}")
        print(f"    {THIN_SEP_CHAR * 75}")
        for r in pp:
            repo = (r.get("repo") or "")[:24]
            cond = (r.get("Condition") or "")[:7]
            n = fv(r.get("n_total"), 0)
            ns = fv(r.get("n_success"), 0)
            sp = fv(r.get("Success%"), 1)
            de = fv(r.get("ΔEdges_success_mean"))
            dn = fv(r.get("ΔNodes_success_mean"))
            dl = fv(r.get("ΔLOC_success_mean"))
            print(f"    {repo:25s} {cond:8s} {n:>4s} {ns:>5s} {sp:>6s}%"
                  f" {de:>7s} {dn:>7s} {dl:>8s}")

    print()


def print_rq2(outdir: Path, width: int) -> None:
    overall = load_csv(outdir / "rq2_overall.csv")

    if not overall:
        print("  [No RQ2 data found]\n")
        return

    print(hline(width))
    print(centered("RQ2: Code Quality Deltas  (successful refactorings only)", width))
    print(hline(width))
    print()

    r_with = row_get(overall, "Condition", "with")
    r_wo = row_get(overall, "Condition", "without")
    r_pval = row_get(overall, "Condition", "p_vs_zero")

    metrics = [
        ("Δruff_issues",           "Lint issues (ruff)"),
        ("Δmi_avg",                "Maintainability (MI)"),
        ("Δcc_dplus_funcs",        "Complex funcs (CC D+)"),
        ("Δpyexam_arch",           "Arch smells"),
        ("Δpyexam_code",           "Code smells"),
        ("Δpyexam_struct",         "Structural smells"),
        ("Δbandit_high",           "Security (bandit)"),
        ("Δtest_pass_pct",         "Test pass %"),
        ("Δcoverage_line_percent", "Coverage %"),
        ("Δmypy_errors",           "Type errors (mypy)"),
    ]

    if r_with and r_wo:
        n_w = fv(r_with.get("n"), 0)
        n_o = fv(r_wo.get("n"), 0)
        print(f"  n(with)={n_w}  n(without)={n_o}")
        print()
        print(f"    {'Metric':25s}  {'WITH mean':>10s}  {'WITH std':>9s}"
              f"  {'W/O mean':>10s}  {'W/O std':>9s}"
              f"  {'p(with)':>10s}  {'p(w/o)':>10s}")
        print(f"    {THIN_SEP_CHAR * 80}")

        for delta_key, label in metrics:
            mw = fv((r_with or {}).get(f"{delta_key}_mean"))
            sw = fv((r_with or {}).get(f"{delta_key}_std"))
            mo = fv((r_wo or {}).get(f"{delta_key}_mean"))
            so = fv((r_wo or {}).get(f"{delta_key}_std"))
            pw = sig((r_pval or {}).get(f"{delta_key}_with_wilcoxon_p")) if r_pval else "—"
            po = sig((r_pval or {}).get(f"{delta_key}_without_wilcoxon_p")) if r_pval else "—"
            print(f"    {label:25s}  {mw:>10s}  {sw:>9s}  {mo:>10s}  {so:>9s}  {pw:>10s}  {po:>10s}")

    print()


def print_rq3(outdir: Path, width: int) -> None:
    rows = load_csv(outdir / "rq3_by_cycle_bin.csv")

    if not rows:
        print("  [No RQ3 data found]\n")
        return

    print(hline(width))
    print(centered("RQ3: Scalability by Cycle Size", width))
    print(hline(width))
    print()

    # Bin rows (not the interaction row)
    bin_rows = [r for r in rows if r.get("CycleBin") != "Interaction"]
    inter = row_get(rows, "CycleBin", "Interaction")

    if bin_rows:
        print(f"    {'Bin':10s} {'Cond':8s} {'n':>4s} {'Succ':>5s} {'S%':>7s}"
              f" {'ΔEdge':>7s} {'ΔNode':>7s} {'ΔLOC':>8s}"
              f" {'NoTestRegr%':>12s}")
        print(f"    {THIN_SEP_CHAR * 72}")
        for r in bin_rows:
            bn = (r.get("CycleBin") or "")[:9]
            co = (r.get("Condition") or "")[:7]
            n = fv(r.get("n"), 0)
            ns = fv(r.get("n_success"), 0)
            sp = fv(r.get("Success%"), 1)
            de = fv(r.get("ΔEdges_mean"))
            dn = fv(r.get("ΔNodes_mean"))
            dl = fv(r.get("ΔLOC_mean"))
            nr = fv(r.get("NoTestRegression%"), 1)
            print(f"    {bn:10s} {co:8s} {n:>4s} {ns:>5s} {sp:>6s}%"
                  f" {de:>7s} {dn:>7s} {dl:>8s} {nr:>11s}%")

        # McNemar per bin
        print()
        print("  McNemar Tests (per bin):")
        for r in bin_rows:
            if r.get("Condition") == "with" and r.get("Success_p_McNemar_two_sided"):
                bn = r.get("CycleBin", "?")
                p2 = sig(r.get("Success_p_McNemar_two_sided"))
                p1 = sig(r.get("Success_p_McNemar_one_sided"))
                b = fv(r.get("b_with_better"), 0)
                c = fv(r.get("c_without_better"), 0)
                print(f"    {bn}: two-sided p={p2}  one-sided p={p1}  (b={b}, c={c})")

    if inter:
        print()
        print("  Scalability Interaction (does WITH help more on larger cycles?):")
        dd = fv(inter.get("Diff_of_diffs"), 4)
        pi = sig(inter.get("p_interaction_one_sided"))
        print(f"    Diff-of-diffs = {dd}    p (one-sided) = {pi}")
        for label, prefix in [("Large", "Large"), ("Small", "Small")]:
            sw = fv(inter.get(f"Success%_with_{prefix}"), 1)
            so = fv(inter.get(f"Success%_without_{prefix}"), 1)
            print(f"    {label}: WITH {sw}%  WITHOUT {so}%")

    print()


def print_diff_stats(
    diff_stats: List[Dict[str, Any]],
    top_n: int,
    width: int,
) -> None:
    if not diff_stats:
        print("  [No diff/patch files found]\n")
        return

    print(hline(width))
    print(centered("Diff Statistics  (patch file summaries)", width))
    print(hline(width))
    print()

    # Aggregate
    total_patches = len(diff_stats)
    total_files = sum(d["files"] for d in diff_stats)
    total_added = sum(d["added"] for d in diff_stats)
    total_removed = sum(d["removed"] for d in diff_stats)

    by_cond: Dict[str, List[Dict[str, Any]]] = {"with": [], "without": []}
    for d in diff_stats:
        by_cond.setdefault(d["condition"], []).append(d)

    print(f"  Total patches found: {total_patches}")
    print(f"  Total files changed: {total_files}    Lines: +{total_added} / -{total_removed}")
    print()
    print(f"    {'Condition':10s} {'Patches':>8s} {'Files':>8s} {'Added':>8s} {'Removed':>8s} {'Avg Churn':>10s}")
    print(f"    {THIN_SEP_CHAR * 50}")
    for cond in ("with", "without"):
        ds = by_cond.get(cond, [])
        if not ds:
            continue
        nf = sum(d["files"] for d in ds)
        na = sum(d["added"] for d in ds)
        nr = sum(d["removed"] for d in ds)
        avg = (na + nr) / len(ds) if ds else 0
        print(f"    {cond:10s} {len(ds):>8d} {nf:>8d} {'+' + str(na):>8s} {'-' + str(nr):>8s} {avg:>10.1f}")

    # Top N diffs by total churn
    if top_n > 0:
        sorted_diffs = sorted(diff_stats, key=lambda d: d["total_changed"], reverse=True)
        show = sorted_diffs[:top_n]
        print()
        print(f"  Top {len(show)} Diffs by Churn (files touched, lines ±):")
        print(f"    {'Repo':20s} {'Cycle':15s} {'Cond':7s} {'Files':>5s} {'Added':>7s} {'Removed':>8s}")
        print(f"    {THIN_SEP_CHAR * 65}")
        for d in show:
            repo = (d["repo"] or "")[:19]
            cid = (d["cycle_id"] or "")[:14]
            cond = (d["condition"] or "")[:6]
            print(f"    {repo:20s} {cid:15s} {cond:7s} {d['files']:>5d}"
                  f" {'+' + str(d['added']):>7s} {'-' + str(d['removed']):>8s}")
            # Show which files were touched (no content — just filenames)
            for fname in d["file_list"][:5]:
                print(f"      → {fname}")
            if len(d["file_list"]) > 5:
                print(f"      → ... and {len(d['file_list']) - 5} more files")

    print()


def print_diff_examples(
    diff_stats: List[Dict[str, Any]],
    results_roots: List[str],
    exp_ids: List[str],
    repos_file: Path,
    cycles_file: Path,
    top_n: int,
    width: int,
) -> None:
    """Print abbreviated diffstats (like git diff --stat) for the top diffs.

    This shows file-level change bars — NO source code — making it safe
    for screenshots while still conveying the scope of changes.
    """
    if not diff_stats or top_n <= 0:
        return

    script_dir = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(script_dir / "table_makers"))
    from rq_utils import branch_for, map_roots_exps

    cfgs = map_roots_exps(results_roots, exp_ids)

    sorted_diffs = sorted(diff_stats, key=lambda d: d["total_changed"], reverse=True)
    show = sorted_diffs[:top_n]

    print(hline(width))
    print(centered("Diffstat Summaries  (top diffs, no source code)", width))
    print(hline(width))
    print()

    for d in show:
        repo = d["repo"]
        cid = d["cycle_id"]
        cond = d["condition"]

        # Find the actual patch file
        patch_path = None
        for results_root, with_id, wo_id in cfgs:
            exp_label = with_id if cond == "with" else wo_id
            branch = branch_for(exp_label, cid)
            candidates = [
                Path(results_root) / repo / "branches" / branch / "openhands" / "git_diff.patch",
                Path(results_root) / repo / branch / "openhands" / "git_diff.patch",
                Path(results_root) / repo / branch / "openhands" / "diff.patch",
            ]
            for p in candidates:
                if p.exists() and p.stat().st_size > 0:
                    patch_path = p
                    break
            if patch_path:
                break

        print(f"  {repo} / {cid} ({cond})")
        print(f"  {THIN_SEP_CHAR * min(60, width - 4)}")

        if not patch_path:
            print("    [patch file not found]")
            print()
            continue

        # Build a git-diff-stat-style display from the patch
        file_stats: Dict[str, Tuple[int, int]] = {}
        try:
            text = patch_path.read_text(encoding="utf-8", errors="replace")
            current_file = None
            for line in text.splitlines():
                if line.startswith("diff --git"):
                    parts = line.split(" b/", 1)
                    current_file = parts[1].strip() if len(parts) == 2 else None
                    if current_file and current_file not in file_stats:
                        file_stats[current_file] = (0, 0)
                elif current_file:
                    if line.startswith("+") and not line.startswith("+++"):
                        a, r = file_stats[current_file]
                        file_stats[current_file] = (a + 1, r)
                    elif line.startswith("-") and not line.startswith("---"):
                        a, r = file_stats[current_file]
                        file_stats[current_file] = (a, r + 1)
        except Exception:
            print("    [error reading patch]")
            print()
            continue

        max_bar = 40
        max_change = max((a + r) for a, r in file_stats.values()) if file_stats else 1
        scale = max_bar / max(max_change, 1)

        total_a = 0
        total_r = 0
        for fname, (added, removed) in sorted(file_stats.items()):
            total_a += added
            total_r += removed
            bar_a = "+" * max(1, int(added * scale)) if added else ""
            bar_r = "-" * max(1, int(removed * scale)) if removed else ""
            change_str = f"+{added}" if removed == 0 else (f"-{removed}" if added == 0 else f"+{added}/-{removed}")
            # Truncate filename for display
            display_name = fname if len(fname) <= 45 else "..." + fname[-(42):]
            print(f"    {display_name:45s} | {change_str:>12s} {bar_a}{bar_r}")

        print(f"    {len(file_stats)} file(s), +{total_a} -{total_r}")
        print()


# ─── main ────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Print a screenshot-friendly summary of experiment results."
    )
    ap.add_argument("--outdir", required=True,
                    help="Directory containing rq1/rq2/rq3 CSVs (from run_make_rq_tables.sh)")
    ap.add_argument("--width", type=int, default=100,
                    help="Terminal width for formatting (default: 100)")

    # Optional: diff analysis (requires knowing the results layout)
    ap.add_argument("--results-roots", nargs="+", default=None,
                    help="Results root directories (same as run_make_rq_tables.sh)")
    ap.add_argument("--exp-ids", nargs="+", default=None,
                    help="Experiment IDs (same as run_make_rq_tables.sh)")
    ap.add_argument("--repos-file", default=None,
                    help="Path to repos.txt")
    ap.add_argument("--cycles-file", default=None,
                    help="Path to cycles_to_analyze.txt")
    ap.add_argument("--top-diffs", type=int, default=3,
                    help="Show diffstat summaries for the top N diffs by churn (0 to disable)")

    args = ap.parse_args()
    W = args.width
    outdir = Path(args.outdir)

    print()
    print(hline(W))
    print(centered("EXPERIMENT RESULTS SUMMARY", W))
    print(centered("(screenshot this output)", W))
    print(hline(W))
    print()

    # RQ1
    print_rq1(outdir, W)

    # RQ2
    print_rq2(outdir, W)

    # RQ3
    print_rq3(outdir, W)

    # Diff statistics (optional)
    has_diff_args = (
        args.results_roots
        and args.exp_ids
        and args.repos_file
        and args.cycles_file
    )
    if has_diff_args:
        diff_stats = collect_diff_stats(
            args.results_roots,
            args.exp_ids,
            Path(args.repos_file),
            Path(args.cycles_file),
        )
        print_diff_stats(diff_stats, args.top_diffs, W)
        print_diff_examples(
            diff_stats,
            args.results_roots,
            args.exp_ids,
            Path(args.repos_file),
            Path(args.cycles_file),
            args.top_diffs,
            W,
        )
    else:
        print(f"  [Diff analysis skipped — pass --results-roots, --exp-ids, --repos-file, --cycles-file to include]")
        print()

    print(hline(W))
    print(centered("END OF SUMMARY", W))
    print(hline(W))
    print()


if __name__ == "__main__":
    main()
