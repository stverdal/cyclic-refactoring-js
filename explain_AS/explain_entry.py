from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from engine import run_explain_engine
from llm import LLMClient
from context import require_language


LLM_BLOCKED_EXIT_CODE = 42


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _mode_params(params_json: Optional[str]) -> Dict[str, Any]:
    if params_json:
        return json.loads(params_json)
    env = os.environ.get("ATD_MODE_PARAMS_JSON", "").strip()
    return json.loads(env) if env else {}


def _need_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"Missing required env var {name}. This should be provided by the pipeline.")
    return v


def _need_env_int(name: str) -> int:
    s = _need_env(name)
    try:
        v = int(s)
    except Exception:
        raise SystemExit(f"Env var {name} must be an int (got: {s!r})")
    if v <= 0:
        raise SystemExit(f"Env var {name} must be > 0 (got: {v})")
    return v


def _find_cycle_in_catalog(catalog: Dict[str, Any], cycle_id: str) -> Dict[str, Any]:
    for scc in (catalog.get("sccs") or []):
        for cyc in (scc.get("cycles") or []):
            if str(cyc.get("id")) == str(cycle_id):
                return cyc
    raise KeyError(f"cycle_id {cycle_id!r} not found in cycle_catalog.json")


def _language_from_scc_report(scc_report: Dict[str, Any]) -> str:
    lang = str(((scc_report.get("input") or {}).get("language") or "")).strip()
    return require_language(lang)


def main() -> None:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--repo-root", required=True)
    argument_parser.add_argument("--src-root", required=True)
    argument_parser.add_argument("--scc-report", required=True)
    argument_parser.add_argument("--cycle-catalog", required=True)
    argument_parser.add_argument("--cycle-id", required=True)
    argument_parser.add_argument("--out-prompt", required=True)
    argument_parser.add_argument("--params-json", default=None)
    args = argument_parser.parse_args()

    repo_root = str(Path(args.repo_root).resolve())
    scc_report_path = Path(args.scc_report).resolve()
    cycle_catalog_path = Path(args.cycle_catalog).resolve()
    out_prompt_path = Path(args.out_prompt).resolve()
    out_prompt_path.parent.mkdir(parents=True, exist_ok=True)

    mode_params = _mode_params(args.params_json)

    scc_report = _load_json(scc_report_path)
    cycle_catalog = _load_json(cycle_catalog_path)
    cycle = _find_cycle_in_catalog(cycle_catalog, args.cycle_id)

    language = _language_from_scc_report(scc_report)

    transcript_path = str(out_prompt_path.parent / "transcript.jsonl")
    llm_usage_path = out_prompt_path.parent / "llm_usage.json"

    orchestrator_id = str(mode_params.get("orchestrator") or "multi_agent").strip()
    temperature = float(mode_params.get("temperature", 0.2))
    top_p = mode_params.get("top_p")
    top_k = mode_params.get("top_k")
    seed = mode_params.get("seed")
    if top_p is not None:
        top_p = float(top_p)
    if top_k is not None:
        top_k = int(top_k)
    if seed is not None:
        seed = int(seed)

    should_call_llm = orchestrator_id != "minimal"

    # Print early header so blocked runs still show useful context in logs
    try:
        print("=== Explain entry ===")
        print(f"repo_root     : {repo_root}")
        print(f"cycle_id      : {args.cycle_id}")
        print(f"language      : {language}")
        print(f"orchestrator  : {orchestrator_id}")
        print(f"edge_variant  : {mode_params.get('edge_variant', 'E0')}")
        print(f"synth_variant : {mode_params.get('synthesizer_variant', 'S0')}")
        print(f"aux_agent     : {mode_params.get('auxiliary_agent', 'none')}")
        print(f"out_prompt    : {str(out_prompt_path)}")
        print("")
    except Exception:
        pass

    try:
        if should_call_llm:
            llm_url = _need_env("LLM_URL")
            api_key = _need_env("LLM_API_KEY")
            model = _need_env("LLM_MODEL")
            context_length = _need_env_int("LLM_CONTEXT_LENGTH")

            client = LLMClient(
                url=llm_url,
                api_key=api_key,
                model=model,
                context_length=context_length,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                seed=seed,
            )
        else:
            # Dummy client (never used).
            client = LLMClient(
                url="http://localhost/unused",
                api_key="unused",
                model="unused",
                context_length=8192,
                temperature=0.0,
            )

        result = run_explain_engine(
            client=client,
            transcript_path=transcript_path,
            repo_root=repo_root,
            src_root=str(Path(args.src_root).resolve()),
            language=language,
            cycle=cycle,
            scc_report=scc_report,
            params=mode_params,
        )

        out_prompt_path.write_text(result.final_prompt_text, encoding="utf-8")

        llm_usage_payload = {
            "language": language,
            "orchestrator": orchestrator_id,
            "model": getattr(client, "model", ""),
            "context_length": getattr(client, "context_length", 0),
            "temperature": getattr(client, "temperature", 0.0),
            "accumulated_usage": client.usage.as_dict(),
            "last_call_usage": client.last_usage,
        }
        llm_usage_path.write_text(json.dumps(llm_usage_payload, indent=2, sort_keys=True), encoding="utf-8")

        print(result.final_prompt_text)

    except requests.HTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (400, 413, 422):
            raise SystemExit(1)
        if status in (401, 403, 404):
            raise SystemExit(1)
        raise SystemExit(LLM_BLOCKED_EXIT_CODE)

    except requests.RequestException:
        raise SystemExit(LLM_BLOCKED_EXIT_CODE)


if __name__ == "__main__":
    main()
