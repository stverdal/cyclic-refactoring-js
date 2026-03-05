#!/usr/bin/env bash
# quality_collect_jsts.sh
#
# Minimal JS/TS quality collector: runs npm test (with JUnit reporter) + ESLint.
#
# Usage:
#   ./quality_collect_jsts.sh <REPO_PATH> [LABEL] [SRC_HINT]
#
# Per-repo setup discovery (external folder, not inside repo):
#   REPO_SETUP_DIR="${REPO_SETUP_DIR:-<script_dir>/repo-test-setups-jsts}"
#   Setup file name: <repo-name>-test-setup.sh
#
# Writes to: OUT_DIR if set, else .quality/<repo>/<label>
set -euo pipefail

export TZ=UTC

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <REPO_PATH> [LABEL] [SRC_HINT]" >&2
  exit 2
fi

REPO_PATH="$(realpath "$1")"
REPO_NAME="$(basename "$REPO_PATH")"
LABEL="${2:-current}"
SRC_HINT="${3:-}"

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

# --- Git worktree (isolated checkout) ----------------------------------------
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

# Record node/npm versions
node --version > "$OUT_ABS/node_version.txt" 2>&1 || true
npm --version  > "$OUT_ABS/npm_version.txt" 2>&1 || true

# --- Source detection ---------------------------------------------------------
detect_src_dir() {
  local root="$1"; local hint="${2:-}"
  if [[ -n "$hint" && -d "$root/$hint" ]]; then
    echo "$hint"
    return
  fi
  for candidate in src lib app .; do
    if [[ -d "$root/$candidate" ]]; then
      echo "$candidate"
      return
    fi
  done
  echo "."
}

SRC_DIR="$(detect_src_dir "$WT_ROOT" "$SRC_HINT")"
echo "$SRC_DIR" > "$OUT_ABS/src_paths.txt"
echo "Source dir: $SRC_DIR"

# --- Per-repo setup discovery -------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SETUP_DIR="${REPO_SETUP_DIR:-$SCRIPT_DIR/repo-test-setups-jsts}"
REPO_SETUP_FILE="$REPO_SETUP_DIR/${REPO_NAME}-test-setup.sh"

if [[ -f "$SCRIPT_DIR/../timing.sh" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/../timing.sh"
  export TIMING_PHASE="quality_collect_jsts"
  export TIMING_REPO="$REPO_NAME"
  export TIMING_BRANCH="$LABEL"
fi

# --- Install dependencies ----------------------------------------------------
echo "== Step: install dependencies =="
if declare -F timing_mark >/dev/null 2>&1; then timing_mark "start_npmInstall"; fi

if [[ -f "$REPO_SETUP_FILE" ]]; then
  echo "Using per-repo test setup: $REPO_SETUP_FILE"
  # shellcheck disable=SC1090
  source "$REPO_SETUP_FILE"
fi

# Force public registry via env var (overrides ALL .npmrc files: project,
# user, global, and built-in) and --userconfig /dev/null to skip any
# .npmrc that embeds expired/revoked auth tokens (causes E401 in Docker).
export npm_config_registry=https://registry.npmjs.org

# Default install if no custom QUALITY_INSTALL was defined
if ! declare -f QUALITY_INSTALL >/dev/null 2>&1; then
  npm install \
    --userconfig /dev/null \
    --engine-strict false \
    --legacy-peer-deps \
    2>&1 | tail -20 || true
else
  echo "Using custom QUALITY_INSTALL for $REPO_NAME"
  QUALITY_INSTALL
fi

if declare -F timing_mark >/dev/null 2>&1; then timing_mark "end_npmInstall"; fi

# --- Run tests ----------------------------------------------------------------
: "${JSTS_TEST_TIMEOUT:=15m}"
TEST_LOG="$OUT_ABS/test_full.log"
RC_FILE="$OUT_ABS/test_exit_code.txt"

echo "== Step: run tests =="
if declare -F timing_mark >/dev/null 2>&1; then timing_mark "start_test"; fi

run_tests() {
  if declare -f QUALITY_TEST >/dev/null 2>&1; then
    echo "Using custom QUALITY_TEST for $REPO_NAME"
    QUALITY_TEST
    return $?
  fi

  # Try jest with JUnit reporter first
  if npx jest --version >/dev/null 2>&1; then
    echo "Detected Jest"
    # Install jest-junit reporter if not already present
    npm install --save-dev jest-junit --userconfig /dev/null 2>/dev/null || true
    timeout -k 30s "$JSTS_TEST_TIMEOUT" \
      npx jest --ci \
        --reporters=default --reporters=jest-junit \
        --forceExit \
        2>&1 | tee "$TEST_LOG" || true
    JEST_RC=${PIPESTATUS[0]}

    # jest-junit outputs to junit.xml by default
    if [[ -f "junit.xml" ]]; then
      cp junit.xml "$OUT_ABS/test_results.xml"
    fi
    return $JEST_RC

  # Try vitest
  elif npx vitest --version >/dev/null 2>&1; then
    echo "Detected Vitest"
    timeout -k 30s "$JSTS_TEST_TIMEOUT" \
      npx vitest run --reporter=junit --outputFile "$OUT_ABS/test_results.xml" \
        2>&1 | tee "$TEST_LOG" || true
    return ${PIPESTATUS[0]}

  # Try mocha
  elif npx mocha --version >/dev/null 2>&1; then
    echo "Detected Mocha"
    npm install --save-dev mocha-junit-reporter --userconfig /dev/null 2>/dev/null || true
    timeout -k 30s "$JSTS_TEST_TIMEOUT" \
      npx mocha --reporter mocha-junit-reporter \
        --reporter-options mochaFile="$OUT_ABS/test_results.xml" \
        2>&1 | tee "$TEST_LOG" || true
    return ${PIPESTATUS[0]}

  # Fallback: npm test
  else
    echo "Using npm test (generic)"
    timeout -k 30s "$JSTS_TEST_TIMEOUT" \
      npm test 2>&1 | tee "$TEST_LOG" || true
    return ${PIPESTATUS[0]}
  fi
}

set +e
run_tests
TEST_RC=$?
set -e

echo "$TEST_RC" > "$RC_FILE"
if declare -F timing_mark >/dev/null 2>&1; then timing_mark "end_test"; fi

if [[ $TEST_RC -ne 0 ]]; then
  echo "Tests failed with exit code $TEST_RC" >&2
fi

# --- ESLint -------------------------------------------------------------------
echo "== Step: ESLint =="
if declare -F timing_mark >/dev/null 2>&1; then timing_mark "start_eslint"; fi

if npx eslint --version >/dev/null 2>&1; then
  # Determine what to lint
  ESLINT_TARGET="$SRC_DIR"
  if [[ "$ESLINT_TARGET" == "." ]]; then
    ESLINT_TARGET="."
  fi

  npx eslint "$ESLINT_TARGET" \
    --format json \
    --no-error-on-unmatched-pattern \
    -o "$OUT_ABS/eslint.json" \
    2>"$OUT_ABS/eslint_stderr.txt" || true
else
  echo "ESLint not found, skipping"
fi

if declare -F timing_mark >/dev/null 2>&1; then timing_mark "end_eslint"; fi

# --- Record tool versions ----------------------------------------------------
{
  echo -n "node: "; node --version || true
  echo -n "npm: "; npm --version || true
  echo -n "eslint: "; npx eslint --version 2>/dev/null || echo "not installed"
  echo -n "jest: "; npx jest --version 2>/dev/null || echo "not installed"
  echo -n "vitest: "; npx vitest --version 2>/dev/null || echo "not installed"
} > "$OUT_ABS/tool_versions.txt" 2>&1 || true

npm ls --depth=0 > "$OUT_ABS/npm_ls.txt" 2>&1 || true

# --- Exit with test result code -----------------------------------------------
if [[ $TEST_RC -ne 0 ]]; then
  exit "$TEST_RC"
fi

echo "==> Collected metrics in $OUT_ABS"
