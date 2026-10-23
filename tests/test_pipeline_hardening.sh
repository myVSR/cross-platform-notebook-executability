#!/bin/bash

set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

DB_FILE="$TEST_DIR/hardening-test.sqlite"
REPOS_DIR="$TEST_DIR/repos"
LOG_DIR="$TEST_DIR/logs"
LOG_FILE=""
mkdir -p "$REPOS_DIR" "$LOG_DIR"

export DB_FILE REPOS_DIR LOG_DIR LOG_FILE

source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/db.sh"
source "$PROJECT_ROOT/src/checks.sh"
source "$PROJECT_ROOT/src/repo.sh"

ensure_pipeline_tables

NOTEBOOK_PATHS=" notebooks/author's-analysis.ipynb "
SETUP_PATHS="setup.py"
REQUIREMENT_PATHS="requirements.txt"

GITHUB_ID=$(get_or_create_repo_id "https://github.com/example/project's.git")
CODEBERG_ID=$(get_or_create_repo_id "https://codeberg.org/example/project's.git")

if [ "$GITHUB_ID" = "$CODEBERG_ID" ]; then
    echo "[TEST] FAILED: platform-specific repositories shared an ID"
    exit 1
fi

insert_notebooks_from_paths "$GITHUB_ID" "$NOTEBOOK_PATHS"
insert_notebooks_from_paths "$GITHUB_ID" "$NOTEBOOK_PATHS"
insert_notebooks_from_paths "$CODEBERG_ID" "$NOTEBOOK_PATHS"

GITHUB_NOTEBOOK_ID=$(get_notebook_id_from_db "$GITHUB_ID" "notebooks/author's-analysis.ipynb")
CODEBERG_NOTEBOOK_ID=$(get_notebook_id_from_db "$CODEBERG_ID" "notebooks/author's-analysis.ipynb")

if [ -z "$GITHUB_NOTEBOOK_ID" ] || [ -z "$CODEBERG_NOTEBOOK_ID" ]; then
    echo "[TEST] FAILED: escaped notebook lookup returned no ID"
    exit 1
fi

if [ "$GITHUB_NOTEBOOK_ID" = "$CODEBERG_NOTEBOOK_ID" ]; then
    echo "[TEST] FAILED: notebooks from different repositories shared an ID"
    exit 1
fi

GITHUB_NOTEBOOK_COUNT=$(sqlite3 "$DB_FILE" \
    "SELECT notebooks_count FROM repositories WHERE id=$GITHUB_ID;")
if [ "$GITHUB_NOTEBOOK_COUNT" -ne 1 ]; then
    echo "[TEST] FAILED: expected one unique notebook, found $GITHUB_NOTEBOOK_COUNT"
    exit 1
fi

create_repository_run "$GITHUB_ID" "https://github.com/example/project's.git"
finalize_repository_run "$RUN_ID" "TEST_FAILURE" "Repository can't be cloned" 1.5

SAVED_RUN=$(sqlite3 "$DB_FILE" \
    "SELECT url || '|' || run_status || '|' || error_message
     FROM repository_runs WHERE id=$RUN_ID;")
EXPECTED_RUN="https://github.com/example/project's.git|TEST_FAILURE|Repository can't be cloned"
if [ "$SAVED_RUN" != "$EXPECTED_RUN" ]; then
    echo "[TEST] FAILED: escaped run data was not stored correctly"
    exit 1
fi

create_repository_run "$GITHUB_ID" "https://github.com/example/interrupted.git"
INTERRUPTED_RUN_ID="$RUN_ID"
REPO_START_TIME=$(now_sec)

set +e
(
    RUN_ID="$INTERRUPTED_RUN_ID"
    false
    finalize_active_run_on_exit
)
HANDLER_EXIT_CODE="$?"
set -e

if [ "$HANDLER_EXIT_CODE" -ne 1 ]; then
    echo "[TEST] FAILED: exit handler did not preserve the original exit code"
    exit 1
fi

INTERRUPTED_STATUS=$(sqlite3 "$DB_FILE" \
    "SELECT run_status FROM repository_runs WHERE id=$INTERRUPTED_RUN_ID;")
if [ "$INTERRUPTED_STATUS" != "PIPELINE_INTERRUPTED" ]; then
    echo "[TEST] FAILED: active run was left as $INTERRUPTED_STATUS"
    exit 1
fi

INSIDE_DIR="$REPOS_DIR/incomplete"
OUTSIDE_DIR="$TEST_DIR/outside"
mkdir -p "$INSIDE_DIR" "$OUTSIDE_DIR"

cleanup_repository_directory "$INSIDE_DIR"
if [ -e "$INSIDE_DIR" ]; then
    echo "[TEST] FAILED: safe repository cleanup did not remove its target"
    exit 1
fi

if cleanup_repository_directory "$OUTSIDE_DIR"; then
    echo "[TEST] FAILED: cleanup accepted a path outside REPOS_DIR"
    exit 1
fi
if [ ! -d "$OUTSIDE_DIR" ]; then
    echo "[TEST] FAILED: cleanup removed a path outside REPOS_DIR"
    exit 1
fi

echo "[TEST] Pipeline SQL and cleanup hardening PASSED"
