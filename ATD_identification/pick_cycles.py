#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import networkx as nx


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -----------------------------
# Cycle helpers (DIRECTED graph)
# -----------------------------

def canonicalize_cycle(nodes: List[str]) -> Tuple[str, ...]:
    """
    Canonicalize a directed cycle by rotation ONLY (direction preserved).

    For directed cycles, reversing a node order generally changes edge directions
    and can fabricate a cycle that doesn't exist in the graph. So we only rotate
    to the lexicographically smallest starting node.
    """
    if not nodes:
        return ()
    cyc = list(nodes)
    i = min(range(len(cyc)), key=lambda j: cyc[j])
    return tuple(cyc[i:] + cyc[:i])


def cycle_edge_tuples(nodes: List[str]) -> List[Tuple[str, str]]:
    m = len(nodes)
    return [(nodes[i], nodes[(i + 1) % m]) for i in range(m)]


def cycle_edges(nodes: List[str], relation: str) -> List[Dict[str, str]]:
    m = len(nodes)
    return [{"source": nodes[i], "target": nodes[(i + 1) % m], "relation": relation} for i in range(m)]


def _node_in_excluded_dir(node_id: str, exclude_dirs: List[str]) -> bool:
    """Return True if *node_id* has a path segment matching any of *exclude_dirs*.

    Comparison is case-insensitive on the directory name.
    Accepts both exact segment matches and prefix/suffix patterns:
        - 'VPA Framework' matches path segment 'VPA Framework'
        - Also matches as a path prefix, e.g. 'src/VPA Framework/foo.cs'
    """
    if not exclude_dirs:
        return False
    parts = node_id.replace("\\", "/").split("/")
    exclude_lower = [d.lower().strip("/") for d in exclude_dirs]
    for part in parts[:-1]:  # skip filename, only check directory segments
        pl = part.lower()
        for excl in exclude_lower:
            if pl == excl:
                return True
    # Also check if the full relative path starts with an excluded prefix
    norm = node_id.replace("\\", "/").lower()
    for excl in exclude_lower:
        if norm.startswith(excl + "/") or ("/" + excl + "/") in norm:
            return True
    return False


# -------------------------
# Parsing / loading helpers
# -------------------------

def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _relation_from_graph(graph: Dict[str, Any]) -> str:
    edges = graph.get("edges") or []
    if edges and isinstance(edges, list) and isinstance(edges[0], dict):
        return str(edges[0].get("relation") or "dep")
    return "dep"


def _build_full_graph(graph_json: Dict[str, Any]) -> nx.DiGraph:
    G = nx.DiGraph()
    for n in (graph_json.get("nodes") or []):
        nid = str(n["id"])
        G.add_node(nid)
    for e in (graph_json.get("edges") or []):
        s = str(e["source"])
        t = str(e["target"])
        if s != t:
            G.add_edge(s, t)
    return G


def _scc_node_lists(scc_report: Dict[str, Any]) -> List[List[str]]:
    out: List[List[str]] = []
    for scc in (scc_report.get("sccs") or []):
        nodes = [str(n["id"]) for n in (scc.get("nodes") or []) if isinstance(n, dict) and "id" in n]
        if len(nodes) >= 2:
            out.append(nodes)
    return out


def _global_pagerank_map(scc_report: Dict[str, Any]) -> Dict[str, float]:
    """
    Extract global PageRank from scc_report.json.

    Expected shape (as in your example):
      "node_features": {
        "<repo-relative-path>": {"pagerank": <float>, ...},
        ...
      }

    If missing or malformed, returns {} and callers should fall back to 0.0.
    """
    out: Dict[str, float] = {}
    nf = scc_report.get("node_features")
    if not isinstance(nf, dict):
        return out

    for node_id, feats in nf.items():
        if not isinstance(node_id, str):
            continue
        if not isinstance(feats, dict):
            continue
        pr = feats.get("pagerank")
        if isinstance(pr, (int, float)):
            out[node_id] = float(pr)

    return out


# -------------------------
# Cycle sampling
# -------------------------

def _sample_cycles_in_scc(
    Gscc: nx.DiGraph,
    *,
    max_len: int,
    attempts: int,
    rng: random.Random,
) -> List[List[str]]:
    """
    Find cycles by repeated bounded random walks:
      - pick start node
      - walk up to max_len, stop if we return to a node already on the path
      - if that closes a cycle back to the repeated node -> record that cycle
    This is not complete enumeration; it's a fast sampler.
    """
    nodes = list(Gscc.nodes())
    if not nodes:
        return []

    seen: Set[Tuple[str, ...]] = set()
    found: List[List[str]] = []

    # Precompute successors for speed
    succ = {u: list(Gscc.successors(u)) for u in nodes}

    for _ in range(attempts):
        start = rng.choice(nodes)
        path = [start]
        pos = {start: 0}
        cur = start

        for _step in range(max_len):
            nxts = succ.get(cur) or []
            if not nxts:
                break
            cur = rng.choice(nxts)

            if cur in pos:
                # cycle detected (cur repeats). The cycle is the subpath from
                # first occurrence of 'cur' back to 'cur'.
                i = pos[cur]
                cyc = path[i:] + [cur]  # closes at cur
                if cyc and cyc[0] == cyc[-1]:
                    cyc = cyc[:-1]
                cyc_nodes = cyc
                if 2 <= len(cyc_nodes) <= max_len:
                    key = canonicalize_cycle(cyc_nodes)
                    if key and key not in seen:
                        seen.add(key)
                        found.append(list(key))
                break

            path.append(cur)
            pos[cur] = len(path) - 1

    # Prefer longer cycles first (useful for sampling diversity)
    found.sort(key=lambda ns: (len(ns), tuple(ns)), reverse=True)
    return found


# -------------------------
# Edge-disjoint packing (per SCC)
# -------------------------

def _pack_edge_disjoint_cycles(
    cycles: List[List[str]],
    pr: Dict[str, float],
    *,
    max_keep: int,
) -> List[List[str]]:
    """
    Greedy packing to enforce pairwise edge-disjointness among returned cycles.

    - Sort candidates by (length desc, avg_pagerank desc, lexical tie-break)
    - Accept a cycle iff none of its directed edges have been used
    - Stop once we have max_keep (if max_keep > 0)

    This returns a *maximal* edge-disjoint subset relative to the provided candidates
    (not claiming global optimum over all possible cycles in the SCC).
    """
    used_edges: Set[Tuple[str, str]] = set()
    kept: List[List[str]] = []

    def avg_pr(cyc: List[str]) -> float:
        return float(sum(pr.get(n, 0.0) for n in cyc) / max(1, len(cyc)))

    # Best-first ordering; deterministic tie-breaker on tuple(cyc)
    ordered = sorted(
        cycles,
        key=lambda cyc: (len(cyc), avg_pr(cyc), tuple(cyc)),
        reverse=True,
    )

    for cyc in ordered:
        edges = cycle_edge_tuples(cyc)
        if any(e in used_edges for e in edges):
            continue
        kept.append(cyc)
        used_edges.update(edges)
        if max_keep > 0 and len(kept) >= max_keep:
            break

    return kept


# -------------------------
# Main
# -------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Generate cycle_catalog.json by sampling cycles inside SCCs (no full enumeration). "
            "Cycles are always enforced to be edge-disjoint within each SCC."
        )
    )
    ap.add_argument("--dependency-graph", required=True, help="Path to dependency_graph.json")
    ap.add_argument("--scc-report", required=True, help="Path to scc_report.json (SCC-only)")
    ap.add_argument("--out", required=True, help="Output path for cycle_catalog.json")
    ap.add_argument("--repo", default="", help="Repo name (optional, for metadata)")
    ap.add_argument("--base-branch", default="", help="Base branch (optional, for metadata)")
    ap.add_argument("--max-cycle-len", type=int, default=8)
    ap.add_argument("--attempts-per-scc", type=int, default=5000)
    ap.add_argument("--max-cycles-per-scc", type=int, default=200)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument(
        "--exclude-dirs", nargs="*", default=[],
        help=(
            "Directory names or path prefixes to exclude. Any cycle containing "
            "a node under one of these directories is discarded. "
            "Example: --exclude-dirs 'VPA Framework' tests"
        ),
    )
    args = ap.parse_args()

    dep_path = Path(args.dependency_graph).resolve()
    scc_path = Path(args.scc_report).resolve()
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    dep = _load_json(dep_path)
    scc = _load_json(scc_path)

    relation = _relation_from_graph(dep)
    G = _build_full_graph(dep)

    # Global PageRank (computed on full dependency graph) is expected to be present in scc_report.json
    global_pr = _global_pagerank_map(scc)

    scc_nodes_list = _scc_node_lists(scc)
    rng = random.Random(args.seed)

    catalog_sccs: List[Dict[str, Any]] = []
    total_cycles = 0

    for scc_idx, node_ids in enumerate(scc_nodes_list):
        sub = G.subgraph(node_ids).copy()
        if sub.number_of_nodes() < 2:
            continue

        # Restrict PR map to SCC nodes (values are still global PR scores)
        pr = {n: float(global_pr.get(n, 0.0)) for n in sub.nodes()}

        sampled = _sample_cycles_in_scc(
            sub,
            max_len=args.max_cycle_len,
            attempts=args.attempts_per_scc,
            rng=rng,
        )

        # ALWAYS enforce edge-disjointness (within SCC) before applying max cap
        sampled = _pack_edge_disjoint_cycles(
            sampled,
            pr,
            max_keep=args.max_cycles_per_scc,
        )

        # Filter out cycles with nodes in excluded directories
        if args.exclude_dirs:
            before = len(sampled)
            sampled = [
                cyc for cyc in sampled
                if not any(_node_in_excluded_dir(n, args.exclude_dirs) for n in cyc)
            ]
            excluded = before - len(sampled)
            if excluded:
                print(f"  scc_{scc_idx}: excluded {excluded} cycle(s) by --exclude-dirs")

        cycles_out: List[Dict[str, Any]] = []
        for j, cyc_nodes in enumerate(sampled):
            avg_pr_val = float(sum(pr.get(n, 0.0) for n in cyc_nodes) / max(1, len(cyc_nodes)))
            cyc_id = f"scc_{scc_idx}_cycle_{j}"
            cycles_out.append(
                {
                    "id": cyc_id,
                    "length": len(cyc_nodes),
                    "nodes": cyc_nodes,
                    "edges": cycle_edges(cyc_nodes, relation),
                    "metrics": {
                        "pagerank_avg": avg_pr_val,
                        "pagerank_min": float(min(pr.get(n, 0.0) for n in cyc_nodes)) if cyc_nodes else 0.0,
                        "pagerank_max": float(max(pr.get(n, 0.0) for n in cyc_nodes)) if cyc_nodes else 0.0,
                    },
                }
            )

        total_cycles += len(cycles_out)
        catalog_sccs.append(
            {
                "id": f"scc_{scc_idx}",
                "node_count": sub.number_of_nodes(),
                "edge_count": sub.number_of_edges(),
                "cycles": cycles_out,
            }
        )

    payload: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": utc_now(),
        "input": {
            "dependency_graph": str(dep_path),
            "scc_report": str(scc_path),
            "repo": args.repo,
            "base_branch": args.base_branch,
        },
        "params": {
            "max_cycle_len": args.max_cycle_len,
            "attempts_per_scc": args.attempts_per_scc,
            "max_cycles_per_scc": args.max_cycles_per_scc,
            "seed": args.seed,
            "exclude_dirs": args.exclude_dirs or [],
            "edge_disjoint": True,
            "edge_disjoint_scope": "within_scc",
            "directed_canonicalization": "rotation_only",
            "pagerank_source": "scc_report.node_features.pagerank",
            "pagerank_scope": "global",
        },
        "summary": {
            "scc_count": len(catalog_sccs),
            "cycle_count": total_cycles,
        },
        "sccs": catalog_sccs,
    }

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote: {out_path}")
    print(f"  sccs={len(catalog_sccs)} cycles={total_cycles}")


if __name__ == "__main__":
    main()
