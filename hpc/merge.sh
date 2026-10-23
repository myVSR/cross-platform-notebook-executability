#!/bin/bash
###############################################################################
# Merge task databases.
#   bash merge.sh results/<run>                    -> results/<run>/merged.sqlite (one run)
#   bash merge.sh --study results/study.sqlite results/<run1> results/<run2> ...
#        -> one study database from several runs (main pass, top-ups, re-runs), rebuilt from scratch
#           every time, runs merged in the order given (oldest first), so the latest run of a
#           resource is its last pass. ReproScore outcomes are filled in for runs without one.
###############################################################################
set -euo pipefail
BUNDLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOME/.notebookfair/env.sh" ] && source "$HOME/.notebookfair/env.sh"
if [ "${1:-}" = "--study" ]; then
    TARGET="${2:?usage: bash merge.sh --study results/study.sqlite results/<run>...}"
    shift 2
    [ $# -gt 0 ] || { echo "no run folders given"; exit 1; }
    rm -f "$TARGET"
    STUDY_MERGE=1
    sources=()
    for run in "$@"; do sources+=("$run"/task-*/db.sqlite); done
else
    RUN_DIR="${1:?usage: bash merge.sh results/<run>}"
    TARGET="$RUN_DIR/merged.sqlite"
    sources=("$RUN_DIR"/task-*/db.sqlite)
fi
keep=()
[ -n "${STUDY_MERGE:-}" ] && keep=(--keep-going)       # study: a bad task database is reported, not fatal
python3 "$BUNDLE/pipeline/merge_task_dbs.py" --target "$TARGET" "${keep[@]}" "${sources[@]}"
# ReproScore outcome for runs that ended before execution (e.g. NO_NOTEBOOKS) and so have none yet
python3 "$BUNDLE/src/reproscore_stage.py" --db "$TARGET" outcome
sqlite3 -header -column "$TARGET" \
  "SELECT r.platform, rr.run_status, COUNT(*) AS runs FROM repository_runs rr JOIN repositories r ON r.id = rr.repository_id GROUP BY 1, 2 ORDER BY 1, 3 DESC;"
