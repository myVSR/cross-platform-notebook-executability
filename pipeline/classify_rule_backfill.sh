#!/bin/bash
###############################################################################
# classify_rule_backfill.sh
#
# Rule-based-only pass over every executed notebook that has no rule-based
# result yet. Takes seconds per notebook and does not use Ollama. The LLM
# workers (classify_backfill.sh) keep running and add the LLM result later.
###############################################################################
set -o pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"

DB_FILE="$PROJECT_ROOT/data/output/db/db.sqlite"
REPOS_DIR="$PROJECT_ROOT/data/output/cloned_repos"

sqlite3() { command sqlite3 -cmd ".timeout 30000" "$@"; }

mapfile -t ROWS < <(sqlite3 "$DB_FILE" "
    SELECT ne.notebook_id || '|' || ne.repository_id || '|' || ne.notebook_name
    FROM notebook_executions ne
    WHERE NOT EXISTS (
        SELECT 1 FROM notebook_classifications nc
        WHERE nc.notebook_id = ne.notebook_id AND nc.rule_category IS NOT NULL
    )
    GROUP BY ne.notebook_id
    ORDER BY ne.notebook_id;
")

echo "[SETUP] ${#ROWS[@]} notebooks have no rule-based result yet"

DONE=0; OK=0; FAIL=0; SKIP=0
for row in "${ROWS[@]}"; do
    DONE=$((DONE + 1))
    IFS='|' read -r NOTEBOOK_ID REPO_ID NOTEBOOK_NAME <<< "$row"

    REPO_DIR=$(find "$REPOS_DIR" -maxdepth 1 -type d -name "${REPO_ID}_*" | head -1)
    if [ -z "$REPO_DIR" ] || [ ! -f "$REPO_DIR/$NOTEBOOK_NAME" ]; then
        echo "[SKIP] notebook $NOTEBOOK_ID: file not on disk (repo_id=$REPO_ID, $NOTEBOOK_NAME)"
        SKIP=$((SKIP + 1))
        continue
    fi

    if timeout 180 python3 -m analysis.notebook_classification classify "$REPO_DIR/$NOTEBOOK_NAME" \
        --rule-only \
        --db-file "$DB_FILE" \
        --notebook-id "$NOTEBOOK_ID" >/tmp/rule_out.log 2>&1; then
        OK=$((OK + 1))
    else
        FAIL=$((FAIL + 1))
        echo "[FAIL] notebook $NOTEBOOK_ID ($REPO_DIR/$NOTEBOOK_NAME):"
        tail -3 /tmp/rule_out.log
    fi

    if [ $((DONE % 50)) -eq 0 ]; then
        echo "[PROGRESS] done=$DONE/${#ROWS[@]} ok=$OK fail=$FAIL skip=$SKIP"
    fi
done

echo "[FINAL] done=$DONE ok=$OK fail=$FAIL skip=$SKIP"
