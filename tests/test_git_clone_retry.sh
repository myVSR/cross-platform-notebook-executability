#!/bin/bash
# Offline test: git_clone_with_retry retries after HTTP 429 and gives up on other errors.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
LOG_FILE="$TEST_DIR/log"; : > "$LOG_FILE"
GIT_RATE_LIMIT_WAIT=0
source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/repo.sh"

CALLS="$TEST_DIR/calls"
git() {   # stand-in: first call rate-limited, then success (or always 404 for "missing")
    echo x >> "$CALLS"
    local dir="${@: -1}" url="${@: -2:1}"
    if [[ "$url" == *missing* ]]; then
        echo "fatal: repository '$url' not found" >&2; return 128
    fi
    if [ "$(wc -l < "$CALLS")" -eq 1 ]; then
        echo "fatal: unable to access '$url': The requested URL returned error: 429" >&2; return 128
    fi
    mkdir -p "$dir"
}

failures=0
: > "$CALLS"
if git_clone_with_retry https://codeberg.org/a/b "$TEST_DIR/b" >/dev/null && [ -d "$TEST_DIR/b" ] && [ "$(wc -l < "$CALLS")" -eq 2 ]; then
    echo "[TEST] ok: retried after 429"
else echo "[TEST] FAILED: 429 retry"; failures=$((failures + 1)); fi

: > "$CALLS"
if ! git_clone_with_retry https://codeberg.org/a/missing "$TEST_DIR/m" >/dev/null && [ "$(wc -l < "$CALLS")" -eq 1 ]; then
    echo "[TEST] ok: no retry on other errors"
else echo "[TEST] FAILED: retried a non-429 error"; failures=$((failures + 1)); fi

[ "$failures" -eq 0 ] && echo "[TEST] git clone retry tests passed" || exit 1
