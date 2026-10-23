#!/bin/bash
###############################################################################
# repo.sh — Per-repository orchestration and batch SQLite flow
###############################################################################

export REPO_URL

source "$(dirname "${BASH_SOURCE[0]}")/usage.sh"

detect_platform() {
    case "$1" in
        *github.com*) echo "github" ;;
        *codeberg.org*) echo "codeberg" ;;
        *zenodo.org*) echo "zenodo" ;;
        *) echo "unknown" ;;
    esac
}

trim_whitespace() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

# A notebook path of the resource as it is on disk: the path itself if that file exists (file names may begin or end
# with a space), otherwise without surrounding whitespace (supplied lists such as "a.ipynb; b.ipynb").
# (Study run 2026-10-11: " CSC1185-Week2-Kocharekar-Atharva.ipynb" was trimmed and then not found.)
notebook_rel_path() {
    local path="${1//$'\r'/}"
    if [ -n "${REPO_DIR:-}" ] && [ -f "$REPO_DIR/$path" ]; then
        printf '%s' "$path"
    else
        trim_whitespace "$path"
    fi
}

# Free space on the node-local scratch filesystems while a resource runs. The disk guard of
# pipeline/run_full_sample.sh only checks before a resource starts; a resource (or another task on the same node)
# can fill the disk during its run. Notebooks then fail for lack of space and their results cannot be written
# (study run 2026-10-11: 4 resources finalised as SUCCESS with 24 notebooks unrecorded, one Zenodo download failed).
# disk_check STAGE sets DISK_LOW (kept for the rest of the run) when less than DISK_MIN_FREE_MB is free;
# finalize_repository_run then records the run as DISK_FULL, a pipeline error that the next top-up runs again.
DISK_MIN_FREE_MB="${DISK_MIN_FREE_MB:-1024}"
disk_check() {
    local stage="$1" dir free
    for dir in "${REPO_DIR:-}" "${REPO_VENV_DIR:-}" "${RUN_ARTIFACT_DIR:-}" "${TMPDIR:-/tmp}"; do
        [ -n "$dir" ] && [ -e "$dir" ] || continue
        free=$(df -Pk "$dir" 2>/dev/null | awk 'NR==2 {print int($4 / 1024)}')
        if [[ "$free" =~ ^[0-9]+$ ]] && [ "$free" -lt "$DISK_MIN_FREE_MB" ]; then
            [ -n "${DISK_LOW:-}" ] || DISK_LOW="$stage: ${free} MB free on $(df -P "$dir" 2>/dev/null | awk 'NR==2 {print $6}')"
            log "[DISK] low disk space at $stage: ${free} MB free for $dir"
            return 1
        fi
    done
    return 0
}

create_repository_run() {
    local repository_id="$1"
    local repository_url_sql

    [[ "$repository_id" =~ ^[0-9]+$ ]] || return 1
    repository_url_sql=$(sql_escape "$2")

    RUN_ID=$(
        sqlite3 "$DB_FILE" <<EOF
INSERT INTO repository_runs (repository_id, url, run_status, started_at)
VALUES ($repository_id, '$repository_url_sql', 'RUNNING', datetime('now'));
SELECT last_insert_rowid();
EOF
    )
    [ -n "$RUN_ID" ] || return 1
    export RUN_ID
}

finalize_repository_run() {
    local run_id="$1"
    local status_sql
    local error_message_sql
    local duration="$4"

    [[ "$run_id" =~ ^[0-9]+$ ]] || return 1
    [[ "$duration" =~ ^[0-9]+([.][0-9]+)?$ ]] || duration=0
    local status="$2" message="$3"
    disk_check finalize || true
    if [ -n "${DISK_LOW:-}" ] && [ "$status" != "DISK_FULL" ]; then
        message="Node-local disk space ran out during the run ($DISK_LOW); the run would have been recorded as $status: $message"
        status="DISK_FULL"
    fi
    status_sql=$(sql_escape "$status")
    error_message_sql=$(sql_escape "$message")

    log "[REPO] Finalizing run $run_id — status: $status"
    # an argument, not a here-document: bash needs a temporary file for that, which fails on a full disk
    sqlite3 "$DB_FILE" "UPDATE repository_runs
SET run_status='$status_sql', error_message='$error_message_sql', finished_at=datetime('now'), duration_seconds=$duration
WHERE id=$run_id;"
}

finalize_active_run_on_exit() {
    local exit_code="$?"
    local current_status=""
    local duration=0

    if [ -n "${RUN_ID:-}" ] &&
       [[ "$RUN_ID" =~ ^[0-9]+$ ]] &&
       [ -f "${DB_FILE:-}" ]; then

        current_status=$(sqlite3 "$DB_FILE" \
            "SELECT run_status FROM repository_runs WHERE id=$RUN_ID;")

        if [ "$current_status" = "RUNNING" ]; then
            if [ -n "${REPO_START_TIME:-}" ]; then
                duration=$(elapsed_sec "$REPO_START_TIME")
            fi

            finalize_repository_run \
                "$RUN_ID" \
                "PIPELINE_INTERRUPTED" \
                "Pipeline stopped unexpectedly with exit code $exit_code" \
                "$duration" || true
        fi
    fi

    trap - EXIT
    exit "$exit_code"
}

get_notebook_language_stats() {
    [[ "$1" =~ ^[0-9]+$ ]] || return 1
    sqlite3 "$DB_FILE" <<EOF
SELECT COUNT(*), SUM(CASE WHEN LOWER(language)='python' THEN 1 ELSE 0 END)
FROM notebooks WHERE repository_id=$1;
EOF
}

# Find a repository in the DB by its path, or insert it if it's new.
# Also detects and stores which platform the URL came from.
# Outputs the repository's row id.
get_or_create_repo_id() {
    local repo_path
    # host removed, then only Zenodo's leading records/ (an owner or repository named e.g. medical-records must stay)
    repo_path=$(echo "$1" | sed -E 's#^https?://[^/]+/##; s#^records?/##; s#\.git$##')
    local platform
    platform=$(detect_platform "$1")
    local repo_path_sql
    local platform_sql
    repo_path_sql=$(sql_escape "$repo_path")
    platform_sql=$(sql_escape "$platform")
    local notebook_paths_sql
    local setup_paths_sql
    local requirement_paths_sql

    notebook_paths_sql=$(sql_escape "${NOTEBOOK_PATHS:-}")
    setup_paths_sql=$(sql_escape "${SETUP_PATHS:-}")
    requirement_paths_sql=$(sql_escape "${REQUIREMENT_PATHS:-}")
    local existing_id
    existing_id=$(sqlite3 "$DB_FILE" \
        "SELECT id FROM repositories
         WHERE repository='$repo_path_sql'
           AND platform='$platform_sql'
         LIMIT 1;")
    if [ -n "$existing_id" ]; then
        echo "$existing_id"
        return 0
    fi
    sqlite3 "$DB_FILE" <<EOF
INSERT INTO repositories (repository, platform, notebooks, setups, requirements, notebooks_count, setups_count, requirements_count)
VALUES ('$repo_path_sql', '$platform_sql', '$notebook_paths_sql', '$setup_paths_sql', '$requirement_paths_sql', 0, 0, 0);
SELECT last_insert_rowid();
EOF
}

# Populates the notebooks table from user-supplied paths (single-repo mode).
# Accepts platform notebook URLs or plain relative paths.
# In batch mode the notebooks are already in the DB, so this is a safe no-op.
insert_notebooks_from_paths() {
    local repo_id="$1"
    local notebook_paths="$2"

    [[ "$repo_id" =~ ^[0-9]+$ ]] || return 1
    [ -z "$notebook_paths" ] && return 0

    IFS=';' read -ra nb_array <<<"$notebook_paths"
    for nb_path in "${nb_array[@]}"; do
        nb_path=$(notebook_rel_path "$nb_path")
        [ -z "$nb_path" ] && continue

        # Strip platform notebook URLs down to relative paths
        # handles GitHub /blob/ URLs and Codeberg /src/ URLs
        if [[ "$nb_path" == https://github.com/* || "$nb_path" == https://codeberg.org/* ]]; then
            nb_path=$(echo "$nb_path" | sed -E 's|https?://[^/]+/[^/]+/[^/]+/(blob|src)/[^/]+/||')
        fi
        # Only register .ipynb files
        [[ "$nb_path" != *.ipynb ]] && continue

        local nb_path_sql
        nb_path_sql=$(sql_escape "$nb_path")

        local existing
        existing=$(sqlite3 "$DB_FILE" \
            "SELECT id FROM notebooks WHERE repository_id=$repo_id AND name='$nb_path_sql' LIMIT 1;")
        if [ -z "$existing" ]; then
            sqlite3 "$DB_FILE" \
                "INSERT INTO notebooks (repository_id, name, language) VALUES ($repo_id, '$nb_path_sql', 'python');"
            log "[REPO] Registered notebook: $nb_path" >&2
        fi
    done

    sqlite3 "$DB_FILE" \
        "UPDATE repositories
         SET notebooks_count=(
             SELECT COUNT(*)
             FROM notebooks
             WHERE repository_id=$repo_id
         )
         WHERE id=$repo_id;"
}

cleanup_repository_directory() {
    local candidate="$1"
    local repos_root
    local resolved_candidate

    [ -n "$candidate" ] || return 1
    repos_root=$(realpath -m "$REPOS_DIR") || return 1
    resolved_candidate=$(realpath -m "$candidate") || return 1

    case "$resolved_candidate" in
        "$repos_root"/*) ;;
        *)
            log "[CLEANUP] Refusing path outside repository workspace: $candidate"
            return 1
            ;;
    esac

    if [ -e "$resolved_candidate" ] || [ -L "$resolved_candidate" ]; then
        rm -rf -- "$resolved_candidate"
        log "[CLEANUP] Removed repository folder: $resolved_candidate"
    fi
}

# One pipeline_sessions row per pipeline process (host, CPU, memory, OS,
# Python, pipeline commit, limits in force); every run points to it.
ensure_pipeline_session() {
    [[ "${PIPELINE_SESSION_ID:-}" =~ ^[0-9]+$ ]] && return 0
    PIPELINE_SESSION_ID=$(python3 "$PROJECT_ROOT/src/provenance.py" --db "$DB_FILE" session) || {
        log "[PROVENANCE] Could not record the pipeline session"
        PIPELINE_SESSION_ID=""
        return 0
    }
    export PIPELINE_SESSION_ID
    log "[PROVENANCE] Pipeline session $PIPELINE_SESSION_ID"
}

# Record which version of the resource was acquired and every file
# (Git: commit SHA and tree; Zenodo: record id/version/revision and the
# manifest written by src/zenodo_fetch.py). Usage: record_acquisition <seconds>
record_acquisition() {
    local -a source=()
    if [ "$PLATFORM" = "zenodo" ]; then
        [ -f "$REPO_DIR/.zenodo-acquisition.json" ] || return 0
        source=(--zenodo-manifest "$REPO_DIR/.zenodo-acquisition.json")
    fi
    python3 "$PROJECT_ROOT/src/provenance.py" --db "$DB_FILE" acquisition \
        --run-id "$RUN_ID" --repository-id "$REPO_ID" --platform "$PLATFORM" \
        --url "$REPO_URL" --dir "$REPO_DIR" --wall-seconds "$1" "${source[@]}" >>"$LOG_FILE" 2>&1 ||
        log "[PROVENANCE] Could not record the acquisition of $REPO_URL"
}

# git clone --depth 1 with retries when the forge rate-limits (HTTP 429).
# Codeberg allows 2,000 requests per 10 minutes and answers Retry-After: 600,
# which git does not expose, so the wait is GIT_RATE_LIMIT_WAIT seconds.
# Usage: git_clone_with_retry <url> <dir>; output goes to LOG_FILE.
git_clone_with_retry() {
    local url="$1" dir="$2" attempt errors
    errors=$(mktemp "${TMPDIR:-/tmp}/git-clone.XXXXXX")
    for attempt in 1 2 3; do
        if LC_ALL=C git clone --depth 1 "$url" "$dir" >>"$LOG_FILE" 2>"$errors"; then
            cat "$errors" >>"$LOG_FILE"; rm -f "$errors"
            return 0
        fi
        cat "$errors" >>"$LOG_FILE"
        rm -rf "$dir"
        if [ "$attempt" -lt 3 ] && grep -q "error: 429" "$errors"; then
            log "[REPO] Rate limited (HTTP 429); retrying in ${GIT_RATE_LIMIT_WAIT:-610}s"
            sleep "${GIT_RATE_LIMIT_WAIT:-610}"
            continue
        fi
        break
    done
    rm -f "$errors"
    return 1
}

# File-based licence with licensee (the detector behind GitHub's licence field,
# run in Docker; src/licence.py), stored in resource_licences next to the API
# value. Runs on the freshly acquired files; never fails a run.
record_licence_detection() {
    local result
    result=$(python3 "$PROJECT_ROOT/src/licence.py" "$REPO_DIR" --json 2>>"$LOG_FILE")
    python3 "$PROJECT_ROOT/src/provenance.py" --db "$DB_FILE" licence --run-id "$RUN_ID" \
        --result-json "$result" >>"$LOG_FILE" 2>&1 ||
        log "[PROVENANCE] Could not record the licence of $REPO_URL"
    log "[LICENCE] $(jq -r '"\(.status) \(.licence_file // "") \(.error // "")"' <<<"$result" 2>/dev/null)"
}

# ReproScore readiness (RRS) of the fresh clone, before anything is installed or executed
# (src/reproscore_stage.py; the clone is deleted after the run). Never fails a run.
record_readiness() {
    local result
    result=$(python3 "$PROJECT_ROOT/src/reproscore_stage.py" --db "$DB_FILE" rrs --run-id "$RUN_ID" \
        --repo-dir "$REPO_DIR" 2>>"$LOG_FILE") || { log "[REPROSCORE] readiness not recorded"; return 0; }
    log "[REPROSCORE] $(jq -r '"\(.status) RRS \(.rrs) (E \(.score_e) A \(.score_a) D \(.score_d) C \(.score_c) S \(.score_s)) \(.error // "")"' <<<"$result" 2>/dev/null)"
}

# Store the original notebooks (as acquired, gzip) in the run's artefact folder,
# so that original and executed versions can be compared later (e.g. on the website).
store_original_notebooks() {
    local nb
    IFS=';' read -ra _store_nbs <<<"$NOTEBOOK_PATHS"
    for nb in "${_store_nbs[@]}"; do
        nb=$(trim_whitespace "$nb")
        [ -n "$nb" ] && [ -f "$REPO_DIR/$nb" ] || continue
        mkdir -p "$RUN_ARTIFACT_DIR/original/$(dirname "$nb")"
        gzip -c "$REPO_DIR/$nb" > "$RUN_ARTIFACT_DIR/original/$nb.gz"
    done
    store_resource_files
}

# Store README, licence, citation and dependency files (gzip) under resource/ in the artefact
# folder: the clone is deleted after the run, but licence detection (licensee needs Docker,
# not available on the cluster) and classification (README) run afterwards from the artefacts.
store_resource_files() {
    local summary
    summary=$(python3 "$PROJECT_ROOT/src/resource_files.py" "$REPO_DIR" "$RUN_ARTIFACT_DIR" \
        --notebooks "$NOTEBOOK_PATHS" 2>>"$LOG_FILE") || { log "[ARTIFACTS] Could not store the resource files"; return 0; }
    log "[ARTIFACTS] Resource files stored: $(jq -r '"\(.stored | length) files, \(.bytes) bytes, \(.skipped | length) skipped"' <<<"$summary" 2>/dev/null)"
}

# After a run: compress executed notebooks and comparisons, record every stored file,
# and explain each ModuleNotFoundError/ImportError from the dependency records.
finish_run_artifacts() {
    [ -n "${RUN_ARTIFACT_DIR:-}" ] && [ -d "$RUN_ARTIFACT_DIR" ] || return 0
    find "$RUN_ARTIFACT_DIR/executed" "$RUN_ARTIFACT_DIR/comparisons" -type f ! -name '*.gz' \
        -exec gzip -f {} + 2>/dev/null || true          # the folders do not exist when a run stops before execution
    if [[ "${RUN_ID:-}" =~ ^[0-9]+$ ]]; then
        python3 "$PROJECT_ROOT/src/provenance.py" --db "$DB_FILE" artifacts --run-id "$RUN_ID" \
            --dir "$RUN_ARTIFACT_DIR" >> "$LOG_FILE" 2>&1 || log "[PROVENANCE] Could not record the artefacts of run $RUN_ID"
        python3 "$PROJECT_ROOT/src/provenance.py" --db "$DB_FILE" classify-dependency-errors --run-id "$RUN_ID" \
            >> "$LOG_FILE" 2>&1 || log "[PROVENANCE] Could not classify the dependency errors of run $RUN_ID"
        if [ "${REPROSCORE_ENABLED:-true}" = true ]; then
            log "[REPROSCORE] outcome: $(python3 "$PROJECT_ROOT/src/reproscore_stage.py" --db "$DB_FILE" outcome \
                --run-id "$RUN_ID" 2>>"$LOG_FILE" | jq -c '{status, ros, rrs, rcs, install_success, notebook_exec_rate, import_success_rate}' 2>/dev/null)"
        fi
    fi
}

# Print the notebooks of an acquired resource folder, separated by ";".
# Excluded: checkpoint copies, macOS resource forks (__MACOSX/, ._*), executed
# and Zenodo notebooks that are byte-identical to
# another notebook of the same record (listed by src/zenodo_fetch.py).
discover_notebooks() {
    local dir="$1" discovered="" nb rel dup
    local -A duplicate_notebooks=()
    if [ -f "$dir/.zenodo-acquisition.json" ]; then
        while IFS= read -r dup; do
            [ -n "$dup" ] && duplicate_notebooks["$dup"]=1
        done < <(jq -r '.duplicate_notebooks[]?.path' "$dir/.zenodo-acquisition.json")
    fi
    while IFS= read -r nb; do
        rel="${nb#$dir/}"
        if [ -n "${duplicate_notebooks[$rel]:-}" ]; then
            log "[REPO] Skipping identical copy of another notebook: $rel" >&2
            continue
        fi
        [ -n "$discovered" ] && discovered="${discovered};${rel}" || discovered="$rel"
    done < <(find "$dir" -mindepth 1 \
        -name "*.ipynb" \
        \( -type f -o -xtype f \) \
        -not -path "*/.ipynb_checkpoints/*" \
        -not -path "*/__MACOSX/*" \
        -not -name "._*" \
        | LC_ALL=C sort)
    printf '%s' "$discovered"
}

process_repo() {
    DISK_LOW=""               # set by disk_check: the run is recorded as DISK_FULL
    COMPARISON_FAILED=""      # set by compare_notebook_outputs: the run is recorded as COMPARISON_FAILED
    if ! [[ "${REPO_ID:-}" =~ ^[0-9]+$ ]]; then
        log "[REPO] Invalid or missing repository ID"
        return 1
    fi

    REPO_START_TIME=$(now_sec)
    REPO_URL="$1"
    NOTEBOOK_PATHS="$2"
    SETUP_PATHS="$3"
    REQUIREMENT_PATHS="$4"
    export REPO_URL
    local repo_basename
    repo_basename=$(basename "${REPO_URL%/}" .git)
    REPO_NAME="${REPO_ID}_${repo_basename}"
    REPO_DIR="$REPOS_DIR/$REPO_NAME"
    log "[REPO] ── Starting: $REPO_NAME ──────────────────────────────"
    LOG_FILE="${LOG_DIR}/${REPO_NAME}.log"
    >"$LOG_FILE"
    export LOG_FILE
    create_repository_run "$REPO_ID" "$REPO_URL"
    RUN_ARTIFACT_DIR="$ARTIFACT_DIR/run-${RUN_ID:-$$}"
    mkdir -p "$RUN_ARTIFACT_DIR"
    export RUN_ARTIFACT_DIR
    ensure_pipeline_session
    if [[ "${PIPELINE_SESSION_ID:-}" =~ ^[0-9]+$ ]] && [[ "${RUN_ID:-}" =~ ^[0-9]+$ ]]; then
        sqlite3 "$DB_FILE" "UPDATE repository_runs SET session_id=$PIPELINE_SESSION_ID WHERE id=$RUN_ID;"
    fi

    # Single-repo mode: populate notebooks table from user-supplied URLs/paths.
    # Batch mode: notebooks already in DB — this becomes a no-op.
    insert_notebooks_from_paths "$REPO_ID" "$NOTEBOOK_PATHS"

    # Normalize NOTEBOOK_PATHS: strip platform notebook URLs to relative paths
    # so all downstream code (notebooks.sh etc.) gets plain relative paths
    local normalized=""
    IFS=';' read -ra _nb_array <<<"$NOTEBOOK_PATHS"
    for _nb in "${_nb_array[@]}"; do
        _nb=$(notebook_rel_path "$_nb")
        [[ "$_nb" == https://github.com/* || "$_nb" == https://codeberg.org/* ]] && _nb=$(echo "$_nb" | sed -E 's|https?://[^/]+/[^/]+/[^/]+/(blob|src)/[^/]+/||')
        [ -n "$normalized" ] && normalized="${normalized};${_nb}" || normalized="$_nb"
    done
    NOTEBOOK_PATHS="$normalized"

    PLATFORM=$(detect_platform "$REPO_URL")

    if [ "$PLATFORM" = "unknown" ]; then
        finalize_repository_run \
            "$RUN_ID" \
            "UNSUPPORTED_PLATFORM" \
            "Only GitHub, Codeberg, and Zenodo are supported" \
            "$(elapsed_sec "$REPO_START_TIME")"
        return 0
    fi

    # Validate every source before requesting and saving its metadata.
    # 1 = not obtainable (gone, private, restricted); 2 = could not be checked (network): a pipeline error, re-run
    local validation=0
    if [ "$PLATFORM" = "zenodo" ]; then
        validate_zenodo "$REPO_URL" || validation=$?
        if [ "$validation" -eq 1 ]; then
            finalize_repository_run "$RUN_ID" "INVALID_ZENODO_RECORD" "Zenodo API validation failed" "$(elapsed_sec "$REPO_START_TIME")"
            return 0
        fi
    else
        validate_repo "$REPO_URL" || validation=$?
        if [ "$validation" -eq 1 ]; then
            finalize_repository_run "$RUN_ID" "INVALID_REPOSITORY_URL" "git ls-remote failed" "$(elapsed_sec "$REPO_START_TIME")"
            return 0
        fi
    fi
    if [ "$validation" -ne 0 ]; then
        finalize_repository_run "$RUN_ID" "VALIDATION_UNREACHABLE" \
            "The source could not be reached to check the resource (network or server error; run again)" \
            "$(elapsed_sec "$REPO_START_TIME")"
        return 0
    fi

    if ! fetch_and_save_repo_metadata \
        "$REPO_ID" \
        "$REPO_URL" \
        "$PLATFORM"; then

        finalize_repository_run \
            "$RUN_ID" \
            "METADATA_FETCH_FAILED" \
            "Could not fetch and save normalized repository metadata" \
            "$(elapsed_sec "$REPO_START_TIME")"
        return 0
    fi

    # Always acquire into an empty folder. Reusing a folder from an earlier run
    # (git pull, or a Zenodo download on top of old files) let files from that
    # run - flattened and unflattened archive copies, notebooks written during
    # execution - be discovered as extra notebooks of this resource.
    usage_begin acquire
    local acquire_start
    acquire_start=$(date +%s.%N)
    if [ -e "$REPO_DIR" ] && ! cleanup_repository_directory "$REPO_DIR"; then
        usage_end "$RUN_ID" acquire "" 1
        finalize_repository_run "$RUN_ID" "REPO_DIR_NOT_CLEAN" \
            "Could not remove the folder of an earlier run: $REPO_DIR" \
            "$(elapsed_sec "$REPO_START_TIME")"
        return 0
    fi

    if [ "$PLATFORM" = "zenodo" ]; then
        log "[REPO] Downloading Zenodo record into $REPO_DIR..."

        if ! fetch_zenodo "$REPO_URL" "$REPO_DIR" >>"$LOG_FILE" 2>&1; then
            record_acquisition "$(usage_elapsed "$acquire_start")"
            usage_end "$RUN_ID" acquire "$REPO_DIR" 1
            finalize_repository_run \
                "$RUN_ID" \
                "ZENODO_DOWNLOAD_FAILED" \
                "Could not download or extract the Zenodo record: ${ZENODO_ERROR_MESSAGE:-unknown reason}" \
                "$(elapsed_sec "$REPO_START_TIME")"
            return 0
        fi
    else
        log "[REPO] Cloning into $REPO_DIR..."

        if ! git_clone_with_retry "$REPO_URL" "$REPO_DIR"; then
            usage_end "$RUN_ID" acquire "" 1
            finalize_repository_run \
                "$RUN_ID" \
                "GIT_CLONE_FAILED" \
                "Could not clone the repository" \
                "$(elapsed_sec "$REPO_START_TIME")"
            return 0
        fi
    fi

    record_acquisition "$(usage_elapsed "$acquire_start")"
    usage_end "$RUN_ID" acquire "$REPO_DIR" 0
    disk_check acquisition || true

    usage_begin licence
    record_licence_detection
    usage_end "$RUN_ID" licence "" 0

    if [ "${REPROSCORE_ENABLED:-true}" = true ] && [ -d "$REPO_DIR" ]; then
        usage_begin readiness
        record_readiness
        usage_end "$RUN_ID" readiness "" 0
    fi

    if [ ! -d "$REPO_DIR" ]; then
        finalize_repository_run "$RUN_ID" "REPO_DIR_MISSING" "Directory not found after clone" "$(elapsed_sec "$REPO_START_TIME")"
        return 0
    fi

    # Auto-discover notebooks if none provided
    if [ -z "$NOTEBOOK_PATHS" ]; then
        log "[REPO] No notebook paths provided — auto-discovering .ipynb files..."
        discovered=$(discover_notebooks "$REPO_DIR")

        if [ -z "$discovered" ]; then
            finalize_repository_run "$RUN_ID" "NO_NOTEBOOKS" "No .ipynb files found in repo" "$(elapsed_sec "$REPO_START_TIME")"
            return 0
        fi

        log "[REPO] Discovered notebooks: $discovered"
        NOTEBOOK_PATHS="$discovered"
        insert_notebooks_from_paths "$REPO_ID" "$NOTEBOOK_PATHS"
    fi

    # Check notebook availability only after supplied paths were registered or
    # missing paths were discovered from the acquired repository content.
    stats=$(get_notebook_language_stats "$REPO_ID")
    total_notebooks=$(echo "$stats" | cut -d'|' -f1)
    python_notebooks=$(echo "$stats" | cut -d'|' -f2)
    log "[REPO] Notebooks: total=$total_notebooks python=$python_notebooks"

    if [ "$total_notebooks" -eq 0 ]; then
        finalize_repository_run "$RUN_ID" "NO_NOTEBOOKS" "No notebooks found" "$(elapsed_sec "$REPO_START_TIME")"
        return 0
    fi
    if [ "$python_notebooks" -eq 0 ]; then
        finalize_repository_run "$RUN_ID" "NO_PYTHON_NOTEBOOKS" "No Python notebooks found" "$(elapsed_sec "$REPO_START_TIME")"
        return 0
    fi

    if [ "${FAST_FIRST:-false}" = "true" ] &&
       [[ "${FAST_FIRST_MAX_NOTEBOOKS:-0}" =~ ^[0-9]+$ ]] &&
       [ "${FAST_FIRST_MAX_NOTEBOOKS:-0}" -gt 0 ] &&
       [ "$python_notebooks" -gt "$FAST_FIRST_MAX_NOTEBOOKS" ]; then
        finalize_repository_run \
            "$RUN_ID" \
            "DEFERRED_LONG_REPO" \
            "Deferred during fast-first pass because it has $python_notebooks Python notebooks (limit: $FAST_FIRST_MAX_NOTEBOOKS)" \
            "$(elapsed_sec "$REPO_START_TIME")"
        return 0
    fi

    usage_begin classify
    if ! classify_repository_notebooks "$REPO_ID" "$REPO_DIR" "$NOTEBOOK_PATHS"; then
        usage_end "$RUN_ID" classify "" 1
        finalize_repository_run \
            "$RUN_ID" \
            "CLASSIFICATION_FAILED" \
            "Notebook classification failed" \
            "$(elapsed_sec "$REPO_START_TIME")"
        return 0
    fi

    usage_end "$RUN_ID" classify "" 0

    store_original_notebooks

    usage_begin environment
    if ! setup_pyenv_env "$REPO_DIR" "" "$SETUP_PATHS"; then
        usage_end "$RUN_ID" environment "${REPO_VENV_DIR:-}" 1
        finish_run_artifacts
        finalize_repository_run "$RUN_ID" "$ENV_ERROR_TYPE" "$ENV_ERROR_MESSAGE" "$(elapsed_sec "$REPO_START_TIME")"
        cleanup_pyenv_env
        return 0
    fi

    usage_end "$RUN_ID" environment "${REPO_VENV_DIR:-}" 0
    disk_check environment || true

    usage_begin execute
    if ! run_in_pyenv_env "$REPO_DIR"; then
        if [ "$ENV_ERROR_TYPE" != "NOTEBOOK_EXECUTION_ERROR" ]; then
            usage_end "$RUN_ID" execute "$REPO_DIR" 1
            finish_run_artifacts
            analyze_env_error "$LOG_FILE"
            finalize_repository_run "$RUN_ID" "$ENV_ERROR_TYPE" "$ENV_ERROR_MESSAGE" "$(elapsed_sec "$REPO_START_TIME")"
            cleanup_pyenv_env
            return 0
        fi
        # Every notebook was run but none left an executed copy (time limit, dead kernel, nbconvert error): that is
        # a result of the resource, not an environment failure. Record every notebook (EXEC_FAIL with its reason)
        # like any other run. (Study run 2026-10: 24 such resources were recorded as ENV_RUN_FAIL without executions.)
        log "[REPO] No notebook produced an executed copy: recording every notebook as failed"
        usage_end "$RUN_ID" execute "$REPO_DIR" 1
    else
        usage_end "$RUN_ID" execute "$REPO_DIR" 0
    fi

    NOTEBOOKS_COUNT=$(echo "$NOTEBOOK_PATHS" | awk -F';' '{print NF}')
    export NOTEBOOKS_COUNT
    local total_time
    disk_check execution || true
    if [ -n "$DISK_LOW" ]; then
        # notebooks failed for lack of space and the comparison could not write its results: not a result of the
        # resource; finalize_repository_run records DISK_FULL and the next top-up runs the resource again
        log "[REPO] Disk space ran out during the run ($DISK_LOW): outputs not compared"
        finish_run_artifacts
        cleanup_pyenv_env
        finalize_repository_run "$RUN_ID" "SUCCESS" "Repository executed successfully" "$(elapsed_sec "$REPO_START_TIME")"
        return 0
    fi
    usage_begin compare
    compare_notebook_outputs
    usage_end "$RUN_ID" compare "" 0
    finish_run_artifacts
    cleanup_pyenv_env

    total_time=$(elapsed_sec "$REPO_START_TIME")
    if [ -n "$COMPARISON_FAILED" ]; then
        # the comparison step itself failed (e.g. the pipeline's Python could not import a module): a pipeline error
        finalize_repository_run "$RUN_ID" "COMPARISON_FAILED" "The output comparison failed for: $COMPARISON_FAILED" "$total_time"
        log "[REPO] ── Done: $REPO_NAME (${total_time}s) ──────────────────────"
        return 0
    fi
    finalize_repository_run "$RUN_ID" "SUCCESS" "Repository executed successfully" "$total_time"
    log "[REPO] ── Done: $REPO_NAME (${total_time}s) ──────────────────────"
    isExecutedSuccessfully="true"
    export RUN_ID NOTEBOOKS_COUNT
}

process_sqlite_flow() {
    processed_repo_ids=()
    local processed_count=0
    log "[BATCH] Processing next $TARGET_COUNT unexecuted repositories."

    while [ $processed_count -lt "$TARGET_COUNT" ]; do
        local not_in_clause=""
        if [ ${#processed_repo_ids[@]} -gt 0 ]; then
            not_in_clause="AND r.id NOT IN ($(
                IFS=,
                echo "${processed_repo_ids[*]}"
            ))"
        fi

        repo_data=$(
            sqlite3 "$DB_FILE" <<EOF
.mode csv
.headers off
SELECT r.id, r.repository, r.platform, r.notebooks, r.setups, r.requirements
FROM repositories r
WHERE r.notebooks IS NOT NULL AND TRIM(r.notebooks) != ''
AND r.notebooks_count != 0
AND r.id NOT IN (SELECT DISTINCT repository_id FROM repository_runs)
$not_in_clause
ORDER BY r.id LIMIT 1;
EOF
        )
        if [ -z "$repo_data" ]; then
            log "[BATCH] No more repositories."
            break
        fi

        IFS=',' read -r REPO_ID REPO_PATH PLATFORM NOTEBOOK_PATHS SETUP_PATHS REQUIREMENT_PATHS <<<"$repo_data"
        # if an old platform has not platform value, default to github
        PLATFORM=$(echo "$PLATFORM" | tr -d '\r\n"')
        [ -z "$PLATFORM" ] && PLATFORM="github"
        REPO_PATH=$(echo "$REPO_PATH" | tr -d '\r\n"')
        case "$PLATFORM" in
            github)
                REPO_URL="https://github.com/${REPO_PATH}"
                ;;
            codeberg)
                REPO_URL="https://codeberg.org/${REPO_PATH}"
                ;;
            zenodo)
                REPO_URL="https://zenodo.org/records/${REPO_PATH}"
                ;;
            *)
                log "[BATCH] Unknown platform '$PLATFORM' for repo $REPO_ID, skipping"
                processed_repo_ids+=("$REPO_ID")
                continue
                ;;
        esac
        NOTEBOOK_PATHS=$(echo "$NOTEBOOK_PATHS" | tr -d '\r\n"')
        REQUIREMENT_PATHS=$(echo "$REQUIREMENT_PATHS" | tr -d '\r\n"')
        SETUP_PATHS=$(echo "$SETUP_PATHS" | tr -d '\r\n"')
        log "[BATCH] Repo $REPO_ID: $REPO_URL"

        processed_repo_ids+=("$REPO_ID")
        isExecutedSuccessfully="false"

        if ! process_repo "$REPO_URL" "$NOTEBOOK_PATHS" "$SETUP_PATHS" "$REQUIREMENT_PATHS"; then
            cleanup_repository_directory "${REPO_DIR:-}" || true
            processed_count=$((processed_count + 1))
            continue
        fi
        [[ "$isExecutedSuccessfully" == "true" ]] && processed_count=$((processed_count + 1))
    done
    log "[BATCH] Finished. Processed $processed_count repositories."
}

# --- Zenodo helpers ---

# Get the record ID number from a Zenodo URL
zenodo_record_id() {
    echo "$1" | sed -E 's#.*/records?/([0-9]+).*#\1#'
}

# Download a Zenodo record into an empty folder with src/zenodo_fetch.py:
# every notebook and every archive (zip/tar/rar/7z), data files within size
# limits, MD5-checked; skipped files and the record version are listed in
# "$dest/.zenodo-acquisition.json". On failure ZENODO_ERROR_MESSAGE holds the
# reason. Returns 0 on success, 1 on failure, 3 if nothing was downloadable.
fetch_zenodo() {
    local url="$1" dest="$2"
    local id status=0
    id=$(zenodo_record_id "$url")
    ZENODO_ERROR_MESSAGE=""

    python3 "$PROJECT_ROOT/src/zenodo_fetch.py" "$id" "$dest" || status=$?
    if [ "$status" -ne 0 ]; then
        ZENODO_ERROR_MESSAGE=$(jq -r '.error // empty' "$dest/.zenodo-acquisition.json" 2>/dev/null)
        [ -n "$ZENODO_ERROR_MESSAGE" ] || ZENODO_ERROR_MESSAGE="zenodo_fetch.py exited with status $status"
        log "[ZENODO] $ZENODO_ERROR_MESSAGE"
    fi
    export ZENODO_ERROR_MESSAGE
    return "$status"
}

# Fetch metadata for a GitHub repo. Usage: fetch_github_metadata <owner/repo>
# Prints 8 lines: title, description, authors, license, keywords, doi, created_at, updated_at
# api_get PLATFORM URL [curl header args...]
# GET with up to 3 attempts (no answer, 429, 5xx). Every answer — also errors — is stored exactly as
# received with its provenance in metadata_snapshots (src/metadata_snapshot.py: run, URL, request and
# response time, status, attempts, token used or not, selected headers, size, SHA-256, body), so the
# metadata can be normalised again later without querying the platform. Prints the body and returns 0
# for HTTP 200, else returns 1.
api_get() {
    local platform="$1" url="$2"
    shift 2
    local tmp status attempt=0 requested responded token_flag=() rc=1
    tmp=$(mktemp -d)
    case " $* " in *"Authorization:"*) token_flag=(--token-used) ;; esac
    requested=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
    while :; do
        attempt=$((attempt + 1))
        status=$(curl -sS -L --connect-timeout 10 --max-time 30 -D "$tmp/headers" -o "$tmp/body" \
            -w '%{http_code}' "$@" "$url" 2>>"${LOG_FILE:-/dev/null}") || status=${status:-000}
        case "$status" in
            000|429|5??) [ "$attempt" -lt 3 ] && { sleep $((2 * attempt)); continue; } ;;
        esac
        break
    done
    responded=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
    if [[ "${RUN_ID:-}" =~ ^[0-9]+$ ]] && [ -n "${DB_FILE:-}" ]; then
        python3 "$PROJECT_ROOT/src/metadata_snapshot.py" --db "$DB_FILE" --run-id "$RUN_ID" --platform "$platform" \
            --url "$url" --status "$status" --requested-at "$requested" --responded-at "$responded" \
            --headers-file "$tmp/headers" --body-file "$tmp/body" --attempts "$attempt" "${token_flag[@]}" \
            --tool "curl $(curl --version 2>/dev/null | head -1 | cut -d' ' -f2)" 2>>"${LOG_FILE:-/dev/null}" ||
            echo "[METADATA] could not store the API snapshot of $url" >&2
    fi
    if [ "$status" = 200 ]; then
        cat "$tmp/body"
        rc=0
    else
        echo "[METADATA] $platform API: HTTP $status for $url" >&2
    fi
    rm -rf "$tmp"
    return $rc
}

fetch_github_metadata() {
    local repo_path="$1"
    local response
    local -a headers=(-H "Accept: application/vnd.github+json")

    if [ -n "${GITHUB_TOKEN:-}" ]; then
        headers+=(-H "Authorization: Bearer $GITHUB_TOKEN")
    fi

    if ! response=$(api_get github "https://api.github.com/repos/$repo_path" "${headers[@]}"); then
        echo "[METADATA] GitHub API request failed for $repo_path" >&2
        return 1
    fi

    if [ -z "$response" ] ||
       echo "$response" | jq -e '.message? != null' >/dev/null 2>&1; then
        echo "[METADATA] GitHub API returned no usable data for $repo_path" >&2
        return 1
    fi

    echo "$response" | normalize_github_metadata
}

# Map a GitHub repository JSON (stdin) to the 8 normalized metadata lines.
# Line breaks inside a value are replaced by a space so that every field
# stays on one line (fetch_and_save_repo_metadata expects exactly 8 lines).
normalize_github_metadata() {
    jq -r '
        [
            (.name // ""),
            (.description // ""),
            (.owner.login // ""),
            (.license.spdx_id // "" | if . == "NOASSERTION" then "" else . end),
            ((.topics // []) | join("; ")),
            "",
            (.created_at // ""),
            (.updated_at // "")
        ]
        | map(tostring | gsub("[\r\n]+"; " "))
        | .[]
    '
}

# Fetch metadata for a Codeberg repository.
# Usage: fetch_codeberg_metadata <owner/repo>
# Prints: title, description, authors, license, keywords, DOI,
# created_at, updated_at
fetch_codeberg_metadata() {
    local repo_path="$1"
    local response
    local -a headers=(-H "Accept: application/json")

    if [ -n "${CODEBERG_TOKEN:-}" ]; then
        headers+=(-H "Authorization: token $CODEBERG_TOKEN")
    fi

    if ! response=$(api_get codeberg "https://codeberg.org/api/v1/repos/$repo_path" "${headers[@]}"); then
        echo "[METADATA] Codeberg API: no data for $repo_path" >&2
        return 1
    fi

    echo "$response" | normalize_codeberg_metadata
}

# Map a Codeberg (Forgejo) repository JSON (stdin) to the 8 normalized
# metadata lines, with line breaks inside a value replaced by a space.
normalize_codeberg_metadata() {
    jq -r '
        [
            (.name // ""),
            (.description // ""),
            (
                if (.owner.full_name // "") != ""
                then .owner.full_name
                else (.owner.login // .owner.username // "")
                end
            ),
            (
                if .license == null then ""
                elif (.license | type) == "object"
                then (.license.spdx_id // .license.key // .license.name // "")
                else (.license | tostring)
                end
            ),
            ((.topics // []) | join("; ")),
            "",
            (.created_at // ""),
            (.updated_at // "")
        ]
        | map(tostring | gsub("[\r\n]+"; " "))
        | .[]
    '
}

# Fetch normalized metadata for a Zenodo record.
fetch_zenodo_metadata() {
    local record_id
    local response

    record_id=$(zenodo_record_id "$1")

    if ! response=$(api_get zenodo "https://zenodo.org/api/records/$record_id" -H "Accept: application/json"); then
        if [ -n "${ZENODO_TOKEN:-}" ]; then
            response=$(api_get zenodo "https://zenodo.org/api/records/$record_id" -H "Accept: application/json" \
                -H "Authorization: Bearer $ZENODO_TOKEN") || {
                echo "[METADATA] Zenodo API: no data for record $record_id" >&2
                return 1
            }
        else
            echo "[METADATA] Zenodo API: no data for record $record_id" >&2
            return 1
        fi
    fi

    echo "$response" | jq -r '
        [
            (.metadata.title // .title // ""),
            (.metadata.description // ""),
            (
                (.metadata.creators // [])
                | map(.name // .person_or_org.name // "")
                | join("; ")
            ),
            (
                if .metadata.license == null then ""
                elif (.metadata.license | type) == "object"
                then (.metadata.license.id // .metadata.license.title // "")
                else (.metadata.license | tostring)
                end
            ),
            ((.metadata.keywords // []) | join("; ")),
            (.doi // .metadata.doi // .pids.doi.identifier // ""),
            (.created // .metadata.publication_date // ""),
            (.modified // .updated // "")
        ]
        | map(tostring | gsub("[\r\n]+"; " "))
        | .[]
    '
}

fetch_and_save_repo_metadata() {
    local repo_id="$1"
    local repo_url="$2"
    local platform="$3"
    local repo_path
    local -a metadata

    repo_path=$(echo "$repo_url" |
        sed -E 's#^https?://[^/]+/##; s#^records?/##; s#\.git$##')       # only Zenodo's leading records/

    case "$platform" in
        github)
            mapfile -t metadata < <(fetch_github_metadata "$repo_path")
            ;;
        codeberg)
            mapfile -t metadata < <(fetch_codeberg_metadata "$repo_path")
            ;;
        zenodo)
            mapfile -t metadata < <(fetch_zenodo_metadata "$repo_path")
            ;;
        *)
            log "[METADATA] Unsupported platform: $platform"
            return 1
            ;;
    esac

    if [ "${#metadata[@]}" -ne 8 ]; then
        log "[METADATA] Expected 8 fields for $repo_url, received ${#metadata[@]}"
        return 1
    fi

    if ! save_repo_metadata \
        "$repo_id" \
        "${metadata[0]}" \
        "${metadata[1]}" \
        "${metadata[2]}" \
        "${metadata[3]}" \
        "${metadata[4]}" \
        "${metadata[5]}" \
        "${metadata[6]}" \
        "${metadata[7]}"; then
        log "[METADATA] Could not save metadata for $repo_url (repo_id '$repo_id')"
        return 1
    fi

    log "[METADATA] Saved normalized metadata for $repo_url"
}
