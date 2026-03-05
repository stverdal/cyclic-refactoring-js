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

# Neutralise ALL .npmrc files so expired/revoked auth tokens cannot cause E401.
# Strategy:
#   1. Set npm_config_registry env var        → overrides registry in any .npmrc
#   2. Set npm_config_userconfig env var       → points user config to empty file
#   3. Set npm_config_globalconfig env var     → points global config to empty file
#   4. Move aside the project-level .npmrc     → npm always reads it, no flag to skip
#
# Using env vars instead of --userconfig/--globalconfig flags avoids the npm bug
# "double loading config, previously loaded as user" that occurs when both flags
# resolve to the same path or to /dev/null.

export npm_config_registry=https://registry.npmjs.org

_EMPTY_USER_NPMRC="$(mktemp /tmp/.npmrc-user-XXXXXX)"
_EMPTY_GLOBAL_NPMRC="$(mktemp /tmp/.npmrc-global-XXXXXX)"
export npm_config_userconfig="$_EMPTY_USER_NPMRC"
export npm_config_globalconfig="$_EMPTY_GLOBAL_NPMRC"

_PROJ_NPMRC_MOVED=false
if [[ -f "$WT_ROOT/.npmrc" ]]; then
  echo "  Moving aside project .npmrc (may contain expired auth tokens)..."
  mv "$WT_ROOT/.npmrc" "$WT_ROOT/.npmrc.atd-bak"
  _PROJ_NPMRC_MOVED=true
fi

_restore_npmrc() {
  if [[ "$_PROJ_NPMRC_MOVED" == true && -f "$WT_ROOT/.npmrc.atd-bak" ]]; then
    mv "$WT_ROOT/.npmrc.atd-bak" "$WT_ROOT/.npmrc"
  fi
  rm -f "$_EMPTY_USER_NPMRC" "$_EMPTY_GLOBAL_NPMRC"
}
trap '_restore_npmrc' EXIT

# Default install if no custom QUALITY_INSTALL was defined
: "${NPM_INSTALL_TIMEOUT:=60}"
NPM_LOG="$OUT_ABS/npm_install.log"

if ! declare -f QUALITY_INSTALL >/dev/null 2>&1; then
  echo "  npm install (timeout ${NPM_INSTALL_TIMEOUT}s)..."
  timeout "${NPM_INSTALL_TIMEOUT}" bash -c '
    cd "$1" && npm install \
      --ignore-scripts \
      --engine-strict false \
      --legacy-peer-deps \
      --prefer-offline \
      2>&1
  ' _ "$WT_ROOT" > "$NPM_LOG" 2>&1 || echo "  ⚠ npm install exited with $? (timed out or failed — continuing)"

  # Show which packages triggered auth errors
  if grep -qi 'E401\|401 Unauthorized\|expired\|revoked' "$NPM_LOG" 2>/dev/null; then
    echo "  ⚠ Auth errors detected. Packages that triggered E401:"
    grep -i 'E401\|401 Unauthorized\|expired\|revoked' "$NPM_LOG" | head -10 | sed 's/^/    /'
    echo "    Full log: $NPM_LOG"
  fi
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
  if timeout 10 npx --no-install jest --version >/dev/null 2>&1; then
    echo "Detected Jest"
    # Install jest-junit reporter if not already present (timeout to avoid auth hangs)
    timeout 30 npm install --save-dev jest-junit --prefer-offline 2>/dev/null || true
    timeout -k 30s "$JSTS_TEST_TIMEOUT" \
      npx --no-install jest --ci \
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
  elif timeout 10 npx --no-install vitest --version >/dev/null 2>&1; then
    echo "Detected Vitest"
    timeout -k 30s "$JSTS_TEST_TIMEOUT" \
      npx --no-install vitest run --reporter=junit --outputFile "$OUT_ABS/test_results.xml" \
        2>&1 | tee "$TEST_LOG" || true
    return ${PIPESTATUS[0]}

  # Try mocha
  elif timeout 10 npx --no-install mocha --version >/dev/null 2>&1; then
    echo "Detected Mocha"
    # Install reporter if not already present (timeout to avoid auth hangs)
    timeout 30 npm install --save-dev mocha-junit-reporter --prefer-offline 2>/dev/null || true
    timeout -k 30s "$JSTS_TEST_TIMEOUT" \
      npx --no-install mocha --reporter mocha-junit-reporter \
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

if timeout 10 npx --no-install eslint --version >/dev/null 2>&1; then
  # Determine what to lint
  ESLINT_TARGET="$SRC_DIR"
  if [[ "$ESLINT_TARGET" == "." ]]; then
    ESLINT_TARGET="."
  fi

  npx --no-install eslint "$ESLINT_TARGET" \
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
  echo -n "eslint: "; timeout 5 npx --no-install eslint --version 2>/dev/null || echo "not installed"
  echo -n "jest: "; timeout 5 npx --no-install jest --version 2>/dev/null || echo "not installed"
  echo -n "vitest: "; timeout 5 npx --no-install vitest --version 2>/dev/null || echo "not installed"
} > "$OUT_ABS/tool_versions.txt" 2>&1 || true

npm ls --depth=0 > "$OUT_ABS/npm_ls.txt" 2>&1 || true

# --- Exit with test result code -----------------------------------------------
if [[ $TEST_RC -ne 0 ]]; then
  exit "$TEST_RC"
fi

echo "==> Collected metrics in $OUT_ABS"
