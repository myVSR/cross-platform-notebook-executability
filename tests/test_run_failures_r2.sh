#!/bin/bash
# Offline tests for the fixes after study round 2 (2026-10-20):
#  - a repository folder named "*.ipynb" is not a notebook; a notebook name beginning with a space keeps it;
#  - a run during which the node-local disk ran low is recorded as DISK_FULL, not SUCCESS;
#  - the kernel runs with the resource's venv active (shell !pip installs go into the venv, never the pipeline's Python).
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

DB_FILE="$TEST_DIR/db.sqlite"
REPOS_DIR="$TEST_DIR/repos"
LOG_DIR="$TEST_DIR/logs"
LOG_FILE=""
mkdir -p "$REPOS_DIR" "$LOG_DIR"
export DB_FILE REPOS_DIR LOG_DIR LOG_FILE

source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/db.sh"
source "$PROJECT_ROOT/src/repo.sh"
ensure_pipeline_tables
fail() { echo "[TEST] FAILED: $*"; exit 1; }

# --- discovery and paths -------------------------------------------------------------------------------------
REPO_DIR="$REPOS_DIR/13_listspam.ipynb"
mkdir -p "$REPO_DIR/folder.ipynb"
echo '{}' > "$REPO_DIR/listspam.ipynb"
echo '{}' > "$REPO_DIR/ CSC1185-Week2.ipynb"
ln -s "listspam.ipynb" "$REPO_DIR/link.ipynb"
actual=$(discover_notebooks "$REPO_DIR" 2>/dev/null)
[ "$actual" = " CSC1185-Week2.ipynb;link.ipynb;listspam.ipynb" ] || fail "discovered '$actual'"

[ "$(notebook_rel_path ' CSC1185-Week2.ipynb')" = " CSC1185-Week2.ipynb" ] || fail "existing name with a space was trimmed"
[ "$(notebook_rel_path ' listspam.ipynb ')" = "listspam.ipynb" ] || fail "supplied path not trimmed"

REPO_ID=$(get_or_create_repo_id "https://codeberg.org/mollydma/listspam.ipynb")
insert_notebooks_from_paths "$REPO_ID" "$actual"
[ -n "$(get_notebook_id_from_db "$REPO_ID" " CSC1185-Week2.ipynb")" ] || fail "notebook with a leading space not registered as is"
[ "$(sqlite3 "$DB_FILE" "SELECT notebooks_count FROM repositories WHERE id=$REPO_ID")" -eq 3 ] || fail "notebook count"

# --- disk ----------------------------------------------------------------------------------------------------
create_repository_run "$REPO_ID" "https://codeberg.org/mollydma/listspam.ipynb"
DISK_LOW=""
DISK_MIN_FREE_MB=0
finalize_repository_run "$RUN_ID" "SUCCESS" "Repository executed successfully" 1 >/dev/null
[ "$(sqlite3 "$DB_FILE" "SELECT run_status FROM repository_runs WHERE id=$RUN_ID")" = "SUCCESS" ] || fail "status changed with enough disk"

create_repository_run "$REPO_ID" "https://codeberg.org/mollydma/listspam.ipynb"
DISK_LOW=""
DISK_MIN_FREE_MB=999999999            # every filesystem is "low"
disk_check execution >/dev/null && fail "disk_check passed below the limit"
[[ "$DISK_LOW" == execution:* ]] || fail "DISK_LOW='$DISK_LOW'"
finalize_repository_run "$RUN_ID" "SUCCESS" "Repository executed successfully" 1 >/dev/null
row=$(sqlite3 "$DB_FILE" "SELECT run_status || '|' || error_message FROM repository_runs WHERE id=$RUN_ID")
[[ "$row" == "DISK_FULL|Node-local disk space ran out during the run (execution:"* ]] || fail "finalised as '$row'"
[[ "$row" == *"would have been recorded as SUCCESS"* ]] || fail "original status not kept: '$row'"
grep -q "'DISK_FULL'" "$PROJECT_ROOT/pipeline/topup_from_reserve.py" || fail "DISK_FULL is not a pipeline error for the top-up"

# --- kernel environment -------------------------------------------------------------------------------------
source "$PROJECT_ROOT/src/pyenv.sh" 2>/dev/null || true
RUN_ARTIFACT_DIR="$TEST_DIR/artifacts"
REPO_VENV_DIR="$TEST_DIR/venv"
REPO_PYTHON="$REPO_VENV_DIR/bin/python"
mkdir -p "$RUN_ARTIFACT_DIR/install"
echo '{"import_path": ["src"]}' > "$RUN_ARTIFACT_DIR/install/dependencies.json"
echo '{"uploaded_prior_to": "2024-05-01T23:59:59Z"}' > "$RUN_ARTIFACT_DIR/install/install-report.json"
RUN_ID=7
write_run_kernelspec "$REPO_DIR" "$RUN_ARTIFACT_DIR/install/dependencies.json"
python3 - "$RUN_ARTIFACT_DIR/kernels/nbf-run-7/kernel.json" "$REPO_VENV_DIR" <<'PY' || fail "kernel environment"
import json, sys
env = json.load(open(sys.argv[1]))['env']
venv = sys.argv[2]
assert env['PATH'].split(':')[0] == venv + '/bin', env['PATH']
assert env['VIRTUAL_ENV'] == venv and env['PIP_REQUIRE_VIRTUALENV'] == 'true'
assert env['PIP_UPLOADED_PRIOR_TO'] == '2024-05-01T23:59:59Z'
assert env['PYTHONPATH'].endswith('/src')
PY

echo "[TEST] Round-2 run failure tests passed"
