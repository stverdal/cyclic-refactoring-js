from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agents.boundary import run_boundary_agent
from agents.edge import Edge, run_edge_agent
from agents.graph import run_graph_agent
from agents.review import run_review_agent
from agents.synthesizer import run_synthesizer_agent
from context import filtered_cycle_nodes, read_cycle_files, require_language
from llm import LLMClient
from minimal_prompt import build_minimal_prompt


@dataclass(frozen=True)
class ExplainEngineResult:
    cycle_nodes: List[str]
    final_prompt_text: str  # written to prompt.txt


def _parse_scc_id_from_cycle_id(cycle_id: str) -> Optional[str]:
    """
    cycle ids look like: "scc_3_cycle_17"
    """
    s = str(cycle_id or "")
    if s.startswith("scc_") and "_cycle_" in s:
        return s.split("_cycle_", 1)[0]
    return None


def _build_scc_text_from_report(scc_report: Dict[str, Any], scc_id: str) -> str:
    for scc in (scc_report.get("sccs") or []):
        if str(scc.get("id")) != scc_id:
            continue

        nodes = [str(n.get("id")) for n in (scc.get("nodes") or []) if isinstance(n, dict)]
        edges = scc.get("edges") or []

        lines: List[str] = []
        lines.append(f"SCC id: {scc_id}")
        lines.append("")
        lines.append("Nodes:")
        for n in nodes:
            lines.append(f"- {n}")

        lines.append("")
        lines.append("Edges:")
        for e in edges:
            if not isinstance(e, dict):
                continue
            src = str(e.get("source") or "")
            tgt = str(e.get("target") or "")
            if src and tgt:
                lines.append(f"- {src} -> {tgt}")

        return "\n".join(lines).strip()

    return ""


def _extract_revised_explanation(reviewer_text: str) -> str:
    """
    Reviewer output headings:
      Issues found (if any)
      Suggested revisions
      Revised explanation (this should replace the synthesizer output)

    If we can find the "Revised explanation" section, return only that section body.
    Otherwise, fall back to returning the full reviewer text.
    """
    txt = (reviewer_text or "").strip()
    if not txt:
        return ""

    m = re.search(r"(?im)^\s*Revised explanation.*\s*$", txt)
    if not m:
        return txt

    tail = txt[m.end():].strip()
    return tail or txt


def _get_auxiliary_agent(params: Dict[str, Any]) -> str:
    """
    Non-legacy config:
      params["auxiliary_agent"] in {"none","boundary","graph","review"}
    """
    aux = str(params.get("auxiliary_agent") or "none").strip().lower()
    if aux not in {"none", "boundary", "graph", "review"}:
        raise ValueError(f"auxiliary_agent must be one of none|boundary|graph|review (got {aux!r})")

    # Optional: hard fail if legacy flags are present, to avoid silent behavior drift.
    legacy_keys = {"enable_boundary_agent", "enable_graph_agent", "enable_reviewer_agent"}
    present = [k for k in legacy_keys if k in params]
    if present:
        raise ValueError(
            "Legacy aux flags are not supported anymore. "
            f"Remove {present} and use auxiliary_agent instead."
        )

    return aux


def _run_minimal(*, cycle: Dict[str, Any], language: str = "python") -> ExplainEngineResult:
    cycle_nodes = filtered_cycle_nodes([str(n) for n in (cycle.get("nodes") or [])], skip_init=True, language=language)
    prompt = build_minimal_prompt(cycle_nodes, language=language)
    return ExplainEngineResult(cycle_nodes=cycle_nodes, final_prompt_text=prompt)


def _run_multi_agent(
    *,
    client: LLMClient,
    transcript_path: str,
    repo_root: str,
    language: str,
    cycle: Dict[str, Any],
    scc_report: Dict[str, Any],
    params: Dict[str, Any],
) -> ExplainEngineResult:
    language = require_language(language)

    edge_variant_id = str(params.get("edge_variant") or "E0").strip()
    synthesizer_variant_id = str(params.get("synthesizer_variant") or "S0").strip()
    auxiliary_agent = _get_auxiliary_agent(params)

    raw_nodes = [str(n) for n in (cycle.get("nodes") or [])]
    cycle_nodes = filtered_cycle_nodes(raw_nodes, skip_init=True, language=language)

    raw_edges = list(cycle.get("edges") or [])
    filtered_edges: List[Edge] = []
    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        a = str(e.get("source") or "")
        b = str(e.get("target") or "")
        if not a or not b:
            continue
        if a not in cycle_nodes or b not in cycle_nodes:
            continue
        filtered_edges.append(Edge(a=a, b=b))

    files_by_node = read_cycle_files(repo_root=repo_root, cycle_nodes=cycle_nodes, skip_init=True)

    edge_reports: List[str] = []
    for edge in filtered_edges:
        report = run_edge_agent(
            client=client,
            transcript_path=transcript_path,
            language=language,
            cycle_nodes=cycle_nodes,
            edge=edge,
            files_by_node=files_by_node,
            edge_variant_id=edge_variant_id,
        )
        edge_reports.append(report)

    aux_context = ""
    if auxiliary_agent == "boundary":
        boundary_text = run_boundary_agent(
            client=client,
            transcript_path=transcript_path,
            language=language,
            cycle_nodes=cycle_nodes,
        )
        aux_context = "=== Boundary heuristic agent ===\n" + boundary_text.strip()

    elif auxiliary_agent == "graph":
        cycle_id = str(cycle.get("id") or "")
        scc_id = _parse_scc_id_from_cycle_id(cycle_id) or ""
        scc_text = _build_scc_text_from_report(scc_report, scc_id) if scc_id else ""
        graph_text = run_graph_agent(
            client=client,
            transcript_path=transcript_path,
            language=language,
            cycle_nodes=cycle_nodes,
            scc_text=scc_text,
        )
        aux_context = "=== Structural context agent ===\n" + graph_text.strip()

    synthesizer_text = run_synthesizer_agent(
        client=client,
        transcript_path=transcript_path,
        language=language,
        cycle_nodes=cycle_nodes,
        edge_reports=edge_reports,
        aux_context=aux_context,
        synthesizer_variant_id=synthesizer_variant_id,
    ).strip()

    if auxiliary_agent == "review":
        reviewer_text = run_review_agent(
            client=client,
            transcript_path=transcript_path,
            language=language,
            cycle_nodes=cycle_nodes,
            edge_reports=edge_reports,
            synthesizer_text=synthesizer_text,
            aux_context=aux_context,  # will be "" in review-mode, but harmless
        ).strip()
        synthesizer_text = _extract_revised_explanation(reviewer_text).strip() or synthesizer_text

    minimal = build_minimal_prompt(cycle_nodes, language=language)

    explanation_block_parts: List[str] = []
    explanation_block_parts.append("=== Cycle explanation (multi-agent) ===")
    explanation_block_parts.append(synthesizer_text)

    explanation_block_parts.append("")
    explanation_block_parts.append("=== Per-edge reports (appendix) ===")
    for i, (edge, report) in enumerate(zip(filtered_edges, edge_reports), 1):
        explanation_block_parts.append("")
        explanation_block_parts.append(f"--- Edge {i}: {edge.a} -> {edge.b} ---")
        explanation_block_parts.append(report.strip())

    explanation_block = "\n".join(explanation_block_parts).strip() + "\n"
    final_prompt = (minimal + "\n" + explanation_block).rstrip() + "\n"

    return ExplainEngineResult(cycle_nodes=cycle_nodes, final_prompt_text=final_prompt)


def run_explain_engine(
    *,
    client: LLMClient,
    transcript_path: str,
    repo_root: str,
    language: str,
    cycle: Dict[str, Any],
    scc_report: Dict[str, Any],
    params: Dict[str, Any],
) -> ExplainEngineResult:
    language = require_language(language)

    orchestrator_id = str(params.get("orchestrator") or "multi_agent").strip()
    if orchestrator_id not in {"minimal", "multi_agent"}:
        raise ValueError("orchestrator must be 'minimal' or 'multi_agent'")

    if orchestrator_id == "minimal":
        return _run_minimal(cycle=cycle, language=language)

    return _run_multi_agent(
        client=client,
        transcript_path=transcript_path,
        repo_root=repo_root,
        language=language,
        cycle=cycle,
        scc_report=scc_report,
        params=params,
    )
