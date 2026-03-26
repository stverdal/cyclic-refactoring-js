#!/usr/bin/env bash
set -euo pipefail

# New simple interface:
#   ./run_make_rq_tables.sh \
#     --results-roots resultsA resultsB resultsC \
#     --exp-ids       expA     expB     expC     \
#     --repos-file repos.txt \
#     --cycles-file cycles_to_analyze.txt \
#     --outdir analysis_out
#
# WITHOUT is derived as "<EXP>_without_explanation" for each item.

RESULTS_ROOTS=""
EXP_IDS=""
REPOS_FILE="repos.txt"
CYCLES_FILE="cycles_to_analyze.txt"
OUTDIR="analysis_out"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --results-roots) shift; while [[ $# -gt 0 && "${1:0:2}" != "--" ]]; do RESULTS_ROOTS+="${1} "; shift; done ;;
    --exp-ids) shift; while [[ $# -gt 0 && "${1:0:2}" != "--" ]]; do EXP_IDS+="${1} "; shift; done ;;
    --repos-file) REPOS_FILE="$2"; shift 2 ;;
    --cycles-file) CYCLES_FILE="$2"; shift 2 ;;
    --outdir) OUTDIR="$2"; shift 2 ;;
    -h|--help)
      cat <<EOF
Usage:
  $0 --results-roots <ROOT...> [--exp-ids <EXP...>] --repos-file repos.txt --cycles-file cycles_to_analyze.txt --outdir out
Notes:
  - If --exp-ids is omitted, all experiment IDs are auto-discovered from the results.
  - When given, roots and EXP IDs must be the same length and are paired by position.
  - WITHOUT is derived as "<EXP>_without_explanation".
EOF
      exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$RESULTS_ROOTS" ]]; then
  echo "ERROR: require --results-roots" >&2; exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# shellcheck disable=SC2206
ROOTS_ARR=( $RESULTS_ROOTS )

# Auto-discover exp IDs if not provided
if [[ -z "$EXP_IDS" ]]; then
  echo "[INFO] No --exp-ids given, auto-discovering from results..."
  # For each results root, find all unique exp IDs from atd-* branch dirs
  for root in "${ROOTS_ARR[@]}"; do
    discovered=""
    for repo_dir in "$root"/*/branches; do
      [[ -d "$repo_dir" ]] || continue
      for branch in "$repo_dir"/atd-*; do
        [[ -d "$branch" ]] || continue
        bname="$(basename "$branch")"
        # Extract exp_id: strip 'atd-' prefix, then strip '-scc-N-cycle-N' suffix
        exp_part="$(echo "$bname" | sed 's/^atd-//; s/[_-]scc[_-][0-9]*[_-]cycle[_-][0-9]*$//')"
        [[ -n "$exp_part" ]] && discovered+="${exp_part}"$'\n'
      done
    done
    # Deduplicate, exclude *-without-explanation / *-without_explanation (derived automatically)
    unique_exps="$(echo "$discovered" | sort -u | grep -v '[_-]without[_-]explanation$' || true)"
    if [[ -z "$unique_exps" ]]; then
      echo "ERROR: no experiment branches found in $root" >&2; exit 1
    fi
    # For a single root, we need one exp-id. If multiple found, use all of them
    # by repeating the root for each exp-id.
    while IFS= read -r eid; do
      [[ -z "$eid" ]] && continue
      EXP_IDS+="${eid} "
      # If we have more exp IDs than roots, duplicate the root
    done <<< "$unique_exps"
  done
  echo "[INFO] Discovered exp IDs: $EXP_IDS"
fi

EXP_ARR=( $EXP_IDS )

# If there are more exp IDs than roots (from auto-discovery), duplicate roots
if [[ ${#ROOTS_ARR[@]} -eq 1 && ${#EXP_ARR[@]} -gt 1 ]]; then
  single_root="${ROOTS_ARR[0]}"
  ROOTS_ARR=()
  for _ in "${EXP_ARR[@]}"; do
    ROOTS_ARR+=( "$single_root" )
  done
fi
RQ_FLAGS=( --results-roots "${ROOTS_ARR[@]}" --exp-ids "${EXP_ARR[@]}" --repos-file "$REPOS_FILE" --cycles-file "$CYCLES_FILE" --outdir "$OUTDIR" )

echo "==> RQ tables"
echo "    roots:      ${ROOTS_ARR[*]}"
echo "    exp-ids:    ${EXP_ARR[*]}"
echo "    repos:      $REPOS_FILE"
echo "    cycles:     $CYCLES_FILE"
echo "    outdir:     $OUTDIR"
echo

echo "[RQ1]"
"$PYTHON_BIN" "$SCRIPT_DIR/table_makers/make_rq1_tables.py" "${RQ_FLAGS[@]}"

echo "[RQ2]"
"$PYTHON_BIN" "$SCRIPT_DIR/table_makers/make_rq2_tables.py" "${RQ_FLAGS[@]}"

echo "[RQ3]"
"$PYTHON_BIN" "$SCRIPT_DIR/table_makers/make_rq3_tables.py" "${RQ_FLAGS[@]}"

echo
echo "✅ Done. CSVs written to: $OUTDIR"
