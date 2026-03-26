from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agents.boundary import SCCEdge, run_boundary_agent
from agents.edge import Edge, run_edge_agent
from agents.graph import run_graph_agent
from agents.project_context import run_project_context_agent
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


def _extract_scc_edges_from_report(scc_report: Dict[str, Any], scc_id: str) -> List[SCCEdge]:
    """
    Return structured edges for the SCC identified by *scc_id*.
    Falls back to an empty list when the SCC is not found.
    """
    for scc in (scc_report.get("sccs") or []):
        if str(scc.get("id")) != scc_id:
            continue

        edges_raw = scc.get("edges") or []
        result: List[SCCEdge] = []
        for e in edges_raw:
            if not isinstance(e, dict):
                continue
            src = str(e.get("source") or "")
            tgt = str(e.get("target") or "")
            if src and tgt:
                result.append(SCCEdge(source=src, target=tgt))
        return result

    return []


def _get_auxiliary_agent(params: Dict[str, Any]) -> str:
    """
    Non-legacy config:
      params["auxiliary_agent"] in {"none","boundary","graph","project"}
    """
    aux = str(params.get("auxiliary_agent") or "none").strip().lower()
    if aux not in {"none", "boundary", "graph", "project"}:
        raise ValueError(f"auxiliary_agent must be one of none|boundary|graph|project (got {aux!r})")

    # Optional: hard fail if legacy flags are present, to avoid silent behavior drift.
    legacy_keys = {"enable_boundary_agent", "enable_graph_agent", "enable_project_agent"}
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
    src_root: str,
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

    # Extract structured SCC edges (used by boundary + graph agents)
    cycle_id = str(cycle.get("id") or "")
    scc_id = _parse_scc_id_from_cycle_id(cycle_id) or ""
    scc_edges: List[SCCEdge] = _extract_scc_edges_from_report(scc_report, scc_id) if scc_id else []

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
            scc_edges=scc_edges,
            src_root=src_root,
        )
        aux_context = "=== Boundary heuristic agent ===\n" + boundary_text.strip()

    elif auxiliary_agent == "graph":
        graph_text = run_graph_agent(
            client=client,
            transcript_path=transcript_path,
            language=language,
            cycle_nodes=cycle_nodes,
            scc_edges=scc_edges,
        )
        aux_context = "=== Structural context agent ===\n" + graph_text.strip()

    elif auxiliary_agent == "project":
        project_text = run_project_context_agent(
            client=client,
            transcript_path=transcript_path,
            repo_root=repo_root,
        )
        aux_context = "=== Project context agent ===\n" + project_text.strip()

    synthesizer_text = run_synthesizer_agent(
        client=client,
        transcript_path=transcript_path,
        language=language,
        cycle_nodes=cycle_nodes,
        edge_reports=edge_reports,
        aux_context=aux_context,
        synthesizer_variant_id=synthesizer_variant_id,
    ).strip()

    # Build final prompt: minimal base prompt + synthesizer output only
    # (per-edge reports are already consumed by the synthesizer —
    #  appending them again only wastes downstream token budget).
    minimal = build_minimal_prompt(cycle_nodes, language=language)

    explanation_block_parts: List[str] = []
    explanation_block_parts.append("=== Cycle explanation (multi-agent) ===")
    explanation_block_parts.append(synthesizer_text)

    explanation_block = "\n".join(explanation_block_parts).strip() + "\n"
    final_prompt = (minimal + "\n" + explanation_block).rstrip() + "\n"

    return ExplainEngineResult(cycle_nodes=cycle_nodes, final_prompt_text=final_prompt)


def run_explain_engine(
    *,
    client: LLMClient,
    transcript_path: str,
    repo_root: str,
    src_root: str = "",
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
        src_root=src_root,
        language=language,
        cycle=cycle,
        scc_report=scc_report,
        params=params,
    )
