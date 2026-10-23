#!/bin/bash
###############################################################################
# cleanup_run_artifacts.sh
#
# Removes large generated pipeline artifacts without touching evaluation
# databases, logs, PDFs, or knowledge-graph outputs.
#
# Default mode is dry-run:
#   bash pipeline/cleanup_run_artifacts.sh
#
# Actually delete:
#   DRY_RUN=false bash pipeline/cleanup_run_artifacts.sh
#
# Also remove comparison JSON files:
#   DRY_RUN=false CLEAN_COMPARISONS=true bash pipeline/cleanup_run_artifacts.sh
###############################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="$PROJECT_ROOT/data/output"
REPOS_DIR="$OUTPUT_DIR/cloned_repos"
COMP_DIR="$OUTPUT_DIR/comparisons"

DRY_RUN="${DRY_RUN:-true}"
CLEAN_COMPARISONS="${CLEAN_COMPARISONS:-false}"

safe_clean_directory_contents() {
    local target="$1"
    local label="$2"
    local resolved_target
    local resolved_output

    [ -d "$target" ] || return 0

    resolved_target="$(realpath "$target")"
    resolved_output="$(realpath "$OUTPUT_DIR")"

    case "$resolved_target" in
        "$resolved_output"/*) ;;
        *)
            echo "[ERROR] Refusing to clean outside output directory: $resolved_target" >&2
            return 1
            ;;
    esac

    echo "[CLEANUP] $label: $resolved_target"
    if [ "$DRY_RUN" = "true" ]; then
        find "$resolved_target" -mindepth 1 -maxdepth 1 -print
    else
        find "$resolved_target" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    fi
}

echo "[CLEANUP] dry run: $DRY_RUN"
safe_clean_directory_contents "$REPOS_DIR" "cloned/downloaded repositories"

if [ "$CLEAN_COMPARISONS" = "true" ]; then
    safe_clean_directory_contents "$COMP_DIR" "comparison JSON files"
else
    echo "[CLEANUP] keeping comparison files; set CLEAN_COMPARISONS=true to remove them"
fi

echo "[CLEANUP] kept: data/output/db, data/output/logs, data/output/pdf, data/output/kg, data/output/full_run_progress.txt"
