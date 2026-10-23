#!/bin/bash
###############################################################################
# config.sh — Central configuration for the CPRPMC pipeline
###############################################################################
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Output folders can be set from the environment (e.g. one set per SLURM array task);
# the defaults are unchanged.
INPUT_DIR="$PROJECT_ROOT/data/input"
OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_ROOT/data/output}"
REPOS_DIR="${REPOS_DIR:-$OUTPUT_DIR/cloned_repos}"
COMP_DIR="${COMP_DIR:-$OUTPUT_DIR/comparisons}"
LOG_DIR="${LOG_DIR:-$OUTPUT_DIR/logs}"

# Input DB — the authors’ original repo list, READ ONLY
DB_DIR="$PROJECT_ROOT/data"
SOURCE_DB_FILE="$DB_DIR/db.sqlite"

# Output DB — created fresh by pipeline, stores all execution results
OUTPUT_DB_DIR="$PROJECT_ROOT/data/output/db"
OUTPUT_DB_FILE="$OUTPUT_DB_DIR/db.sqlite"
# data/output/db/db.sqlite is the frozen thesis evaluation database (read-only).
# Re-runs set DB_FILE to a working copy, e.g. DB_FILE=data/output/db/work/step1.sqlite.
DB_FILE="${DB_FILE:-$OUTPUT_DB_FILE}"

TARGET_COUNT="${TARGET_COUNT:-10}"

# Per-run artefacts (original and executed notebooks, install list, pip freeze, comparisons),
# kept outside the resource folder; and the pinned Jupyter tool environment that drives the
# kernels (separate from every resource venv; config/tool-requirements.txt).
ARTIFACT_DIR="${ARTIFACT_DIR:-$OUTPUT_DIR/artifacts}"
TOOL_ENV_DIR="${TOOL_ENV_DIR:-$HOME/.notebookfair/tool-env}"
TOOL_ENV_PYTHON="${TOOL_ENV_PYTHON:-3.11}"
# Time travel: pip only sees releases uploaded before the resource's version date
# (src/install_env.py). Set to false to install packages as of today.
PIP_TIME_TRAVEL="${PIP_TIME_TRAVEL:-true}"
# Every pip call of the pipeline ignores the machine's pip configuration and uses PyPI, which has the
# upload-time metadata the time travel needs (the 2026-10 study run on the HPC went through a mirror without it,
# configured on the cluster, behind a proxy that often failed). Proxies (https_proxy) are kept.
NBF_PIP_INDEX_URL="${NBF_PIP_INDEX_URL:-https://pypi.org/simple}"
unset PIP_EXTRA_INDEX_URL PIP_FIND_LINKS PIP_TRUSTED_HOST PIP_NO_INDEX
export PIP_CONFIG_FILE=/dev/null PIP_INDEX_URL="$NBF_PIP_INDEX_URL" PIP_RETRIES="${PIP_RETRIES:-10}" \
       PIP_DEFAULT_TIMEOUT="${PIP_DEFAULT_TIMEOUT:-60}" NBF_PIP_INDEX_URL

# Python version selection (src/python_version.py): default when nothing in the
# resource says otherwise (3.10, as in the thesis run), and the range pyenv can
# build on this host; requests outside it are moved to the nearest end.
PYTHON_DEFAULT_VERSION="${PYTHON_DEFAULT_VERSION:-3.10}"
PYTHON_MIN_SUPPORTED="${PYTHON_MIN_SUPPORTED:-3.8}"
PYTHON_MAX_SUPPORTED="${PYTHON_MAX_SUPPORTED:-3.14}"

CLASSIFICATION_ENABLED="${CLASSIFICATION_ENABLED:-false}"  # the classifier is not part of this package; the study ran without it
# ReproScore stage (src/reproscore_stage.py): readiness (RRS) on the fresh clone, outcome (ROS/RCS)
# from the run's records. ReproScore is part of this repository (third_party/reproscore, see VENDORED.md);
# ROS/RCS use the rubric config/reproscore_rubric.yaml (adds the output-reproduction component R).
REPROSCORE_ENABLED="${REPROSCORE_ENABLED:-true}"
REPROSCORE_DIR="${REPROSCORE_DIR:-$PROJECT_ROOT/third_party/reproscore}"                  # 1.1
REPROSCORE_BASELINE_DIR="${REPROSCORE_BASELINE_DIR-$PROJECT_ROOT/third_party/reproscore-1.0}"  # paper version; '' = off
REPROSCORE_RUBRIC="${REPROSCORE_RUBRIC:-$PROJECT_ROOT/config/reproscore_rubric.yaml}"
export REPROSCORE_DIR REPROSCORE_BASELINE_DIR REPROSCORE_RUBRIC
CLASSIFICATION_RULE_ONLY="${CLASSIFICATION_RULE_ONLY:-false}"
CLASSIFICATION_LLM_ONLY="${CLASSIFICATION_LLM_ONLY:-false}"
if [ "$CLASSIFICATION_RULE_ONLY" = "true" ] && [ "$CLASSIFICATION_LLM_ONLY" = "true" ]; then
    echo "[ERROR] CLASSIFICATION_RULE_ONLY and CLASSIFICATION_LLM_ONLY cannot both be true." >&2
    echo "        At least one classification method must run; set at most one of them to true." >&2
    exit 1
fi
CLASSIFICATION_MODEL="${CLASSIFICATION_MODEL:-gemma3:4b}"
CLASSIFICATION_OLLAMA_URL="${CLASSIFICATION_OLLAMA_URL:-http://localhost:11434}"
CLASSIFICATION_TIMEOUT="${CLASSIFICATION_TIMEOUT:-600}"
CLASSIFICATION_CONFIDENCE_THRESHOLD="${CLASSIFICATION_CONFIDENCE_THRESHOLD:-0.55}"

export GIT_TERMINAL_PROMPT=0

initialize_directories() {
    mkdir -p "$INPUT_DIR"
    mkdir -p "$REPOS_DIR" "$COMP_DIR" "$LOG_DIR"
    mkdir -p "$OUTPUT_DB_DIR"
    log "[INIT] Initialized directory structure"
}

ensure_working_db() {
    if [ ! -f "$DB_FILE" ]; then
        if [ -f "$SOURCE_DB_FILE" ]; then
            cp "$SOURCE_DB_FILE" "$DB_FILE"
            log "[INIT] Working DB created from $SOURCE_DB_FILE"
        else
            log "[INIT] No source DB found; starting with empty working DB"
        fi
    fi
}

export PROJECT_ROOT INPUT_DIR OUTPUT_DIR REPOS_DIR COMP_DIR LOG_DIR \
       DB_DIR SOURCE_DB_FILE OUTPUT_DB_DIR OUTPUT_DB_FILE DB_FILE TARGET_COUNT \
       CLASSIFICATION_ENABLED CLASSIFICATION_RULE_ONLY CLASSIFICATION_LLM_ONLY CLASSIFICATION_MODEL \
       CLASSIFICATION_OLLAMA_URL CLASSIFICATION_TIMEOUT \
       CLASSIFICATION_CONFIDENCE_THRESHOLD PYTHON_DEFAULT_VERSION PYTHON_MIN_SUPPORTED PYTHON_MAX_SUPPORTED \
       ARTIFACT_DIR TOOL_ENV_DIR TOOL_ENV_PYTHON PIP_TIME_TRAVEL
