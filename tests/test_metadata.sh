#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

DB_FILE="$TEST_DIR/metadata-test.sqlite"
export DB_FILE

source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/db.sh"
source "$PROJECT_ROOT/src/checks.sh"
source "$PROJECT_ROOT/src/repo.sh"

ensure_pipeline_tables

if ! validate_zenodo "https://zenodo.org/records/3362625"; then
    echo "[TEST] FAILED: valid Zenodo record was rejected"
    exit 1
fi

if validate_zenodo "https://zenodo.org/records/not-a-number"; then
    echo "[TEST] FAILED: malformed Zenodo URL was accepted"
    exit 1
fi

if validate_zenodo "https://zenodo.org/records/999999999999999999"; then
    echo "[TEST] FAILED: missing Zenodo record was accepted"
    exit 1
fi

GITHUB_ID=$(sqlite3 "$DB_FILE" "
    INSERT INTO repositories (repository, platform)
    VALUES ('octocat/Hello-World', 'github');
    SELECT last_insert_rowid();
")

CODEBERG_ID=$(sqlite3 "$DB_FILE" "
    INSERT INTO repositories (repository, platform)
    VALUES ('tplasdio/ipynb-py-convert', 'codeberg');
    SELECT last_insert_rowid();
")

ZENODO_ID=$(sqlite3 "$DB_FILE" "
    INSERT INTO repositories (repository, platform)
    VALUES ('3362625', 'zenodo');
    SELECT last_insert_rowid();
")


fetch_and_save_repo_metadata \
    "$GITHUB_ID" \
    "https://github.com/octocat/Hello-World" \
    "github"

fetch_and_save_repo_metadata \
    "$CODEBERG_ID" \
    "https://codeberg.org/tplasdio/ipynb-py-convert" \
    "codeberg"

fetch_and_save_repo_metadata \
    "$ZENODO_ID" \
    "https://zenodo.org/records/3362625" \
    "zenodo"

METADATA_COUNT=$(sqlite3 "$DB_FILE" \
    "SELECT COUNT(*) FROM repository_metadata;")

if [ "$METADATA_COUNT" -ne 3 ]; then
    echo "[TEST] FAILED: expected 3 metadata rows, found $METADATA_COUNT"
    exit 1
fi

EMPTY_TITLES=$(sqlite3 "$DB_FILE" \
    "SELECT COUNT(*) FROM repository_metadata
     WHERE title IS NULL OR TRIM(title) = '';")

if [ "$EMPTY_TITLES" -ne 0 ]; then
    echo "[TEST] FAILED: $EMPTY_TITLES metadata rows have no title"
    exit 1
fi

echo "[TEST] Metadata extraction and storage PASSED"

sqlite3 -header -column "$DB_FILE" "
    SELECT
        r.platform,
        r.repository,
        m.title,
        m.authors,
        m.license,
        m.doi
    FROM repositories r
    JOIN repository_metadata m ON m.repo_id = r.id
    ORDER BY r.platform;
"
