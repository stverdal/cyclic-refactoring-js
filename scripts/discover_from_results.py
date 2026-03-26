#!/usr/bin/env python3
"""
Scan a results directory and auto-generate repos.txt + cycles_to_analyze.txt
from whatever data actually exists on disk.

Usage:
  python3 scripts/discover_from_results.py \
      --results-root results \
      --exp-id expA \
      --repos-out repos.txt \
      --cycles-out cycles_to_analyze.txt

How it works:
  1. Lists results/<repo>/branches/<branch>/ directories.
  2. A branch is a **baseline** if it does NOT start with "atd-".
     (Convention: experiment branches are named  atd-<exp_id>-<cycle_id>.)
  3. A branch is an **experiment** if it matches  atd-<exp_id>-<cycle_id>.
     The cycle_id is extracted from the branch name.
  4. Only repos that have at least one baseline with ATD metrics are emitted
     to repos.txt.
  5. Only cycle_ids whose experiment branch has ATD metrics are emitted
     to cycles_to_analyze.txt.

Optional:
  --exp-id   If given, only consider experiment branches for this exp id
             (plus the _without_explanation variant).  Without this flag,
             all atd-* branches are considered.
  --require-baseline   (default: true)  Skip repos with no baseline metrics.
  --language <lang>    Language to put in the 4th column of repos.txt (default: "unknown").
                       Or use --repos-all <path> to look it up from repos_all.txt.
  --repos-all <path>   Path to repos_all.txt; used to fill in src_rel and language columns.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ATD_CANDIDATES = [
    "ATD_identification/ATD_metrics.json",
    "ATD_identification/scc_report.json",
]


def has_atd_metrics(branch_dir: Path) -> bool:
    return any((branch_dir / c).exists() for c in ATD_CANDIDATES)


def parse_experiment_branch(name: str) -> Optional[Tuple[str, str]]:
    """
    Parse 'atd-<exp_id>-<cycle_id>' -> (exp_id, cycle_id).

    cycle_id may contain hyphens (e.g. scc_0_cycle_0 after sanitize is scc-0-cycle-0),
    so we need to be careful. The exp_id is everything between the first 'atd-'
    and the last occurrence of a cycle-id-like pattern (scc_N_cycle_N or scc-N-cycle-N).
    """
    if not name.startswith("atd-"):
        return None

    rest = name[4:]  # strip 'atd-'

    # Try to find the cycle_id part: scc_N_cycle_N or scc-N-cycle-N
    # The cycle id in the original data is like scc_0_cycle_3,
    # but after sanitize() it becomes scc-0-cycle-3 (underscores stay, actually).
    # Let's check: sanitize replaces [^A-Za-z0-9._/-] with -, collapses --.
    # Underscore IS in [^...] so it gets replaced with -.
    # So scc_0_cycle_0 -> scc-0-cycle-0

    # Pattern: scc-<N>-cycle-<N> at the end
    m = re.search(r'(scc[_-]\d+[_-]cycle[_-]\d+)$', rest)
    if m:
        cycle_sanitized = m.group(1)
        exp_part = rest[:m.start()].rstrip('-')
        # Reverse the sanitization on cycle_id: scc-0-cycle-0 -> scc_0_cycle_0
        cycle_id = re.sub(r'scc[_-](\d+)[_-]cycle[_-](\d+)', r'scc_\1_cycle_\2', cycle_sanitized)
        return (exp_part, cycle_id)

    return None


def load_repos_all(path: Path) -> Dict[str, Tuple[str, str, str]]:
    """
    Parse repos_all.txt -> {repo_name: (branch, src_rel, language)}.
    Handles trailing comments like  (Bare 4354 LOC).
    Stops at lines starting with 'NOT '.
    """
    out: Dict[str, Tuple[str, str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("NOT "):
            break  # everything after "NOT WORKING" is excluded
        # Strip trailing parenthetical comments:  (Bare 4354 LOC)
        line = re.sub(r'\s*\(.*\)\s*$', '', line).strip()
        parts = line.split()
        if len(parts) < 2:
            continue
        repo = parts[0]
        branch = parts[1]
        src_rel = parts[2] if len(parts) >= 3 else "."
        lang = parts[3] if len(parts) >= 4 else "unknown"
        out[repo] = (branch, src_rel, lang)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover repos + cycles from results/ directory.")
    ap.add_argument("--results-root", required=True, help="Path to results/ directory")
    ap.add_argument("--exp-id", default=None,
                    help="Only consider experiment branches for this exp-id (+ _without_explanation variant)")
    ap.add_argument("--repo", default=None, action="append", dest="repos_filter",
                    help="Only include this repo (can be repeated, e.g. --repo jinja --repo kombu)")
    ap.add_argument("--repos-out", default="repos.txt", help="Output repos.txt path")
    ap.add_argument("--cycles-out", default="cycles_to_analyze.txt", help="Output cycles_to_analyze.txt path")
    ap.add_argument("--repos-all", default=None,
                    help="Path to repos_all.txt for src_rel + language lookup")
    ap.add_argument("--language", default="unknown",
                    help="Default language for repos.txt 4th column (if --repos-all not given)")
    ap.add_argument("--require-baseline", action="store_true", default=True,
                    help="Only include repos that have a baseline with ATD metrics (default: true)")
    ap.add_argument("--no-require-baseline", action="store_false", dest="require_baseline")
    args = ap.parse_args()

    results_root = Path(args.results_root).resolve()
    if not results_root.is_dir():
        raise SystemExit(f"Results root not found: {results_root}")

    # Load repos_all for metadata lookup  (auto-detect if not given)
    repos_all: Dict[str, Tuple[str, str, str]] = {}
    repos_all_arg = args.repos_all
    if not repos_all_arg:
        # Auto-detect: check for repos_all.txt next to results root
        candidate = results_root.parent / "repos_all.txt"
        if candidate.exists():
            repos_all_arg = str(candidate)
            print(f"[INFO] Auto-detected repos_all.txt at {candidate}")
    if repos_all_arg:
        repos_all_path = Path(repos_all_arg).resolve()
        if repos_all_path.exists():
            repos_all = load_repos_all(repos_all_path)
            print(f"[INFO] Loaded {len(repos_all)} repos from {repos_all_path}")

    # Determine which exp_ids to consider
    exp_ids_filter: Optional[set] = None
    if args.exp_id:
        # After sanitize(), underscores become dashes in branch names.
        # Include both forms so the filter matches.
        eid = args.exp_id
        wo = f"{eid}_without_explanation"
        eid_san = eid.replace("_", "-")
        wo_san = wo.replace("_", "-")
        exp_ids_filter = {eid, wo, eid_san, wo_san}

    # Scan results/
    repos_found: Dict[str, str] = {}  # repo -> baseline_branch
    cycles_found: Dict[Tuple[str, str], List[str]] = {}  # (repo, baseline) -> [cycle_ids]

    repos_filter_set = set(args.repos_filter) if args.repos_filter else None
    if repos_filter_set:
        print(f"[INFO] Filtering to repos: {repos_filter_set}")

    for repo_dir in sorted(results_root.iterdir()):
        if not repo_dir.is_dir():
            continue
        repo = repo_dir.name
        if repos_filter_set and repo not in repos_filter_set:
            continue
        branches_dir = repo_dir / "branches"
        if not branches_dir.is_dir():
            print(f"  [DEBUG] {repo}: no branches/ directory, skipping")
            continue

        # Find baseline branch(es) — those not starting with atd-
        baselines: List[str] = []
        baselines_no_metrics: List[str] = []
        experiment_branches: List[str] = []

        # Discover branch directories recursively.
        # A directory is a "branch leaf" if it contains ATD_identification/,
        # code_quality_checks/, or starts with atd-.
        # Otherwise, descend into its children (handles branches like dev/v5).
        def discover_branches(parent: Path, prefix: str = "") -> None:
            for child in sorted(parent.iterdir()):
                if not child.is_dir():
                    continue
                bname = f"{prefix}{child.name}" if prefix else child.name

                # If it starts with atd-, it's an experiment branch at any depth
                if child.name.startswith("atd-"):
                    experiment_branches.append(bname)
                    continue

                # Check if this looks like a branch leaf (has known subdirs/files)
                is_leaf = (
                    (child / "ATD_identification").is_dir()
                    or (child / "code_quality_checks").is_dir()
                    or any(child.iterdir())  # has content
                    and not any(  # but none of its children are directories with branches
                        (child / c).is_dir()
                        and not c.startswith("ATD_")
                        and not c.startswith("code_quality")
                        and c not in {"ATD_identification", "code_quality_checks", ".git"}
                        for c in [d.name for d in child.iterdir() if d.is_dir()]
                    )
                )

                # More reliable: if it has ATD_identification/ or code_quality_checks/, it's a leaf
                has_known = (
                    (child / "ATD_identification").is_dir()
                    or (child / "code_quality_checks").is_dir()
                )

                if has_known:
                    # It's a real branch directory
                    if has_atd_metrics(child):
                        baselines.append(bname)
                    else:
                        baselines_no_metrics.append(bname)
                else:
                    # No known dirs — check if children are subdirectories (nested branch like dev/v5)
                    subdirs = [d for d in child.iterdir() if d.is_dir()]
                    if subdirs:
                        discover_branches(child, prefix=f"{bname}/")
                    else:
                        # Leaf with no known structure — treat as baseline without metrics
                        baselines_no_metrics.append(bname)

        discover_branches(branches_dir)

        # If require_baseline is off, include baselines without metrics too
        if not args.require_baseline:
            baselines.extend(baselines_no_metrics)
            baselines_no_metrics = []

        if not baselines:
            if baselines_no_metrics:
                print(f"  [DEBUG] {repo}: found branch(es) {baselines_no_metrics} but none have ATD metrics")
                print(f"  [DEBUG]   checked for: {ATD_CANDIDATES}")
                for bname in baselines_no_metrics:
                    bd = branches_dir / bname
                    atd_dir = bd / "ATD_identification"
                    if atd_dir.is_dir():
                        files = [f.name for f in atd_dir.iterdir() if f.is_file()]
                        print(f"  [DEBUG]   {bname}/ATD_identification/ contains: {files}")
                    else:
                        print(f"  [DEBUG]   {bname}/ATD_identification/ does not exist")
                print(f"  [HINT]  Try --no-require-baseline to include {repo} anyway")
            else:
                print(f"  [DEBUG] {repo}: no non-atd branches found (only {len(experiment_branches)} experiment branches)")
            continue

        # Use the first baseline (there should typically be only one)
        baseline = baselines[0]
        repos_found[repo] = baseline

        # Extract cycle_ids from experiment branches
        cids: List[str] = []
        seen_cids: set = set()
        for bname in experiment_branches:
            parsed = parse_experiment_branch(bname)
            if parsed is None:
                print(f"  [DEBUG] Could not parse branch: {bname}")
                continue
            exp_id, cycle_id = parsed
            if exp_ids_filter and exp_id not in exp_ids_filter:
                print(f"  [DEBUG] Skipping branch {bname}: exp_id '{exp_id}' not in filter {exp_ids_filter}")
                continue
            if cycle_id in seen_cids:
                continue
            seen_cids.add(cycle_id)
            cids.append(cycle_id)

        if cids:
            cycles_found[(repo, baseline)] = sorted(set(cids))
        
        if not cids and experiment_branches:
            print(f"  [DEBUG] {repo}: found {len(experiment_branches)} experiment branches but 0 matched.")
            print(f"  [DEBUG]   first 3 branches: {experiment_branches[:3]}")

    # Write repos.txt
    repos_out = Path(args.repos_out)
    lines_repos: List[str] = []
    for repo in sorted(repos_found.keys()):
        baseline = repos_found[repo]
        if repo in repos_all:
            _branch, src_rel, lang = repos_all[repo]
            lines_repos.append(f"{repo} {baseline} {src_rel} {lang}")
        else:
            lines_repos.append(f"{repo} {baseline} . {args.language}")

    repos_out.write_text("\n".join(lines_repos) + ("\n" if lines_repos else ""), encoding="utf-8")
    print(f"Wrote {len(lines_repos)} repos to {repos_out}")

    # Write cycles_to_analyze.txt
    cycles_out = Path(args.cycles_out)
    lines_cycles: List[str] = []
    for (repo, baseline), cids in sorted(cycles_found.items()):
        for cid in cids:
            lines_cycles.append(f"{repo} {baseline} {cid}")

    cycles_out.write_text("\n".join(lines_cycles) + ("\n" if lines_cycles else ""), encoding="utf-8")
    print(f"Wrote {len(lines_cycles)} cycles to {cycles_out}")

    # Summary
    total_repos = len(repos_found)
    repos_with_cycles = len(cycles_found)
    total_cycles = sum(len(v) for v in cycles_found.values())
    print(f"\nSummary:")
    print(f"  Repos discovered:        {total_repos}")
    print(f"  Repos with experiments:  {repos_with_cycles}")
    print(f"  Total unique cycles:     {total_cycles}")

    for repo in sorted(repos_found.keys()):
        baseline = repos_found[repo]
        n = len(cycles_found.get((repo, baseline), []))
        print(f"  {repo} ({baseline}): {n} cycles")


if __name__ == "__main__":
    main()
