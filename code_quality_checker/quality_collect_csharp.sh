#!/usr/bin/env bash
# quality_collect_csharp.sh
#
# Ultra-simple + "honest" runner (with optional per-repo overrides):
# - Default: run `dotnet test` from repo root (no heuristics).
# - Optional per-repo setup file can specify:
#     - DOTNET_WORKDIR: directory (relative to repo root) to run from (e.g. "src")
#     - DOTNET_TEST_TARGET: .sln/.csproj path (relative to DOTNET_WORKDIR if set,
#                          else relative to repo root), passed to `dotnet test`.
#
# This keeps the default behavior strict, but lets you onboard repos where the
# solution/project isn't in the repo root (e.g. src/MyRepo.sln).
#
# Usage:
#   ./quality_collect_csharp.sh <REPO_PATH> [LABEL]
#
# Per-repo setup discovery (external folder, not inside repo):
#   REPO_SETUP_DIR="${REPO_SETUP_DIR:-<script_dir>/repo-test-setups-dotnet}"
#   Setup file name: <repo-name>-test-setup.sh
#
# Writes to: OUT_DIR if set, else .quality/<repo>/<label>
set -euo pipefail

export TZ=UTC
export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1
export DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <REPO_PATH> [LABEL]" >&2
  exit 2
fi

REPO_PATH="$(realpath "$1")"
REPO_NAME="$(basename "$REPO_PATH")"
LABEL="${2:-current}"

IS_GIT=0
if git -C "$REPO_PATH" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  IS_GIT=1
  LABEL="${2:-$(git -C "$REPO_PATH" branch --show-current 2>/dev/null || echo current)}"
fi

OUT_ROOT="${OUT_ROOT:-.quality}"
FINAL_OUT_DIR="${OUT_DIR:-$OUT_ROOT/$REPO_NAME/$LABEL}"
mkdir -p "$FINAL_OUT_DIR"
OUT_ABS="$(realpath "$FINAL_OUT_DIR")"
date -u +'%Y-%m-%dT%H:%M:%SZ' > "$OUT_ABS/run_started_utc.txt" || true

WT_DIR=""
WT_ROOT="$REPO_PATH"
if [[ $IS_GIT -eq 1 ]]; then
  if [[ "${QC_ALLOW_FETCH:-0}" == "1" ]]; then
    git -C "$REPO_PATH" fetch --all --quiet || true
  fi

  if ! git -C "$REPO_PATH" rev-parse --verify --quiet "${LABEL}^{commit}" >/dev/null; then
    echo "Ref '$LABEL' not found in $REPO_PATH" >&2
    exit 1
  fi

  shortsha="$(git -C "$REPO_PATH" rev-parse --short "${LABEL}^{commit}" 2>/dev/null || echo ???)"
  echo "Preparing worktree (detached HEAD $shortsha)"
  WT_DIR="$(mktemp -d -t qcwt.XXXXXX)"
  git -C "$REPO_PATH" worktree add --detach "$WT_DIR" "$LABEL" >/dev/null
  WT_ROOT="$WT_DIR"

  cleanup() {
    git -C "$REPO_PATH" worktree remove --force "$WT_DIR" 2>/dev/null || true
    rm -rf "$WT_DIR" 2>/dev/null || true
  }
  trap cleanup EXIT
fi

if [[ $IS_GIT -eq 1 ]]; then
  git -C "$WT_ROOT" rev-parse --short HEAD > "$OUT_ABS/git_sha.txt" || true
  git -C "$WT_ROOT" branch --show-current  > "$OUT_ABS/git_branch.txt" || true
fi

echo "Repo: $REPO_PATH"
echo "Worktree: $WT_ROOT  Label: $LABEL"
echo "Out: $OUT_ABS"

cd "$WT_ROOT"

dotnet --info > "$OUT_ABS/dotnet_info.txt" 2>&1 || true

# -----------------------------------------------------------------------------
# Per-repo setup discovery (external folder, not inside repo)
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SETUP_DIR="${REPO_SETUP_DIR:-$SCRIPT_DIR/repo-test-setups-dotnet}"
REPO_SETUP_FILE="$REPO_SETUP_DIR/${REPO_NAME}-test-setup.sh"

# Defaults (can be overridden by per-repo setup)
DOTNET_WORKDIR="${DOTNET_WORKDIR:-}"         # e.g. "src"
DOTNET_TEST_TARGET="${DOTNET_TEST_TARGET:-}" # e.g. "src/SharpYaml.sln" or "SharpYaml.sln" if WORKDIR=src

if [[ -f "$REPO_SETUP_FILE" ]]; then
  echo "Using per-repo test setup: $REPO_SETUP_FILE"
  # shellcheck disable=SC1090
  source "$REPO_SETUP_FILE"
else
  echo "No per-repo setup found at: $REPO_SETUP_FILE (using defaults)"
fi

# Record override info for debugging / reproducibility
{
  echo "DOTNET_WORKDIR=${DOTNET_WORKDIR:-}"
  echo "DOTNET_TEST_TARGET=${DOTNET_TEST_TARGET:-}"
  echo "REPO_SETUP_FILE=${REPO_SETUP_FILE:-}"
} > "$OUT_ABS/dotnet_targeting.txt" || true

LOG="$OUT_ABS/dotnet_test.log"
RC_FILE="$OUT_ABS/dotnet_test_exit_code.txt"
TRX_DIR="$OUT_ABS/test_results"
mkdir -p "$TRX_DIR"

: "${DOTNET_TEST_TIMEOUT:=20m}"

# -----------------------------------------------------------------------------
# Run dotnet test (strict default; optional explicit target/workdir override)
# -----------------------------------------------------------------------------
RUN_ROOT="$WT_ROOT"
if [[ -n "${DOTNET_WORKDIR:-}" ]]; then
  if [[ ! -d "$WT_ROOT/$DOTNET_WORKDIR" ]]; then
    echo "DOTNET_WORKDIR '$DOTNET_WORKDIR' does not exist under repo root." | tee -a "$LOG" >&2
    echo "dotnet test failed due to invalid DOTNET_WORKDIR" >&2
    echo "2" > "$RC_FILE"
    exit 2
  fi
  RUN_ROOT="$WT_ROOT/$DOTNET_WORKDIR"
fi

cd "$RUN_ROOT"

set +e
# Restore first with EnableWindowsTargeting so net*-windows projects work on Linux
echo "Running: dotnet restore (EnableWindowsTargeting=true)" | tee "$LOG"
if [[ -n "${DOTNET_TEST_TARGET:-}" ]]; then
  dotnet restore "${DOTNET_TEST_TARGET}" -p:EnableWindowsTargeting=true --nologo 2>&1 | tee -a "$LOG" || true
else
  dotnet restore -p:EnableWindowsTargeting=true --nologo 2>&1 | tee -a "$LOG" || true
fi

if [[ -n "${DOTNET_TEST_TARGET:-}" ]]; then
  echo "Running: dotnet test ${DOTNET_TEST_TARGET}  (workdir: $(pwd))" | tee -a "$LOG"
  timeout -k 30s "$DOTNET_TEST_TIMEOUT" \
    dotnet test "${DOTNET_TEST_TARGET}" \
      --nologo --no-restore \
      /p:CollectCoverage=false \
      /p:EnableWindowsTargeting=true \
      --logger "trx" \
      --results-directory "$TRX_DIR" \
      2>&1 | tee -a "$LOG"
  RC=${PIPESTATUS[0]}
  echo "explicit_target" > "$OUT_ABS/test_strategy.txt"
  echo "${DOTNET_TEST_TARGET}" > "$OUT_ABS/test_target.txt"
else
  echo "Running: dotnet test  (workdir: $(pwd))" | tee -a "$LOG"
  timeout -k 30s "$DOTNET_TEST_TIMEOUT" \
    dotnet test \
      --nologo --no-restore \
      /p:CollectCoverage=false \
      /p:EnableWindowsTargeting=true \
      --logger "trx" \
      --results-directory "$TRX_DIR" \
      2>&1 | tee -a "$LOG"
  RC=${PIPESTATUS[0]}
  echo "workdir_only" > "$OUT_ABS/test_strategy.txt"
  echo "" > "$OUT_ABS/test_target.txt"
fi
set -e

echo "$RC" > "$RC_FILE"

if [[ -n "${DOTNET_WORKDIR:-}" ]]; then
  echo "${DOTNET_WORKDIR}" > "$OUT_ABS/test_workdir.txt" || true
else
  echo "" > "$OUT_ABS/test_workdir.txt" || true
fi

if [[ $RC -ne 0 ]]; then
  echo "dotnet test failed with exit code $RC" >&2
  exit "$RC"
fi

echo "==> Collected test results in $OUT_ABS"
