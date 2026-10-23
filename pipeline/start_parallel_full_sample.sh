#!/bin/bash
###############################################################################
# Start the evaluation-sample pipeline in parallel and leave it running.
#
# Usage from the project root:
#   bash pipeline/start_parallel_full_sample.sh
#
# Optional environment variables:
#   WORKER_COUNT=4              # default: 4
#   FILTER_PLATFORM=github      # optional: github | codeberg | zenodo
#   FILTER_CATEGORY=simulation  # optional
#   LIMIT=10                    # optional, 0 means no limit
#   CLASSIFICATION_ENABLED=true # optional; default inherited by runner
#   CLASSIFICATION_MODEL=gemma3:4b
#   CLASSIFICATION_TIMEOUT=300
#   FAST_FIRST=true             # defer repos with many notebooks
#   FAST_FIRST_MAX_NOTEBOOKS=4
#   NOTEBOOK_TIMEOUT_SECONDS=1200
#   ALLOW_DUPLICATE=1           # bypass existing-worker protection
###############################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER="$SCRIPT_DIR/run_full_sample.sh"

WORKER_COUNT="${WORKER_COUNT:-4}"
ALLOW_DUPLICATE="${ALLOW_DUPLICATE:-0}"

if ! [[ "$WORKER_COUNT" =~ ^[0-9]+$ ]] || [ "$WORKER_COUNT" -lt 1 ]; then
    echo "[ERROR] WORKER_COUNT must be a positive integer (got: $WORKER_COUNT)"
    exit 1
fi

if [ ! -f "$RUNNER" ]; then
    echo "[ERROR] Runner not found: $RUNNER"
    exit 1
fi

if [ "$ALLOW_DUPLICATE" != "1" ]; then
    existing="$(pgrep -af "run_full_sample.sh" || true)"
    if [ -n "$existing" ]; then
        echo "[ERROR] run_full_sample.sh already appears to be running:"
        echo "$existing"
        echo ""
        echo "Use ALLOW_DUPLICATE=1 only if you are sure this is a stale/irrelevant match."
        exit 1
    fi
fi

mkdir -p "$PROJECT_ROOT/data/output/launcher"
RUN_ID="$(date '+%Y%m%d-%H%M%S')"
RUN_DIR="$PROJECT_ROOT/data/output/launcher/full-sample-$RUN_ID"
mkdir -p "$RUN_DIR"

echo "[START] Project      : $PROJECT_ROOT"
echo "[START] Runner       : $RUNNER"
echo "[START] Workers      : $WORKER_COUNT"
echo "[START] Launcher dir : $RUN_DIR"
echo "[START] Filters      : platform=${FILTER_PLATFORM:-<all>} category=${FILTER_CATEGORY:-<all>} limit=${LIMIT:-0}"
echo ""

PIDS_FILE="$RUN_DIR/pids.txt"
: > "$PIDS_FILE"

for i in $(seq 0 $((WORKER_COUNT - 1))); do
    log_file="$RUN_DIR/worker${i}.out"
    env \
        WORKER_COUNT="$WORKER_COUNT" \
        WORKER_INDEX="$i" \
        FILTER_PLATFORM="${FILTER_PLATFORM:-}" \
        FILTER_CATEGORY="${FILTER_CATEGORY:-}" \
        LIMIT="${LIMIT:-0}" \
        CLASSIFICATION_ENABLED="${CLASSIFICATION_ENABLED:-false}" \
        CLASSIFICATION_MODEL="${CLASSIFICATION_MODEL:-gemma3:4b}" \
        CLASSIFICATION_TIMEOUT="${CLASSIFICATION_TIMEOUT:-300}" \
        FAST_FIRST="${FAST_FIRST:-false}" \
        FAST_FIRST_MAX_NOTEBOOKS="${FAST_FIRST_MAX_NOTEBOOKS:-0}" \
        NOTEBOOK_TIMEOUT_SECONDS="${NOTEBOOK_TIMEOUT_SECONDS:-1800}" \
        nohup bash "$RUNNER" > "$log_file" 2>&1 &
    pid="$!"
    echo "$pid worker${i} $log_file" | tee -a "$PIDS_FILE"
done

echo "$RUN_DIR" > "$PROJECT_ROOT/data/output/launcher/latest_full_sample_run.txt"

echo ""
echo "[START] Launched workers. They are detached and will keep running."
echo "[START] Progress files:"
for i in $(seq 0 $((WORKER_COUNT - 1))); do
    echo "  $PROJECT_ROOT/data/output/full_run_progress_worker${i}.txt"
done
echo ""
echo "[START] Check worker processes:"
echo "  ps -p \$(awk '{print \$1}' '$PIDS_FILE' | paste -sd, -) -o pid,etime,cmd"
echo ""
echo "[START] Check sample totals from the database:"
echo "  sqlite3 -header -column '$PROJECT_ROOT/data/output/db/db.sqlite' \"SELECT run_status, COUNT(*) FROM repository_runs GROUP BY run_status;\""
