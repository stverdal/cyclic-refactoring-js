#!/usr/bin/env python3
from __future__ import annotations
import json, math, re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from scipy.stats import binomtest

# ---------- constants ----------
ATD_DIR = "ATD_identification"
ATD_METRICS = f"{ATD_DIR}/ATD_metrics.json"
ATD_METRICS_FALLBACK = f"{ATD_DIR}/scc_report.json"
ATD_MODULE_CYCLES = f"{ATD_DIR}/module_cycles.json"
ATD_MODULE_CYCLES_FALLBACK = f"{ATD_DIR}/cycle_catalog.json"
CQ_METRICS = "code_quality_checks/metrics.json"
CQ_METRICS_FALLBACK = "code_quality_checks.json"

# ---------- basic IO ----------
def read_json(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def read_repos_file(path: Path) -> List[Tuple[str, str, str]]:
    repos = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        repo = parts[0]
        baseline = parts[1]
        src_rel = parts[2] if len(parts) >= 3 else ""
        repos.append((repo, baseline, src_rel))
    return repos

# ---------- helpers ----------
def sanitize(s: str) -> str:
    s = s.replace(" ", "-")
    s = re.sub(r"[^A-Za-z0-9._/-]", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-/")
    return s

def branch_for(exp_label: str, cycle_id: str) -> str:
    return "branches/" + sanitize(f"atd-{exp_label}-{cycle_id}")

def parse_cycles(cycles_file: Path) -> Dict[Tuple[str, str], List[str]]:
    out: Dict[Tuple[str, str], List[str]] = {}
    for line in cycles_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        repo, branch, cid = parts[0], parts[1], parts[2]
        out.setdefault((repo, branch), []).append(cid)
    return out

def load_json_any(base: Path, candidates: List[str]) -> Optional[Dict[str, Any]]:
    for rel in candidates:
        p = base / rel
        if p.exists():
            return read_json(p)
    return None

def mean_or_none(vals: List[Optional[float]]) -> Optional[float]:
    xs = [float(v) for v in vals if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))]
    return (sum(xs) / len(xs)) if xs else None

def std_or_none(vals: List[Optional[float]]) -> Optional[float]:
    xs = [float(v) for v in vals if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))]
    if len(xs) < 2:
        return None
    m = sum(xs)/len(xs)
    return (sum((x-m)**2 for x in xs)/(len(xs)-1)) ** 0.5

def safe_sub(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return float(a) - float(b)

def cycle_size_from_baseline(base_repo_branch_dir: Path, cycle_id: str) -> Optional[int]:
    mod = read_json(base_repo_branch_dir / ATD_MODULE_CYCLES)
    if not mod:
        mod = read_json(base_repo_branch_dir / ATD_MODULE_CYCLES_FALLBACK)
    if not mod:
        return None
    # Support both module_cycles.json (sccs[].representative_cycles[])
    # and cycle_catalog.json (cycles[] flat list)
    for scc in mod.get("sccs", []):
        for cyc in scc.get("representative_cycles", []):
            if str(cyc.get("id")) == str(cycle_id):
                if "length" in cyc and isinstance(cyc["length"], int):
                    return int(cyc["length"])
                nodes = cyc.get("nodes") or []
                return int(len(nodes))
    # cycle_catalog.json stores cycles as a flat list
    for cyc in mod.get("cycles", []):
        cid = cyc.get("id") or cyc.get("cycle_id")
        if str(cid) == str(cycle_id):
            if "length" in cyc and isinstance(cyc["length"], int):
                return int(cyc["length"])
            nodes = cyc.get("nodes") or cyc.get("modules") or []
            return int(len(nodes))
    return None

# ---------- metrics parsing ----------
def get_tests_pass_percent(summary_json: Optional[Dict[str, Any]]) -> Optional[float]:
    if not summary_json:
        return None
    junit = summary_json.get("pytest") or {}
    tests    = junit.get("tests") or 0
    failures = junit.get("failures") or 0
    errors   = junit.get("errors") or 0
    skipped  = junit.get("skipped") or 0
    if tests <= 0:
        return None
    passed = max(0, tests - failures - errors - skipped)
    return round(100.0 * passed / tests, 2)

def get_scc_metrics(atd: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not atd:
        return {"scc_count": None, "max_scc_size": None, "avg_scc_size": None,
                "total_nodes_in_cyclic_sccs": None, "total_edges_in_cyclic_sccs": None,
                "total_loc_in_cyclic_sccs": None, "cycle_pressure_lb": None,
                "avg_density_directed": None, "avg_edge_surplus_lb": None}
    # scc_report.json stores top-level metrics under "global_metrics";
    # ATD_metrics.json (if it exists) stores them at the top level.
    gm = atd.get("global_metrics") or atd
    sccs = atd.get("sccs") or []
    avg_density = None
    avg_surplus = None
    if sccs:
        dens = [s.get("density_directed", 0.0) for s in sccs if isinstance(s, dict)]
        surp = [s.get("edge_surplus_lb", 0) for s in sccs if isinstance(s, dict)]
        if dens:
            avg_density = round(sum(dens) / len(dens), 4)
        if surp:
            avg_surplus = round(sum(surp) / len(surp), 2)
    return {
        "scc_count": gm.get("scc_count"),
        "max_scc_size": gm.get("max_scc_size"),
        "avg_scc_size": gm.get("avg_scc_size"),
        "total_nodes_in_cyclic_sccs": gm.get("total_nodes_in_cyclic_sccs"),
        "total_edges_in_cyclic_sccs": gm.get("total_edges_in_cyclic_sccs"),
        "total_loc_in_cyclic_sccs": gm.get("total_loc_in_cyclic_sccs"),
        "cycle_pressure_lb": gm.get("cycle_pressure_lb"),
        "avg_density_directed": avg_density,
        "avg_edge_surplus_lb": avg_surplus,
    }

def extract_quality_metrics(j: Dict[str, Any]) -> Dict[str, Any]:
    # ---- tests pass % ----
    junit = j.get("pytest") or {}
    tests    = junit.get("tests") or 0
    failures = junit.get("failures") or 0
    errors   = junit.get("errors") or 0
    skipped  = junit.get("skipped") or 0
    passed   = max(0, tests - failures - errors - skipped)
    pass_pct = (100.0 * passed / tests) if tests else 0.0

    # ---- static tool metrics ----
    ruff_issues = (j.get("ruff") or {}).get("issues")
    mi_avg      = (j.get("radon_mi") or {}).get("avg")

    # Radon CC buckets -> D+E+F (heavy/too complex)
    by_rank = ((j.get("radon_cc") or {}).get("by_rank") or {})
    d_rank = by_rank.get("D", 0) or 0
    e_rank = by_rank.get("E", 0) or 0
    f_rank = by_rank.get("F", 0) or 0
    cc_dplus_funcs = d_rank + e_rank + f_rank

    bandit_high = (j.get("bandit") or {}).get("high")

    # ---- PyExamine by type (split columns) ----
    px = j.get("pyexamine") or {}
    wbt = (px.get("weighted_by_type") or {})
    pyexam_arch = wbt.get("Architectural")
    pyexam_code = wbt.get("Code")
    pyexam_struct = wbt.get("Structural")

    # Optional extras (you’re using these in RQ2)
    coverage_line_percent = (j.get("coverage") or {}).get("line_percent")
    mypy_errors = (j.get("mypy") or {}).get("errors")

    return {
        "ruff_issues": ruff_issues,
        "mi_avg": mi_avg,
        "cc_dplus_funcs": cc_dplus_funcs,          # NEW: D+E+F
        "pyexam_arch": pyexam_arch,                # NEW: Architectural
        "pyexam_code": pyexam_code,                # NEW: Code
        "pyexam_struct": pyexam_struct,            # NEW: Structural
        "bandit_high": bandit_high,
        "test_pass_pct": round(pass_pct, 2),
        "coverage_line_percent": coverage_line_percent,
        "mypy_errors": mypy_errors,
    }

# ---------- math / stats ----------
def mcnemar_p(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return float("nan")
    res = binomtest(min(b, c), n=n, p=0.5, alternative="two-sided")
    return float(res.pvalue)

# ---------- new: simple mapping roots <-> exp ids ----------
def map_roots_exps(results_roots: List[str], exp_ids: List[str]) -> List[Tuple[Path, str, str]]:
    """
    Returns a list of (results_root, EXP_WITH, EXP_WITHOUT) by pairing
    each ROOT with the EXP at the same position. WITHOUT is derived as
    '<EXP>_without_explanation'.
    """
    if not results_roots:
        raise SystemExit("Missing --results-roots")
    if not exp_ids:
        raise SystemExit("Missing --exp-ids")
    if len(results_roots) != len(exp_ids):
        raise SystemExit("Expected same number of --results-roots and --exp-ids")
    out = []
    for root, exp in zip(results_roots, exp_ids):
        out.append((Path(root), exp, f"{exp}_without_explanation"))
    return out

# --- experiment labels ---
def exp_family(exp_label: str) -> str:
    s = str(exp_label or "")
    suff = "_without_explanation"
    return s[:-len(suff)] if s.endswith(suff) else s

# --- stats: convenience ---
def mean_std(xs: List[float]) -> Tuple[float, float]:
    if not xs:
        return (float("nan"), float("nan"))
    m = sum(xs)/len(xs)
    if len(xs) < 2:
        return (m, float("nan"))
    v = sum((x-m)*(x-m) for x in xs)/(len(xs)-1)
    return (m, v**0.5)

# --- McNemar (one-sided) ---
def mcnemar_p_one_sided(b: int, c: int) -> Optional[float]:
    """H0: P(with win)=P(without win); H1: P(with win) > P(without win)."""
    try:
        from scipy.stats import binomtest
    except Exception:
        return None
    n = b + c
    if n <= 0:
        return None
    return float(binomtest(b, n=n, p=0.5, alternative="greater").pvalue)

# --- RQ3 bin helpers ---
def parse_bins_arg(bins_arg: Optional[str]) -> List[Tuple[str, int, int]]:
    """
    Parse --bins like: "Small:2-4,Large:5-8"
    Returns list of (label, lo, hi), inclusive.
    """
    if not bins_arg:
        return [("Small", 2, 4), ("Large", 5, 8)]
    out: List[Tuple[str,int,int]] = []
    for tok in bins_arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" not in tok or "-" not in tok:
            raise SystemExit(f"Bad --bins token: {tok} (expected Label:lo-hi)")
        label, rng = tok.split(":", 1)
        lo_s, hi_s = rng.split("-", 1)
        try:
            lo = int(lo_s.strip()); hi = int(hi_s.strip())
        except ValueError:
            raise SystemExit(f"Bad range in --bins token: {tok}")
        if lo > hi:
            lo, hi = hi, lo
        out.append((label.strip(), lo, hi))
    if not out:
        raise SystemExit("Parsed empty --bins")
    return out

def size_to_bin(size: int, bins: List[Tuple[str,int,int]]) -> Optional[str]:
    for label, lo, hi in bins:
        if lo <= size <= hi:
            return label
    return None
