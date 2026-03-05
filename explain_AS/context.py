from __future__ import annotations

import os
from typing import Dict, Iterable, List, Optional, Tuple

from budgeting import TruncationInfo, trim_text_bottom_with_info


# Hard cap per file body (before any per-prompt budgeting). Keeps edge prompts sane.
EDGE_FILE_HARD_CAP_CHARS = 40000


def is_init_py(node_id: str) -> bool:
    normalized_path = (node_id or "").replace("\\", "/")
    return normalized_path.endswith("/__init__.py") or normalized_path == "__init__.py"


def is_index_js(node_id: str) -> bool:
    """True for JS/TS barrel files (index.js, index.ts, index.jsx, index.tsx)."""
    normalized_path = (node_id or "").replace("\\", "/")
    base = normalized_path.rsplit("/", 1)[-1]
    return base in ("index.js", "index.ts", "index.jsx", "index.tsx")


def is_boilerplate_entry(node_id: str, language: str) -> bool:
    """True for language-specific boilerplate entry files (__init__.py, index.js, etc.)."""
    if language == "python":
        return is_init_py(node_id)
    if language == "javascript":
        return is_index_js(node_id)
    return False


def filtered_cycle_nodes(nodes: List[str], *, skip_init: bool = True, language: str = "python") -> List[str]:
    normalized_nodes = [str(n) for n in (nodes or [])]
    if skip_init:
        normalized_nodes = [n for n in normalized_nodes if not is_boilerplate_entry(n, language)]
    return normalized_nodes


def node_to_abs(repo_root: str, node_id: str) -> str:
    return os.path.join(repo_root, node_id)


def read_text_file(abs_path: str) -> str:
    with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def read_cycle_files(
    *,
    repo_root: str,
    cycle_nodes: Iterable[str],
    skip_init: bool = True,
) -> Dict[str, str]:
    """
    Returns: repo-relative node id -> raw file text.
    Skips __init__.py if requested.
    Missing files are silently ignored (engine can decide to error later).
    """
    file_text_by_node_id: Dict[str, str] = {}
    for node in cycle_nodes:
        node_id = str(node)
        if skip_init and is_init_py(node_id):
            continue
        abs_path = node_to_abs(repo_root, node_id)
        if not os.path.exists(abs_path):
            continue
        file_text_by_node_id[node_id] = read_text_file(abs_path)
    return file_text_by_node_id


def _truncation_note(*, label: str, info: TruncationInfo) -> str:
    return (
        f"[NOTE: TRUNCATED. Showing first {info.kept_chars:,} chars out of {info.total_chars:,} "
        f"due to context budget for {label}.]\n"
    )


def format_block_for_prompt(
    *,
    label: str,
    repo_rel_path: str,
    block_text: str,
    max_chars: int,
) -> Tuple[str, bool]:
    """
    Generic block wrapper with BEGIN/END and a truncation note inside the block when truncated.
    Returns (block, was_truncated).
    """
    header = f"--- BEGIN {repo_rel_path} ---\n"
    footer = f"\n--- END {repo_rel_path} ---\n"

    raw_body = block_text or ""
    approx_note_len = 160  # conservative
    overhead = len(header) + len(footer) + approx_note_len
    body_budget = max(0, int(max_chars) - overhead)

    trimmed_body, info = trim_text_bottom_with_info(raw_body, body_budget)
    note = _truncation_note(label=label, info=info) if info.truncated else ""
    block = header + note + trimmed_body + footer

    # Second-pass guard if the approximation was off.
    if len(block) > max_chars:
        extra = len(block) - max_chars
        adjusted_body_budget = max(0, body_budget - extra - 10)
        trimmed_body_2, info_2 = trim_text_bottom_with_info(raw_body, adjusted_body_budget)
        note_2 = _truncation_note(label=label, info=info_2) if info_2.truncated else ""
        block = header + note_2 + trimmed_body_2 + footer
        return block, info_2.truncated

    return block, info.truncated


def cap_file_text_hard(file_text: str) -> Tuple[str, bool]:
    """
    Hard-cap a file's raw text before any prompt-specific budgeting.
    Returns (capped_text, was_truncated).
    """
    capped, info = trim_text_bottom_with_info(file_text or "", EDGE_FILE_HARD_CAP_CHARS)
    return capped, info.truncated


def cycle_chain_str(nodes: List[str]) -> str:
    pretty = [n.replace("\\", "/").strip() for n in nodes if n and n.strip()]
    if not pretty:
        return "N/A"
    return " -> ".join(pretty + [pretty[0]])


def edge_str(a: str, b: str) -> str:
    a2 = (a or "").replace("\\", "/")
    b2 = (b or "").replace("\\", "/")
    return f"{a2} -> {b2}"


def get_file_text(files_by_node: Dict[str, str], node_id: str) -> str:
    return files_by_node.get(node_id, "")


def require_language(language: Optional[str]) -> str:
    if language not in ("python", "csharp", "javascript"):
        raise ValueError(f"language must be 'python', 'csharp', or 'javascript' (got {language!r})")
    return language
