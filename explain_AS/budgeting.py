from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import List, Tuple

CHARS_PER_TOKEN = 3


def estimate_tokens_from_chars(character_count: int) -> int:
    if character_count <= 0:
        return 0
    return int(ceil(character_count / CHARS_PER_TOKEN))


def estimate_tokens_from_text(text: str) -> int:
    return estimate_tokens_from_chars(len(text or ""))


def tokens_to_chars(token_count: int) -> int:
    return max(0, int(token_count)) * CHARS_PER_TOKEN


def single_block_char_budget(
    *,
    block_text: str,
    wrapper_len_chars: int,
    total_tokens_budget: int,
) -> int:
    """
    Shared helper for the common "single wrapped block" budgeting pattern:
      need_tokens = tokens(body) + tokens(wrapper)
      allocated = min(need_tokens, total_tokens_budget)
      char_budget = tokens_to_chars(allocated), but at least 1 char.

    Notes:
    - wrapper_len_chars should be the exact wrapper length in CHARS for that block
      (e.g. from context.prompt_block_wrapper_len(path)).
    - The returned char budget is the TOTAL max_chars passed to format_block_for_prompt
      (wrapper + body + trunc suffix if needed).
    """
    total_tokens_budget = max(0, int(total_tokens_budget))
    wrapper_len_chars = max(0, int(wrapper_len_chars))

    need_tokens = estimate_tokens_from_text(block_text or "") + estimate_tokens_from_chars(wrapper_len_chars)
    allocated_tokens = min(int(need_tokens), int(total_tokens_budget))

    if allocated_tokens <= 0:
        return 1
    return max(1, tokens_to_chars(int(allocated_tokens)))


@dataclass(frozen=True)
class TruncationInfo:
    truncated: bool
    kept_chars: int
    total_chars: int


def trim_text_bottom_with_info(text: str, max_chars: int) -> Tuple[str, TruncationInfo]:
    """
    Keep the TOP of the text and truncate from the bottom if needed.
    """
    normalized_text = text or ""
    if max_chars <= 0:
        return "", TruncationInfo(truncated=(len(normalized_text) > 0), kept_chars=0, total_chars=len(normalized_text))
    if len(normalized_text) <= max_chars:
        return normalized_text, TruncationInfo(truncated=False, kept_chars=len(normalized_text), total_chars=len(normalized_text))
    kept = normalized_text[:max_chars]
    return kept, TruncationInfo(truncated=True, kept_chars=max_chars, total_chars=len(normalized_text))


def allocate_token_budgets_even_share_with_redistribution(
    *,
    item_token_needs: List[int],
    total_tokens: int,
) -> List[int]:
    """
    Deterministic allocation:
    - Give each item an equal share.
    - Items that need less than share create leftover.
    - Leftover is redistributed evenly across items still needing more.
    Repeats until no leftover or no remaining unmet needs.
    """
    item_count = len(item_token_needs)
    if item_count == 0:
        return []
    if total_tokens <= 0:
        return [0] * item_count

    normalized_needs = [max(0, int(x)) for x in item_token_needs]
    allocations = [0] * item_count

    remaining_tokens = int(total_tokens)

    # First pass: equal share
    equal_share = remaining_tokens // item_count
    for index in range(item_count):
        give = min(equal_share, normalized_needs[index])
        allocations[index] += give
        remaining_tokens -= give

    # Redistribution loop
    while remaining_tokens > 0:
        still_needing_indexes = [i for i in range(item_count) if allocations[i] < normalized_needs[i]]
        if not still_needing_indexes:
            break

        share_among_remaining = max(1, remaining_tokens // len(still_needing_indexes))
        progressed = False

        for i in still_needing_indexes:
            if remaining_tokens <= 0:
                break
            want = normalized_needs[i] - allocations[i]
            give = min(want, share_among_remaining, remaining_tokens)
            if give > 0:
                allocations[i] += give
                remaining_tokens -= give
                progressed = True

        if progressed:
            continue

        # Edge case: integer division produced too-small shares; give 1 token round-robin
        for i in still_needing_indexes:
            if remaining_tokens <= 0:
                break
            if allocations[i] < normalized_needs[i]:
                allocations[i] += 1
                remaining_tokens -= 1
                progressed = True

        if not progressed:
            break

    return allocations


def allocate_two_way_with_redistribution(*, need_a: int, need_b: int, total_tokens: int) -> Tuple[int, int]:
    """
    Special-case for Edge Agent: split evenly between two inputs, then shift leftover.
    """
    total = max(0, int(total_tokens))
    if total <= 0:
        return 0, 0

    a_need = max(0, int(need_a))
    b_need = max(0, int(need_b))

    half = total // 2
    allocated_a = min(half, a_need)
    allocated_b = min(total - allocated_a, b_need)  # ensure sum <= total

    leftover = total - (allocated_a + allocated_b)
    if leftover <= 0:
        return allocated_a, allocated_b

    remaining_need_a = a_need - allocated_a
    remaining_need_b = b_need - allocated_b

    if remaining_need_a <= 0 and remaining_need_b <= 0:
        return allocated_a, allocated_b

    if remaining_need_a >= remaining_need_b:
        give_a = min(leftover, remaining_need_a)
        allocated_a += give_a
        leftover -= give_a
        if leftover > 0:
            allocated_b += min(leftover, remaining_need_b)
    else:
        give_b = min(leftover, remaining_need_b)
        allocated_b += give_b
        leftover -= give_b
        if leftover > 0:
            allocated_a += min(leftover, remaining_need_a)

    return allocated_a, allocated_b
