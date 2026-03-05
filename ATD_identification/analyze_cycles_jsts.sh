#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./ATD_identification/analyze_cycles_jsts.sh <REPO_PATH> <ENTRY_SUBDIR> <OUTPUT_DIR> [--depcruise-config <path>]
#
# Produces:
#   <OUTPUT_DIR>/depcruise.json          (raw dependency-cruiser output)
#   <OUTPUT_DIR>/dependency_graph.json   (canonical schema)
#
# Monorepo detection:
#   The script auto-detects monorepos via:
#     1. "workspaces" field in root package.json (npm/Yarn)
#     2. pnpm-workspace.yaml
#     3. lerna.json
#   When detected, dependency-cruiser is run across all workspace packages
#   with --include-only scoping and --combinedDependencies.
#
# NOTE:
#   SCC extraction + cycle selection are separate steps run later.

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <REPO_PATH> <ENTRY_SUBDIR> <OUTPUT_DIR> [--depcruise-config <path>]" >&2
  exit 2
fi

REPO_PATH="$(cd "$1" && pwd)"
ENTRY_SUBDIR="${2%/}"
OUTPUT_DIR="$(mkdir -p "$3" && cd "$3" && pwd)"
shift 3

DEPCRUISE_CONFIG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --depcruise-config)
      DEPCRUISE_CONFIG="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

[[ -d "$REPO_PATH" ]] || { echo "ERROR: repo path not found: $REPO_PATH" >&2; exit 1; }

command -v node >/dev/null 2>&1 || { echo "ERROR: node not found in PATH" >&2; exit 3; }
command -v npx >/dev/null 2>&1 || { echo "ERROR: npx not found in PATH" >&2; exit 3; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found in PATH" >&2; exit 3; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_GRAPH_PY="$SCRIPT_DIR/build_dependency_graph_jsts.py"
[[ -f "$BUILD_GRAPH_PY" ]] || { echo "ERROR: missing: $BUILD_GRAPH_PY" >&2; exit 4; }

DEPCRUISE_JSON="$OUTPUT_DIR/depcruise.json"
GRAPH_JSON="$OUTPUT_DIR/dependency_graph.json"

if [[ -f "$SCRIPT_DIR/../timing.sh" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/../timing.sh"
  export TIMING_PHASE="analyze_cycles_jsts"
  export TIMING_REPO="$(basename "$REPO_PATH")"
fi

echo "== Analyze cycles (JS/TS: graph-only) =="
echo "Repo    : $REPO_PATH"
echo "Entry   : $ENTRY_SUBDIR"
echo "Out dir : $OUTPUT_DIR"
echo

# ---- Monorepo detection ----
MONOREPO_DETECTED=false
WORKSPACE_DIRS=""
INCLUDE_ONLY_PATTERN=""

detect_monorepo() {
  local root="$1"

  # 1. Check package.json "workspaces" field (npm/Yarn)
  if [[ -f "$root/package.json" ]]; then
    local ws
    ws=$(python3 -c "
import json, glob, os, sys
pkg = json.load(open('$root/package.json'))
ws = pkg.get('workspaces', [])
# Yarn can nest workspaces under 'packages' key
if isinstance(ws, dict):
    ws = ws.get('packages', [])
if not isinstance(ws, list) or not ws:
    sys.exit(1)
# Resolve globs to actual directories
dirs = []
for pattern in ws:
    matches = sorted(glob.glob(os.path.join('$root', pattern)))
    for m in matches:
        if os.path.isdir(m) and os.path.isfile(os.path.join(m, 'package.json')):
            dirs.append(os.path.relpath(m, '$root'))
if dirs:
    print(' '.join(dirs))
else:
    sys.exit(1)
" 2>/dev/null) && {
      MONOREPO_DETECTED=true
      WORKSPACE_DIRS="$ws"
      echo "Monorepo detected via package.json workspaces: $WORKSPACE_DIRS"
      return 0
    }
  fi

  # 2. Check pnpm-workspace.yaml
  if [[ -f "$root/pnpm-workspace.yaml" ]]; then
    local ws
    ws=$(python3 -c "
import yaml, glob, os, sys
with open('$root/pnpm-workspace.yaml') as f:
    data = yaml.safe_load(f) or {}
patterns = data.get('packages', [])
if not patterns:
    sys.exit(1)
dirs = []
for pattern in patterns:
    matches = sorted(glob.glob(os.path.join('$root', pattern)))
    for m in matches:
        if os.path.isdir(m) and os.path.isfile(os.path.join(m, 'package.json')):
            dirs.append(os.path.relpath(m, '$root'))
if dirs:
    print(' '.join(dirs))
else:
    sys.exit(1)
" 2>/dev/null) && {
      MONOREPO_DETECTED=true
      WORKSPACE_DIRS="$ws"
      echo "Monorepo detected via pnpm-workspace.yaml: $WORKSPACE_DIRS"
      return 0
    }
  fi

  # 3. Check lerna.json
  if [[ -f "$root/lerna.json" ]]; then
    local ws
    ws=$(python3 -c "
import json, glob, os, sys
lerna = json.load(open('$root/lerna.json'))
patterns = lerna.get('packages', ['packages/*'])
dirs = []
for pattern in patterns:
    matches = sorted(glob.glob(os.path.join('$root', pattern)))
    for m in matches:
        if os.path.isdir(m) and os.path.isfile(os.path.join(m, 'package.json')):
            dirs.append(os.path.relpath(m, '$root'))
if dirs:
    print(' '.join(dirs))
else:
    sys.exit(1)
" 2>/dev/null) && {
      MONOREPO_DETECTED=true
      WORKSPACE_DIRS="$ws"
      echo "Monorepo detected via lerna.json: $WORKSPACE_DIRS"
      return 0
    }
  fi

  return 1
}

# Only run monorepo detection if entry is "." or looks like a workspace root
if [[ "$ENTRY_SUBDIR" == "." || "$ENTRY_SUBDIR" == "" ]]; then
  detect_monorepo "$REPO_PATH" || true
elif [[ -f "$REPO_PATH/package.json" ]]; then
  # Try detection anyway — the entry might be inside a monorepo
  detect_monorepo "$REPO_PATH" || true
fi

# ---- Step 1: Install dependencies if needed ----
echo "== Step 1: ensure dependencies are installed =="
_npm_ok=false
if [[ -f "$REPO_PATH/package-lock.json" || -f "$REPO_PATH/yarn.lock" || -f "$REPO_PATH/pnpm-lock.yaml" ]]; then
  # Force public registry via env var (overrides ALL .npmrc files: project,
  # user, global, and built-in). Use --userconfig and --globalconfig /dev/null
  # to fully bypass any .npmrc that embeds expired/revoked auth tokens.
  echo "  Attempt 1: npm install (bypass all .npmrc)..."
  ( cd "$REPO_PATH" && \
    npm_config_registry=https://registry.npmjs.org \
    npm install \
      --ignore-scripts \
      --userconfig /dev/null \
      --globalconfig /dev/null \
      --engine-strict false \
      --legacy-peer-deps \
      2>&1 ) && _npm_ok=true

  # Attempt 2: if node_modules still missing, try without the lockfile
  if [[ ! -d "$REPO_PATH/node_modules" ]]; then
    echo "  Attempt 2: npm install without lockfile..."
    ( cd "$REPO_PATH" && \
      npm_config_registry=https://registry.npmjs.org \
      npm install \
        --ignore-scripts \
        --userconfig /dev/null \
        --globalconfig /dev/null \
        --no-package-lock \
        --engine-strict false \
        --legacy-peer-deps \
        2>&1 ) && _npm_ok=true
  else
    _npm_ok=true
  fi

  if [[ -d "$REPO_PATH/node_modules" ]]; then
    _npm_ok=true
    echo "  ✔ node_modules present"
  else
    echo "  ⚠ WARNING: npm install failed — node_modules not created."
    echo '    Path alias resolution ($lib/*, $apis/*, etc.) will not work.'
    echo '    Cycles depending on aliased imports will be MISSED.'
    echo "    Fix: manually run 'npm install' in $REPO_PATH, then re-run."
  fi
fi

# ---- Step 1b: SvelteKit sync (generate .svelte-kit/tsconfig.json) ----
TSCONFIG_PATH=""
if [[ -f "$REPO_PATH/svelte.config.js" || -f "$REPO_PATH/svelte.config.ts" ]]; then
  if [[ ! -d "$REPO_PATH/.svelte-kit" ]]; then
    if [[ "$_npm_ok" == true ]]; then
      echo "SvelteKit project detected — running 'npx svelte-kit sync'..."
      ( cd "$REPO_PATH" && npx svelte-kit sync 2>&1 || true )
      if [[ ! -d "$REPO_PATH/.svelte-kit" ]]; then
        echo '  ⚠ WARNING: svelte-kit sync failed — .svelte-kit/ not generated.'
        echo '    tsconfig path aliases ($lib, $apis) will not be resolved.'
      fi
    else
      echo '  ⚠ WARNING: skipping svelte-kit sync (npm install failed).'
      echo '    tsconfig path aliases ($lib, $apis) will not be resolved.'
    fi
  fi
fi

# Discover tsconfig.json (prefer repo root, fall back to .svelte-kit)
if [[ -f "$REPO_PATH/tsconfig.json" ]]; then
  TSCONFIG_PATH="$REPO_PATH/tsconfig.json"
elif [[ -f "$REPO_PATH/.svelte-kit/tsconfig.json" ]]; then
  TSCONFIG_PATH="$REPO_PATH/.svelte-kit/tsconfig.json"
fi

# ---- Step 2: Run dependency-cruiser ----
echo "== Step 2: dependency-cruiser → $DEPCRUISE_JSON =="
if declare -F timing_mark >/dev/null 2>&1; then timing_mark "start_depcruise"; fi

DEPCRUISE_ARGS=("--output-type" "json")

# Add config if provided; otherwise use --no-config
if [[ -n "$DEPCRUISE_CONFIG" && -f "$DEPCRUISE_CONFIG" ]]; then
  DEPCRUISE_ARGS+=("--config" "$DEPCRUISE_CONFIG")
else
  DEPCRUISE_ARGS+=("--no-config")
fi

# Add tsconfig for path resolution (helps with some imports;
# full alias resolution is handled by build_dependency_graph_jsts.py)
if [[ -n "$TSCONFIG_PATH" ]]; then
  DEPCRUISE_ARGS+=("--ts-config" "$TSCONFIG_PATH")
fi

# Build depcruise excludes
DEPCRUISE_ARGS+=("--exclude" "node_modules|dist|build|\\.next|\\.nuxt|coverage|\\.git")

if [[ "$MONOREPO_DETECTED" == "true" && -n "$WORKSPACE_DIRS" ]]; then
  # Monorepo: scan all workspace directories
  # Build --include-only pattern from workspace dirs
  # e.g., "packages/foo" "packages/bar" → "^(packages/foo|packages/bar)"
  local_pattern=""
  for dir in $WORKSPACE_DIRS; do
    if [[ -z "$local_pattern" ]]; then
      local_pattern="^($dir"
    else
      local_pattern="$local_pattern|$dir"
    fi
  done
  local_pattern="$local_pattern)"
  INCLUDE_ONLY_PATTERN="$local_pattern"
  DEPCRUISE_ARGS+=("--include-only" "$INCLUDE_ONLY_PATTERN")

  echo "Scanning workspace dirs: $WORKSPACE_DIRS"
  echo "Include-only pattern  : $INCLUDE_ONLY_PATTERN"
  # shellcheck disable=SC2086
  ( cd "$REPO_PATH" && npx depcruise "${DEPCRUISE_ARGS[@]}" $WORKSPACE_DIRS > "$DEPCRUISE_JSON" )
else
  # Single-package repo: scan the entry subdir only
  local_entry="$ENTRY_SUBDIR"
  if [[ "$local_entry" == "." || -z "$local_entry" ]]; then
    local_entry="src"
    # Fallback: try common source directories
    for candidate in src lib app .; do
      if [[ -d "$REPO_PATH/$candidate" ]]; then
        local_entry="$candidate"
        break
      fi
    done
  fi
  [[ -d "$REPO_PATH/$local_entry" ]] || { echo "ERROR: entry subdir not found: $REPO_PATH/$local_entry" >&2; exit 1; }

  echo "Scanning entry dir: $local_entry"
  ( cd "$REPO_PATH" && npx depcruise "${DEPCRUISE_ARGS[@]}" "$local_entry" > "$DEPCRUISE_JSON" )
fi

if declare -F timing_mark >/dev/null 2>&1; then timing_mark "end_depcruise"; fi
[[ -s "$DEPCRUISE_JSON" ]] || { echo "ERROR: dependency-cruiser did not produce $DEPCRUISE_JSON" >&2; exit 10; }

# ---- Step 3: Convert to canonical dependency graph ----
echo
echo "== Step 3: build canonical dependency graph → $GRAPH_JSON =="
if declare -F timing_mark >/dev/null 2>&1; then timing_mark "start_buildDependencyGraph"; fi

BUILD_GRAPH_ARGS=("$BUILD_GRAPH_PY" "$DEPCRUISE_JSON"
  --repo-root "$REPO_PATH"
  --entry "$ENTRY_SUBDIR"
  --out "$GRAPH_JSON")

# Pass tsconfig for alias resolution ($lib/*, $apis/*, etc.)
if [[ -n "$TSCONFIG_PATH" ]]; then
  BUILD_GRAPH_ARGS+=(--tsconfig "$TSCONFIG_PATH")
fi

python3 "${BUILD_GRAPH_ARGS[@]}"

if declare -F timing_mark >/dev/null 2>&1; then timing_mark "end_buildDependencyGraph"; fi
[[ -s "$GRAPH_JSON" ]] || { echo "ERROR: graph builder did not produce $GRAPH_JSON" >&2; exit 11; }

echo
echo "✅ Done. Outputs:"
echo "  - $DEPCRUISE_JSON"
echo "  - $GRAPH_JSON"
