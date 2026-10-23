#!/usr/bin/env bash
###############################################################################
# classify-only.sh — reclassify one notebook without a full pipeline run.
#
# Skips dependency install / pyenv / execution entirely: just validates the
# source, clones (or downloads, for Zenodo) if not already present, and runs
# classify_repository_notebooks() — the same rule/LLM classification step the
# full pipeline uses, writing into the same real notebook_classifications
# table. Much faster than a full rerun when only the classification result is
# needed.
#
# Required env: WEB_REPO_URL, WEB_NOTEBOOK_PATHS, WEB_JOB_DIR
###############################################################################
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

: "${WEB_REPO_URL:?WEB_REPO_URL is required}"
: "${WEB_JOB_DIR:?WEB_JOB_DIR is required}"
SKIP_CLASSIFY="${SKIP_CLASSIFY:-false}"
if [ "$SKIP_CLASSIFY" != "true" ]; then
    : "${WEB_NOTEBOOK_PATHS:?WEB_NOTEBOOK_PATHS is required}"
fi
WEB_NOTEBOOK_PATHS="${WEB_NOTEBOOK_PATHS:-}"

LOG_FILE=""
source "$PROJECT_ROOT/config/config.sh"
source "$PROJECT_ROOT/src/logging.sh"
source "$PROJECT_ROOT/src/checks.sh"
source "$PROJECT_ROOT/src/db.sh"
source "$PROJECT_ROOT/src/notebooks.sh"
source "$PROJECT_ROOT/src/classification.sh"
source "$PROJECT_ROOT/src/repo.sh"

OUTPUT_DB_DIR="$PROJECT_ROOT/data/output/db"
OUTPUT_DB_FILE="$OUTPUT_DB_DIR/db.sqlite"
DB_FILE="$OUTPUT_DB_FILE"
CLASSIFICATION_ENABLED=true
CLASSIFICATION_RULE_ONLY="${CLASSIFICATION_RULE_ONLY:-true}"

export PROJECT_ROOT OUTPUT_DB_DIR OUTPUT_DB_FILE DB_FILE CLASSIFICATION_ENABLED CLASSIFICATION_RULE_ONLY
export PYTHONPATH="${PROJECT_ROOT}${PYTHONPATH:+:$PYTHONPATH}"

# REPOS_DIR normally defaults under WEB_JOB_DIR (disposable scratch space,
# often a WSL-native /tmp path for speed — see runner_server.py). Acquisition
# calls that need the clone to actually persist (SKIP_CLASSIFY=true, used to
# attach an uploaded notebook to a repo) pass REPOS_DIR_OVERRIDE to land it in
# the real, permanent data/output/cloned_repos/ instead.
REPOS_DIR="${REPOS_DIR_OVERRIDE:-$WEB_JOB_DIR/output/cloned_repos}"
LOG_DIR="$WEB_JOB_DIR/output/logs"
mkdir -p "$WEB_JOB_DIR/work" "$REPOS_DIR" "$LOG_DIR"
cd "$WEB_JOB_DIR/work"

initialize_directories
ensure_pipeline_tables

REPO_URL="$WEB_REPO_URL"
NOTEBOOK_PATHS="$WEB_NOTEBOOK_PATHS"

REPO_ID="$(get_or_create_repo_id "$REPO_URL")"
if ! [[ "$REPO_ID" =~ ^[1-9][0-9]*$ ]]; then
    echo "[CLASSIFY-ONLY] Could not get/create a repository id for $REPO_URL" >&2
    exit 1
fi
export REPO_ID

REPO_NAME="${REPO_ID}_$(basename "${REPO_URL%/}" .git)"
REPO_DIR="$REPOS_DIR/$REPO_NAME"
LOG_FILE="$LOG_DIR/${REPO_NAME}.log"
: > "$LOG_FILE"
export LOG_FILE REPO_DIR

insert_notebooks_from_paths "$REPO_ID" "$NOTEBOOK_PATHS"

PLATFORM=$(detect_platform "$REPO_URL")
if [ "$PLATFORM" = "zenodo" ]; then
    validate_zenodo "$REPO_URL" || { echo "[CLASSIFY-ONLY] Invalid Zenodo record" >&2; exit 1; }
    if [ ! -d "$REPO_DIR" ]; then
        fetch_zenodo "$REPO_URL" "$REPO_DIR" >>"$LOG_FILE" 2>&1
    fi
else
    validate_repo "$REPO_URL" || { echo "[CLASSIFY-ONLY] Invalid repository URL" >&2; exit 1; }
    if [ -d "$REPO_DIR" ]; then
        git -C "$REPO_DIR" pull >>"$LOG_FILE" 2>&1 || true
    else
        git clone --depth 1 "$REPO_URL" "$REPO_DIR" >>"$LOG_FILE" 2>&1
    fi
fi

if [ ! -d "$REPO_DIR" ]; then
    echo "[CLASSIFY-ONLY] Repository directory missing after acquisition" >&2
    exit 1
fi

if [ "$SKIP_CLASSIFY" != "true" ]; then
    classify_repository_notebooks "$REPO_ID" "$REPO_DIR" "$NOTEBOOK_PATHS"
fi
echo "REPO_DIR=$REPO_DIR"
echo "[CLASSIFY-ONLY] Done."
