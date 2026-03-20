#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/build_cycles_to_analyze.sh -c pipeline.yaml \
#     --total 100 \
#     --out cycles_to_analyze.txt
#
# Optional:
#   --min-size 2 --max-size 8
#
# This will:
#  1) ALWAYS rebuild cycle_catalog.json for any baseline that has graph+scc_report
#  2) write cycles_to_analyze.txt using even-by-cycle-size selection + repo fairness

CFG="pipeline.yaml"
if [[ "${1:-}" == "-c" ]]; then
  CFG="${2:-}"; shift 2
fi

TOTAL=""
OUT_PATH="cycles_to_analyze.txt"
MIN_SIZE=""
MAX_SIZE=""
STRATEGY=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --total)    TOTAL="$2";    shift 2 ;;
    --out)      OUT_PATH="$2"; shift 2 ;;
    --min-size) MIN_SIZE="$2"; shift 2 ;;
    --max-size) MAX_SIZE="$2"; shift 2 ;;
    --strategy) STRATEGY="$2"; shift 2 ;;
    --*)        EXTRA_ARGS+=("$1" "$2"); shift 2 ;;
    *)          echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$TOTAL" ]]; then
  echo "ERROR: --total is required" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REPOS_FILE="$(python3 - <<PY
import yaml, pathlib
cfg=yaml.safe_load(pathlib.Path("$CFG").read_text(encoding="utf-8"))
print(cfg["repos_file"])
PY
)"
RESULTS_ROOT="$(python3 - <<PY
import yaml, pathlib
cfg=yaml.safe_load(pathlib.Path("$CFG").read_text(encoding="utf-8"))
print(cfg["results_root"])
PY
)"

ARGS=( --repos-file "$REPOS_FILE" --results-root "$RESULTS_ROOT" --total "$TOTAL" --output "$OUT_PATH" )
[[ -n "$MIN_SIZE" ]] && ARGS+=( --min-size "$MIN_SIZE" )
[[ -n "$MAX_SIZE" ]] && ARGS+=( --max-size "$MAX_SIZE" )
[[ -n "$STRATEGY" ]] && ARGS+=( --strategy "$STRATEGY" )

python3 "$ROOT/ATD_identification/build_cycles_to_analyze.py" "${ARGS[@]}"
echo "✅ Wrote: $OUT_PATH"
