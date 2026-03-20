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
    Stops at blank lines or lines starting with non-repo text.
    """
    out: Dict[str, Tuple[str, str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("NOT "):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        # Skip comment-like things (lines with parenthetical notes only)
        repo = parts[0]
        branch = parts[1]
        src_rel = parts[2] if len(parts) >= 3 else ""
        lang = parts[3] if len(parts) >= 4 else "unknown"
        # Only take clean entries (no parenthetical notes in repo name)
        if "(" in repo or repo.isupper():
            continue
        out[repo] = (branch, src_rel, lang)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover repos + cycles from results/ directory.")
    ap.add_argument("--results-root", required=True, help="Path to results/ directory")
    ap.add_argument("--exp-id", default=None,
                    help="Only consider experiment branches for this exp-id (+ _without_explanation variant)")
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

    # Load repos_all for metadata lookup
    repos_all: Dict[str, Tuple[str, str, str]] = {}
    if args.repos_all:
        repos_all_path = Path(args.repos_all).resolve()
        if repos_all_path.exists():
            repos_all = load_repos_all(repos_all_path)

    # Determine which exp_ids to consider
    exp_ids_filter: Optional[set] = None
    if args.exp_id:
        exp_ids_filter = {args.exp_id, f"{args.exp_id}_without_explanation"}

    # Scan results/
    repos_found: Dict[str, str] = {}  # repo -> baseline_branch
    cycles_found: Dict[Tuple[str, str], List[str]] = {}  # (repo, baseline) -> [cycle_ids]

    for repo_dir in sorted(results_root.iterdir()):
        if not repo_dir.is_dir():
            continue
        repo = repo_dir.name
        branches_dir = repo_dir / "branches"
        if not branches_dir.is_dir():
            continue

        # Find baseline branch(es) — those not starting with atd-
        baselines: List[str] = []
        experiment_branches: List[str] = []

        for branch_dir in sorted(branches_dir.iterdir()):
            if not branch_dir.is_dir():
                continue
            bname = branch_dir.name
            if bname.startswith("atd-"):
                experiment_branches.append(bname)
            else:
                if args.require_baseline and not has_atd_metrics(branch_dir):
                    continue
                baselines.append(bname)

        if not baselines:
            # Could not find a baseline; skip this repo
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
                continue
            exp_id, cycle_id = parsed
            if exp_ids_filter and exp_id not in exp_ids_filter:
                continue
            if cycle_id in seen_cids:
                continue
            # Optionally check that the branch actually has metrics
            branch_dir = branches_dir / bname
            if has_atd_metrics(branch_dir):
                seen_cids.add(cycle_id)
                cids.append(cycle_id)

        if cids:
            cycles_found[(repo, baseline)] = sorted(set(cids))

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
