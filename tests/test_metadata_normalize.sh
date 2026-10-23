#!/bin/bash
# Offline test: every platform normalizer prints exactly 8 single-line fields,
# also when API values contain line breaks (aiocoap/aiocoap on Codeberg has
# "\r\n" in its description, which used to give 9 lines -> METADATA_FETCH_FAILED).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/repo.sh"

failures=0

# Usage: expect_fields <name> <expected description> <normalizer> <json>
expect_fields() {
    local name="$1" expected_description="$2" normalizer="$3" json="$4"
    local -a fields
    mapfile -t fields < <("$normalizer" <<< "$json")
    if [ "${#fields[@]}" -ne 8 ]; then
        echo "[TEST] FAILED: $name gave ${#fields[@]} lines instead of 8"
        failures=$((failures + 1))
        return
    fi
    if [ "${fields[1]}" != "$expected_description" ]; then
        echo "[TEST] FAILED: $name description is '${fields[1]}'"
        failures=$((failures + 1))
        return
    fi
    echo "[TEST] ok: $name"
}

expect_fields "codeberg CRLF description" "The Python CoAP library. Its issue tracking is at GitHub." normalize_codeberg_metadata '{"name": "aiocoap", "description": "The Python CoAP library.\r\nIts issue tracking is at GitHub.",
 "owner": {"full_name": "", "login": "aiocoap"}, "topics": ["python", "coap"],
 "created_at": "2020-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}'

expect_fields "codeberg null fields" "" normalize_codeberg_metadata '{"name": "x", "description": null, "owner": null, "license": null, "topics": null}'

expect_fields "github LF description" "line one line two" normalize_github_metadata '{"name": "repo", "description": "line one\n\nline two", "owner": {"login": "someone"},
 "license": {"spdx_id": "NOASSERTION"}, "topics": [], "created_at": "", "updated_at": ""}'

expect_fields "github newline in topic" "" normalize_github_metadata '{"name": "repo", "description": null, "owner": {"login": "someone"},
 "license": null, "topics": ["a\nb"]}'

if [ "$failures" -ne 0 ]; then
    echo "[TEST] $failures metadata normalization test(s) failed"
    exit 1
fi
echo "[TEST] Metadata normalization tests passed"
