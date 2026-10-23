#!/bin/bash
###############################################################################
# classify_backfill.sh
#
# Runs hybrid (rule-based + Ollama) classification on every notebook that
# already has a recorded execution but no classification yet. Meant to be
# run inside the notebookfair-pipeline:local container, with the project
# mounted, alongside (or after) the normal execution workers.
#
# Env vars:
#   WORKER_COUNT / WORKER_INDEX — same slicing pattern as run_full_sample.sh
#   CLASSIFICATION_MODEL   (default gemma3:4b)
#   CLASSIFICATION_OLLAMA_URL (default http://host.docker.internal:11434)
#   CLASSIFICATION_TIMEOUT (default 300)
###############################################################################
set -o pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"

DB_FILE="$PROJECT_ROOT/data/output/db/db.sqlite"
REPOS_DIR="$PROJECT_ROOT/data/output/cloned_repos"

sqlite3() { command sqlite3 -cmd ".timeout 30000" "$@"; }

WORKER_COUNT="${WORKER_COUNT:-1}"
WORKER_INDEX="${WORKER_INDEX:-0}"
MODEL="${CLASSIFICATION_MODEL:-gemma3:4b}"
OLLAMA_URL="${CLASSIFICATION_OLLAMA_URL:-http://host.docker.internal:11434}"
TIMEOUT="${CLASSIFICATION_TIMEOUT:-1800}"

echo "[SETUP] Worker $WORKER_INDEX of $WORKER_COUNT, model=$MODEL, ollama=$OLLAMA_URL, timeout=${TIMEOUT}s"

# Every executed notebook that has no successful LLM result yet (never classified, or rule-only because the LLM call failed).
# Deterministic pseudo-random order so any partial run is an unbiased sample across categories/platforms.
mapfile -t ROWS < <(sqlite3 "$DB_FILE" "
    SELECT ne.notebook_id || '|' || ne.repository_id || '|' || ne.notebook_name
    FROM notebook_executions ne
    WHERE NOT EXISTS (
        SELECT 1 FROM notebook_classifications nc
        WHERE nc.notebook_id = ne.notebook_id AND nc.llm_category IS NOT NULL
    )
    GROUP BY ne.notebook_id
    ORDER BY (ne.notebook_id * 2654435761) % 4294967296;
")

TOTAL=${#ROWS[@]}
echo "[SETUP] $TOTAL notebooks need classification (before slicing)"

DONE=0
OK=0
FAIL=0
SKIP=0
IDX=0
for row in "${ROWS[@]}"; do
    IDX=$((IDX + 1))
    [ $(( (IDX - 1) % WORKER_COUNT )) -eq "$WORKER_INDEX" ] || continue
    DONE=$((DONE + 1))

    IFS='|' read -r NOTEBOOK_ID REPO_ID NOTEBOOK_NAME <<< "$row"

    REPO_DIR=$(find "$REPOS_DIR" -maxdepth 1 -type d -name "${REPO_ID}_*" | head -1)
    if [ -z "$REPO_DIR" ]; then
        echo "[SKIP] notebook $NOTEBOOK_ID: repo dir for repo_id=$REPO_ID no longer on disk"
        SKIP=$((SKIP + 1))
        continue
    fi

    NOTEBOOK_FILE="$REPO_DIR/$NOTEBOOK_NAME"
    if [ ! -f "$NOTEBOOK_FILE" ]; then
        echo "[SKIP] notebook $NOTEBOOK_ID: file not found at $NOTEBOOK_FILE"
        SKIP=$((SKIP + 1))
        continue
    fi

    if timeout "$((TIMEOUT + 120))" python3 -m analysis.notebook_classification classify "$NOTEBOOK_FILE" \
        --db-file "$DB_FILE" \
        --notebook-id "$NOTEBOOK_ID" \
        --model "$MODEL" \
        --ollama-url "$OLLAMA_URL" \
        --timeout "$TIMEOUT" \
        --confidence-threshold 0.55 >/tmp/classify_out_$WORKER_INDEX.log 2>&1; then
        OK=$((OK + 1))
    else
        FAIL=$((FAIL + 1))
        echo "[FAIL] notebook $NOTEBOOK_ID ($NOTEBOOK_FILE):"
        tail -3 /tmp/classify_out_$WORKER_INDEX.log
    fi

    if [ $((DONE % 10)) -eq 0 ]; then
        echo "[PROGRESS] worker=$WORKER_INDEX done=$DONE ok=$OK fail=$FAIL skip=$SKIP"
    fi
done

echo "[FINAL] worker=$WORKER_INDEX done=$DONE ok=$OK fail=$FAIL skip=$SKIP"
