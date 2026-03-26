from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Tuple

from budgeting import (
    allocate_token_budgets_even_share_with_redistribution,
    estimate_tokens_from_chars,
    estimate_tokens_from_text,
    tokens_to_chars,
)
from context import format_block_for_prompt, prompt_block_wrapper_len
from llm import Agent, LLMClient


PROJECT_MIN_OUTPUT_TOKENS_RESERVED = 1600
PROJECT_SAFETY_MARGIN_TOKENS = 1000

# Readme discovery + inclusion knobs
PROJECT_MAX_README_FILES = 3
PROJECT_MAX_WALK_FILES = 20000  # hard safety cap on directory walk
PROJECT_MAX_README_DEPTH = 6    # ignore very deep docs trees

# Per-readme hard cap before prompt budgeting (keeps huge READMEs sane)
PROJECT_README_HARD_CAP_CHARS = 60000


PROJECT_PROMPT_PREAMBLE = """You are the Project Context Agent.
You infer what the repository is about from README files only.

Rules:
- Stay grounded in the provided README text. Do not guess beyond it.
- No tables, no JSON.
- If you see truncation notes, assume some context may be missing.
- Base summaries on the specific facts in the provided reports and context. Avoid generic statements.
""".strip()


@dataclass(frozen=True)
class ReadmeDoc:
    repo_rel_path: str
    text: str


def _norm_path(p: str) -> str:
    return (p or "").replace("\\", "/").strip()


def _read_text_file(abs_path: str) -> str:
    with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _cap_text_top(text: str, max_chars: int) -> Tuple[str, bool]:
    s = text or ""
    if max_chars <= 0:
        return "", bool(s)
    if len(s) <= max_chars:
        return s, False
    return s[:max_chars], True


def _is_readme_candidate(filename: str) -> bool:
    name = (filename or "").strip().lower()
    return name.startswith("readme") and name.endswith(".md")


def _path_depth(repo_rel_path: str) -> int:
    p = _norm_path(repo_rel_path).strip("/")
    if not p:
        return 0
    return p.count("/")


def _find_readme_paths(repo_root: str) -> List[str]:
    """
    Deterministic README discovery:
    1) Prefer root-level README*.md (sorted)
    2) Then other README*.md by depth asc, path lexicographic
    Caps walk work for safety.
    """
    repo_root = os.path.abspath(repo_root)

    root_candidates: List[str] = []
    try:
        for name in sorted(os.listdir(repo_root)):
            if _is_readme_candidate(name):
                root_candidates.append(_norm_path(name))
    except Exception:
        pass

    walk_candidates: List[str] = []
    seen = set(root_candidates)

    visited = 0
    for dirpath, dirnames, filenames in os.walk(repo_root):
        visited += 1
        if visited > int(PROJECT_MAX_WALK_FILES):
            break

        rel_dir = _norm_path(os.path.relpath(dirpath, repo_root))
        if rel_dir == ".":
            rel_dir = ""

        if _path_depth(rel_dir) > int(PROJECT_MAX_README_DEPTH):
            dirnames[:] = []
            continue

        for fn in filenames:
            if not _is_readme_candidate(fn):
                continue
            rel_path = _norm_path(os.path.join(rel_dir, fn)) if rel_dir else _norm_path(fn)
            if rel_path in seen:
                continue
            seen.add(rel_path)
            walk_candidates.append(rel_path)

    walk_candidates.sort(key=lambda p: (_path_depth(p), p))
    return root_candidates + walk_candidates


def _load_readmes(repo_root: str, max_files: int) -> List[ReadmeDoc]:
    repo_root = os.path.abspath(repo_root)
    max_files = max(0, int(max_files))
    if max_files <= 0:
        return []

    paths = _find_readme_paths(repo_root)[:max_files]
    out: List[ReadmeDoc] = []

    for rel in paths:
        abs_path = os.path.join(repo_root, rel)
        if not os.path.exists(abs_path):
            continue
        try:
            raw = _read_text_file(abs_path)
        except Exception:
            continue

        capped, was_truncated = _cap_text_top(raw, int(PROJECT_README_HARD_CAP_CHARS))
        if was_truncated:
            capped = (capped.rstrip() + "\n[Hard-capped]\n").lstrip("\n")

        out.append(ReadmeDoc(repo_rel_path=rel, text=capped))
    return out


def build_project_context_user_prompt(
    *,
    repo_root: str,
    context_length: int,
) -> str:
    readmes = _load_readmes(repo_root, max_files=int(PROJECT_MAX_README_FILES))

    prompt_prefix = f"""{PROJECT_PROMPT_PREAMBLE}

README sources (may be truncated):
""".rstrip() + "\n"

    prompt_suffix = """

Write a natural-language memo for another agent.
Focus on project purpose, key concepts or terminology, architectural cues if visible, and important uncertainty.
""".lstrip()

    overhead_tokens_estimate = estimate_tokens_from_text(prompt_prefix + prompt_suffix)

    total_input_tokens_budget = (
        int(context_length)
        - int(PROJECT_SAFETY_MARGIN_TOKENS)
        - int(PROJECT_MIN_OUTPUT_TOKENS_RESERVED)
        - int(overhead_tokens_estimate)
    )
    total_input_tokens_budget = max(0, int(total_input_tokens_budget))

    if not readmes:
        block_id = "README (none found)"
        block_text = "No README*.md files were found."
        block_char_budget = max(1, tokens_to_chars(int(total_input_tokens_budget))) if total_input_tokens_budget > 0 else 1
        block, _ = format_block_for_prompt(
            repo_rel_path=block_id,
            block_text=block_text,
            max_chars=int(block_char_budget),
        )
        return (prompt_prefix + block + "\n" + prompt_suffix).strip() + "\n"

    needs_tokens: List[int] = []
    block_ids: List[str] = []
    texts: List[str] = []

    for d in readmes:
        block_id = f"README: {d.repo_rel_path}"
        need = estimate_tokens_from_text(d.text) + estimate_tokens_from_chars(prompt_block_wrapper_len(block_id))
        needs_tokens.append(int(need))
        block_ids.append(block_id)
        texts.append(d.text)

    allocations_tokens = allocate_token_budgets_even_share_with_redistribution(
        item_token_needs=needs_tokens,
        total_tokens=int(total_input_tokens_budget),
    )

    rendered_blocks: List[str] = []
    for block_id, text, alloc in zip(block_ids, texts, allocations_tokens):
        alloc_chars_total = max(1, tokens_to_chars(int(alloc))) if alloc > 0 else 1
        block, _ = format_block_for_prompt(
            repo_rel_path=block_id,
            block_text=text,
            max_chars=int(alloc_chars_total),
        )
        rendered_blocks.append(block.strip())

    readme_section = "\n\n".join([b for b in rendered_blocks if b.strip()]).strip() or "N/A"
    return (prompt_prefix + readme_section + "\n" + prompt_suffix).strip() + "\n"


def run_project_context_agent(
    *,
    client: LLMClient,
    transcript_path: str,
    repo_root: str,
) -> str:
    agent = Agent(name="project_context")

    user_prompt = build_project_context_user_prompt(
        repo_root=repo_root,
        context_length=int(client.context_length),
    )

    return agent.ask(
        client=client,
        transcript_path=transcript_path,
        user_prompt=user_prompt,
        min_output_tokens_reserved=int(PROJECT_MIN_OUTPUT_TOKENS_RESERVED),
        safety_margin_tokens=int(PROJECT_SAFETY_MARGIN_TOKENS),
        max_output_chars_soft=None,
    )
