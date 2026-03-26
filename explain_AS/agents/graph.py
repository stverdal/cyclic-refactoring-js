from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from agents.boundary import SCCEdge
from budgeting import estimate_tokens_from_text, single_block_char_budget
from context import cycle_chain_str, format_block_for_prompt, prompt_block_wrapper_len, require_language
from language import edge_semantics_text
from llm import Agent, LLMClient


GRAPH_MIN_OUTPUT_TOKENS_RESERVED = 2000
GRAPH_SAFETY_MARGIN_TOKENS = 1000

# One knob for all "top" lists and edge sample sections.
GRAPH_TOP_K = 10


GRAPH_PROMPT_PREAMBLE = """You are the Structural Context Agent.
You infer how the cycle sits within the SCC from a summarized SCC graph.

Rules:
- Focus on observations that may help another agent understand the cycle's wider graph context.
- Stay factual based on the provided evidence. Do not invent missing nodes/edges.
- No tables, no JSON.
- If you see truncation notes, assume some context may be missing.
- Base summaries on the specific facts in the provided reports and context. Avoid generic statements and avoid just listing the cycle dependencies.
""".strip()


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").strip()


def _cycle_edge_set(cycle_nodes: Sequence[str]) -> Set[Tuple[str, str]]:
    nodes = [_norm_path(x) for x in (cycle_nodes or []) if x and str(x).strip()]
    if len(nodes) < 2:
        return set()
    out: Set[Tuple[str, str]] = set()
    for i in range(len(nodes)):
        a = nodes[i]
        b = nodes[(i + 1) % len(nodes)]
        if a and b:
            out.add((a, b))
    return out


def _build_edge_pairs(scc_edges: Iterable[SCCEdge]) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for e in (scc_edges or []):
        a = _norm_path(e.source)
        b = _norm_path(e.target)
        if a and b:
            pairs.append((a, b))
    return pairs


def _degrees(edge_pairs: Iterable[Tuple[str, str]]) -> Tuple[Counter, Counter]:
    indeg: Counter = Counter()
    outdeg: Counter = Counter()
    for a, b in edge_pairs:
        outdeg[a] += 1
        indeg[b] += 1
    return indeg, outdeg


def _top_counts(items: Iterable[str], k: int) -> List[Tuple[str, int]]:
    return Counter(list(items)).most_common(max(0, int(k)))


def _sample_edges_prioritizing_groups(
    *,
    edges: List[Tuple[str, str]],
    group_key: str,
    top_k: int,
) -> Tuple[List[Tuple[str, str]], int]:
    """
    Deterministic edge sampling:
    - Group edges by selected endpoint (source or target).
    - Order groups by size desc, then group name.
    - Within group, sort edges lexicographically.
    - Pick edges round-robin across groups up to top_k.
    Returns (samples, omitted_count).
    """
    top_k = max(0, int(top_k))
    if top_k <= 0 or not edges:
        return [], max(0, len(edges))

    if group_key not in {"source", "target"}:
        raise ValueError("group_key must be 'source' or 'target'")

    groups: Dict[str, List[Tuple[str, str]]] = {}
    for a, b in edges:
        g = a if group_key == "source" else b
        groups.setdefault(g, []).append((a, b))

    for g in list(groups.keys()):
        groups[g].sort()

    group_order = sorted(groups.keys(), key=lambda g: (-len(groups[g]), g))

    picks_per_group = {g: 0 for g in group_order}
    picked_total = 0
    depth_idx = 0
    while picked_total < top_k:
        progressed = False
        for g in group_order:
            lst = groups[g]
            if depth_idx < len(lst) and picked_total < top_k:
                picks_per_group[g] += 1
                picked_total += 1
                progressed = True
        if not progressed:
            break
        depth_idx += 1

    samples: List[Tuple[str, str]] = []
    for g in group_order:
        n = picks_per_group.get(g, 0)
        if n > 0:
            samples.extend(groups[g][:n])
        if len(samples) >= top_k:
            break

    samples = samples[:top_k]
    omitted = max(0, len(edges) - len(samples))
    return samples, omitted


def _summarize_scc_for_cycle(
    *,
    cycle_nodes: List[str],
    scc_edges: List[SCCEdge],
    top_k: int,
) -> str:
    cycle = [_norm_path(x) for x in (cycle_nodes or []) if x and str(x).strip()]
    cycle_set = set(cycle)

    edge_pairs = _build_edge_pairs(scc_edges)
    scc_nodes = sorted(set([a for a, _ in edge_pairs] + [b for _, b in edge_pairs]))
    scc_node_set = set(scc_nodes)

    cycle_edges_set = _cycle_edge_set(cycle)

    internal_cycle_edges = [(a, b) for (a, b) in edge_pairs if a in cycle_set and b in cycle_set]
    chord_edges = sorted(set(internal_cycle_edges) - cycle_edges_set)

    out_to_noncycle = [(a, b) for (a, b) in edge_pairs if a in cycle_set and b in scc_node_set and b not in cycle_set]
    in_from_noncycle = [(a, b) for (a, b) in edge_pairs if b in cycle_set and a in scc_node_set and a not in cycle_set]

    indeg, outdeg = _degrees(edge_pairs)
    hub_nodes = sorted(scc_nodes, key=lambda n: (-(indeg[n] + outdeg[n]), n))[: max(0, int(top_k))]

    lines: List[str] = []
    lines.append(f"SCC summary: {len(scc_node_set)} nodes, {len(edge_pairs)} edges")
    lines.append(f"Cycle summary: {len(cycle_set)} nodes, {len(cycle_edges_set)} edges (as given)")
    lines.append("")

    lines.append("Extra edges among cycle nodes (chords / alternate routes):")
    if chord_edges:
        shown = chord_edges[: max(0, int(top_k))]
        for a, b in shown:
            lines.append(f"- {a} -> {b}")
        if len(chord_edges) > len(shown):
            lines.append(f"- ... +{len(chord_edges) - len(shown)} more")
    else:
        lines.append("- (none visible)")
    lines.append("")

    lines.append("Hub candidates (by in+out degree within SCC):")
    if hub_nodes:
        for n in hub_nodes:
            lines.append(f"- {n} (in={indeg[n]}, out={outdeg[n]}, total={indeg[n] + outdeg[n]})")
    else:
        lines.append("- N/A")
    lines.append("")

    lines.append("Cycle -> non-cycle connections (top targets):")
    top_targets = _top_counts((b for _, b in out_to_noncycle), top_k)
    if top_targets:
        for node, cnt in top_targets:
            lines.append(f"- {node} ({cnt})")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("Cycle -> non-cycle edge samples:")
    out_samples, out_omitted = _sample_edges_prioritizing_groups(
        edges=sorted(out_to_noncycle), group_key="target", top_k=top_k,
    )
    if out_samples:
        for a, b in out_samples:
            lines.append(f"- {a} -> {b}")
        if out_omitted:
            lines.append(f"- ... +{out_omitted} more")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("Non-cycle -> cycle connections (top sources):")
    top_sources = _top_counts((a for a, _ in in_from_noncycle), top_k)
    if top_sources:
        for node, cnt in top_sources:
            lines.append(f"- {node} ({cnt})")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("Non-cycle -> cycle edge samples:")
    in_samples, in_omitted = _sample_edges_prioritizing_groups(
        edges=sorted(in_from_noncycle), group_key="source", top_k=top_k,
    )
    if in_samples:
        for a, b in in_samples:
            lines.append(f"- {a} -> {b}")
        if in_omitted:
            lines.append(f"- ... +{in_omitted} more")
    else:
        lines.append("- (none)")

    return "\n".join(lines).strip() or "N/A"


def build_graph_user_prompt(
    *,
    language: str,
    cycle_nodes: List[str],
    scc_edges: List[SCCEdge],
    context_length: int,
) -> str:
    semantics = edge_semantics_text(language)
    chain = cycle_chain_str(cycle_nodes)

    scc_summary_text = _summarize_scc_for_cycle(
        cycle_nodes=cycle_nodes, scc_edges=scc_edges, top_k=int(GRAPH_TOP_K),
    ).strip() or "N/A"

    prompt_prefix = f"""{GRAPH_PROMPT_PREAMBLE}

---

{semantics}

Cycle:
{chain}

SCC summary (may be truncated):
"""
    prompt_suffix = """

Write a natural-language memo for another agent.
Focus on how the cycle sits in the SCC, any visible hubs or bridges, outside-cycle connections, and important uncertainty.
""".lstrip()

    overhead_tokens_estimate = estimate_tokens_from_text(prompt_prefix + prompt_suffix)
    total_input_tokens_budget = (
        int(context_length)
        - int(GRAPH_SAFETY_MARGIN_TOKENS)
        - int(GRAPH_MIN_OUTPUT_TOKENS_RESERVED)
        - int(overhead_tokens_estimate)
    )
    total_input_tokens_budget = max(0, int(total_input_tokens_budget))

    block_id = "SCC summary"
    scc_block_char_budget = single_block_char_budget(
        block_text=scc_summary_text,
        wrapper_len_chars=prompt_block_wrapper_len(block_id),
        total_tokens_budget=int(total_input_tokens_budget),
    )

    scc_block, _scc_truncated = format_block_for_prompt(
        repo_rel_path=block_id,
        block_text=scc_summary_text,
        max_chars=int(scc_block_char_budget),
    )

    return (prompt_prefix + scc_block + "\n" + prompt_suffix).strip() + "\n"


def run_graph_agent(
    *,
    client: LLMClient,
    transcript_path: str,
    language: str,
    cycle_nodes: List[str],
    scc_edges: List[SCCEdge],
) -> str:
    language = require_language(language)
    graph_agent = Agent(name="graph")

    user_prompt = build_graph_user_prompt(
        language=language, cycle_nodes=cycle_nodes,
        scc_edges=scc_edges, context_length=int(client.context_length),
    )

    return graph_agent.ask(
        client=client,
        transcript_path=transcript_path,
        user_prompt=user_prompt,
        min_output_tokens_reserved=int(GRAPH_MIN_OUTPUT_TOKENS_RESERVED),
        safety_margin_tokens=int(GRAPH_SAFETY_MARGIN_TOKENS),
        max_output_chars_soft=None,
    )
