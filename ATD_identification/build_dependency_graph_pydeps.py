#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


def load_pydeps_module_dict(pydeps_json: str) -> Dict[str, Dict]:
    raw = json.loads(Path(pydeps_json).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("pydeps JSON is not a dict")
    if "imports" in raw and isinstance(raw["imports"], dict):
        raise ValueError(
            "pydeps JSON appears to be the {'imports': {...}} format. "
            "Re-run pydeps with --show-deps --deps-output (module objects format)."
        )
    return raw


def repo_rel(path: str, root: str) -> str:
    return os.path.relpath(os.path.realpath(path), os.path.realpath(root)).replace("\\", "/")


def exists_with_exact_case(p: str) -> bool:
    try:
        p = os.path.realpath(p)
        drive, rest = os.path.splitdrive(p)
        parts = [part for part in rest.replace("\\", "/").split("/") if part]
        cur = drive + (os.sep if drive or p.startswith(os.sep) else "")
        if not cur:
            cur = os.sep
        for seg in parts:
            try:
                entries = os.listdir(cur)
            except FileNotFoundError:
                return False
            if seg not in entries:
                return False
            cur = os.path.join(cur, seg)
        return True
    except Exception:
        return False

def is_in_vendor_dir(path: str, repo_root: str) -> bool:
    """
    Return True if path is inside any directory named 'vendor' or 'vendors'
    (case-insensitive), relative to repo_root.
    """
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(repo_root))
    except Exception:
        return False

    parts = rel.replace("\\", "/").split("/")

    for p in parts:
        if p.lower() in ("vendor", "vendors"):
            return True

    return False


# Common test-directory names (case-insensitive match on path segments)
_TEST_DIR_NAMES = frozenset({
    "test", "tests", "__tests__", "__test__",
    "spec", "specs", "__mocks__", "fixtures",
    "testutils", "testing",
})


def _is_test_file(filename: str) -> bool:
    """Return True if *filename* (basename only) looks like a test file.

    Matches: test_foo.py, foo_test.py, foo.test.py, foo.spec.py, etc.
    """
    fl = filename.lower()
    for infix in (".test.", ".spec.", ".tests.", ".specs."):
        if infix in fl:
            return True
    # Strip extension(s) → stem
    stem = fl
    while "." in stem:
        stem = stem.rsplit(".", 1)[0]
    if stem.startswith("test_") or stem.endswith("_test") or stem.endswith("_tests"):
        return True
    return False


def is_in_test_dir(path: str, repo_root: str) -> bool:
    """
    Return True if *path* is inside a directory whose name (case-insensitive)
    matches a common test-directory pattern, relative to *repo_root*, **or**
    if the file itself is a colocated test file (test_foo.py, foo_test.py,
    foo.spec.py, etc.).

    Also matches segments that start with 'test_' or end with '_test'/'_tests'.
    """
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(repo_root))
    except Exception:
        return False

    parts = rel.replace("\\", "/").split("/")

    # Check the filename (last segment) for colocated test file patterns
    if parts and _is_test_file(parts[-1]):
        return True

    for p in parts:
        pl = p.lower()
        if pl in _TEST_DIR_NAMES:
            return True
        if pl.startswith("test_") or pl.endswith("_test") or pl.endswith("_tests"):
            return True

    return False



# ---------- TYPE_CHECKING-aware import filter ----------

def expr_has_type_checking(test: ast.AST) -> bool:
    class Finder(ast.NodeVisitor):
        found = False

        def visit_Name(self, n: ast.Name):
            if n.id == "TYPE_CHECKING":
                self.found = True

        def visit_Attribute(self, n: ast.Attribute):
            if n.attr == "TYPE_CHECKING":
                self.found = True
            self.generic_visit(n)

    f = Finder()
    try:
        f.visit(test)
    except Exception:
        return False
    return f.found


def resolve_from_target(cur_mod: str, level: int, module: Optional[str]) -> Optional[str]:
    parts = cur_mod.split(".")
    if level > 0:
        if level > len(parts):
            return None
        base = parts[:-level]
    else:
        base = parts
    if module:
        return ".".join([*base, module]) if base else module
    return ".".join(base) if base else None


def imports_excluding_type_checking(abs_path: str, cur_mod: str) -> Set[str]:
    out: Set[str] = set()
    try:
        src = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src, filename=abs_path)
    except Exception:
        return out

    def visit(node: ast.AST, under_tc: bool = False):
        tc_here = isinstance(node, ast.If) and expr_has_type_checking(node.test)
        now_tc = under_tc or tc_here

        if isinstance(node, ast.Import) and not now_tc:
            for a in node.names:
                if a.name:
                    out.add(a.name)

        elif isinstance(node, ast.ImportFrom) and not now_tc:
            target = resolve_from_target(cur_mod, getattr(node, "level", 0) or 0, node.module)
            if target:
                out.add(target)

        for child in ast.iter_child_nodes(node):
            visit(child, now_tc)

    visit(tree, False)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Build canonical dependency_graph.json from pydeps module-object JSON.")
    ap.add_argument("pydeps_json", help="pydeps JSON (module objects, each with path/imports)")
    ap.add_argument("--repo-root", required=True, help="Repo root")
    ap.add_argument("--entry", required=True, help="Entry/source subdir within repo (stored as metadata)")
    ap.add_argument("--out", required=True, help="Output path for dependency_graph.json")
    ap.add_argument("--language", default="python", help="Language label (default: python)")
    ap.add_argument(
        "--exclude-tests",
        action="store_true",
        default=True,
        help="Exclude files inside common test directories (default: enabled).",
    )
    ap.add_argument(
        "--no-exclude-tests",
        dest="exclude_tests",
        action="store_false",
        help="Disable automatic test-directory exclusion.",
    )
    args = ap.parse_args()

    repo_root = os.path.realpath(args.repo_root)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    raw = load_pydeps_module_dict(args.pydeps_json)

    # module -> abs path (only if file exists + inside repo)
    mod_abs: Dict[str, str] = {}
    for mod, obj in raw.items():
        if not isinstance(obj, dict):
            continue

        p = obj.get("path")
        if not p or not isinstance(p, str):
            continue

        rp = os.path.realpath(p)

        # Must be inside repo
        if not rp.startswith(repo_root):
            continue

        # Skip vendor / vendors directories
        if is_in_vendor_dir(rp, repo_root):
            continue

        # Skip test directories (unless --no-exclude-tests)
        if args.exclude_tests and is_in_test_dir(rp, repo_root):
            continue

        # Case-sensitive existence check
        if not exists_with_exact_case(rp):
            continue

        mod_abs[mod] = rp


    # module -> repo-rel node id (file path)
    mod_id: Dict[str, str] = {m: repo_rel(p, repo_root) for m, p in mod_abs.items()}

    # Precompute AST imports (outside TYPE_CHECKING) for each source file
    seen_imports_by_src: Dict[str, Set[str]] = {}
    for mod, abs_p in mod_abs.items():
        src_id = mod_id[mod]
        # Deduplicate work if multiple modules map to same file id
        if src_id not in seen_imports_by_src:
            seen_imports_by_src[src_id] = imports_excluding_type_checking(abs_p, mod)

    # Build edges
    edges: List[Tuple[str, str]] = []
    for src_mod, obj in raw.items():
        src_id = mod_id.get(src_mod)
        if not src_id:
            continue

        imports = obj.get("imports") or []
        if not isinstance(imports, list):
            continue

        src_seen = seen_imports_by_src.get(src_id, set())

        for dep_mod in imports:
            if not isinstance(dep_mod, str):
                continue
            dep_id = mod_id.get(dep_mod)
            if not dep_id or dep_id == src_id:
                continue

            # Keep only if source file imports dep_mod outside TYPE_CHECKING.
            # Symmetric prefix matching handles import X vs import X.Y normalization differences.
            if src_seen:
                keep = any(
                    dep_mod == s
                    or s.startswith(dep_mod + ".")
                    or dep_mod.startswith(s + ".")
                    for s in src_seen
                )
                if not keep:
                    continue

            edges.append((src_id, dep_id))

    # Emit nodes deduped by id
    id_to_abs: Dict[str, str] = {}
    for m, nid in mod_id.items():
        id_to_abs.setdefault(nid, mod_abs[m])

    node_rows = [{"id": nid, "kind": "file", "abs_path": id_to_abs[nid]} for nid in sorted(id_to_abs)]
    edge_rows = [{"source": s, "target": t, "relation": "import"} for (s, t) in sorted(set(edges))]

    payload = {
        "schema_version": 1,
        "language": args.language,
        "repo_root": repo_root,
        "entry": args.entry.strip().rstrip("/"),
        "nodes": node_rows,
        "edges": edge_rows,
    }

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote: {out_path} (nodes={len(node_rows)} edges={len(edge_rows)})")


if __name__ == "__main__":
    main()
