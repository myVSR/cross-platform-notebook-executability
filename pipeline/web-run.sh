#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WEB_JOB_DIR="${WEB_JOB_DIR:-/job}"

: "${WEB_REPO_URL:?WEB_REPO_URL is required}"
: "${WEB_NOTEBOOK_PATHS:?WEB_NOTEBOOK_PATHS is required}"

case "$WEB_REPO_URL" in
    https://github.com/*/*|https://codeberg.org/*/*|https://zenodo.org/record/*|https://zenodo.org/records/*) ;;
    *)
        echo "[WEB] Unsupported or unsafe repository URL" >&2
        exit 2
        ;;
esac

validate_notebook_paths() {
    local raw_paths="$1"
    local path
    local count=0

    IFS=';' read -ra paths <<<"$raw_paths"
    for path in "${paths[@]}"; do
        path="${path#"${path%%[![:space:]]*}"}"
        path="${path%"${path##*[![:space:]]}"}"
        [ -z "$path" ] && continue
        count=$((count + 1))
        if [ "$count" -gt 2000 ] ||
           [[ "$path" = /* ]] ||
           [[ "$path" = *\\* ]] ||
           [[ "$path" = *$'\n'* ]] ||
           [[ "/$path/" = *"/../"* ]] ||
           [[ "$path" != *.ipynb ]]; then
            echo "[WEB] Unsafe notebook path rejected" >&2
            exit 2
        fi
    done

    if [ "$count" -eq 0 ]; then
        echo "[WEB] At least one notebook path is required" >&2
        exit 2
    fi
}

validate_notebook_paths "$WEB_NOTEBOOK_PATHS"

LOG_FILE=""
source "$PROJECT_ROOT/config/config.sh"
source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/checks.sh"
source "$PROJECT_ROOT/src/db.sh"
source "$PROJECT_ROOT/src/pyenv.sh"
source "$PROJECT_ROOT/src/requirements.sh"
source "$PROJECT_ROOT/src/notebooks.sh"
source "$PROJECT_ROOT/src/classification.sh"
source "$PROJECT_ROOT/src/repo.sh"

OUTPUT_DIR="$WEB_JOB_DIR/output"
REPOS_DIR="$OUTPUT_DIR/cloned_repos"
COMP_DIR="$OUTPUT_DIR/comparisons"
LOG_DIR="$OUTPUT_DIR/logs"
OUTPUT_DB_DIR="$PROJECT_ROOT/data/output/db"
OUTPUT_DB_FILE="$OUTPUT_DB_DIR/db.sqlite"
DB_FILE="$OUTPUT_DB_FILE"
VENV_BASE_DIR="$WEB_JOB_DIR/venvs"
CLASSIFICATION_RULE_ONLY="${CLASSIFICATION_RULE_ONLY:-true}"
RUN_ID=""

export PROJECT_ROOT OUTPUT_DIR REPOS_DIR COMP_DIR LOG_DIR OUTPUT_DB_DIR \
       OUTPUT_DB_FILE DB_FILE VENV_BASE_DIR CLASSIFICATION_RULE_ONLY
export PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$WEB_JOB_DIR/work" "$OUTPUT_DB_DIR" "$VENV_BASE_DIR"
cd "$WEB_JOB_DIR/work"

initialize_directories
ensure_pipeline_tables

REPO_URL="$WEB_REPO_URL"
NOTEBOOK_PATHS="$WEB_NOTEBOOK_PATHS"
SETUP_PATHS="${WEB_SETUP_PATHS:-}"
REQUIREMENT_PATHS="${WEB_REQUIREMENT_PATHS:-}"
export REPO_URL NOTEBOOK_PATHS SETUP_PATHS REQUIREMENT_PATHS

REPO_ID="$(get_or_create_repo_id "$REPO_URL")"
if ! [[ "$REPO_ID" =~ ^[1-9][0-9]*$ ]]; then
    echo "[WEB] The pipeline could not create its repository record" >&2
    exit 3
fi
export REPO_ID

trap finalize_active_run_on_exit EXIT

pipeline_exit=0
process_repo "$REPO_URL" "$NOTEBOOK_PATHS" "$SETUP_PATHS" "$REQUIREMENT_PATHS" || pipeline_exit=$?

python3 "$PROJECT_ROOT/pipeline/export_web_result.py" \
    --database "$DB_FILE" \
    --repository-id "$REPO_ID" \
    --output "$WEB_JOB_DIR/result.json"

exit "$pipeline_exit"
