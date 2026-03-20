#!/usr/bin/env python3
"""
RQ1 (iterationless): WITH vs WITHOUT explanations. Multi-root aware.

Marker support:
- If a branch dir contains `.copied_metrics_marker`, we treat it as "no changes":
  post metrics = baseline metrics, test% = baseline test%.
  (So deltas are 0 and the run is not a success.)

- Accepts multiple results roots + experiment IDs and aggregates across them.
- Adds std dev columns and p-values:
    * Success_p (two-sided & one-sided): McNemar tests over paired (with vs without)
      successes per (repo, cycle_id, exp-family, results_root).
    * ΔEdges_success_wilcoxon_p: Wilcoxon paired over ΔEdges on pairs where both sides are success.
      (Robust: all-zero diffs -> p=1.0; no pairs -> p=None)
- Replaces average "Tests%" with a binary **NoTestRegression%**:
    * percent of runs where tests did not regress vs baseline (post >= base).

Outputs:
  - rq1_per_project.csv
  - rq1_with_vs_without.csv
  - rq1_per_cycle.csv
"""

from __future__ import annotations
import argparse, csv, statistics, sys, math
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from rq_utils import (
    read_repos_file, get_tests_pass_percent, get_scc_metrics, parse_cycles, branch_for,
    load_json_any, mean_or_none, std_or_none, safe_sub, cycle_size_from_baseline,
    mcnemar_p, map_roots_exps, exp_family, mcnemar_p_one_sided
)

ATD_METRICS = ["ATD_identification/ATD_metrics.json", "ATD_metrics.json",
               "ATD_identification/scc_report.json", "scc_report.json"]
QUALITY_METRICS = ["code_quality_checks/metrics.json", "metrics.json",
                   "code_quality_checks.json"]

# ----------------- helpers -----------------
def median_or_none(vals: List[Optional[float]]) -> Optional[float]:
    xs = [float(v) for v in vals if isinstance(v, (int, float))]
    if not xs:
        return None
    try:
        return float(statistics.median(xs))
    except Exception:
        return None

def rate_bool(xs: List[Optional[bool]]) -> Optional[float]:
    vals = [x for x in xs if isinstance(x, bool)]
    if not vals:
        return None
    return 100.0 * sum(1 for v in vals if v) / len(vals)

def pct(a: Optional[int], b: Optional[int]) -> Optional[float]:
    if not isinstance(a, int) or not isinstance(b, int) or b == 0:
        return None
    return 100.0 * a / b

def safe_wilcoxon(x: List[float], y: List[float]) -> Optional[float]:
    """Wilcoxon signed-rank, robust to degenerate cases.
       - if all diffs == 0 -> p = 1.0
       - if no pairs -> None
    """
    if len(x) != len(y) or len(x) == 0:
        return None
    diffs = []
    for a, b in zip(x, y):
        try:
            da = float(a); db = float(b)
            if math.isnan(da) or math.isnan(db) or math.isinf(da) or math.isinf(db):
                continue
            diffs.append(da - db)
        except Exception:
            continue
    if len(diffs) == 0:
        return None
    nonzero = [d for d in diffs if d != 0.0]
    if len(nonzero) == 0:
        return 1.0  # identical series
    try:
        from scipy.stats import wilcoxon
        _, p = wilcoxon(x, y, zero_method="wilcox", correction=False,
                        alternative="two-sided", mode="auto")
        return float(p)
    except Exception:
        return None

def proportion_wilson_ci(k: int, n: int, conf: float = 0.95) -> Tuple[Optional[float], Optional[float]]:
    """Wilson score CI for p = k/n."""
    if n <= 0:
        return (None, None)
    from math import sqrt
    # z for 95% CI
    z = 1.96 if abs(conf - 0.95) < 1e-12 else 1.96
    phat = k / n
    denom = 1 + z*z/n
    center = (phat + z*z/(2*n)) / denom
    half = z * sqrt((phat*(1-phat) + z*z/(4*n)) / n) / denom
    return (center - half, center + half)

# ---- NEW: outcome classifier for breakdown table ----
def classify_outcome(row: Dict[str, Any]) -> str:
    """
    Returns one of: 'success', 'behavior_regressed', 'structure_not_improved', 'both_failed', 'other_error'
    based on:
      - structural improvement: post_edges < pre_edges
      - test non-regression: delta_tests_vs_base >= 0  (None => unknown)
    """
    pre = row.get("pre_edges"); post = row.get("post_edges")
    if not isinstance(pre, (int, float)) or not isinstance(post, (int, float)):
        return "other_error"

    dtests = row.get("delta_tests_vs_base")
    tests_ok = None if (dtests is None) else (dtests >= 0)

    struct_improved = post < pre
    struct_not_improved = post >= pre

    if tests_ok is True and struct_improved:
        return "success"
    if tests_ok is False and struct_improved:
        return "behavior_regressed"
    if tests_ok is True and struct_not_improved:
        return "structure_not_improved"
    if tests_ok is False and struct_not_improved:
        return "both_failed"
    return "other_error"

# ----------------- main -----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-roots", nargs="+", required=True)
    ap.add_argument("--exp-ids", nargs="+", required=True)
    ap.add_argument("--repos-file", required=True)
    ap.add_argument("--cycles-file", required=True)
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()

    cfgs = map_roots_exps(args.results_roots, args.exp_ids)

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    repos = read_repos_file(Path(args.repos_file))
    cycles_map = parse_cycles(Path(args.cycles_file))

    per_cycle_rows: List[Dict[str, Any]] = []

    # --------- Collect all per-cycle rows across ALL roots/experiments ----------
    for results_root, WITH_ID, WO_ID in cfgs:
        for repo, baseline_branch, _src_rel in repos:
            repo_dir = Path(results_root) / repo
            baseline_dir = repo_dir / "branches" / baseline_branch

            base_atd   = load_json_any(baseline_dir, ATD_METRICS)
            base_qual  = load_json_any(baseline_dir, QUALITY_METRICS)
            if base_atd is None or base_qual is None:
                print(f"[WARN] Missing baseline ATD or quality metrics for {repo}@{baseline_branch} under {results_root}", file=sys.stderr)
                continue

            pre = get_scc_metrics(base_atd)
            pre_edges = pre.get("total_edges_in_cyclic_sccs")
            pre_nodes = pre.get("total_nodes_in_cyclic_sccs")
            pre_loc   = pre.get("total_loc_in_cyclic_sccs")
            base_tests = get_tests_pass_percent(base_qual)

            cids = cycles_map.get((repo, baseline_branch), [])[:]
            if not cids:
                continue

            def collect_one(dirpath: Path, cid: str, variant_label: str, condition_out: str) -> Optional[Dict[str, Any]]:
                """
                Read branch metrics, respecting `.copied_metrics_marker`:
                  - if present => use baseline metrics & baseline tests.
                  - else       => load ATD/CQ from the branch directory.
                """
                copied_marker = (dirpath / ".copied_metrics_marker").exists()

                if copied_marker:
                    # Force post == pre, and tests == baseline.
                    post_edges = pre_edges
                    post_nodes = pre_nodes
                    post_loc   = pre_loc
                    tests_pass = base_tests
                else:
                    atd = load_json_any(dirpath, ATD_METRICS)
                    qual = load_json_any(dirpath, QUALITY_METRICS)
                    if atd is None:
                        return None
                    post = get_scc_metrics(atd)
                    post_edges = post.get("total_edges_in_cyclic_sccs")
                    post_nodes = post.get("total_nodes_in_cyclic_sccs")
                    post_loc   = post.get("total_loc_in_cyclic_sccs")
                    tests_pass = get_tests_pass_percent(qual) if qual is not None else None

                d_edges = safe_sub(post_edges, pre_edges)
                d_nodes = safe_sub(post_nodes, pre_nodes)
                d_loc   = safe_sub(post_loc,   pre_loc)

                succ: Optional[bool] = None
                if (pre_edges is not None) and (post_edges is not None):
                    tests_ok = (base_tests is None) or (tests_pass is None) or (tests_pass >= base_tests)
                    # Success requires strictly fewer cycle edges than baseline.
                    succ = (post_edges < pre_edges) and tests_ok

                size = cycle_size_from_baseline(baseline_dir, cid)

                return {
                    "repo": repo,
                    "results_root": str(results_root),
                    "cycle_id": cid,
                    "cycle_size": size,
                    "variant_label": variant_label,
                    "exp_label": variant_label,  # family derived via exp_family()
                    "condition": condition_out,  # with / without
                    "succ": succ,
                    "pre_edges": pre_edges, "post_edges": post_edges, "delta_edges": d_edges,
                    "pre_nodes": pre_nodes, "post_nodes": post_nodes, "delta_nodes": d_nodes,
                    "pre_loc": pre_loc,     "post_loc": post_loc,     "delta_loc":   d_loc,
                    "tests_pass_pct": tests_pass,
                    "delta_tests_vs_base": safe_sub(tests_pass, base_tests),
                }

            for cid in cids:
                with_dir = repo_dir / branch_for(WITH_ID, cid)
                wo_dir   = repo_dir / branch_for(WO_ID,   cid)
                row_with = collect_one(with_dir, cid, WITH_ID, "with")
                row_wo   = collect_one(wo_dir,   cid, WO_ID,   "without")
                if row_with: per_cycle_rows.append(row_with)
                if row_wo:   per_cycle_rows.append(row_wo)

    # ---------- Per-project aggregation (ACROSS ALL EXPERIMENTS/ROOTS) ----------
    def aggregate_rows(rows: List[Dict[str, Any]], repo_name: str, condition_label: str) -> Optional[Dict[str, Any]]:
        rows_c = [r for r in rows if r["repo"] == repo_name and r["condition"] == condition_label]
        if not rows_c:
            return None
        n_total = len(rows_c)
        n_success = sum(1 for r in rows_c if isinstance(r.get("succ"), bool) and r["succ"] is True)
        succ_pct = pct(n_success, n_total)

        succ_rows = [r for r in rows_c if r.get("succ") is True]
        de_succ = [r.get("delta_edges") for r in succ_rows]
        dn_succ = [r.get("delta_nodes") for r in succ_rows]
        dl_succ = [r.get("delta_loc")   for r in succ_rows]

        valid_edge_pairs = [r for r in rows_c if isinstance(r.get("pre_edges"), (int, float)) and isinstance(r.get("post_edges"), (int, float))]
        zero_change = [ (r["post_edges"] == r["pre_edges"]) for r in valid_edge_pairs ]

        # Percent of non-regressions relative to baseline
        nt_vals = [r.get("delta_tests_vs_base") for r in rows_c if r.get("delta_tests_vs_base") is not None]
        no_reg = (100.0 * sum(1 for v in nt_vals if v >= 0) / len(nt_vals)) if nt_vals else None

        # ---- Outcome breakdown ----
        cats = {"success":0, "behavior_regressed":0, "structure_not_improved":0, "both_failed":0, "other_error":0}
        for r in rows_c:
            cats[classify_outcome(r)] += 1

        def pct_cat(k: str) -> Optional[float]:
            return round(100.0 * cats[k] / n_total, 2) if n_total > 0 else None

        return {
            "repo": repo_name,
            "Condition": condition_label,
            "n_total": n_total,
            "n_success": n_success,
            "Success%": round(succ_pct, 2) if succ_pct is not None else None,
            "ΔEdges_success_mean": mean_or_none(de_succ),
            "ΔEdges_success_std":  std_or_none(de_succ),
            "ΔEdges_success_median": median_or_none(de_succ),
            "ΔNodes_success_mean": mean_or_none(dn_succ),
            "ΔNodes_success_std":  std_or_none(dn_succ),
            "ΔLOC_success_mean": mean_or_none(dl_succ),
            "ΔLOC_success_std":  std_or_none(dl_succ),
            "ZeroChange%": round(rate_bool(zero_change), 2) if rate_bool(zero_change) is not None else None,
            "NoTestRegression%": round(no_reg, 2) if no_reg is not None else None,
            "BehaviorRegressed%": pct_cat("behavior_regressed"),
            "StructureNotImproved%": pct_cat("structure_not_improved"),
            "BothFailed%": pct_cat("both_failed"),
            "OtherError%": pct_cat("other_error"),
        }

    per_project_rows: List[Dict[str, Any]] = []
    repo_names = sorted({r["repo"] for r in per_cycle_rows})
    for repo_name in repo_names:
        for cond in ("with", "without"):
            row = aggregate_rows(per_cycle_rows, repo_name, cond)
            if row:
                per_project_rows.append(row)

    # ---------- Write per-project ----------
    if per_project_rows:
        proj_path = Path(args.outdir) / "rq1_per_project.csv"
        with proj_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(per_project_rows[0].keys()))
            w.writeheader()
            for r in per_project_rows:
                w.writerow(r)
        print(f"Wrote: {proj_path}")
    else:
        print("[WARN] No per-project rows produced", file=sys.stderr)

    # ---------- WITH vs WITHOUT pooled across ALL rows ----------
    pool = {"with": [], "without": []}
    for r in per_cycle_rows:
        pool[r["condition"]].append(r)

    def aggregate_pool(rows: List[Dict[str, Any]], condition_label: str) -> Optional[Dict[str, Any]]:
        if not rows:
            return None
        n_total = len(rows)
        n_success = sum(1 for r in rows if isinstance(r.get("succ"), bool) and r["succ"] is True)
        succ_pct = pct(n_success, n_total)

        succ_rows = [r for r in rows if r.get("succ") is True]
        de_succ = [r.get("delta_edges") for r in succ_rows]
        dn_succ = [r.get("delta_nodes") for r in succ_rows]
        dl_succ = [r.get("delta_loc")   for r in succ_rows]

        valid_edge_pairs = [r for r in rows if isinstance(r.get("pre_edges"), (int, float)) and isinstance(r.get("post_edges"), (int, float))]
        zero_change = [ (r["post_edges"] == r["pre_edges"]) for r in valid_edge_pairs ]

        nt_vals = [r.get("delta_tests_vs_base") for r in rows if r.get("delta_tests_vs_base") is not None]
        no_reg = (100.0 * sum(1 for v in nt_vals if v >= 0) / len(nt_vals)) if nt_vals else None

        # ---- Outcome breakdown ----
        cats = {"success":0, "behavior_regressed":0, "structure_not_improved":0, "both_failed":0, "other_error":0}
        for r in rows:
            cats[classify_outcome(r)] += 1

        def pct_cat(k: str) -> Optional[float]:
            return round(100.0 * cats[k] / n_total, 2) if n_total > 0 else None

        return {
            "Condition": condition_label,
            "n_total": n_total,
            "n_success": n_success,
            "Success%": round(succ_pct, 2) if succ_pct is not None else None,
            "ΔEdges_success_mean": mean_or_none(de_succ),
            "ΔEdges_success_std":  std_or_none(de_succ),
            "ΔEdges_success_median": median_or_none(de_succ),
            "ΔNodes_success_mean": mean_or_none(dn_succ),
            "ΔNodes_success_std":  std_or_none(dn_succ),
            "ΔLOC_success_mean": mean_or_none(dl_succ),
            "ΔLOC_success_std":  std_or_none(dl_succ),
            "ZeroChange%": round(rate_bool(zero_change), 2) if rate_bool(zero_change) is not None else None,
            "NoTestRegression%": round(no_reg, 2) if no_reg is not None else None,
            "BehaviorRegressed%": pct_cat("behavior_regressed"),
            "StructureNotImproved%": pct_cat("structure_not_improved"),
            "BothFailed%": pct_cat("both_failed"),
            "OtherError%": pct_cat("other_error"),
        }

    rows_with_without: List[Dict[str, Any]] = []
    for label_out in ("with", "without"):
        agg = aggregate_pool(pool[label_out], label_out)
        if agg:
            rows_with_without.append(agg)

    # ---------- Paired significance tests ----------
    def paired_success_counts() -> Tuple[int,int,int,int,int,int]:
        """
        Count pairs for McNemar:
          b = with succeeded, without failed
          c = without succeeded, with failed

        Returns:
          (b, c, total_pairs, matched_pairs, both_success, both_fail)
        where:
          total_pairs    = number of distinct (repo, cycle, expfam, root) that had at least one side
          matched_pairs  = number of those that had both sides
          both_success   = matched pairs where both succeeded
          both_fail      = matched pairs where both failed
        """
        with_map: Dict[Tuple[str,str,str,str], Dict[str,Any]] = {}
        wo_map:   Dict[Tuple[str,str,str,str], Dict[str,Any]] = {}
        keys_seen = set()

        for r in per_cycle_rows:
            key = (r["repo"], r["cycle_id"], exp_family(r.get("exp_label")), r.get("results_root"))
            keys_seen.add(key)
            if r["condition"] == "with":
                with_map[key] = r
            elif r["condition"] == "without":
                wo_map[key] = r

        b = c = 0
        matched = 0
        both_success = 0
        both_fail = 0

        for k in set(with_map.keys()).intersection(wo_map.keys()):
            matched += 1
            w = with_map[k].get("succ")
            o = wo_map[k].get("succ")
            if isinstance(w, bool) and isinstance(o, bool):
                if w and not o:
                    b += 1
                elif o and not w:
                    c += 1
                elif w and o:
                    both_success += 1
                else:
                    both_fail += 1

        total_pairs = len(keys_seen)
        return b, c, total_pairs, matched, both_success, both_fail

    def paired_delta_edges() -> Tuple[List[float], List[float]]:
        with_map: Dict[Tuple[str,str,str,str], Dict[str,Any]] = {}
        wo_map:   Dict[Tuple[str,str,str,str], Dict[str,Any]] = {}
        X, Y = [], []
        for r in per_cycle_rows:
            key = (r["repo"], r["cycle_id"], exp_family(r.get("exp_label")), r.get("results_root"))
            if r["condition"] == "with":
                with_map[key] = r
            elif r["condition"] == "without":
                wo_map[key] = r
        for k in set(with_map.keys()).intersection(wo_map.keys()):
            rw = with_map[k]; ro = wo_map[k]
            if rw.get("succ") is True and ro.get("succ") is True:
                de_w = rw.get("delta_edges"); de_o = ro.get("delta_edges")
                if isinstance(de_w, (int,float)) and isinstance(de_o, (int,float)):
                    X.append(float(de_w)); Y.append(float(de_o))
        return X, Y

    b, c, total_pairs, matched, both_success, both_fail = paired_success_counts()

    # Two-sided (kept for completeness) and one-sided (directional: with > without)
    success_p_two_sided = mcnemar_p(b, c) if (b + c) > 0 else None
    success_p_one_sided = mcnemar_p_one_sided(b, c)

    x, y = paired_delta_edges()
    pairs_edges = len(x)
    nz = [a - b_ for a, b_ in zip(x, y) if (a is not None and b_ is not None and (a - b_) != 0.0)]
    pairs_edges_nonzero = len(nz)
    wil_p = (safe_wilcoxon(x, y) if pairs_edges > 0 else None)

    # Effect size on discordant pairs: share of "with" wins among (b+c)
    p_with_wins = (b / (b + c)) if (b + c) > 0 else None
    ci_lo, ci_hi = proportion_wilson_ci(b, b + c) if (b + c) > 0 else (None, None)

    if rows_with_without:
        rows_with_without.append({
            "Condition": "stats",
            "n_total": None, "n_success": None, "Success%": None,
            "ΔEdges_success_mean": None, "ΔEdges_success_std": None, "ΔEdges_success_median": None,
            "ΔNodes_success_mean": None, "ΔNodes_success_std": None,
            "ΔLOC_success_mean": None, "ΔLOC_success_std": None,
            "ZeroChange%": None, "NoTestRegression%": None,

            # McNemar results
            "Success_p_McNemar_two_sided": success_p_two_sided,
            "Success_p_McNemar_one_sided": success_p_one_sided,

            # Wilcoxon on paired-success ΔEdges
            "ΔEdges_success_wilcoxon_p": wil_p,

            # Pair counts
            "pairs_success": b + c,              # discordant pairs used by McNemar
            "pairs_edges": pairs_edges,          # paired-success for Wilcoxon
            "pairs_edges_nonzero": pairs_edges_nonzero,
            "b_with_better": b,
            "c_without_better": c,
            "pairs_total_possible": total_pairs,  # distinct (repo,cycle,expfam,root)
            "pairs_matched": matched,             # had both with & without
            "both_success": both_success,
            "both_fail": both_fail,

            # Effect size on discordant pairs
            "with_win_share": p_with_wins,       # b / (b + c)
            "with_win_share_ci_lo": ci_lo,       # Wilson 95% CI
            "with_win_share_ci_hi": ci_hi,
        })

    wv_path = Path(args.outdir) / "rq1_with_vs_without.csv"
    if rows_with_without:
        all_keys = []
        for r in rows_with_without:
            for k in r.keys():
                if k not in all_keys: all_keys.append(k)
        with wv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=all_keys); w.writeheader()
            for r in rows_with_without:
                w.writerow(r)
        print(f"Wrote: {wv_path}")
    else:
        print("[WARN] No data for rq1_with_vs_without.csv", file=sys.stderr)

    # ---------- per-cycle (raw) ----------
    if per_cycle_rows:
        fields = [
            "repo", "results_root", "cycle_id", "cycle_size", "condition", "succ",
            "pre_edges","post_edges","delta_edges",
            "pre_nodes","post_nodes","delta_nodes",
            "pre_loc","post_loc","delta_loc",
            "tests_pass_pct","delta_tests_vs_base",
            "variant_label","exp_label",
            "struct_improved","tests_nonregressed",
        ]
        pc_path = Path(args.outdir) / "rq1_per_cycle.csv"
        with pc_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in per_cycle_rows:
                row_out = {k: r.get(k) for k in fields if k not in ("struct_improved","tests_nonregressed")}
                pre = r.get("pre_edges"); post = r.get("post_edges")
                row_out["struct_improved"] = (isinstance(pre,(int,float)) and isinstance(post,(int,float)) and (post < pre))
                dt = r.get("delta_tests_vs_base")
                row_out["tests_nonregressed"] = (None if dt is None else (dt >= 0))
                w.writerow(row_out)
        print(f"Wrote: {pc_path}")
    else:
        print("[WARN] No per-cycle rows produced", file=sys.stderr)

if __name__ == "__main__":
    main()
