#!/bin/bash
# Integration test: analysis/compare_notebook.py writes the thesis row (version 1) and
# comparison version 2 (score variants + one row per cell); a notebook without an executed
# copy gets an execution row without scores; status comes from the run's own log.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT
export DB_FILE="$TEST_DIR/db.sqlite" LOG_FILE="" PROJECT_ROOT
source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/db.sh"
ensure_pipeline_tables >/dev/null
sqlite3 "$DB_FILE" "INSERT INTO repositories (id, repository, platform) VALUES (1, 'o/r', 'github');
  INSERT INTO notebooks (id, repository_id, name) VALUES (5, 1, 'nb/a.ipynb'), (6, 1, 'nb/slow.ipynb'), (7, 1, 'nb/deep.ipynb');
  INSERT INTO repository_runs (id, repository_id, run_status) VALUES (9, 1, 'RUNNING');"
export RUN_ID=9 REPO_URL=https://github.com/o/r NOTEBOOK_EXEC_LOG="$TEST_DIR/execution.log"
printf 'SUCCESS_WITH_ERRORS|9_r|nb/a.ipynb|12\nEXEC_FAIL|9_r|nb/slow.ipynb|1800|NOTEBOOK_TIMEOUT\n' > "$NOTEBOOK_EXEC_LOG"
python3 - "$TEST_DIR" <<'PY'
import json, sys
d = sys.argv[1]
cell = lambda src, outs: {'cell_type': 'code', 'source': src, 'outputs': outs, 'metadata': {}, 'execution_count': 1}
out = lambda t: {'output_type': 'stream', 'name': 'stdout', 'text': t}
nb = lambda cells: {'cells': cells, 'metadata': {}, 'nbformat': 4, 'nbformat_minor': 5}
json.dump(nb([cell('import x', []), cell('print(1)', [out('1\n')]), cell('print(o)', [out('<o at 0x7f0000000001>\n')]),
              cell('f()', [out('ok\n')])]), open(f'{d}/a.ipynb', 'w'))
json.dump(nb([cell('import x', []), cell('print(1)', [out('1\n')]), cell('print(o)', [out('<o at 0x7f00000000ff>\n')]),
              cell('f()', [{'output_type': 'error', 'ename': 'NameError', 'evalue': "name 'f' is not defined", 'traceback': []}])]),
          open(f'{d}/a_executed.ipynb', 'w'))
json.dump(nb([cell('slow()', [out('done\n')])]), open(f'{d}/slow.ipynb', 'w'))
PY
python3 "$PROJECT_ROOT/analysis/compare_notebook.py" "$TEST_DIR/a.ipynb" "$TEST_DIR/a_executed.ipynb" nb/a.ipynb 1 \
    --json "$TEST_DIR/a.json" > /dev/null 2>&1
python3 "$PROJECT_ROOT/analysis/compare_notebook.py" "$TEST_DIR/slow.ipynb" "$TEST_DIR/missing.ipynb" nb/slow.ipynb 1 \
    --json "$TEST_DIR/slow.json" --missing-output > /dev/null 2>&1

# the diff fails (nbdime RecursionError on an nbformat test notebook, study run 2026-10-11): the execution is still recorded
cp "$TEST_DIR/a_executed.ipynb" "$TEST_DIR/deep_executed.ipynb"
python3 - "$PROJECT_ROOT" "$TEST_DIR" <<'PY' > /dev/null 2>&1
import sys
root, d = sys.argv[1:3]
sys.path.insert(0, root)
import analysis.compare_notebook as cn
def boom(a, b):
    raise RecursionError('maximum recursion depth exceeded')
cn.diff_notebooks_safe = boom
sys.argv = ['compare_notebook.py', f'{d}/a.ipynb', f'{d}/deep_executed.ipynb', 'nb/deep.ipynb', '1', '--json', f'{d}/deep.json']
cn.main()
PY

fail() { echo "[TEST] FAILED: $1"; exit 1; }
deep=$(sqlite3 "$DB_FILE" "SELECT execution_status, error_type, total_code_cells, error_message LIKE '%outputs not compared: RecursionError%',
  (SELECT COUNT(*) FROM notebook_reproducibility_metrics WHERE notebook_id = 7) FROM notebook_executions WHERE notebook_id = 7")
[ "$deep" = "SUCCESS_WITH_ERRORS|NameError|4|1|0" ] || fail "row of a notebook whose diff failed: $deep"
# the failure is classified by the same rule as a compared notebook (type plus context)
deep_cat=$(sqlite3 "$DB_FILE" "SELECT error_category, error_category_by_type FROM notebook_executions WHERE notebook_id = 7")
[ "$deep_cat" = "CODE_ERROR|CODE_ERROR" ] || fail "category of a notebook whose diff failed: $deep_cat"
row=$(sqlite3 "$DB_FILE" "SELECT e.execution_status, e.execution_duration, e.error_type, m.reproducibility_score,
  m.comparison_version, m.score_strict, m.score_normalised, m.cells_assessable, m.cells_empty
  FROM notebook_executions e JOIN notebook_reproducibility_metrics m ON m.notebook_execution_id = e.id WHERE e.notebook_id = 5")
# thesis score 0.5 (empty cell + identical cell of 4); v2: 3 assessable, strict 1/3, normalised 2/3
[ "$row" = "SUCCESS_WITH_ERRORS|12.0|NameError|0.5|2|0.3333|0.6667|3|1" ] || fail "metrics row: $row"
cells=$(sqlite3 "$DB_FILE" "SELECT group_concat(outcome, ',') FROM (SELECT outcome FROM notebook_cell_comparisons ORDER BY cell_index)")
[ "$cells" = "empty,identical,identical normalised,error" ] || fail "cell outcomes: $cells"
slow=$(sqlite3 "$DB_FILE" "SELECT execution_status, execution_duration, error_type, total_code_cells,
  (SELECT COUNT(*) FROM notebook_reproducibility_metrics WHERE notebook_id = 6) FROM notebook_executions WHERE notebook_id = 6")
[ "$slow" = "EXEC_FAIL|1800.0|NOTEBOOK_TIMEOUT|1|0" ] || fail "missing-output row: $slow"
echo "[TEST] compare_notebook integration tests passed"
