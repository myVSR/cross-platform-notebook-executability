#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

DB_FILE="$TEST_DIR/discovery-test.sqlite"
REPOS_DIR="$TEST_DIR/repos"
LOG_DIR="$TEST_DIR/logs"
LOG_FILE=""
CLASSIFICATION_ENABLED=false
mkdir -p "$REPOS_DIR" "$LOG_DIR"

ARTIFACT_DIR="$TEST_DIR/artifacts"         # required by process_repo (run_array.sbatch sets it on the cluster)
export DB_FILE REPOS_DIR LOG_DIR LOG_FILE CLASSIFICATION_ENABLED ARTIFACT_DIR

source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/db.sh"
source "$PROJECT_ROOT/src/checks.sh"
source "$PROJECT_ROOT/src/classification.sh"
source "$PROJECT_ROOT/src/repo.sh"

ensure_pipeline_tables

REPO_ID=$(sqlite3 "$DB_FILE" "
    INSERT INTO repositories (repository, platform)
    VALUES ('example/project', 'github');
    SELECT last_insert_rowid();
")
export REPO_ID

# Keep the test local: a mocked `git clone` writes the notebook into the (always fresh) clone folder,
# remote/API and environment operations are mocked, and process_repo's real discovery path runs.
git() {
    if [ "${1:-}" = clone ]; then
        local target="${!#}"
        mkdir -p "$target"
        printf '{"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}\n' >"$target/discovered.ipynb"
    fi
    return 0
}
validate_repo() { return 0; }
fetch_and_save_repo_metadata() { return 0; }
process_requirements() { return 0; }
setup_pyenv_env() {
    ENV_ERROR_TYPE="TEST_STOP_AFTER_DISCOVERY"
    ENV_ERROR_MESSAGE="Discovery regression test completed"
    return 1
}
cleanup_pyenv_env() { return 0; }

process_repo "https://github.com/example/project" "" "" ""

DISCOVERED_COUNT=$(sqlite3 "$DB_FILE" "
    SELECT COUNT(*)
    FROM notebooks
    WHERE repository_id=$REPO_ID AND name='discovered.ipynb';
")

if [ "$DISCOVERED_COUNT" -ne 1 ]; then
    echo "[TEST] FAILED: automatic discovery did not register the notebook"
    exit 1
fi

RUN_STATUS=$(sqlite3 "$DB_FILE" "
    SELECT run_status
    FROM repository_runs
    WHERE repository_id=$REPO_ID
    ORDER BY id DESC
    LIMIT 1;
")

if [ "$RUN_STATUS" != "TEST_STOP_AFTER_DISCOVERY" ]; then
    echo "[TEST] FAILED: process_repo stopped before automatic discovery"
    exit 1
fi

echo "[TEST] Automatic notebook discovery order PASSED"
