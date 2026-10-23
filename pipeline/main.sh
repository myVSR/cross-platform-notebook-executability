#!/bin/bash
###############################################################################
# CPRPMC Reproducibility Pipeline — Main Orchestrator
#
# Author:      the authors <anonymous@example.org>
# Co-authors:  Vasundhara Shaw
# Institution: an anonymised institution
# License:     GPL-3.0
#
# Usage:
#   From repo root:  ./run.sh
#   Directly:        bash pipeline/main.sh
###############################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$PROJECT_ROOT/config/config.sh"
source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/checks.sh"
source "$PROJECT_ROOT/src/db.sh"
source "$PROJECT_ROOT/src/pyenv.sh"
source "$PROJECT_ROOT/src/requirements.sh"
source "$PROJECT_ROOT/src/notebooks.sh"
source "$PROJECT_ROOT/src/classification.sh"
source "$PROJECT_ROOT/src/repo.sh"

RUN_ID=""

trap finalize_active_run_on_exit EXIT

export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"

initialize_directories
ensure_working_db

log "[MAIN] Starting pipeline..."
log "[MAIN] PROJECT_ROOT : $PROJECT_ROOT"
log "[MAIN] Database     : $DB_FILE"
log "[MAIN] Logs         : $LOG_DIR"

ensure_pipeline_tables

prompt_for_input() {
    read -p "Enter repo URL: " REPO_URL
    read -p "Enter notebook paths (semicolon-separated): " NOTEBOOK_PATHS
    read -p "Enter setup paths (semicolon-separated, optional): " SETUP_PATHS
    read -p "Enter requirements paths (semicolon-separated, optional): " REQUIREMENT_PATHS
}

ollama_server_available() {
    command -v curl >/dev/null 2>&1 || return 1
    curl -fsS "$CLASSIFICATION_OLLAMA_URL/api/tags" >/dev/null 2>&1
}

ollama_model_available() {
    command -v curl >/dev/null 2>&1 || return 1
    curl -fsS "$CLASSIFICATION_OLLAMA_URL/api/tags" 2>/dev/null \
        | python3 -c 'import json,sys; data=json.load(sys.stdin); wanted=sys.argv[1]; names={m.get("name") for m in data.get("models", [])}; raise SystemExit(0 if wanted in names else 1)' "$CLASSIFICATION_MODEL" \
        >/dev/null 2>&1
}

local_llm_available() {
    ollama_server_available && ollama_model_available
}

start_ollama_server() {
    ollama_server_available && return 0
    command -v ollama >/dev/null 2>&1 || return 1

    log "[CLASSIFICATION] Starting Ollama server in the background..."
    nohup ollama serve >> "$LOG_DIR/ollama.log" 2>&1 &

    local attempt
    for attempt in $(seq 1 20); do
        if ollama_server_available; then
            return 0
        fi
        sleep 1
    done

    return 1
}

install_or_prepare_local_llm() {
    if ! command -v ollama >/dev/null 2>&1; then
        log "[CLASSIFICATION] Installing Ollama..."
        curl -fsSL https://ollama.com/install.sh | sh
    fi

    start_ollama_server || return 1

    if ! ollama_model_available; then
        log "[CLASSIFICATION] Pulling Ollama model: $CLASSIFICATION_MODEL"
        ollama pull "$CLASSIFICATION_MODEL"
    fi

    local_llm_available
}

offer_local_llm_setup() {
    local required="${1:-false}"

    if local_llm_available; then
        log "[CLASSIFICATION] Local LLM is available: $CLASSIFICATION_MODEL"
        return 0
    fi

    echo ""
    echo "Local LLM classification is not available."
    echo "It needs Ollama running locally with model: $CLASSIFICATION_MODEL"
    read -p "Do you want to install/setup Ollama and pull the model now? (y/N): " install_choice

    case "$install_choice" in
        y|Y|yes|YES)
            if install_or_prepare_local_llm; then
                log "[CLASSIFICATION] Local LLM is ready: $CLASSIFICATION_MODEL"
                return 0
            fi
            echo "[ERROR] Ollama setup did not complete successfully."
            if [ "$required" = "true" ]; then
                exit 1
            fi
            log "[CLASSIFICATION] Falling back to rule-based only"
            CLASSIFICATION_RULE_ONLY=true
            CLASSIFICATION_LLM_ONLY=false
            return 0
            ;;
        *)
            if [ "$required" = "true" ]; then
                echo "[ERROR] Local LLM only was selected, but the local LLM is not available."
                exit 1
            fi
            log "[CLASSIFICATION] Local LLM unavailable; falling back to rule-based only"
            CLASSIFICATION_RULE_ONLY=true
            CLASSIFICATION_LLM_ONLY=false
            return 0
            ;;
    esac
}

configure_classification_mode() {
    echo ""
    echo "How should notebooks be classified?"
    echo "  1. Rule-based only"
    echo "  2. Rule-based + local LLM if available"
    echo "  3. Local LLM only"
    echo "  4. Disable classification"
    echo ""
    read -p "Enter your choice (1, 2, 3, or 4): " classification_choice

    CLASSIFICATION_ENABLED=true
    CLASSIFICATION_RULE_ONLY=false
    CLASSIFICATION_LLM_ONLY=false

    case "$classification_choice" in
        1)
            CLASSIFICATION_RULE_ONLY=true
            log "[CLASSIFICATION] Mode selected: rule-based only"
            ;;
        2)
            log "[CLASSIFICATION] Mode selected: rule-based + local LLM if available"
            offer_local_llm_setup false
            ;;
        3)
            CLASSIFICATION_LLM_ONLY=true
            log "[CLASSIFICATION] Mode selected: local LLM only"
            offer_local_llm_setup true
            ;;
        4)
            CLASSIFICATION_ENABLED=false
            log "[CLASSIFICATION] Mode selected: disabled"
            ;;
        *)
            echo "[ERROR] Invalid classification choice. Please enter 1, 2, 3, or 4."
            exit 1
            ;;
    esac

    export CLASSIFICATION_ENABLED CLASSIFICATION_RULE_ONLY CLASSIFICATION_LLM_ONLY
}

print_run_summary() {
    local elapsed=$(( $(date +%s) - $1 ))
    local total success failed
    total=$(sqlite3   "$DB_FILE" "SELECT COUNT(*) FROM repository_runs;")
    success=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM repository_runs WHERE run_status = 'SUCCESS';")
    failed=$(sqlite3  "$DB_FILE" "SELECT COUNT(*) FROM repository_runs WHERE run_status NOT IN ('SUCCESS');")

    local classification_mode
    if [ "${CLASSIFICATION_ENABLED:-true}" != "true" ]; then
        classification_mode="Disabled"
    elif [ "${CLASSIFICATION_RULE_ONLY:-false}" = "true" ]; then
        classification_mode="Rule-based only"
    elif [ "${CLASSIFICATION_LLM_ONLY:-false}" = "true" ]; then
        classification_mode="Local LLM only"
    else
        classification_mode="Rule-based + local LLM"
    fi

    local score_count avg_score reproducibility_summary
    score_count=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM notebook_reproducibility_metrics WHERE reproducibility_score IS NOT NULL;")
    if [ "$score_count" -gt 0 ]; then
        avg_score=$(sqlite3 "$DB_FILE" "SELECT ROUND(AVG(reproducibility_score) * 100, 1) FROM notebook_reproducibility_metrics WHERE reproducibility_score IS NOT NULL;")
        local plural=""
        [ "$score_count" -ne 1 ] && plural="s"
        reproducibility_summary="${avg_score}% avg (${score_count} notebook${plural})"
    else
        reproducibility_summary="N/A (no notebooks executed yet)"
    fi

    echo ""
    echo "════════════════════════════════════════"
    echo "        PIPELINE RUN SUMMARY            "
    echo "════════════════════════════════════════"
    echo "  Total runs in DB  : $total"
    echo "  Successful        : $success"
    echo "  Failed/Skipped    : $failed"
    echo "  Classification    : $classification_mode"
    echo "  Reproducibility   : $reproducibility_summary"
    echo "  Elapsed time      : ${elapsed}s"
    echo "  Results stored in : $DB_FILE"
    echo "  Logs directory    : $LOG_DIR"
    echo "════════════════════════════════════════"
    echo ""
}

RUN_START=$(date +%s)

echo ""
echo "How would you like to run the pipeline?"
echo "  1. Single repo  — enter a repo URL interactively"
echo "  2. Batch mode   — process repos from the SQLite database"
echo ""
read -p "Enter your choice (1 or 2): " choice

if [ "$choice" -eq 1 ]; then
    configure_classification_mode
    prompt_for_input
    REPO_ID=$(get_or_create_repo_id "$REPO_URL")
    export REPO_ID
    process_repo "$REPO_URL" "$NOTEBOOK_PATHS" "$SETUP_PATHS" "$REQUIREMENT_PATHS"
elif [ "$choice" -eq 2 ]; then
    configure_classification_mode
    process_sqlite_flow
else
    echo "[ERROR] Invalid choice. Please enter 1 or 2."
    exit 1
fi

print_run_summary "$RUN_START"
