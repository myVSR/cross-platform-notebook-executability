#!/bin/bash
# Offline test: api_get (src/repo.sh) stores every metadata answer with its provenance in
# metadata_snapshots (src/metadata_snapshot.py) — success and error — and never the token.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR=$(mktemp -d)
trap 'kill "$server" 2>/dev/null; rm -rf "$TEST_DIR"' EXIT

mkdir -p "$TEST_DIR/www/repos/o"
cat > "$TEST_DIR/www/repos/o/r" <<'JSON'
{"name": "r", "full_name": "o/r", "description": "line one\r\nline two", "owner": {"login": "o"},
 "license": {"spdx_id": "MIT"}, "topics": ["notebooks", "science"], "stargazers_count": 7,
 "created_at": "2020-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}
JSON
port=$((30000 + RANDOM % 20000))
(cd "$TEST_DIR/www" && exec python3 -m http.server "$port" --bind 127.0.0.1 >/dev/null 2>&1) &
server=$!
for _ in $(seq 50); do curl -s "http://127.0.0.1:$port/" >/dev/null && break; sleep 0.1; done

export DB_FILE="$TEST_DIR/db.sqlite" LOG_FILE="$TEST_DIR/log" RUN_ID=5
sqlite3 "$DB_FILE" "CREATE TABLE repository_runs (id INTEGER PRIMARY KEY, repository_id INTEGER, run_status TEXT);
                    INSERT INTO repository_runs VALUES (5, 9, 'RUNNING');"
source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/repo.sh"

failures=0
check() { if [ "$2" = "$3" ]; then echo "[TEST] ok: $1"; else echo "[TEST] FAILED: $1: '$2' != '$3'"; failures=$((failures + 1)); fi; }

body=$(api_get github "http://127.0.0.1:$port/repos/o/r" -H "Accept: application/json" -H "Authorization: Bearer s3cr3t-token")
check "body returned" "$(jq -r .full_name <<<"$body")" "o/r"
check "normaliser still gives 8 lines" "$(normalize_github_metadata <<<"$body" | wc -l)" "8"
q() { sqlite3 "$DB_FILE" "$1"; }
check "one snapshot" "$(q 'SELECT COUNT(*) FROM metadata_snapshots')" "1"
check "run and repository" "$(q "SELECT repository_run_id || '/' || repository_id || '/' || platform FROM metadata_snapshots")" "5/9/github"
check "status and attempts" "$(q "SELECT http_status || '/' || attempts FROM metadata_snapshots")" "200/1"
check "token used, flag only" "$(q 'SELECT token_used FROM metadata_snapshots')" "1"
check "json queryable" "$(q "SELECT json_extract(response_json, '$.stargazers_count') FROM metadata_snapshots")" "7"
check "sha256 of the body" "$(q 'SELECT response_sha256 FROM metadata_snapshots')" "$(sha256sum < "$TEST_DIR/www/repos/o/r" | cut -d' ' -f1)"
check "request time format" "$(q "SELECT requested_at GLOB '????-??-??T??:??:??.???Z' AND responded_at >= requested_at FROM metadata_snapshots")" "1"
check "response headers kept" "$(q "SELECT json_extract(response_headers_json, '$.date') IS NOT NULL AND json_extract(response_headers_json, '$.server') IS NOT NULL FROM metadata_snapshots")" "1"
check "tool recorded" "$(q "SELECT tool LIKE 'curl %' FROM metadata_snapshots")" "1"

api_get github "http://127.0.0.1:$port/repos/o/missing" -H "Accept: application/json" >/dev/null 2>&1
check "error answer returns 1" "$?" "1"
check "error answer stored" "$(q 'SELECT http_status FROM metadata_snapshots ORDER BY id DESC LIMIT 1')" "404"
check "error body kept" "$(q 'SELECT response_json IS NOT NULL OR response_text_excerpt IS NOT NULL FROM metadata_snapshots ORDER BY id DESC LIMIT 1')" "1"
check "token never stored" "$(sqlite3 "$DB_FILE" .dump | grep -c s3cr3t)" "0"

[ "$failures" -eq 0 ] && echo "[TEST] metadata snapshot tests passed" || exit 1
