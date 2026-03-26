from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4


def utc_timestamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_execution_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8]


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------- Resume / skipping ----------------

def _maybe_skip_completed_phase(
    *,
    branch_results_dir: Path,
    phase: str,
    validate_unit_inputs,
    validate_unit_outputs,
    unit_run,
) -> bool:
    """
    Skip rerun if phase already completed successfully
    and inputs/outputs still validate.
    """
    status_path = branch_results_dir / f"status_{phase}.json"

    if not status_path.exists():
        return False

    try:
        status = read_json(status_path)
    except Exception:
        return False

    if str(status.get("outcome", "")).strip() != "ok":
        return False

    # Re-check inputs/outputs
    in_outcome, _, _ = validate_unit_inputs(unit_run)
    if in_outcome != "ok":
        return False

    out_outcome, _, _ = validate_unit_outputs(unit_run)
    if out_outcome != "ok":
        return False

    print(f"[resume] Skipping {unit_run.repo_spec.repo}:{unit_run.refactor_branch} phase={phase}")
    return True


# ---------------- Branch naming ----------------

def sanitize_git_branch_name(candidate: str) -> str:
    candidate = candidate.strip().replace(" ", "-")
    candidate = re.sub(r"[^A-Za-z0-9._/-]+", "-", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-").rstrip("/")
    return candidate


def make_refactor_branch_name(experiment_id: str, mode_id: str, cycle_id: str) -> str:
    branch_name = sanitize_git_branch_name(f"atd-{experiment_id}-{mode_id}-{cycle_id}")
    if not branch_name:
        raise ValueError("refactor branch name became empty after sanitation")
    return branch_name


def results_dir_for_branch(results_root: Path, repo_name: str, branch_name: str) -> Path:
    return results_root / repo_name / "branches" / branch_name


# ---------------- Subprocess ----------------

def run_subprocess_command(
    command: List[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> int:
    print("$ " + " ".join(command))
    merged = os.environ.copy()
    if env:
        merged.update(env)
    process = subprocess.run(command, cwd=str(cwd) if cwd else None, env=merged)
    return int(process.returncode)


# ---------------- LLM env ----------------

def make_llm_environment(pipeline_config) -> Dict[str, str]:
    base_url = pipeline_config.llm.base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        raise ValueError(
            f"llm.base_url must end with '/v1' (got: {pipeline_config.llm.base_url}). "
            "Example: http://host.docker.internal:8012/v1"
        )

    return {
        "LLM_BASE_URL": base_url,
        "LLM_URL": f"{base_url}/chat/completions",
        "LLM_MODEL": pipeline_config.llm.model_raw,
        "LLM_API_KEY": pipeline_config.llm.api_key,
        # explain step reads this
        "LLM_CONTEXT_LENGTH": str(int(pipeline_config.llm.context_length)),
        "RUNTIME_IMAGE": pipeline_config.openhands.runtime_image,
        "MAX_ITERS": str(pipeline_config.openhands.max_iters),
        "COMMIT_MESSAGE": pipeline_config.openhands.commit_message,
    }


# ---------------- Branch cleanup helpers ----------------

def _git(repo_dir: Path, args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_dir)] + args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def maybe_delete_refactor_branch(
    *,
    enabled: bool,
    repo_dir: Path,
    experiment_id: str,
    base_branch: str,
    refactor_branch: str,
) -> None:
    if not enabled:
        return

    prefix = f"atd-{experiment_id}-"
    if not refactor_branch.startswith(prefix):
        return
    if refactor_branch == base_branch:
        return

    if _git(repo_dir, ["show-ref", "--verify", "--quiet", f"refs/heads/{refactor_branch}"]).returncode != 0:
        return

    cur = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    cur_branch = (cur.stdout or "").strip()
    if cur_branch == refactor_branch:
        if _git(repo_dir, ["show-ref", "--verify", "--quiet", f"refs/heads/{base_branch}"]).returncode == 0:
            _git(repo_dir, ["checkout", "-q", base_branch])
        else:
            _git(repo_dir, ["checkout", "-q", "--detach"])

    _git(repo_dir, ["branch", "-D", refactor_branch])
    _git(repo_dir, ["worktree", "prune"])


# ---------------- Status json ----------------

@dataclass(frozen=True)
class ExperimentUnitInfo:
    repo: str
    base_branch: str
    branch: str
    entry: Optional[str] = None
    cycle_id: Optional[str] = None
    mode_id: Optional[str] = None


def write_phase_status_json(
    *,
    out_dir: Path,
    phase: str,
    rid: str,
    unit: ExperimentUnitInfo,
    outcome: str,  # "started" | "ok" | "failed" | "skipped" | "blocked"
    reason: str = "",
    returncode: Optional[int] = None,
    cmd: Optional[List[str]] = None,
    artifacts: Optional[Dict[str, str]] = None,
    duration_sec: Optional[float] = None,
) -> None:
    payload: Dict[str, Any] = {
        "ts_utc": utc_timestamp_now(),
        "run_id": rid,
        "phase": phase,
        "outcome": outcome,
        "reason": reason,
        "returncode": returncode,
        "duration_sec": duration_sec,
        "unit": {
            "repo": unit.repo,
            "base_branch": unit.base_branch,
            "branch": unit.branch,
            "entry": unit.entry,
            "cycle_id": unit.cycle_id,
            "mode_id": unit.mode_id,
        },
        "cmd": cmd,
        "artifacts": artifacts or {},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / f"status_{phase}.json", payload)


@dataclass(frozen=True)
class ExperimentUnitRun:
    pipeline_config: Any
    repo_spec: Any
    cycle_spec: Any
    mode_spec: Any

    repo_checkout_dir: Path
    branch_results_dir: Path
    refactor_branch: str

    unit_info: ExperimentUnitInfo
    execution_id: str


Decision = Tuple[str, str, Dict[str, str]]

ValidateInputs = Callable[[ExperimentUnitRun], Decision]
BuildCommand = Callable[[ExperimentUnitRun], List[str]]
BuildEnvironment = Callable[[ExperimentUnitRun], Optional[Dict[str, str]]]
ValidateOutputs = Callable[[ExperimentUnitRun], Decision]


# Convention: tools that cannot reach the LLM should exit 42.
LLM_BLOCKED_EXIT_CODE = 42


def execute_phase_for_all_experiment_units(
    pipeline_config,
    experiment_units,
    *,
    phase: str,
    cwd: Path,
    validate_unit_inputs: ValidateInputs,
    build_unit_command: BuildCommand,
    build_unit_environment: BuildEnvironment,
    validate_unit_outputs: ValidateOutputs,
    stop_on_llm_blocked: bool = False,
) -> bool:
    """Returns True if execution was stopped early due to LLM unavailability."""
    for repo_spec, cycle_spec, mode_spec in experiment_units:
        repo_checkout_dir = (pipeline_config.projects_dir / repo_spec.repo).resolve()
        refactor_branch = make_refactor_branch_name(pipeline_config.experiment_id, mode_spec.id, cycle_spec.cycle_id)

        branch_results_dir = results_dir_for_branch(pipeline_config.results_root, repo_spec.repo, refactor_branch)
        branch_results_dir.mkdir(parents=True, exist_ok=True)

        unit_info = ExperimentUnitInfo(
            repo=repo_spec.repo,
            base_branch=repo_spec.base_branch,
            branch=refactor_branch,
            entry=repo_spec.entry,
            cycle_id=cycle_spec.cycle_id,
            mode_id=mode_spec.id,
        )
        rid = generate_execution_id()

        unit_run = ExperimentUnitRun(
            pipeline_config=pipeline_config,
            repo_spec=repo_spec,
            cycle_spec=cycle_spec,
            mode_spec=mode_spec,
            repo_checkout_dir=repo_checkout_dir,
            branch_results_dir=branch_results_dir,
            refactor_branch=refactor_branch,
            unit_info=unit_info,
            execution_id=rid,
        )

        # Resume support: skip if already completed successfully
        if _maybe_skip_completed_phase(
            branch_results_dir=branch_results_dir,
            phase=phase,
            validate_unit_inputs=validate_unit_inputs,
            validate_unit_outputs=validate_unit_outputs,
            unit_run=unit_run,
        ):
            continue

        outcome, reason, artifacts = validate_unit_inputs(unit_run)
        if outcome != "ok":
            write_phase_status_json(
                out_dir=branch_results_dir,
                phase=phase,
                rid=rid,
                unit=unit_info,
                outcome=outcome,
                reason=reason,
                returncode=0 if outcome in {"skipped", "blocked"} else 2,
                artifacts=artifacts,
            )

            if stop_on_llm_blocked and outcome == "blocked" and reason == "llm_unavailable":
                print(f"[fail-fast] LLM unavailable during phase={phase}; stopping remaining units.")
                return True

            continue

        try:
            cmd = build_unit_command(unit_run)
            env = build_unit_environment(unit_run)
        except Exception as exc:
            write_phase_status_json(
                out_dir=branch_results_dir,
                phase=phase,
                rid=rid,
                unit=unit_info,
                outcome="failed",
                reason=f"build error: {exc}",
                returncode=2,
            )
            continue

        write_phase_status_json(
            out_dir=branch_results_dir,
            phase=phase,
            rid=rid,
            unit=unit_info,
            outcome="started",
            cmd=cmd,
        )

        t0 = time.time()
        rc = run_subprocess_command(cmd, cwd=cwd, env=env)
        duration = float(time.time() - t0)

        if rc == LLM_BLOCKED_EXIT_CODE:
            write_phase_status_json(
                out_dir=branch_results_dir,
                phase=phase,
                rid=rid,
                unit=unit_info,
                outcome="blocked",
                reason="llm_unavailable",
                returncode=rc,
                cmd=cmd,
                duration_sec=duration,
            )

            if stop_on_llm_blocked:
                print(f"[fail-fast] LLM unavailable during phase={phase}; stopping remaining units.")
                return True

            continue

        if rc != 0:
            write_phase_status_json(
                out_dir=branch_results_dir,
                phase=phase,
                rid=rid,
                unit=unit_info,
                outcome="failed",
                reason=f"{phase} exited nonzero",
                returncode=rc,
                cmd=cmd,
                duration_sec=duration,
            )
            continue

        outcome, reason, artifacts = validate_unit_outputs(unit_run)
        write_phase_status_json(
            out_dir=branch_results_dir,
            phase=phase,
            rid=rid,
            unit=unit_info,
            outcome=outcome,
            reason=reason,
            returncode=0 if outcome != "failed" else 3,
            cmd=cmd,
            artifacts=artifacts,
            duration_sec=duration,
        )

        if stop_on_llm_blocked and outcome == "blocked" and reason == "llm_unavailable":
            print(f"[fail-fast] LLM unavailable during phase={phase}; stopping remaining units.")
            return True

    return False
