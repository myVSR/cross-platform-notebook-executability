#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

source "$PROJECT_ROOT/config/config.sh"

OUTPUT_DIR="$TEST_DIR/output"
REPOS_DIR="$OUTPUT_DIR/cloned_repos"
COMP_DIR="$OUTPUT_DIR/comparisons"
LOG_DIR="$OUTPUT_DIR/logs"
OUTPUT_DB_DIR="$OUTPUT_DIR/db"
OUTPUT_DB_FILE="$OUTPUT_DB_DIR/test.sqlite"
DB_FILE="$OUTPUT_DB_FILE"
LOG_FILE=""
CLASSIFICATION_ENABLED=false
CLASSIFICATION_RULE_ONLY=true

export OUTPUT_DIR REPOS_DIR COMP_DIR LOG_DIR OUTPUT_DB_DIR OUTPUT_DB_FILE DB_FILE
export LOG_FILE CLASSIFICATION_ENABLED CLASSIFICATION_RULE_ONLY
export PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}"

source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/checks.sh"
source "$PROJECT_ROOT/src/db.sh"
source "$PROJECT_ROOT/src/pyenv.sh"
source "$PROJECT_ROOT/src/requirements.sh"
source "$PROJECT_ROOT/src/notebooks.sh"
source "$PROJECT_ROOT/src/classification.sh"
source "$PROJECT_ROOT/src/repo.sh"

initialize_directories
ensure_pipeline_tables

TEST_REPO="https://github.com/binder-examples/requirements"
TEST_NOTEBOOKS="index.ipynb"
TEST_REQUIREMENTS="requirements.txt"

REPO_ID=$(get_or_create_repo_id "$TEST_REPO")
export REPO_ID
process_repo "$TEST_REPO" "$TEST_NOTEBOOKS" "" "$TEST_REQUIREMENTS"

RUN_STATUS=$(sqlite3 "$DB_FILE" "SELECT run_status FROM repository_runs ORDER BY id DESC LIMIT 1;")
echo "[TEST] Run status: $RUN_STATUS"

if [ "$RUN_STATUS" = "SUCCESS" ]; then
    echo "[TEST] PASSED"
else
    echo "[TEST] FAILED: expected SUCCESS, got $RUN_STATUS"; exit 1
fi
