#!/usr/bin/env python3
"""Build canonical dependency_graph.json from dependency-cruiser JSON output.

Usage:
    python3 build_dependency_graph_jsts.py depcruise.json \
        --repo-root /path/to/repo \
        --entry src \
        --out dependency_graph.json \
        [--tsconfig /path/to/tsconfig.json]

The dependency-cruiser JSON schema (v5+) has the shape:
    {
      "modules": [
        {
          "source": "src/foo.ts",
          "dependencies": [
            {
              "resolved": "src/bar.ts",
              "dependencyTypes": ["local-import"],
              "dynamic": false,
              ...
            }
          ]
        }
      ]
    }

This script converts it to the canonical pipeline schema:
    {
      "schema_version": 1,
      "language": "javascript",
      "repo_root": "/abs/path",
      "entry": "src",
      "nodes": [{"id": "src/foo.ts", "kind": "file", "abs_path": "..."}],
      "edges": [{"source": "src/foo.ts", "target": "src/bar.ts", "relation": "import"}]
    }

Filtering:
    - Excludes edges targeting node_modules, dist, build, .next, .nuxt, coverage
    - Excludes type-only edges (dependencyTypes containing "type-only")
    - Excludes edges from/to non-existent files

Alias resolution:
    When --tsconfig is given, reads compilerOptions.paths and resolves
    unresolved $lib/*, $apis/* etc. imports to real files on disk.
    Also follows tsconfig "extends" chains to find inherited path aliases.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Directories that should never appear as graph nodes
_EXCLUDED_PREFIXES = (
    "node_modules/",
    "dist/",
    "build/",
    ".next/",
    ".nuxt/",
    "coverage/",
    ".git/",
)

# dependency-cruiser dependency types that indicate type-only imports
_TYPE_ONLY_DEP_TYPES = {"type-only"}


def _is_excluded(source: str) -> bool:
    """Return True if the source path is in an excluded directory."""
    normalized = source.replace("\\", "/")
    for prefix in _EXCLUDED_PREFIXES:
        if normalized.startswith(prefix) or f"/{prefix}" in normalized:
            return True
    return False


def _is_type_only(dep: Dict[str, Any]) -> bool:
    """Return True if the dependency is a TypeScript type-only import."""
    dep_types = dep.get("dependencyTypes") or []
    if not isinstance(dep_types, list):
        return False
    # If all dependency types are type-only, exclude the edge
    return len(dep_types) > 0 and all(dt in _TYPE_ONLY_DEP_TYPES for dt in dep_types)


def _is_local(dep: Dict[str, Any]) -> bool:
    """Return True if the dependency is a local (non-npm) dependency."""
    resolved = dep.get("resolved") or ""
    if not resolved or resolved.startswith("node_modules/"):
        return False
    # Also check the "module" field — npm packages don't have relative resolved paths
    dep_types = dep.get("dependencyTypes") or []
    # dependency-cruiser tags npm deps as "npm", "npm-dev", etc.
    npm_types = {"npm", "npm-dev", "npm-optional", "npm-peer", "npm-bundled", "npm-no-pkg"}
    if isinstance(dep_types, list) and all(dt in npm_types for dt in dep_types):
        return False
    return True


# ---- tsconfig alias resolution ------------------------------------------------

def _load_tsconfig_paths(tsconfig_path: str) -> Dict[str, List[str]]:
    """Load compilerOptions.paths from tsconfig.json, following 'extends' chains.

    Returns a dict mapping alias patterns (e.g. "$lib/*") to lists of
    replacement patterns (e.g. ["src/lib/*"]), with paths resolved
    relative to the *repo root* (not the tsconfig directory).
    """
    tsconfig_file = Path(tsconfig_path).resolve()
    if not tsconfig_file.is_file():
        return {}

    try:
        data = json.loads(tsconfig_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    paths: Dict[str, List[str]] = {}
    tsconfig_dir = tsconfig_file.parent

    # Follow "extends" first (inherited paths)
    extends = data.get("extends")
    if isinstance(extends, str):
        parent_path = (tsconfig_dir / extends).resolve()
        # If extends points to a directory, append tsconfig.json
        if parent_path.is_dir():
            parent_path = parent_path / "tsconfig.json"
        paths.update(_load_tsconfig_paths(str(parent_path)))

    # Own paths override inherited ones
    compiler_opts = data.get("compilerOptions") or {}
    raw_paths = compiler_opts.get("paths") or {}
    for alias, targets in raw_paths.items():
        if not isinstance(targets, list):
            continue
        resolved_targets = []
        for t in targets:
            # Resolve the target relative to the tsconfig dir, then
            # make it relative to the repo root (caller normalizes later)
            abs_target = (tsconfig_dir / t).resolve()
            resolved_targets.append(str(abs_target))
        paths[alias] = resolved_targets

    return paths


def _build_alias_map(
    tsconfig_paths: Dict[str, List[str]], repo_root: str
) -> List[Tuple[str, str]]:
    """Convert tsconfig paths dict into (prefix, replacement_dir) pairs.

    E.g. {"$lib/*": ["/abs/repo/src/lib/*"]}
      → [("$lib/", "/abs/repo/src/lib/")]
    Also handles bare aliases: {"$lib": ["/abs/repo/src/lib"]}
      → [("$lib", "/abs/repo/src/lib")]
    """
    result: List[Tuple[str, str]] = []
    for alias, targets in tsconfig_paths.items():
        if not targets:
            continue
        target = targets[0]  # TS uses only the first match
        if alias.endswith("/*") and target.endswith("/*"):
            prefix = alias[:-1]  # "$lib/" 
            repl = target[:-1]   # "/abs/repo/src/lib/"
            result.append((prefix, repl))
        else:
            result.append((alias, target))
    # Sort longest-prefix-first to avoid partial matches
    result.sort(key=lambda x: -len(x[0]))
    return result


_JS_TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
                     ".svelte", ".vue", ".svelte.ts", ".svelte.js")


def _resolve_alias(
    module_name: str,
    alias_map: List[Tuple[str, str]],
    repo_root: str,
) -> Optional[str]:
    """Try to resolve an aliased import to a repo-relative file path.

    Returns the repo-relative path if a real file is found, else None.
    """
    for prefix, repl_dir in alias_map:
        if not module_name.startswith(prefix):
            continue
        suffix = module_name[len(prefix):]
        candidate_base = repl_dir + suffix

        # Try exact match first
        if os.path.isfile(candidate_base):
            return os.path.relpath(candidate_base, repo_root)

        # Try adding extensions
        for ext in _JS_TS_EXTENSIONS:
            candidate = candidate_base + ext
            if os.path.isfile(candidate):
                return os.path.relpath(candidate, repo_root)

        # Try index files in directory
        if os.path.isdir(candidate_base):
            for ext in _JS_TS_EXTENSIONS:
                idx = os.path.join(candidate_base, "index" + ext)
                if os.path.isfile(idx):
                    return os.path.relpath(idx, repo_root)

    return None


# ---- depcruise loading -------------------------------------------------------

def load_depcruise(path: str) -> Dict[str, Any]:
    """Load and validate dependency-cruiser JSON output."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("dependency-cruiser JSON is not a dict")
    if "modules" not in raw:
        raise ValueError("dependency-cruiser JSON missing 'modules' key")
    return raw


def build_graph(
    depcruise: Dict[str, Any],
    repo_root: str,
    entry: str,
    alias_map: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """Convert dependency-cruiser output to canonical dependency_graph.json."""
    modules = depcruise.get("modules") or []
    alias_map = alias_map or []

    node_ids: Set[str] = set()
    edges: List[Tuple[str, str]] = []

    for mod in modules:
        source = mod.get("source") or ""
        if not source or _is_excluded(source):
            continue

        abs_source = os.path.join(repo_root, source)
        if not os.path.isfile(abs_source):
            continue

        node_ids.add(source)

        for dep in (mod.get("dependencies") or []):
            if not isinstance(dep, dict):
                continue

            # Skip type-only imports
            if _is_type_only(dep):
                continue

            # Skip non-local (npm) deps
            if not _is_local(dep):
                continue

            resolved = dep.get("resolved") or ""
            could_not_resolve = dep.get("couldNotResolve", False)

            # If depcruise couldn't resolve, try alias resolution
            if (not resolved or could_not_resolve) and alias_map:
                module_name = dep.get("module") or ""
                if module_name:
                    alias_resolved = _resolve_alias(module_name, alias_map, repo_root)
                    if alias_resolved:
                        resolved = alias_resolved

            if not resolved or _is_excluded(resolved):
                continue

            # Skip self-edges
            if resolved == source:
                continue

            abs_target = os.path.join(repo_root, resolved)
            if not os.path.isfile(abs_target):
                continue

            node_ids.add(resolved)
            edges.append((source, resolved))

    # Build node rows sorted by id
    node_rows = []
    for nid in sorted(node_ids):
        abs_path = os.path.realpath(os.path.join(repo_root, nid))
        node_rows.append({
            "id": nid,
            "kind": "file",
            "abs_path": abs_path,
        })

    # Deduplicate and sort edges
    edge_rows = [
        {"source": s, "target": t, "relation": "import"}
        for (s, t) in sorted(set(edges))
    ]

    return {
        "schema_version": 1,
        "language": "javascript",
        "repo_root": os.path.realpath(repo_root),
        "entry": entry.strip().rstrip("/"),
        "nodes": node_rows,
        "edges": edge_rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build canonical dependency_graph.json from dependency-cruiser JSON."
    )
    ap.add_argument("depcruise_json", help="Path to dependency-cruiser JSON output")
    ap.add_argument("--repo-root", required=True, help="Repo root directory")
    ap.add_argument("--entry", required=True, help="Entry/source subdir within repo")
    ap.add_argument("--out", required=True, help="Output path for dependency_graph.json")
    ap.add_argument("--tsconfig", default=None,
                    help="Path to tsconfig.json for resolving path aliases ($lib/*, etc.)")
    args = ap.parse_args()

    repo_root = os.path.realpath(args.repo_root)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Build alias map from tsconfig if provided
    alias_map: Optional[List[Tuple[str, str]]] = None
    if args.tsconfig:
        tsconfig_paths = _load_tsconfig_paths(args.tsconfig)
        if tsconfig_paths:
            alias_map = _build_alias_map(tsconfig_paths, repo_root)
            print(f"Loaded {len(alias_map)} path alias(es) from tsconfig")
            for prefix, repl in alias_map:
                print(f"  {prefix} → {os.path.relpath(repl, repo_root)}")

    depcruise = load_depcruise(args.depcruise_json)
    payload = build_graph(depcruise, repo_root, args.entry, alias_map=alias_map)

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote: {out_path} (nodes={len(payload['nodes'])} edges={len(payload['edges'])})")


if __name__ == "__main__":
    main()
