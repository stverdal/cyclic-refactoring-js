#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   run_OpenHands.sh <repo_dir> <base_branch> <new_branch> <prompt_path> <out_dir>

usage() { echo "usage: $0 <repo_dir> <base_branch> <new_branch> <prompt_path> <out_dir>"; exit 1; }
[ $# -eq 5 ] || usage

REPO_DIR="$(cd "$1" && pwd)"
BASE_BRANCH="$2"
NEW_BRANCH="$3"
PROMPT_PATH="$4"
OUT_DIR="$(mkdir -p "$5" && cd "$5" && pwd)"

[ -d "$REPO_DIR/.git" ] || { echo "Not a git repo: $REPO_DIR" >&2; exit 2; }
[ -f "$PROMPT_PATH" ] || { echo "Prompt not found: $PROMPT_PATH" >&2; exit 3; }

# Required env (pipeline provides)
LLM_MODEL="${LLM_MODEL:-}"
LLM_BASE_URL="${LLM_BASE_URL:-}"
LLM_API_KEY="${LLM_API_KEY:-}"

OPENHANDS_IMAGE="${OPENHANDS_IMAGE:-docker.all-hands.dev/all-hands-ai/openhands:0.68}"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-docker.all-hands.dev/all-hands-ai/runtime:0.68-nikolaik}"
MAX_ITERS="${MAX_ITERS:-100}"
COMMIT_MESSAGE="${COMMIT_MESSAGE:-Refactor: break dependency cycle}"

# Optional walltime (seconds). If set, OpenHands is killed and we exit 42.
ATD_WALLTIME_SEC="${ATD_WALLTIME_SEC:-0}"
ATD_GRACE_SEC="${ATD_GRACE_SEC:-60}"

# Delete store by default (you can keep it for debugging).
ATD_KEEP_OPENHANDS_STORE="${ATD_KEEP_OPENHANDS_STORE:-0}"

# Stable outputs
RUN_LOG="$OUT_DIR/run.log"
TRAJ_PATH="$OUT_DIR/trajectory.json"
STATUS_PATH="$OUT_DIR/status.json"
DIFF_PATH="$OUT_DIR/git_diff.patch"

# Local commit identity
GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-atd-bot}"
GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-atd-bot@local}"

[ -n "$LLM_API_KEY" ] || { echo "LLM_API_KEY is required"; exit 5; }
[ -n "$LLM_BASE_URL" ] || { echo "LLM_BASE_URL is required"; exit 6; }
[ -n "$LLM_MODEL" ] || { echo "LLM_MODEL is required"; exit 7; }
[ -n "$OPENHANDS_IMAGE" ] || { echo "OPENHANDS_IMAGE is empty"; exit 8; }

# Host UID/GID (the user running this script inside the devcontainer)
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

ts() { date -Iseconds; }
abs() { python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1"; }

HOST_PWD="${HOST_PWD:-}"
if [ -z "$HOST_PWD" ]; then
  echo "ERROR: HOST_PWD is not set (required for docker volume mapping)."
  exit 9
fi
HOST_PWD="${HOST_PWD%/}"

LLM_BASE_URL="${LLM_BASE_URL%/}"
LLM_BASE_URL_OH="$LLM_BASE_URL"
if [[ "$LLM_BASE_URL_OH" != */v1 ]]; then
  LLM_BASE_URL_OH="${LLM_BASE_URL_OH}/v1"
fi

MODEL_FOR_OH="$LLM_MODEL"
if [[ "$MODEL_FOR_OH" == /* ]]; then
  MODEL_FOR_OH="openai/${MODEL_FOR_OH}"
elif [[ "$MODEL_FOR_OH" != openai/* ]]; then
  MODEL_FOR_OH="openai/${MODEL_FOR_OH}"
fi

PROMPT_ABS="$(abs "$PROMPT_PATH")"
PROMPT_DIR="$(dirname "$PROMPT_ABS")"
PROMPT_BASENAME="$(basename "$PROMPT_ABS")"
PROMPT_IN_CONTAINER="/prompts/$PROMPT_BASENAME"

write_status_json () {
  local outcome="$1"; shift || true
  local reason="${1:-}"; shift || true
  {
    echo "{"
    echo "  \"timestamp\": \"$(ts)\","
    echo "  \"phase\": \"openhands\","
    echo "  \"outcome\": \"${outcome}\","
    echo "  \"reason\": \"${reason}\","
    echo "  \"run_log\": \"${RUN_LOG}\","
    echo "  \"trajectory\": \"${TRAJ_PATH}\","
    echo "  \"diff\": \"${DIFF_PATH}\""
    echo "}"
  } > "$STATUS_PATH"
}

to_host_path () {
  local p="$1"
  case "$p" in
    /workspace/*) printf "%s/%s" "$HOST_PWD" "${p#/workspace/}" ;;
    /workspace)   printf "%s" "$HOST_PWD" ;;
    *) echo "ERROR: path is not under /workspace: $p" >&2; exit 11 ;;
  esac
}

normalize_ownership_hostpath () {
  local host_path="$1"
  [[ -n "${host_path:-}" ]] || return 0
  docker run --rm \
    -v "$host_path:/target:rw" \
    alpine:3.20 \
    sh -lc "chown -R ${HOST_UID}:${HOST_GID} /target >/dev/null 2>&1 || true"
}

ensure_worktree_dir_writable () {
  local repo_host
  repo_host="$(to_host_path "$REPO_DIR")"
  docker run --rm \
    -v "$repo_host:/repo:rw" \
    alpine:3.20 \
    sh -lc "mkdir -p /repo/.atd_worktrees && chown -R ${HOST_UID}:${HOST_GID} /repo/.atd_worktrees >/dev/null 2>&1 || true"
}

WT_ROOT="$REPO_DIR/.atd_worktrees"
WT_PATH="$WT_ROOT/$NEW_BRANCH"
mkdir -p "$WT_ROOT" || true

cleanup_worktree () {
  git -C "$REPO_DIR" worktree remove --force "$WT_PATH" >/dev/null 2>&1 || true
  rm -rf "$WT_PATH" >/dev/null 2>&1 || true
  git -C "$REPO_DIR" worktree prune >/dev/null 2>&1 || true
}

cleanup_openhands_store () {
  if [[ "$ATD_KEEP_OPENHANDS_STORE" == "1" ]]; then
    return
  fi
  rm -rf "$OUT_DIR/openhands_store" >/dev/null 2>&1 || true
}

WT_HOST=""
OUT_DIR_HOST=""
PROMPT_DIR_HOST=""
POST_RUN_NORMALIZED="0"

final_cleanup () {
  local rc="${1:-0}"

  if [[ "$POST_RUN_NORMALIZED" != "1" ]]; then
    [[ -n "${WT_HOST:-}" ]] && normalize_ownership_hostpath "$WT_HOST"
    [[ -n "${OUT_DIR_HOST:-}" ]] && normalize_ownership_hostpath "$OUT_DIR_HOST"
  fi

  cleanup_openhands_store
  cleanup_worktree

  exit "$rc"
}
trap 'final_cleanup $?' EXIT

git -C "$REPO_DIR" show-ref --verify --quiet "refs/heads/$BASE_BRANCH" || {
  write_status_json "config_error" "base_branch_missing_locally"
  exit 10
}

OUT_DIR_HOST="$(to_host_path "$OUT_DIR")"
PROMPT_DIR_HOST="$(to_host_path "$PROMPT_DIR")"

normalize_ownership_hostpath "$OUT_DIR_HOST"
ensure_worktree_dir_writable

if [[ -e "$WT_PATH" ]]; then
  cleanup_worktree
fi

cur_branch="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
if [[ "$cur_branch" == "$NEW_BRANCH" ]]; then
  if git -C "$REPO_DIR" show-ref --verify --quiet "refs/heads/$BASE_BRANCH"; then
    git -C "$REPO_DIR" checkout -q "$BASE_BRANCH" >/dev/null 2>&1 || true
  else
    git -C "$REPO_DIR" checkout -q --detach >/dev/null 2>&1 || true
  fi
fi

echo "Creating worktree: $WT_PATH"

create_worktree_once () {
  if git -C "$REPO_DIR" show-ref --verify --quiet "refs/heads/$NEW_BRANCH"; then
    git -C "$REPO_DIR" worktree add "$WT_PATH" "$NEW_BRANCH" >/dev/null
  else
    git -C "$REPO_DIR" worktree add -b "$NEW_BRANCH" "$WT_PATH" "$BASE_BRANCH" >/dev/null
  fi
}

set +e
create_worktree_once
WT_RC=$?
set -e

if [[ "$WT_RC" -ne 0 ]]; then
  ensure_worktree_dir_writable

  set +e
  create_worktree_once
  WT_RC2=$?
  set -e

  if [[ "$WT_RC2" -ne 0 ]]; then
    : > "$DIFF_PATH" || true
    touch "$RUN_LOG" >/dev/null 2>&1 || true
    write_status_json "failed" "worktree_create_failed"
    exit 20
  fi
fi

WT_HOST="$(to_host_path "$WT_PATH")"
normalize_ownership_hostpath "$WT_HOST"

pushd "$WT_PATH" >/dev/null
git reset --hard -q HEAD
git clean -fdx >/dev/null 2>&1 || true
popd >/dev/null

TTY_FLAGS=()
if [ -t 1 ] && [ -t 0 ]; then
  TTY_FLAGS+=("-it")
elif [ -t 0 ]; then
  TTY_FLAGS+=("-i")
fi

NETWORK_FLAGS=()
if [ -n "${ATD_OPENHANDS_NETWORK_CONTAINER:-}" ]; then
  NETWORK_FLAGS+=( "--network" "container:${ATD_OPENHANDS_NETWORK_CONTAINER}" )
fi

mkdir -p "$OUT_DIR/openhands_store"
touch "$RUN_LOG" "$DIFF_PATH" >/dev/null 2>&1 || true

run_docker() {
  local -a cmd
  cmd=(docker run --rm)
  cmd+=("${TTY_FLAGS[@]}")
  cmd+=("${NETWORK_FLAGS[@]}")

  cmd+=(
    --add-host=host.docker.internal:host-gateway
    --dns 8.8.8.8
    -v /var/run/docker.sock:/var/run/docker.sock
    -v "$WT_HOST:/workspace:rw"
    -v "$OUT_DIR_HOST:/logs:rw"
    -v "$PROMPT_DIR_HOST:/prompts:ro"

    -e FILE_STORE=local
    -e FILE_STORE_PATH=/logs/openhands_store

    -e DOCKER_HOST_ADDR=172.17.0.1
    -e SANDBOX_RUNTIME_CONTAINER_IMAGE="$RUNTIME_IMAGE"
    -e SANDBOX_USER_ID="$HOST_UID"
    -e SANDBOX_VOLUMES="$WT_HOST:/workspace:rw,$OUT_DIR_HOST:/logs:rw"
    -e LOG_ALL_EVENTS=true
    -e SAVE_TRAJECTORY_PATH="/logs/trajectory.json"

    -e LLM_API_KEY="$LLM_API_KEY"
    -e LLM_BASE_URL="$LLM_BASE_URL_OH"
    -e LLM_MODEL="$MODEL_FOR_OH"

    "$OPENHANDS_IMAGE"
    python -m openhands.core.main
      -d "/workspace"
      -f "$PROMPT_IN_CONTAINER"
      -i "$MAX_ITERS"
  )

  "${cmd[@]}"
}

echo "Starting OpenHands..."
set -o pipefail

if [[ "$ATD_WALLTIME_SEC" -gt 0 ]]; then
  timeout --signal=TERM --kill-after="${ATD_GRACE_SEC}s" "${ATD_WALLTIME_SEC}s" \
    run_docker 2>&1 | tee "$RUN_LOG"
  RUN_EXIT=$?
else
  run_docker 2>&1 | tee "$RUN_LOG"
  RUN_EXIT=$?
fi

normalize_ownership_hostpath "$WT_HOST"
normalize_ownership_hostpath "$OUT_DIR_HOST"
POST_RUN_NORMALIZED="1"

if [[ "$RUN_EXIT" -eq 124 ]]; then
  : > "$DIFF_PATH" || true
  write_status_json "blocked" "walltime_timeout"
  exit 42
fi

# Minimal, non-guessy LLM-unavailable detection:
# You said this signature is specific to "LLM not responding" in your setup.
if grep -Fqi "OpenAIException - Connection error" "$RUN_LOG"; then
  : > "$DIFF_PATH" || true
  write_status_json "blocked" "llm_unavailable"
  exit 42
fi

if [[ "$RUN_EXIT" -ne 0 ]]; then
  : > "$DIFF_PATH" || true
  write_status_json "llm_error" "openhands_exited_nonzero"
  exit 20
fi

pushd "$WT_PATH" >/dev/null
git config user.name "$GIT_AUTHOR_NAME"
git config user.email "$GIT_AUTHOR_EMAIL"

if [ -z "$(git status --porcelain)" ]; then
  : > "$DIFF_PATH"
  write_status_json "no_changes" "no_diff_after_llm"
  popd >/dev/null
  exit 0
fi

git add -A
git commit -m "$COMMIT_MESSAGE" >/dev/null 2>&1 || true
git diff --binary "$BASE_BRANCH...$NEW_BRANCH" > "$DIFF_PATH" || true

write_status_json "committed" ""
popd >/dev/null

echo "✅ OpenHands done."
