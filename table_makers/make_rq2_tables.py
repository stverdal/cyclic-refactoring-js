#!/usr/bin/env python3
"""
RQ2 table generator — multi-root, success-filtered deltas vs baseline.

This version:
- DOES NOT write rq2_success_deltas.csv (deltas stay in memory only).
- Tests, per condition ("with", "without"), whether median deltas vs baseline differ
  from zero (Wilcoxon one-sample, two-sided).

Marker support:
- If a branch dir contains `.copied_metrics_marker`, we treat it as "no changes":
  post metrics are recorded as baseline metrics in the trace (so deltas are 0 downstream).

Outputs:
  - rq2_trace.csv          (raw tool metrics per variant; baseline/with/without)
  - rq2_overall.csv        (aggregated deltas across all projects; per-condition means/std + Wilcoxon vs 0)
  - rq2_per_project.csv    (aggregated deltas per project, per condition)
"""
from __future__ import annotations
import argparse, csv, sys, math
from pathlib import Path
from typing import Dict, Any, List, Tuple

from rq_utils import (
    read_json, read_repos_file, CQ_METRICS, CQ_METRICS_FALLBACK,
    extract_quality_metrics,
    parse_cycles, branch_for, map_roots_exps, mean_std, load_json_any
)

_CQ_CANDIDATES = [CQ_METRICS, CQ_METRICS_FALLBACK]

# -------------- metrics --------------
METRICS = [
    "ruff_issues",
    "mi_avg",
    "cc_dplus_funcs",      # Radon CC D+E+F (heavy complexity)
    "pyexam_arch",         # PyExamine weighted by type
    "pyexam_code",
    "pyexam_struct",
    "bandit_high",
    "test_pass_pct",
    "coverage_line_percent",
    "mypy_errors",
]
DELTA_NAMES = {m: f"Δ{m}" for m in METRICS}

# -------------- helpers --------------
def safe_wilcoxon_one_sample(xs: List[float]) -> float | None:
    """
    One-sample Wilcoxon signed-rank vs 0, robust to degenerate cases.
    Returns p-value (two-sided), or None if not computable.
    """
    xs = [float(v) for v in xs if v is not None and v != ""]
    xs = [v for v in xs if not (math.isnan(v) or math.isinf(v))]
    if len(xs) == 0:
        return None
    if all(v == 0.0 for v in xs):
        return 1.0
    try:
        from scipy.stats import wilcoxon
        _, p = wilcoxon(xs, zero_method="wilcox", correction=False,
                        alternative="two-sided", mode="auto")
        return float(p)
    except Exception:
        return None

# -------------- main --------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-roots", nargs="+", required=True)
    ap.add_argument("--exp-ids", nargs="+", required=True)
    ap.add_argument("--repos-file", required=True)
    ap.add_argument("--cycles-file", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--rq1-per-cycle", help="Path to rq1_per_cycle.csv; defaults to <outdir>/rq1_per_cycle.csv")
    args = ap.parse_args()

    cfgs = map_roots_exps(args.results_roots, args.exp_ids)
    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)

    # -------- build trace (with marker handling) --------
    repos_list = read_repos_file(Path(args.repos_file))
    cycles_map = parse_cycles(Path(args.cycles_file))
    trace_rows: List[Dict[str, Any]] = []

    for results_root, with_id, wo_id in cfgs:
        root = Path(results_root)
        for repo, baseline_branch, _src_rel in repos_list:
            repo_dir = root / repo

            baseline_dir = repo_dir / "branches" / baseline_branch
            base = load_json_any(baseline_dir, _CQ_CANDIDATES)
            if base:
                trace_rows.append({
                    "repo": repo, "results_root": str(root),
                    "variant": "baseline", "exp_label": "", "cycle_id": "", **extract_quality_metrics(base)
                })

            for cid in cycles_map.get((repo, baseline_branch), []):
                for variant_label, exp_label in (("with", with_id), ("without", wo_id)):
                    branch = branch_for(exp_label, cid)
                    branch_dir = repo_dir / branch
                    use_base = (branch_dir / ".copied_metrics_marker").exists()
                    if use_base and base:
                        j = base
                    else:
                        j = load_json_any(branch_dir, _CQ_CANDIDATES)
                    if j:
                        trace_rows.append({
                            "repo": repo, "results_root": str(root),
                            "variant": variant_label, "exp_label": exp_label, "cycle_id": str(cid),
                            **extract_quality_metrics(j)
                        })

    # write trace (raw metrics)
    trace_path = outdir / "rq2_trace.csv"
    if trace_rows:
        fields = ["repo","results_root","variant","exp_label","cycle_id"] + METRICS
        with trace_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
            for r in trace_rows: w.writerow({k: r.get(k) for k in fields})
        print(f"Wrote: {trace_path}")
    else:
        print("[WARN] No trace rows produced", file=sys.stderr)

    # -------- success-filtered deltas vs baseline (in-memory only) --------
    import csv as _csv
    rq1_path = Path(args.rq1_per_cycle) if args.rq1_per_cycle else (outdir / "rq1_per_cycle.csv")
    if not rq1_path.exists():
        print(f"[WARN] RQ1 per-cycle not found: {rq1_path} — cannot compute success-filtered deltas", file=sys.stderr)
        return

    def load_csv_dicts(path: Path) -> List[Dict[str, Any]]:
        with path.open("r", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            return [dict(row) for row in reader]

    rq1_rows = load_csv_dicts(rq1_path)

    # Collect success keys (succ == True) per (repo, cycle, exp_label, condition)
    succ_keys = set()
    for r in rq1_rows:
        try:
            succ = str(r.get("succ","")).strip().lower() in ("true","1","yes")
            if not succ: continue
            repo = r.get("repo"); cid = str(r.get("cycle_id"))
            exp = r.get("exp_label") or r.get("variant_label") or ""
            cond = r.get("condition")
            if repo and cid and cond in ("with","without"):
                succ_keys.add((repo, cid, exp, cond))
        except Exception:
            continue

    # Index trace by (repo, results_root, variant, exp_label, cycle_id)
    from collections import defaultdict
    by_key = defaultdict(list)
    for r in trace_rows:
        key = (r.get("repo"), r.get("results_root"), r.get("variant"),
               r.get("exp_label",""), str(r.get("cycle_id","")))
        by_key[key].append(r)

    # Baseline per (repo, results_root)
    baseline_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in trace_rows:
        if r.get("variant") == "baseline":
            baseline_map[(r["repo"], r["results_root"])] = r

    deltas: List[Dict[str, Any]] = []
    for (repo, cid, exp, cond) in succ_keys:
        # All roots that have this (repo, cid, exp, cond)
        for (r_repo, r_root, r_var, r_exp, r_cid), rows in by_key.items():
            if r_repo != repo or r_cid != cid or r_exp != exp or r_var != cond:
                continue
            run = rows[0]
            base = baseline_map.get((repo, r_root))
            if not base:
                # fallback: any baseline for this repo (rare)
                for (rep, rt), b in baseline_map.items():
                    if rep == repo:
                        base = b; break
            if not base:
                continue
            out = {
                "repo": repo,
                "results_root": r_root,
                "variant": cond,        # with/without
                "exp_label": exp,
                "cycle_id": cid,
            }
            for m in METRICS:
                rv = run.get(m)
                bv = base.get(m)
                try:
                    rvf = float(rv) if rv is not None and rv != "" else float("nan")
                    bvf = float(bv) if bv is not None and bv != "" else float("nan")
                    out[DELTA_NAMES[m]] = rvf - bvf
                except Exception:
                    out[DELTA_NAMES[m]] = float("nan")
            deltas.append(out)

    if not deltas:
        print("[WARN] No success-filtered deltas produced", file=sys.stderr)
        return

    # -------- aggregate: per-condition means/std + one-sample Wilcoxon vs 0 --------
    from collections import defaultdict
    by_cond: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in deltas:
        by_cond[r["variant"]].append(r)

    overall_rows: List[Dict[str, Any]] = []
    # means/std per condition
    for cond in ("without","with"):
        xs = by_cond.get(cond, [])
        row = {"Condition": cond, "n": len(xs)}
        for m in METRICS:
            vals = []
            for r in xs:
                v = r.get(DELTA_NAMES[m])
                try:
                    vf = float(v)
                    if math.isnan(vf) or math.isinf(vf): continue
                    vals.append(vf)
                except Exception:
                    continue
            mu, sd = mean_std(vals)
            row[DELTA_NAMES[m] + "_mean"] = (None if math.isnan(mu) else mu)
            row[DELTA_NAMES[m] + "_std"]  = (None if math.isnan(sd) else sd)
        overall_rows.append(row)

    # one-sample Wilcoxon vs zero (per condition, per metric)
    stats_row = {"Condition": "p_vs_zero", "n": None}
    for cond in ("without","with"):
        xs_cond = by_cond.get(cond, [])
        for m in METRICS:
            xs = []
            for r in xs_cond:
                v = r.get(DELTA_NAMES[m])
                try:
                    vf = float(v)
                    if math.isnan(vf) or math.isinf(vf): continue
                    xs.append(vf)
                except Exception:
                    continue
            p = safe_wilcoxon_one_sample(xs)
            stats_row[f"{DELTA_NAMES[m]}_{cond}_wilcoxon_p"] = p
            stats_row[f"{DELTA_NAMES[m]}_{cond}_n"] = len(xs)
    overall_rows.append(stats_row)

    overall_path = outdir / "rq2_overall.csv"
    with overall_path.open("w", newline="", encoding="utf-8") as f:
        keys = []
        for r in overall_rows:
            for k in r.keys():
                if k not in keys: keys.append(k)
        w = csv.DictWriter(f, fieldnames=keys); w.writeheader()
        for r in overall_rows: w.writerow(r)
    print(f"Wrote: {overall_path}")

    # -------- aggregate: per project (WITH/WITHOUT) --------
    per_proj_rows: List[Dict[str, Any]] = []
    proj_cond: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in deltas:
        proj_cond[(r["repo"], r["variant"])].append(r)

    for (repo, cond), xs in sorted(proj_cond.items()):
        row = {"repo": repo, "Condition": cond, "n_succ": len(xs)}
        for m in METRICS:
            vals = []
            for r in xs:
                v = r.get(DELTA_NAMES[m])
                try:
                    vf = float(v)
                    if math.isnan(vf) or math.isinf(vf): continue
                    vals.append(vf)
                except Exception:
                    continue
            mu, sd = mean_std(vals)
            row[DELTA_NAMES[m] + "_mean"] = (None if math.isnan(mu) else mu)
            row[DELTA_NAMES[m] + "_std"]  = (None if math.isnan(sd) else sd)
        per_proj_rows.append(row)

    per_proj_path = outdir / "rq2_per_project.csv"
    if per_proj_rows:
        keys = list(per_proj_rows[0].keys())
        with per_proj_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys); w.writeheader()
            for r in per_proj_rows: w.writerow(r)
        print(f"Wrote: {per_proj_path}")
    else:
        print("[WARN] No per-project rows produced", file=sys.stderr)

if __name__ == "__main__":
    main()
