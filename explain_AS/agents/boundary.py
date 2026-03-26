from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from budgeting import estimate_tokens_from_text, single_block_char_budget
from context import cycle_chain_str, format_block_for_prompt, prompt_block_wrapper_len, require_language
from language import edge_semantics_text
from llm import Agent, LLMClient


BOUNDARY_MIN_OUTPUT_TOKENS_RESERVED = 2000
BOUNDARY_SAFETY_MARGIN_TOKENS = 1000

# Budget knobs for the external connections block
BOUNDARY_MAX_SAMPLES_PER_DIR = 8
BOUNDARY_MAX_FOLDER_BUCKETS_PER_DIR = 6
BOUNDARY_FOLDER_BUCKET_DEPTH = 2


BOUNDARY_PROMPT_PREAMBLE = """You are the Boundary Heuristic Agent.
You infer likely architectural boundaries and boundary violations (if any) from file paths and naming only.

Rules:
- Focus on observations that may help another agent understand possible architectural boundaries around the cycle.
- Stay factual based on the provided evidence. Do not invent missing nodes/edges.
- No tables, no JSON.
- If you see truncation notes, assume some context may be missing.
- Base summaries on the specific facts in the provided reports and context. Avoid generic statements and avoid just listing the cycle dependencies.
""".strip()


@dataclass(frozen=True)
class SCCEdge:
    source: str
    target: str


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").strip()


def _norm_src_root(src_root: str) -> str:
    return _norm_path(src_root).strip("/")


def _bucket_rel(full_path: str, *, src_root: str, depth: int) -> str:
    """
    Return a bucket RELATIVE to src_root.
    Excludes the filename from depth calculation.
    """
    p = _norm_path(full_path).strip("/")
    root = _norm_src_root(src_root)
    if root:
        prefix = root + "/"
        if p.startswith(prefix):
            p = p[len(prefix):]

    parts = [x for x in p.split("/") if x]
    if len(parts) <= 1:
        return ""

    max_folder_parts = max(1, len(parts) - 1)
    take = min(int(depth), max_folder_parts)
    return "/".join(parts[:take])


def _bucket_label(bucket_rel: str, *, src_root: str) -> str:
    root = _norm_src_root(src_root)
    if not bucket_rel:
        return root if root else "(root)"
    if not root:
        return bucket_rel
    return f"{root}/{bucket_rel}".rstrip("/")


def _summarize_paths(
    paths: Sequence[str],
    *,
    src_root: str,
    folder_depth: int,
    max_folders: int,
    max_samples: int,
) -> Tuple[int, List[Tuple[str, int]], List[str], int, int]:
    """
    Returns:
      total,
      folder_counts_top (bucket label -> count),
      samples (repo-relative paths),
      omitted_samples_count,
      omitted_folder_buckets_count
    """
    normalized_full = sorted({_norm_path(p) for p in (paths or []) if p and p.strip()})
    total = len(normalized_full)
    if total <= 0:
        return 0, [], [], 0, 0

    bucket_to_paths: Dict[str, List[str]] = {}
    for full in normalized_full:
        b = _bucket_rel(full, src_root=src_root, depth=folder_depth)
        bucket_to_paths.setdefault(b, []).append(full)

    for b in list(bucket_to_paths.keys()):
        bucket_to_paths[b].sort()

    bucket_order = sorted(
        bucket_to_paths.keys(),
        key=lambda b: (-len(bucket_to_paths[b]), _bucket_label(b, src_root=src_root)),
    )

    labels = [_bucket_label(b, src_root=src_root) for b in bucket_order]
    counts = Counter({label: len(bucket_to_paths[b]) for b, label in zip(bucket_order, labels)})

    all_folder_counts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    folder_counts_top = all_folder_counts[: max(0, int(max_folders))]
    omitted_folder_buckets = max(0, len(all_folder_counts) - len(folder_counts_top))

    max_samples = max(0, int(max_samples))
    if max_samples <= 0:
        return total, folder_counts_top, [], total, omitted_folder_buckets

    picks_per_bucket: Dict[str, int] = {b: 0 for b in bucket_order}
    picked_total = 0
    depth_idx = 0
    while picked_total < max_samples:
        progressed = False
        for b in bucket_order:
            lst = bucket_to_paths[b]
            if depth_idx < len(lst) and picked_total < max_samples:
                picks_per_bucket[b] += 1
                picked_total += 1
                progressed = True
        if not progressed:
            break
        depth_idx += 1

    samples: List[str] = []
    for b in bucket_order:
        n = picks_per_bucket.get(b, 0)
        if n > 0:
            samples.extend(bucket_to_paths[b][:n])
        if len(samples) >= max_samples:
            break
    samples = samples[:max_samples]

    omitted_samples = max(0, total - len(samples))
    return total, folder_counts_top, samples, omitted_samples, omitted_folder_buckets


def _build_adjacency(edges: Iterable[SCCEdge]) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    outgoing: Dict[str, Set[str]] = {}
    incoming: Dict[str, Set[str]] = {}

    for e in edges:
        src = _norm_path(e.source)
        tgt = _norm_path(e.target)
        if not src or not tgt:
            continue
        outgoing.setdefault(src, set()).add(tgt)
        incoming.setdefault(tgt, set()).add(src)

    return outgoing, incoming


def _render_external_connections_block(
    *,
    cycle_nodes: Sequence[str],
    scc_edges: Sequence[SCCEdge],
    src_root: str,
    folder_depth: int,
    max_folder_buckets_per_dir: int,
    max_samples_per_dir: int,
) -> str:
    pretty_cycle = [_norm_path(n) for n in (cycle_nodes or []) if n and n.strip()]
    cycle_set = set(pretty_cycle)
    if not pretty_cycle:
        return "N/A"

    outgoing, incoming = _build_adjacency(scc_edges or [])

    lines: List[str] = []
    for node in pretty_cycle:
        out_ext = sorted([p for p in outgoing.get(node, set()) if p not in cycle_set])
        in_ext = sorted([p for p in incoming.get(node, set()) if p not in cycle_set])

        out_total, out_folders, out_samples, out_omitted_samples, out_omitted_folders = _summarize_paths(
            out_ext, src_root=src_root, folder_depth=folder_depth,
            max_folders=max_folder_buckets_per_dir, max_samples=max_samples_per_dir,
        )
        in_total, in_folders, in_samples, in_omitted_samples, in_omitted_folders = _summarize_paths(
            in_ext, src_root=src_root, folder_depth=folder_depth,
            max_folders=max_folder_buckets_per_dir, max_samples=max_samples_per_dir,
        )

        lines.append(node)

        lines.append(f"- Outgoing outside-cycle: {out_total} total")
        if out_total <= 0:
            lines.append("  - (none)")
        else:
            if out_folders:
                folder_str = ", ".join([f"{k} ({v})" for k, v in out_folders])
                suffix = f", ... +{out_omitted_folders} more buckets" if out_omitted_folders else ""
                lines.append(f"  - By folder: {folder_str}{suffix}")
            lines.append("  - Samples:")
            lines.extend([f"    - {p}" for p in out_samples])
            if out_omitted_samples:
                lines.append(f"    - ... +{out_omitted_samples} more")

        lines.append(f"- Incoming outside-cycle: {in_total} total")
        if in_total <= 0:
            lines.append("  - (none)")
        else:
            if in_folders:
                folder_str = ", ".join([f"{k} ({v})" for k, v in in_folders])
                suffix = f", ... +{in_omitted_folders} more buckets" if in_omitted_folders else ""
                lines.append(f"  - By folder: {folder_str}{suffix}")
            lines.append("  - Samples:")
            lines.extend([f"    - {p}" for p in in_samples])
            if in_omitted_samples:
                lines.append(f"    - ... +{in_omitted_samples} more")

        lines.append("")

    return "\n".join(lines).rstrip() or "N/A"


def build_boundary_user_prompt(
    *,
    language: str,
    cycle_nodes: List[str],
    scc_edges: List[SCCEdge],
    context_length: int,
    src_root: str,
) -> str:
    semantics = edge_semantics_text(language)
    chain = cycle_chain_str(cycle_nodes)

    external_raw = _render_external_connections_block(
        cycle_nodes=cycle_nodes, scc_edges=scc_edges, src_root=src_root,
        folder_depth=int(BOUNDARY_FOLDER_BUCKET_DEPTH),
        max_folder_buckets_per_dir=int(BOUNDARY_MAX_FOLDER_BUCKETS_PER_DIR),
        max_samples_per_dir=int(BOUNDARY_MAX_SAMPLES_PER_DIR),
    )

    prompt_prefix = f"""{BOUNDARY_PROMPT_PREAMBLE}

---

{semantics}

Cycle:
{chain}

External connections (outside-cycle), summarized per cycle file:
"""
    prompt_suffix = """

Write a natural-language memo for another agent.
Focus on likely boundaries, possible boundary tensions around the cycle, and important uncertainty.
""".lstrip()

    overhead_tokens_estimate = estimate_tokens_from_text(prompt_prefix + prompt_suffix)
    total_input_tokens_budget = (
        int(context_length)
        - int(BOUNDARY_SAFETY_MARGIN_TOKENS)
        - int(BOUNDARY_MIN_OUTPUT_TOKENS_RESERVED)
        - int(overhead_tokens_estimate)
    )
    total_input_tokens_budget = max(0, int(total_input_tokens_budget))

    block_id = "External connections summary"
    external_text = (external_raw.strip() or "N/A")
    external_block_char_budget = single_block_char_budget(
        block_text=external_text,
        wrapper_len_chars=prompt_block_wrapper_len(block_id),
        total_tokens_budget=int(total_input_tokens_budget),
    )

    external_block, _external_truncated = format_block_for_prompt(
        repo_rel_path=block_id,
        block_text=external_text,
        max_chars=int(external_block_char_budget),
    )

    return (prompt_prefix + external_block + "\n" + prompt_suffix).strip() + "\n"


def run_boundary_agent(
    *,
    client: LLMClient,
    transcript_path: str,
    language: str,
    cycle_nodes: List[str],
    scc_edges: List[SCCEdge],
    src_root: str,
) -> str:
    language = require_language(language)
    boundary_agent = Agent(name="boundary")

    user_prompt = build_boundary_user_prompt(
        language=language, cycle_nodes=cycle_nodes,
        scc_edges=scc_edges, context_length=int(client.context_length),
        src_root=src_root,
    )

    return boundary_agent.ask(
        client=client,
        transcript_path=transcript_path,
        user_prompt=user_prompt,
        min_output_tokens_reserved=int(BOUNDARY_MIN_OUTPUT_TOKENS_RESERVED),
        safety_margin_tokens=int(BOUNDARY_SAFETY_MARGIN_TOKENS),
        max_output_chars_soft=None,
    )
