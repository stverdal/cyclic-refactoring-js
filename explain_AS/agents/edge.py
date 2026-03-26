from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from agents.prompts.prompts_edge import require_edge_variant
from budgeting import (
    estimate_tokens_from_text,
    allocate_two_way_with_redistribution,
    tokens_to_chars,
)
from context import (
    cap_file_text_hard,
    cycle_chain_str,
    edge_str,
    format_block_for_prompt,
    get_file_text,
    require_language,
)
from language import edge_semantics_text
from llm import Agent, LLMClient


EDGE_MIN_OUTPUT_TOKENS_RESERVED = 2000  # baseline reservation (approx 6k chars at 3 chars/token)
EDGE_SAFETY_MARGIN_TOKENS = 1000


@dataclass(frozen=True)
class Edge:
    a: str  # repo-relative file path
    b: str  # repo-relative file path


def build_edge_user_prompt(
    *,
    client: LLMClient,
    language: str,
    cycle_nodes: List[str],
    edge: Edge,
    files_by_node: Dict[str, str],
    edge_variant_id: str,
) -> str:
    language = require_language(language)
    edge_prompt_variant = require_edge_variant(edge_variant_id)

    semantics = edge_semantics_text(language)
    cycle_chain = cycle_chain_str(cycle_nodes)

    file_a_raw_text = get_file_text(files_by_node, edge.a)
    file_b_raw_text = get_file_text(files_by_node, edge.b)

    file_a_hard_capped_text, file_a_hard_truncated = cap_file_text_hard(file_a_raw_text)
    file_b_hard_capped_text, file_b_hard_truncated = cap_file_text_hard(file_b_raw_text)

    prompt_prefix = f"""{semantics}

Cycle chain:
{cycle_chain}

Analyze this specific edge:
A = {edge.a}
B = {edge.b}

{edge_prompt_variant.output_headings}

File A:
""".rstrip() + "\n"

    file_separator = "\nFile B:\n"
    prompt_suffix = ""

    overhead_tokens_estimate = estimate_tokens_from_text(prompt_prefix + file_separator + prompt_suffix)
    total_input_tokens_budget = (
        int(client.context_length)
        - int(EDGE_SAFETY_MARGIN_TOKENS)
        - int(EDGE_MIN_OUTPUT_TOKENS_RESERVED)
        - int(overhead_tokens_estimate)
    )
    total_input_tokens_budget = max(0, int(total_input_tokens_budget))

    file_a_tokens_needed = estimate_tokens_from_text(file_a_hard_capped_text)
    file_b_tokens_needed = estimate_tokens_from_text(file_b_hard_capped_text)

    file_a_tokens_allocated, file_b_tokens_allocated = allocate_two_way_with_redistribution(
        need_a=int(file_a_tokens_needed),
        need_b=int(file_b_tokens_needed),
        total_tokens=int(total_input_tokens_budget),
    )

    file_a_char_budget = max(1, tokens_to_chars(int(file_a_tokens_allocated))) if file_a_tokens_allocated > 0 else 1
    file_b_char_budget = max(1, tokens_to_chars(int(file_b_tokens_allocated))) if file_b_tokens_allocated > 0 else 1

    if file_a_hard_truncated:
        file_a_hard_capped_text = (
            "[NOTE: HARD-CAPPED to 40,000 chars before prompt budgeting. Some code omitted.]\n"
            + file_a_hard_capped_text
        )
    if file_b_hard_truncated:
        file_b_hard_capped_text = (
            "[NOTE: HARD-CAPPED to 40,000 chars before prompt budgeting. Some code omitted.]\n"
            + file_b_hard_capped_text
        )

    file_a_block, _file_a_was_truncated = format_block_for_prompt(
        repo_rel_path=edge.a,
        block_text=file_a_hard_capped_text,
        max_chars=int(file_a_char_budget),
    )
    file_b_block, _file_b_was_truncated = format_block_for_prompt(
        repo_rel_path=edge.b,
        block_text=file_b_hard_capped_text,
        max_chars=int(file_b_char_budget),
    )

    return prompt_prefix + file_a_block + file_separator + file_b_block + "\n" + prompt_suffix


def run_edge_agent(
    *,
    client: LLMClient,
    transcript_path: str,
    language: str,
    cycle_nodes: List[str],
    edge: Edge,
    files_by_node: Dict[str, str],
    edge_variant_id: str = "E0",
) -> str:
    language = require_language(language)
    edge_prompt_variant = require_edge_variant(edge_variant_id)

    edge_agent = Agent(name="edge")

    user_prompt = build_edge_user_prompt(
        client=client,
        language=language,
        cycle_nodes=cycle_nodes,
        edge=edge,
        files_by_node=files_by_node,
        edge_variant_id=edge_variant_id,
    )

    # No arbitrary output cap: Agent.ask derives soft limit from reserved tokens by default.
    return edge_agent.ask(
        client=client,
        transcript_path=transcript_path,
        user_prompt=user_prompt,
        preamble=edge_prompt_variant.system_prompt,
        edge=edge_str(edge.a, edge.b),
        min_output_tokens_reserved=int(EDGE_MIN_OUTPUT_TOKENS_RESERVED),
        safety_margin_tokens=int(EDGE_SAFETY_MARGIN_TOKENS),
        max_output_chars_soft=None,
    )
