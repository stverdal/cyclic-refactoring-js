#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pick_cycles import _node_in_excluded_dir

# -------------------------
# Parsing / loading helpers
# -------------------------

def parse_repos_file(path: Path) -> List[Tuple[str, str, str, str]]:
    """
    repos.txt line:
      <repo_name> <base_branch> <entry> <language?>
    """
    rows: List[Tuple[str, str, str, str]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 3:
            raise ValueError(f"{path}:{i}: expected >=3 cols (repo, branch, entry)")
        repo, branch, entry = parts[0], parts[1], parts[2]
        lang = parts[3] if len(parts) >= 4 else "unknown"
        rows.append((repo, branch, entry, lang))
    return rows


def load_json(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def iter_catalog_cycles(catalog: Dict[str, Any]):
    # catalog schema: {"sccs":[{"cycles":[...]}]}
    for scc in (catalog.get("sccs") or []):
        for cyc in (scc.get("cycles") or []):
            yield cyc


def cycle_size(cyc: Dict[str, Any]) -> Optional[int]:
    ln = cyc.get("length")
    if isinstance(ln, int):
        return ln
    nodes = cyc.get("nodes")
    if isinstance(nodes, list):
        return len(nodes)
    return None


def cycle_id(cyc: Dict[str, Any]) -> Optional[str]:
    cid = cyc.get("id")
    return str(cid) if cid is not None else None


def cycle_nodes(cyc: Dict[str, Any]) -> List[str]:
    nodes = cyc.get("nodes")
    if isinstance(nodes, list):
        return [str(x) for x in nodes]
    return []


def cycle_pagerank_avg(cyc: Dict[str, Any]) -> float:
    m = cyc.get("metrics")
    if isinstance(m, dict):
        v = m.get("pagerank_avg")
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


# -------------------------
# Bin-based strategy types
# -------------------------

# "We allow a file to appear in at most MAX_NODE_USE_PER_REPO selected cycles
# within the same repo, to reduce intra-repo dependence while keeping enough data."
MAX_NODE_USE_PER_REPO = 2


@dataclass(frozen=True)
class Candidate:
    repo: str
    branch: str
    lang: str
    cid: str
    size: int
    nodes: Tuple[str, ...]
    bin_key: str
    pagerank_avg: float


def parse_bins(spec: str) -> List[Tuple[int, int, str]]:
    """Parse spec like '2-3,4-6,7-8' into [(lo, hi, key), ...]."""
    out: List[Tuple[int, int, str]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            raise ValueError(f"Bad --size-bins item '{part}' (expected 'lo-hi')")
        a, b = part.split("-", 1)
        lo = int(a.strip())
        hi = int(b.strip())
        if lo > hi:
            lo, hi = hi, lo
        out.append((lo, hi, f"{lo}-{hi}"))
    if not out:
        raise ValueError("No bins parsed from --size-bins")
    return out


def bin_for_size(sz: int, bins: List[Tuple[int, int, str]]) -> Optional[str]:
    for lo, hi, key in bins:
        if lo <= sz <= hi:
            return key
    return None


def derive_bin_priority(bins: List[Tuple[int, int, str]]) -> List[str]:
    """Deterministic: larger cycles first (by hi desc, then lo desc)."""
    ordered = sorted(bins, key=lambda t: (t[1], t[0]), reverse=True)
    return [key for _lo, _hi, key in ordered]


def feasible_under_node_cap(
    cand: Candidate,
    node_use: Dict[str, Counter],
) -> bool:
    ru = node_use[cand.repo]
    for n in cand.nodes:
        if ru.get(n, 0) >= MAX_NODE_USE_PER_REPO:
            return False
    return True


def overlap_count(
    cand: Candidate,
    node_use: Dict[str, Counter],
) -> int:
    ru = node_use[cand.repo]
    return sum(1 for n in cand.nodes if ru.get(n, 0) > 0)


def score_candidate_min(
    cand: Candidate,
    per_repo_selected: Counter,
    node_use: Dict[str, Counter],
) -> Tuple:
    """
    Deterministic lexicographic score where SMALLER is better (use min()).
    1) repo fairness: fewer already selected in repo
    2) overlap: fewer reused nodes
    3) size: prefer larger => use negative size
    4) stable tie-break: repo, cid
    """
    return (
        int(per_repo_selected.get(cand.repo, 0)),
        overlap_count(cand, node_use),
        -cand.size,
        cand.repo,
        cand.cid,
    )


# ----------------------------------------
# Fair selection within one exact-size stratum
# ----------------------------------------

def select_for_size_balanced_batch(
    queues_by_repo: Dict[str, deque],
    take_n: int,
    repos_order: List[str],
    repos_rank: Dict[str, int],
    per_repo_selected_global: Counter,
) -> List[Tuple[str, str]]:
    """
    Pick up to take_n cycles for one size bucket.

    Strategy:
      - If there are >= take_n repos with candidates, take 1 from distinct repos (global fairness).
      - Else take 1 from each available repo, then fill remaining fairly.
    """
    chosen: List[Tuple[str, str]] = []
    K = sum(1 for q in queues_by_repo.values() if q)
    if K == 0 or take_n <= 0:
        return chosen

    if K >= take_n:
        candidates = [r for r in repos_order if queues_by_repo.get(r)]
        candidates.sort(key=lambda r: (per_repo_selected_global[r], repos_rank.get(r, 10**9), r))
        for repo in candidates[:take_n]:
            cid = queues_by_repo[repo].popleft()
            chosen.append((repo, cid))
            per_repo_selected_global[repo] += 1
        return chosen

    # K < take_n
    per_size_taken = Counter()
    candidates = [r for r in repos_order if queues_by_repo.get(r)]
    candidates.sort(key=lambda r: (per_repo_selected_global[r], repos_rank.get(r, 10**9), r))

    # one to each repo first
    for repo in candidates:
        if len(chosen) >= take_n:
            break
        q = queues_by_repo.get(repo)
        if not q:
            continue
        cid = q.popleft()
        chosen.append((repo, cid))
        per_repo_selected_global[repo] += 1
        per_size_taken[repo] += 1

    # fill remaining fairly
    remaining = take_n - len(chosen)
    available = {r for r, q in queues_by_repo.items() if q}
    while remaining > 0 and available:
        repo = min(
            available,
            key=lambda r: (per_repo_selected_global[r], per_size_taken[r], repos_rank.get(r, 10**9), r),
        )
        q = queues_by_repo[repo]
        cid = q.popleft()
        chosen.append((repo, cid))
        per_repo_selected_global[repo] += 1
        per_size_taken[repo] += 1
        remaining -= 1
        if not q:
            available.discard(repo)

    return chosen


def pick_one_round_robin(
    by_size_repo_queues: Dict[int, Dict[str, deque]],
    *,
    size_order: List[int],
    repos_rank: Dict[str, int],
    per_repo_selected_global: Counter,
    per_size_selected: Counter,
) -> Optional[Tuple[int, str, str]]:
    """
    Spillover selection: pick ONE cycle while:
      - rotating across sizes (round-robin)
      - choosing repo with fewest global picks, then fewest picks for that size, then repos.txt order
    """
    for sz in size_order:
        repo_queues = by_size_repo_queues.get(sz) or {}
        candidates = [r for r, q in repo_queues.items() if q]
        if not candidates:
            continue

        repo = min(
            candidates,
            key=lambda r: (per_repo_selected_global[r], per_size_selected[sz], repos_rank.get(r, 10**9), r),
        )
        cid = repo_queues[repo].popleft()
        if not repo_queues[repo]:
            del repo_queues[repo]
        if not repo_queues:
            by_size_repo_queues.pop(sz, None)

        per_repo_selected_global[repo] += 1
        per_size_selected[sz] += 1
        return (sz, repo, cid)

    return None


# -------------------------
# Main
# -------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Build per-repo cycle_catalog.json (always rebuilt) and write cycles_to_analyze.txt.\n"
            "Selection is even-by-exact-cycle-size (within [min,max] or observed sizes), with repo-fairness."
        )
    )
    ap.add_argument("--repos-file", required=True)
    ap.add_argument("--results-root", required=True)
    ap.add_argument("--total", type=int, required=True, help="Total cycles to select (global)")
    ap.add_argument("--min-size", type=int, default=None)
    ap.add_argument("--max-size", type=int, default=None)
    ap.add_argument("--ascending-sizes", action="store_true")
    ap.add_argument("--strategy", choices=["balanced", "importance", "bin"], default="balanced",
                    help=(
                        "Selection strategy. 'balanced' (default) distributes evenly across "
                        "cycle sizes with repo fairness. 'importance' ranks all candidates by "
                        "average PageRank (descending) and picks the top --total cycles. "
                        "'bin' uses size-bin targets with node-reuse caps and repo fairness."
                    ))
    ap.add_argument("--output", required=True, help="Path to cycles_to_analyze.txt")
    ap.add_argument("--size-bins", type=str, default="",
                    help='Comma-separated size bins, e.g. "2-3,4-6,7-8" (required for strategy=bin)')
    ap.add_argument("--max-per-repo", type=int, default=0,
                    help="Max cycles per repo (required for strategy=bin; 0=unlimited for other strategies)")

    # Catalog generation knobs
    ap.add_argument("--max-cycle-len", type=int, default=8)
    ap.add_argument("--attempts-per-scc", type=int, default=5000)
    ap.add_argument("--max-cycles-per-scc", type=int, default=200)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument(
        "--exclude-dirs", nargs="*", default=[],
        help=(
            "Directory names or path prefixes to exclude from cycle selection. "
            "Passed through to pick_cycles.py and also applied as a post-filter. "
            "Example: --exclude-dirs 'VPA Framework' tests"
        ),
    )

    args = ap.parse_args()

    if args.total <= 0:
        raise SystemExit("--total must be > 0")

    if args.strategy == "bin":
        if not args.size_bins:
            raise SystemExit("--size-bins is required when --strategy=bin")
        if args.max_per_repo <= 0:
            raise SystemExit("--max-per-repo must be > 0 when --strategy=bin")

    repos_file = Path(args.repos_file).resolve()
    results_root = Path(args.results_root).resolve()
    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    repos = parse_repos_file(repos_file)
    repos_order = [r for (r, _b, _e, _l) in repos]
    repos_rank = {r: i for i, r in enumerate(repos_order)}
    repo_to_branch: Dict[str, str] = {repo: branch for (repo, branch, _e, _l) in repos}
    repo_to_lang: Dict[str, str] = {repo: lang for (repo, _branch, _e, lang) in repos}

    # Collect candidates: by_size[size][repo] = [cycle_id...]
    by_size: Dict[int, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    # For importance strategy: (repo, cycle_id, size, pagerank_avg)
    all_candidates: List[Tuple[str, str, int, float]] = []
    # For bin strategy: full Candidate objects
    bin_candidates: List[Candidate] = []
    # Pre-parse bins once (only used when strategy=bin, but cheap to compute)
    _parsed_bins = parse_bins(args.size_bins) if args.strategy == "bin" and args.size_bins else []

    # Run pick_cycles.py (ALWAYS) to rebuild catalog
    import subprocess
    import sys
    pick_cycles_py = Path(__file__).resolve().parent / "pick_cycles.py"

    for repo, branch, _entry, _lang in repos:
        atd_dir = results_root / repo / "branches" / branch / "ATD_identification"
        graph_json = atd_dir / "dependency_graph.json"
        scc_report = atd_dir / "scc_report.json"
        catalog_json = atd_dir / "cycle_catalog.json"

        if not graph_json.exists() or not scc_report.exists():
            continue

        cmd = [
            sys.executable,
            str(pick_cycles_py),
            "--dependency-graph", str(graph_json),
            "--scc-report", str(scc_report),
            "--out", str(catalog_json),
            "--repo", repo,
            "--base-branch", branch,
            "--max-cycle-len", str(args.max_cycle_len),
            "--attempts-per-scc", str(args.attempts_per_scc),
            "--max-cycles-per-scc", str(args.max_cycles_per_scc),
            "--seed", str(args.seed),
        ]
        if args.exclude_dirs:
            cmd.extend(["--exclude-dirs"] + args.exclude_dirs)
        print("$ " + " ".join(cmd))
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            print(f"[WARN] pick_cycles failed for {repo}@{branch} (rc={rc}); skipping repo")
            continue

        catalog = load_json(catalog_json)
        if not catalog:
            continue

        for cyc in iter_catalog_cycles(catalog):
            sz = cycle_size(cyc)
            cid = cycle_id(cyc)
            if sz is None or cid is None:
                continue
            if args.min_size is not None and sz < args.min_size:
                continue
            if args.max_size is not None and sz > args.max_size:
                continue
            # Post-filter: skip cycles with nodes in excluded directories
            if args.exclude_dirs:
                nodes = cyc.get("nodes") or []
                if any(_node_in_excluded_dir(n, args.exclude_dirs) for n in nodes):
                    continue
            by_size[sz][repo].append(cid)
            pr_avg = float((cyc.get("metrics") or {}).get("pagerank_avg", 0.0))
            all_candidates.append((repo, cid, sz, pr_avg))
            # Also build Candidate for bin strategy
            nodes_list = cycle_nodes(cyc)
            bin_key = ""
            if _parsed_bins:
                bk = bin_for_size(sz, _parsed_bins)
                if bk is not None:
                    bin_key = bk
            bin_candidates.append(Candidate(
                repo=repo,
                branch=repo_to_branch[repo],
                lang=repo_to_lang.get(repo, "unknown"),
                cid=cid,
                size=sz,
                nodes=tuple(nodes_list),
                bin_key=bin_key,
                pagerank_avg=pr_avg,
            ))

    # Deduplicate + deterministic sort
    for sz in list(by_size.keys()):
        for r in list(by_size[sz].keys()):
            by_size[sz][r] = sorted(set(by_size[sz][r]))

    if not by_size:
        raise SystemExit("No cycle candidates found. Did you collect baselines (dependency_graph + scc_report)?")

    # ---- Importance-based strategy ----
    if args.strategy == "importance":
        # Deduplicate candidates
        seen_keys: set = set()
        unique_candidates: List[Tuple[str, str, int, float]] = []
        for repo, cid, sz, pr_avg in all_candidates:
            key = (repo, cid)
            if key not in seen_keys:
                seen_keys.add(key)
                unique_candidates.append((repo, cid, sz, pr_avg))

        # Sort by pagerank_avg descending, then cycle size descending, then deterministic
        unique_candidates.sort(key=lambda c: (-c[3], -c[2], c[0], c[1]))

        selected_imp = unique_candidates[:args.total]

        lines_imp: List[str] = []
        per_repo_imp: Counter = Counter()
        per_size_imp: Counter = Counter()
        for repo, cid, sz, pr_avg in selected_imp:
            branch = repo_to_branch[repo]
            lines_imp.append(f"{repo} {branch} {cid}")
            per_repo_imp[repo] += 1
            per_size_imp[sz] += 1

        out_path.write_text("\n".join(lines_imp) + ("\n" if lines_imp else ""), encoding="utf-8")

        print(f"Strategy: importance (PageRank-based)")
        print(f"Wrote {len(lines_imp)} lines to {out_path}")
        if len(lines_imp) < args.total:
            print(f"[WARN] Requested --total {args.total} but only {len(lines_imp)} candidates available.")

        print(f"Total candidates considered: {len(unique_candidates)}")
        if selected_imp:
            print(f"PageRank range: {selected_imp[-1][3]:.6f} .. {selected_imp[0][3]:.6f}")

        print("Selected per size:")
        for sz in sorted(per_size_imp.keys()):
            print(f"  size={sz}: {per_size_imp[sz]}")

        print("Selected per repo:")
        for repo in repos_order:
            n = per_repo_imp.get(repo, 0)
            if n > 0:
                print(f"  {repo}: {n}")

        lang_imp: Counter = Counter()
        for repo, _cid, _sz, _pr in selected_imp:
            lang_imp[repo_to_lang.get(repo, "unknown")] += 1
        print("Selected per programming language:")
        for lang, n in sorted(lang_imp.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {lang}: {n}")

        print("Done.")
        return
    # ---- End importance strategy ----

    # ---- Bin-based strategy ----
    if args.strategy == "bin":
        bins = parse_bins(args.size_bins)
        bin_priority = derive_bin_priority(bins)

        # Filter candidates to those that match a bin
        pool = [c for c in bin_candidates if c.bin_key]
        if not pool:
            raise SystemExit("No candidates fell into the requested bins.")

        # Deduplicate
        seen_bin_keys: Set[Tuple[str, str]] = set()
        unique_pool: List[Candidate] = []
        for c in pool:
            k = (c.repo, c.cid)
            if k not in seen_bin_keys:
                seen_bin_keys.add(k)
                unique_pool.append(c)
        pool = unique_pool

        available_by_bin: Counter = Counter()
        for c in pool:
            available_by_bin[c.bin_key] += 1

        bins_in_pool = [b for b in bin_priority if available_by_bin.get(b, 0) > 0]
        if not bins_in_pool:
            raise SystemExit("No candidates fell into the requested bins.")

        B = len(bins_in_pool)
        base_q = args.total // B
        rem_q = args.total % B
        bin_target: Dict[str, int] = {b: base_q for b in bins_in_pool}
        for b in bins_in_pool[:rem_q]:
            bin_target[b] += 1

        # Selection state
        selected_bin: List[Candidate] = []
        selected_ids_bin: Set[Tuple[str, str]] = set()
        per_repo_selected_bin: Counter = Counter()
        per_bin_selected: Counter = Counter()
        per_lang_selected_bin: Counter = Counter()
        node_use: Dict[str, Counter] = defaultdict(Counter)

        cands_by_bin: Dict[str, List[Candidate]] = defaultdict(list)
        for c in pool:
            cands_by_bin[c.bin_key].append(c)
        for b in cands_by_bin:
            cands_by_bin[b].sort(key=lambda c: (c.repo, c.size, c.cid))

        def can_take_bin(c: Candidate) -> bool:
            if (c.repo, c.cid) in selected_ids_bin:
                return False
            if args.max_per_repo > 0 and per_repo_selected_bin[c.repo] >= args.max_per_repo:
                return False
            if not feasible_under_node_cap(c, node_use):
                return False
            return True

        def take_bin(c: Candidate) -> None:
            selected_bin.append(c)
            selected_ids_bin.add((c.repo, c.cid))
            per_repo_selected_bin[c.repo] += 1
            per_bin_selected[c.bin_key] += 1
            per_lang_selected_bin[c.lang] += 1
            for n in c.nodes:
                node_use[c.repo][n] += 1

        def pick_best_in_bin(b: str) -> Optional[Candidate]:
            eligible = [c for c in cands_by_bin.get(b, []) if can_take_bin(c)]
            if not eligible:
                return None
            return min(eligible, key=lambda c: score_candidate_min(c, per_repo_selected_bin, node_use))

        # Fill bins in priority order, up to soft targets
        for b in bins_in_pool:
            tgt = int(bin_target.get(b, 0))
            while len(selected_bin) < args.total and per_bin_selected[b] < tgt:
                best = pick_best_in_bin(b)
                if best is None:
                    break
                take_bin(best)

        # Spillover: fill remaining slots from bins in priority order
        for b in bins_in_pool:
            while len(selected_bin) < args.total:
                best = pick_best_in_bin(b)
                if best is None:
                    break
                take_bin(best)
            if len(selected_bin) >= args.total:
                break

        # Write output
        lines_bin = [f"{c.repo} {c.branch} {c.cid}" for c in selected_bin]
        out_path.write_text("\n".join(lines_bin) + ("\n" if lines_bin else ""), encoding="utf-8")

        print(f"Strategy: bin (size-bin targets with node-reuse caps)")
        print(f"Wrote {len(lines_bin)} lines to {out_path}")
        if len(lines_bin) < args.total:
            print(f"[WARN] Requested --total {args.total} but only selected {len(lines_bin)}.")

        distinct_repos_bin = sum(1 for r, n in per_repo_selected_bin.items() if n > 0)
        print(f"Distinct repos covered: {distinct_repos_bin}")
        print(f"Max per repo: {args.max_per_repo}")
        print(f"Node reuse cap per repo: {MAX_NODE_USE_PER_REPO}")
        print(f"Bin priority order: {', '.join(bins_in_pool)}")

        print("Size-bin targets (soft):")
        for b in bins_in_pool:
            tgt = bin_target.get(b, 0)
            sel = per_bin_selected.get(b, 0)
            short = max(0, int(tgt) - int(sel))
            extra = max(0, int(sel) - int(tgt))
            note = ""
            if short > 0:
                note = f" shortfall={short}"
            elif extra > 0:
                note = f" excess={extra}"
            print(f"  bin={b}: target={tgt} available={available_by_bin.get(b, 0)} selected={sel}{note}")

        print("Selected per repo:")
        for repo in repos_order:
            n = per_repo_selected_bin.get(repo, 0)
            if n > 0:
                print(f"  {repo}: {n}")

        print("Selected per programming language:")
        for lang, n in sorted(per_lang_selected_bin.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {lang}: {n}")

        print("Done.")
        return
    # ---- End bin strategy ----

    # Sizes to target (exact sizes)
    sizes = sorted(by_size.keys())
    if not sizes:
        raise SystemExit("No sizes found after filtering.")

    if args.ascending_sizes:
        size_order = list(sizes)
    else:
        size_order = list(reversed(sizes))

    # Compute even quotas per exact size
    base = args.total // len(sizes)
    rem = args.total % len(sizes)

    # Deterministic remainder assignment: give +1 to smallest sizes first
    quotas: Dict[int, int] = {sz: base for sz in sizes}
    for sz in sizes[:rem]:
        quotas[sz] += 1

    # Batch selection by size using repo fairness
    per_repo_selected_global = Counter()
    selected: List[Tuple[int, str, str]] = []  # (size, repo, cycle_id)
    per_size_selected = Counter()
    per_size_available = {sz: sum(len(by_size[sz][r]) for r in by_size[sz]) for sz in sizes}

    # Build working queues: by_size_repo_queues[size][repo] = deque(cycle_ids)
    by_size_repo_queues: Dict[int, Dict[str, deque]] = {}
    for sz in sizes:
        by_size_repo_queues[sz] = {}
        for repo in repos_order:
            cids = by_size.get(sz, {}).get(repo, [])
            if cids:
                by_size_repo_queues[sz][repo] = deque(cids)
        if not by_size_repo_queues[sz]:
            del by_size_repo_queues[sz]

    # Pass 1: fulfill quotas as best as possible
    shortfall = 0
    for sz in size_order:
        want = int(quotas.get(sz, 0))
        if want <= 0:
            continue

        repo_queues = by_size_repo_queues.get(sz, {})
        if not repo_queues:
            shortfall += want
            continue

        got = select_for_size_balanced_batch(
            queues_by_repo=repo_queues,
            take_n=want,
            repos_order=repos_order,
            repos_rank=repos_rank,
            per_repo_selected_global=per_repo_selected_global,
        )

        for repo, cid in got:
            selected.append((sz, repo, cid))
            per_size_selected[sz] += 1

        if len(got) < want:
            shortfall += (want - len(got))

        # Clean empties
        repo_queues = {r: q for r, q in repo_queues.items() if q}
        if repo_queues:
            by_size_repo_queues[sz] = repo_queues
        else:
            by_size_repo_queues.pop(sz, None)

    # Pass 2: spillover (round-robin across sizes) until we hit total or run out
    while len(selected) < args.total and by_size_repo_queues:
        pick = pick_one_round_robin(
            by_size_repo_queues,
            size_order=size_order,
            repos_rank=repos_rank,
            per_repo_selected_global=per_repo_selected_global,
            per_size_selected=per_size_selected,
        )
        if pick is None:
            break
        sz, repo, cid = pick
        selected.append((sz, repo, cid))

    # Write cycles_to_analyze.txt
    lines: List[str] = []
    for sz, repo, cid in selected:
        branch = repo_to_branch[repo]
        lines.append(f"{repo} {branch} {cid}")

    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    # Summary
    total_written = len(lines)
    distinct_repos = sum(1 for r, n in per_repo_selected_global.items() if n > 0)

    print(f"Wrote {total_written} lines to {out_path}")
    if total_written < args.total:
        print(f"[WARN] Requested --total {args.total} but only selected {total_written} (insufficient candidates).")

    print(f"Distinct repos covered: {distinct_repos}")
    print("Target quotas per size:")
    for sz in sizes:
        print(f"  size={sz}: target={quotas[sz]} available={per_size_available.get(sz,0)}")

    print("Selected per size:")
    for sz in sizes:
        print(f"  size={sz}: selected={per_size_selected.get(sz,0)}")

    if shortfall > 0:
        print(f"Shortfall during quota fill (before spillover): {shortfall}")

    # ---- NEW: per-repo + per-language breakdown (minimal add-on) ----
    per_repo_selected = Counter()
    per_lang_selected = Counter()
    for _sz, repo, _cid in selected:
        per_repo_selected[repo] += 1
        per_lang_selected[repo_to_lang.get(repo, "unknown")] += 1

    print("Selected per repo:")
    for repo in repos_order:
        n = per_repo_selected.get(repo, 0)
        if n > 0:
            print(f"  {repo}: {n}")

    print("Selected per programming language:")
    for lang, n in sorted(per_lang_selected.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {lang}: {n}")
    # ---------------------------------------------------------------

    print("Done.")


if __name__ == "__main__":
    main()
