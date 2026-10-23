#!/bin/bash
###############################################################################
# Wait for the current fast-first worker run to finish, then automatically
# restart the deferred long repositories.
#
# This is meant to be launched once before leaving the PC on:
#   bash pipeline/start_long_after_current_short_pass.sh
#
# Defaults are conservative for long notebooks:
#   LONG_WORKER_COUNT=1
#   LONG_NOTEBOOK_TIMEOUT_SECONDS=7200
###############################################################################
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER="$SCRIPT_DIR/run_full_sample.sh"

LONG_WORKER_COUNT="${LONG_WORKER_COUNT:-1}"
LONG_NOTEBOOK_TIMEOUT_SECONDS="${LONG_NOTEBOOK_TIMEOUT_SECONDS:-7200}"
CLASSIFICATION_ENABLED="${CLASSIFICATION_ENABLED:-true}"
CLASSIFICATION_MODEL="${CLASSIFICATION_MODEL:-gemma3:4b}"
CLASSIFICATION_TIMEOUT="${CLASSIFICATION_TIMEOUT:-300}"

mkdir -p "$PROJECT_ROOT/data/output/launcher"
WATCH_ID="$(date '+%Y%m%d-%H%M%S')"
WATCH_DIR="$PROJECT_ROOT/data/output/launcher/long-after-short-$WATCH_ID"
mkdir -p "$WATCH_DIR"
WATCH_LOG="$WATCH_DIR/watcher.out"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$WATCH_LOG"
}

current_run_dir=""
if [ -f "$PROJECT_ROOT/data/output/launcher/latest_full_sample_run.txt" ]; then
    current_run_dir="$(cat "$PROJECT_ROOT/data/output/launcher/latest_full_sample_run.txt")"
fi

log "[WATCH] Project: $PROJECT_ROOT"
log "[WATCH] Current run dir: ${current_run_dir:-<none>}"

if [ -n "$current_run_dir" ] && [ -f "$current_run_dir/pids.txt" ]; then
    mapfile -t pids < <(awk '{print $1}' "$current_run_dir/pids.txt")
    log "[WATCH] Waiting for current fast-first workers: ${pids[*]}"
    for pid in "${pids[@]}"; do
        while kill -0 "$pid" 2>/dev/null; do
            sleep 60
        done
        log "[WATCH] Worker finished: $pid"
    done
else
    log "[WATCH] No current pids.txt found; continuing directly to long phase."
fi

log "[WATCH] Fast-first pass appears finished. Preparing deferred long-repo pass."

source "$HOME/pipeline_env.sh" 2>/dev/null || true
sqlite3 -cmd ".timeout 30000" "$PROJECT_ROOT/data/output/db/db.sqlite" \
    "DELETE FROM repository_runs WHERE run_status='DEFERRED_LONG_REPO';"

LONG_RUN_DIR="$PROJECT_ROOT/data/output/launcher/long-pass-$WATCH_ID"
mkdir -p "$LONG_RUN_DIR"
: > "$LONG_RUN_DIR/pids.txt"

log "[WATCH] Starting long phase with $LONG_WORKER_COUNT worker(s)."
log "[WATCH] Long notebook timeout: ${LONG_NOTEBOOK_TIMEOUT_SECONDS}s"

for i in $(seq 0 $((LONG_WORKER_COUNT - 1))); do
    worker_log="$LONG_RUN_DIR/worker${i}.out"
    env \
        WORKER_COUNT="$LONG_WORKER_COUNT" \
        WORKER_INDEX="$i" \
        LIMIT=0 \
        CLASSIFICATION_ENABLED="$CLASSIFICATION_ENABLED" \
        CLASSIFICATION_MODEL="$CLASSIFICATION_MODEL" \
        CLASSIFICATION_TIMEOUT="$CLASSIFICATION_TIMEOUT" \
        FAST_FIRST=false \
        FAST_FIRST_MAX_NOTEBOOKS=0 \
        NOTEBOOK_TIMEOUT_SECONDS="$LONG_NOTEBOOK_TIMEOUT_SECONDS" \
        nohup bash "$RUNNER" > "$worker_log" 2>&1 &
    pid="$!"
    echo "$pid worker${i} $worker_log" | tee -a "$LONG_RUN_DIR/pids.txt" >> "$WATCH_LOG"
    log "[WATCH] Started long worker $i as PID $pid"
done

echo "$LONG_RUN_DIR" > "$PROJECT_ROOT/data/output/launcher/latest_long_sample_run.txt"
log "[WATCH] Long phase launched and detached: $LONG_RUN_DIR"
