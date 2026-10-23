#!/bin/bash
# Test: pipeline/merge_task_dbs.py merges task databases whose ids collide.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export LOG_FILE="" PROJECT_ROOT
source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/db.sh"
for task in 0 1; do
    DB_FILE="$TEST_DIR/task-$task.sqlite" ensure_pipeline_tables >/dev/null
done
# Both tasks number everything from 1. Task 1 also ran gh/shared, which task 0 ran too.
sqlite3 "$TEST_DIR/task-0.sqlite" "
  INSERT INTO repositories (id, repository, platform) VALUES (1, 'gh/a', 'github'), (2, 'gh/shared', 'github');
  INSERT INTO notebooks (id, repository_id, name) VALUES (1, 1, 'a.ipynb'), (2, 2, 's.ipynb');
  INSERT INTO repository_metadata (repo_id, title, fetched_at) VALUES (2, 'old title', '2026-10-01 10:00:00');
  INSERT INTO pipeline_sessions (id, started_at, hostname) VALUES (1, 'x', 'node001');
  INSERT INTO repository_runs (id, repository_id, run_status, session_id) VALUES (1, 1, 'SUCCESS', 1), (2, 2, 'SUCCESS', 1);
  INSERT INTO notebook_executions (id, repository_run_id, repository_id, notebook_id, notebook_name) VALUES (1, 1, 1, 1, 'a.ipynb');
  INSERT INTO notebook_reproducibility_metrics (id, repository_run_id, notebook_execution_id, repository_id, notebook_id, reproducibility_score)
    VALUES (1, 1, 1, 1, 1, 0.5);
  INSERT INTO resource_acquisitions (id, repository_run_id, repository_id, platform, acquired_at, version_id) VALUES (1, 1, 1, 'github', 'x', 'sha-a');
  INSERT INTO acquisition_files (acquisition_id, path, origin) VALUES (1, 'a.ipynb', 'git tree');"
sqlite3 "$TEST_DIR/task-1.sqlite" "
  INSERT INTO repositories (id, repository, platform) VALUES (1, 'cb/b', 'codeberg'), (2, 'gh/shared', 'github');
  INSERT INTO notebooks (id, repository_id, name) VALUES (1, 1, 'b.ipynb'), (2, 2, 's.ipynb');
  INSERT INTO repository_metadata (repo_id, title, fetched_at) VALUES (2, 'new title', '2026-10-02 10:00:00');
  INSERT INTO pipeline_sessions (id, started_at, hostname) VALUES (1, 'y', 'node002');
  INSERT INTO repository_runs (id, repository_id, run_status, session_id) VALUES (1, 1, 'SUCCESS', 1), (2, 2, 'TIMEOUT_ERROR', 1);
  INSERT INTO notebook_executions (id, repository_run_id, repository_id, notebook_id, notebook_name) VALUES (1, 1, 1, 1, 'b.ipynb');
  INSERT INTO notebook_reproducibility_metrics (id, repository_run_id, notebook_execution_id, repository_id, notebook_id, reproducibility_score)
    VALUES (1, 1, 1, 1, 1, 0.9);
  INSERT INTO resource_acquisitions (id, repository_run_id, repository_id, platform, acquired_at, version_id) VALUES (1, 1, 1, 'codeberg', 'y', 'sha-b');
  INSERT INTO acquisition_files (acquisition_id, path, origin) VALUES (1, 'b.ipynb', 'git tree');"

M="$TEST_DIR/merged.sqlite"
python3 "$PROJECT_ROOT/pipeline/merge_task_dbs.py" --target "$M" "$TEST_DIR/task-0.sqlite" "$TEST_DIR/task-1.sqlite" > /dev/null
fail() { echo "[TEST] FAILED: $1"; exit 1; }
q() { sqlite3 "$M" "$1"; }
[ "$(q "SELECT COUNT(*) FROM repositories")" = 3 ] || fail "repositories: $(q 'SELECT COUNT(*) FROM repositories')"
[ "$(q "SELECT COUNT(*) FROM notebooks")" = 3 ] || fail "notebooks"
[ "$(q "SELECT COUNT(*) FROM repository_runs")" = 4 ] || fail "runs (history of both tasks kept)"
[ "$(q "SELECT title FROM repository_metadata m JOIN repositories r ON r.id = m.repo_id WHERE r.repository = 'gh/shared'")" = "new title" ] || fail "newest metadata"
# every reference points at the right merged row
row=$(q "SELECT r.repository, n.name, m.reproducibility_score, a.version_id, f.path, s.hostname
  FROM notebook_reproducibility_metrics m JOIN notebook_executions e ON e.id = m.notebook_execution_id
  JOIN repository_runs rr ON rr.id = m.repository_run_id JOIN repositories r ON r.id = rr.repository_id
  JOIN notebooks n ON n.id = e.notebook_id JOIN resource_acquisitions a ON a.repository_run_id = rr.id
  JOIN acquisition_files f ON f.acquisition_id = a.id JOIN pipeline_sessions s ON s.id = rr.session_id
  WHERE r.repository = 'cb/b'")
[ "$row" = "cb/b|b.ipynb|0.9|sha-b|b.ipynb|node002" ] || fail "references of task 1: $row"
[ "$(q "SELECT COUNT(*) FROM merge_run_map")" = 4 ] || fail "run map"
if python3 "$PROJECT_ROOT/pipeline/merge_task_dbs.py" --target "$M" "$TEST_DIR/task-0.sqlite" 2>/dev/null; then fail "second merge of the same source was accepted"; fi
[ "$(q "SELECT COUNT(*) FROM repository_runs")" = 4 ] || fail "refused merge changed the target"
echo "[TEST] merge tests passed"
