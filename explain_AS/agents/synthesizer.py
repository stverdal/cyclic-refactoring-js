from __future__ import annotations

from typing import List, Sequence

from agents.prompts.prompts_synthesizer import require_synthesizer_variant
from budgeting import (
    estimate_tokens_from_text,
    tokens_to_chars,
    allocate_token_budgets_even_share_with_redistribution,
)
from context import cycle_chain_str, format_block_for_prompt, require_language
from language import edge_semantics_text
from llm import Agent, LLMClient


SYNTHESIZER_MIN_OUTPUT_TOKENS_RESERVED = 3334  # previously ~10k chars at 3 chars/token
SYNTHESIZER_SAFETY_MARGIN_TOKENS = 1000


def build_synthesizer_user_prompt(
    *,
    client: LLMClient,
    language: str,
    cycle_nodes: List[str],
    edge_reports: Sequence[str],
    aux_context: str,
    synthesizer_variant_id: str,
) -> str:
    language = require_language(language)
    synthesizer_prompt_variant = require_synthesizer_variant(synthesizer_variant_id)

    semantics = edge_semantics_text(language)
    cycle_chain = cycle_chain_str(cycle_nodes)

    prompt_prefix = f"""{semantics}

Cycle:
{cycle_chain}

Edge reports (in cycle order, may be truncated):
""".rstrip() + "\n"

    aux_separator = "\n\nOptional additional context (may be truncated):\n"
    prompt_suffix = f"\n\n{synthesizer_prompt_variant.output_headings}\n"

    normalized_edge_reports = [str(report or "").strip() or "N/A" for report in (edge_reports or [])]
    normalized_aux_text = str(aux_context or "").strip() or "None."

    overhead_tokens_estimate = estimate_tokens_from_text(prompt_prefix + aux_separator + prompt_suffix)

    total_input_tokens_budget = (
        int(client.context_length)
        - int(SYNTHESIZER_SAFETY_MARGIN_TOKENS)
        - int(SYNTHESIZER_MIN_OUTPUT_TOKENS_RESERVED)
        - int(overhead_tokens_estimate)
    )
    total_input_tokens_budget = max(0, int(total_input_tokens_budget))

    budget_items = normalized_edge_reports + [normalized_aux_text]
    budget_item_needs_tokens = [estimate_tokens_from_text(item_text) for item_text in budget_items]

    budget_item_allocations_tokens = allocate_token_budgets_even_share_with_redistribution(
        item_token_needs=budget_item_needs_tokens,
        total_tokens=int(total_input_tokens_budget),
    )

    rendered_edge_blocks: List[str] = []
    for index, (edge_report_text, allocated_tokens) in enumerate(
        zip(normalized_edge_reports, budget_item_allocations_tokens[: len(normalized_edge_reports)]), start=1
    ):
        allocated_chars = max(1, tokens_to_chars(int(allocated_tokens))) if allocated_tokens > 0 else 1
        edge_report_block, _was_truncated = format_block_for_prompt(
            repo_rel_path=f"EDGE_REPORT_{index}.txt",
            block_text=edge_report_text,
            max_chars=int(allocated_chars),
        )
        rendered_edge_blocks.append(f"--- Edge report {index} ---\n{edge_report_block}".strip())

    aux_allocated_tokens = budget_item_allocations_tokens[-1] if budget_item_allocations_tokens else 0
    aux_allocated_chars = max(1, tokens_to_chars(int(aux_allocated_tokens))) if aux_allocated_tokens > 0 else 1
    aux_block, _aux_truncated = format_block_for_prompt(
        repo_rel_path="AUX_CONTEXT.txt",
        block_text=normalized_aux_text,
        max_chars=int(aux_allocated_chars),
    )

    edges_section = "\n\n".join(rendered_edge_blocks).strip() or "N/A"
    return prompt_prefix + edges_section + aux_separator + aux_block + prompt_suffix


def run_synthesizer_agent(
    *,
    client: LLMClient,
    transcript_path: str,
    language: str,
    cycle_nodes: List[str],
    edge_reports: Sequence[str],
    aux_context: str = "",
    synthesizer_variant_id: str = "S0",
) -> str:
    language = require_language(language)
    synthesizer_prompt_variant = require_synthesizer_variant(synthesizer_variant_id)

    synthesizer_agent = Agent(name="synthesizer")

    user_prompt = build_synthesizer_user_prompt(
        client=client,
        language=language,
        cycle_nodes=cycle_nodes,
        edge_reports=edge_reports,
        aux_context=aux_context,
        synthesizer_variant_id=synthesizer_variant_id,
    )

    # No arbitrary output cap: Agent.ask derives soft limit from reserved tokens by default.
    return synthesizer_agent.ask(
        client=client,
        transcript_path=transcript_path,
        user_prompt=user_prompt,
        preamble=synthesizer_prompt_variant.system_prompt,
        min_output_tokens_reserved=int(SYNTHESIZER_MIN_OUTPUT_TOKENS_RESERVED),
        safety_margin_tokens=int(SYNTHESIZER_SAFETY_MARGIN_TOKENS),
        max_output_chars_soft=None,
    )
