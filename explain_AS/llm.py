from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from budgeting import (
    estimate_tokens_from_text,
    trim_text_bottom_with_info,
    tokens_to_chars,
)


def utc_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_jsonl(transcript_path: str, obj: Dict[str, Any]) -> None:
    """
    Append one JSON object as one line. Never throws.
    """
    if not transcript_path:
        return
    try:
        os.makedirs(os.path.dirname(transcript_path), exist_ok=True)
        with open(transcript_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ----------------------------
# console printing (kept)
# ----------------------------

class _Ansi:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"

    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    BLUE = "\x1b[34m"
    MAGENTA = "\x1b[35m"
    CYAN = "\x1b[36m"
    GRAY = "\x1b[90m"


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None:
        return int(default)
    try:
        return int(str(v).strip())
    except Exception:
        return int(default)


def _use_color() -> bool:
    if _env_bool("ATD_EXPLAIN_NO_COLOR", False):
        return False
    try:
        return bool(getattr(sys.stdout, "isatty", lambda: False)())
    except Exception:
        return False


def _should_print() -> bool:
    return _env_bool("ATD_EXPLAIN_PRINT", True)


def _clip_middle(text: str, max_chars: int) -> str:
    s = text or ""
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s

    half = max_chars // 2
    head = s[:half]
    tail = s[-half:]

    snip = "\n... [snip for console] ...\n"
    if _use_color():
        snip = f"\n{_Ansi.GRAY}... [snip for console] ...{_Ansi.RESET}\n"
    return head + snip + tail


def _print_block(title: str, body: str, *, color: str) -> None:
    if not _should_print():
        return
    try:
        use_color = _use_color()
        prefix = color if use_color else ""
        reset = _Ansi.RESET if use_color else ""
        bold = _Ansi.BOLD if use_color else ""
        dim = _Ansi.DIM if use_color else ""

        sys.stdout.write(f"{bold}{prefix}{title}{reset}\n")
        if body:
            sys.stdout.write(f"{dim}{body}{reset}\n")
        sys.stdout.flush()
    except Exception:
        pass


def _print_agent_event(
    *,
    agent_name: str,
    edge_id: Optional[str],
    prompt_text: str,
    reply_text: str,
    prompt_tokens_estimate: int,
    available_completion_tokens: int,
    reserved_min_output_tokens: int,
    max_tokens_for_call: int,
    usage: Optional[Dict[str, int]],
) -> None:
    if not _should_print():
        return

    show_prompt_preview = _env_bool("ATD_EXPLAIN_SHOW_PROMPT_PREVIEW", True)
    prompt_preview_chars = _env_int("ATD_EXPLAIN_PROMPT_PREVIEW_CHARS", 300)

    show_full_prompts = _env_bool("ATD_EXPLAIN_PRINT_PROMPTS", False)
    full_prompt_max_chars = _env_int("ATD_EXPLAIN_PRINT_MAX_CHARS", 4000)

    reply_max_chars = _env_int("ATD_EXPLAIN_REPLY_MAX_CHARS", 200000)

    head = f"[explain] agent={agent_name}"
    if edge_id:
        head += f" edge={edge_id}"

    _print_block(head, "", color=_Ansi.CYAN)

    if show_prompt_preview:
        _print_block(
            "  ├─ prompt preview",
            _clip_middle((prompt_text or "").strip(), prompt_preview_chars),
            color=_Ansi.BLUE,
        )

    if show_full_prompts:
        _print_block(
            "  ├─ prompt (full)",
            _clip_middle(prompt_text or "", full_prompt_max_chars),
            color=_Ansi.BLUE,
        )

    _print_block(
        "  ├─ reply",
        _clip_middle(reply_text or "", reply_max_chars),
        color=_Ansi.GREEN,
    )

    usage_str = ""
    if usage and isinstance(usage.get("prompt_tokens"), int) and isinstance(usage.get("completion_tokens"), int):
        usage_str = (
            f"usage(prompt={usage['prompt_tokens']}, "
            f"completion={usage['completion_tokens']}, total={usage.get('total_tokens')})"
        )

    meta = (
        f"  └─ meta prompt_tokens_est~{prompt_tokens_estimate} "
        f"avail_completion~{available_completion_tokens} "
        f"reserved_min={reserved_min_output_tokens} "
        f"max_tokens_call={max_tokens_for_call}"
    )
    if usage_str:
        meta += f"  {usage_str}"
    _print_block(meta, "", color=_Ansi.GRAY)


# ----------------------------
# usage accumulator
# ----------------------------

@dataclass
class UsageAccumulator:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage: Dict[str, Any]) -> None:
        p = usage.get("prompt_tokens")
        c = usage.get("completion_tokens")
        t = usage.get("total_tokens")
        if isinstance(p, int) and isinstance(c, int):
            if not isinstance(t, int):
                t = p + c
            self.prompt_tokens += p
            self.completion_tokens += c
            self.total_tokens += int(t)

    def as_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "total_tokens": int(self.total_tokens),
        }


# ----------------------------
# LLM client
# ----------------------------

class LLMClient:
    """
    Minimal OpenAI-compatible /v1/chat/completions client.
    The caller chooses per-call max_tokens.
    """
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        context_length: int,
        temperature: float = 0.2,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        seed: Optional[int] = None,
        timeout_sec: int = 600,
    ):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.context_length = int(context_length)
        self.temperature = float(temperature)
        self.top_p = float(top_p) if top_p is not None else None
        self.top_k = int(top_k) if top_k is not None else None
        self.seed = int(seed) if seed is not None else None
        self.timeout_sec = int(timeout_sec)

        self.usage = UsageAccumulator()
        self.last_usage: Optional[Dict[str, int]] = None

    def chat(self, user_prompt: str, *, max_tokens: int) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": float(self.temperature),
            "max_tokens": int(max_tokens),
        }
        if self.top_p is not None:
            payload["top_p"] = float(self.top_p)
        if self.top_k is not None:
            payload["top_k"] = int(self.top_k)
        if self.seed is not None:
            payload["seed"] = int(self.seed)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        r = requests.post(self.url, headers=headers, json=payload, timeout=self.timeout_sec)
        r.raise_for_status()
        data = r.json()

        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            self.usage.add(usage)
            p = usage.get("prompt_tokens")
            c = usage.get("completion_tokens")
            t = usage.get("total_tokens")
            if isinstance(p, int) and isinstance(c, int):
                if not isinstance(t, int):
                    t = p + c
                self.last_usage = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": int(t)}

        return str(data["choices"][0]["message"]["content"] or "").strip()


# ----------------------------
# Agent wrapper
# ----------------------------

@dataclass(frozen=True)
class Agent:
    """
    Immutable agent: name only.  All prompt content is passed via user_prompt + optional preamble.
    """
    name: str

    def ask(
        self,
        *,
        client: LLMClient,
        transcript_path: str,
        user_prompt: str,
        min_output_tokens_reserved: int,
        safety_margin_tokens: int = 1000,
        max_output_chars_soft: Optional[int] = None,
        edge: Optional[str] = None,
        preamble: Optional[str] = None,
    ) -> str:
        """
        Policy:
        - If *preamble* is provided, prepend it to user_prompt with a separator.
        - Estimate tokens with chars/3 approximation.
        - Guarantee completion room:
              minimum_completion_tokens_required = min_output_tokens_reserved
          by truncating the PROMPT (bottom-only) at most once.
        - Set per-call max_tokens to "whatever is left" after prompt + safety margin.
        """
        preamble_text = (preamble or "").strip()
        prompt_text = f"{preamble_text}\n\n--- TASK ---\n\n{user_prompt}" if preamble_text else (user_prompt or "")

        minimum_completion_tokens_required = max(1, int(min_output_tokens_reserved))
        if client.context_length - int(safety_margin_tokens) <= minimum_completion_tokens_required:
            raise ValueError(
                "LLM context_length is too small for the configured completion budgets. "
                f"context_length={client.context_length}, safety_margin_tokens={safety_margin_tokens}, "
                f"minimum_completion_tokens_required={minimum_completion_tokens_required}."
            )

        truncation_marker = "\n\n[Truncated]\n"
        truncation_marker_tokens_estimate = estimate_tokens_from_text(truncation_marker)

        prompt_tokens_estimate = estimate_tokens_from_text(prompt_text)
        available_completion_tokens = client.context_length - int(safety_margin_tokens) - int(prompt_tokens_estimate)

        if available_completion_tokens < minimum_completion_tokens_required:
            target_prompt_tokens = (
                int(client.context_length)
                - int(safety_margin_tokens)
                - int(minimum_completion_tokens_required)
                - int(truncation_marker_tokens_estimate)
            )
            target_prompt_tokens = max(1, target_prompt_tokens)
            target_prompt_chars = tokens_to_chars(target_prompt_tokens)

            trimmed_prompt_text, trim_info = trim_text_bottom_with_info(prompt_text, target_prompt_chars)
            if not trim_info.truncated:
                raise ValueError(
                    "Prompt could not be truncated enough to satisfy minimum completion tokens. "
                    f"prompt_tokens_estimate={prompt_tokens_estimate}, "
                    f"available_completion_tokens={available_completion_tokens}, "
                    f"minimum_completion_tokens_required={minimum_completion_tokens_required}."
                )

            prompt_text = trimmed_prompt_text + truncation_marker

            # Recompute after truncation (must now fit, or we fail loudly)
            prompt_tokens_estimate = estimate_tokens_from_text(prompt_text)
            available_completion_tokens = client.context_length - int(safety_margin_tokens) - int(prompt_tokens_estimate)
            if available_completion_tokens < minimum_completion_tokens_required:
                raise ValueError(
                    "Prompt truncation still did not satisfy minimum completion tokens. "
                    f"available_completion_tokens={available_completion_tokens}, "
                    f"minimum_completion_tokens_required={minimum_completion_tokens_required}."
                )

        max_tokens_for_call = max(1, int(available_completion_tokens))

        append_jsonl(
            transcript_path,
            {
                "ts": utc_ts(),
                "event": "prompt",
                "agent": self.name,
                "edge": edge,
                "text": prompt_text,
                "max_tokens": int(max_tokens_for_call),
                "min_reserved_output_tokens": int(min_output_tokens_reserved),
                "minimum_completion_tokens_required": int(minimum_completion_tokens_required),
                "safety_margin_tokens": int(safety_margin_tokens),
                "prompt_tokens_estimate": int(prompt_tokens_estimate),
                "available_completion_tokens": int(available_completion_tokens),
                "temperature": float(getattr(client, "temperature", 0.0)),
                "top_p": getattr(client, "top_p", None),
                "top_k": getattr(client, "top_k", None),
                "seed": getattr(client, "seed", None),
            },
        )

        reply_text = client.chat(prompt_text, max_tokens=max_tokens_for_call)

        if max_output_chars_soft is not None and max_output_chars_soft > 0 and len(reply_text) > int(max_output_chars_soft):
            trimmed_reply_text, _ = trim_text_bottom_with_info(reply_text, int(max_output_chars_soft))
            reply_text = trimmed_reply_text + "\n\n[Truncated]\n"

        append_jsonl(
            transcript_path,
            {
                "ts": utc_ts(),
                "event": "reply",
                "agent": self.name,
                "edge": edge,
                "text": reply_text,
            },
        )

        if client.last_usage:
            append_jsonl(
                transcript_path,
                {
                    "ts": utc_ts(),
                    "event": "usage",
                    "agent": self.name,
                    "edge": edge,
                    **client.last_usage,
                },
            )

        _print_agent_event(
            agent_name=self.name,
            edge_id=edge,
            prompt_text=prompt_text,
            reply_text=reply_text,
            prompt_tokens_estimate=int(prompt_tokens_estimate),
            available_completion_tokens=int(available_completion_tokens),
            reserved_min_output_tokens=int(min_output_tokens_reserved),
            max_tokens_for_call=int(max_tokens_for_call),
            usage=client.last_usage,
        )

        return reply_text
