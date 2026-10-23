#!/bin/bash
###############################################################################
# notebooks.sh — Notebook comparison and output processing
###############################################################################

compare_notebook_outputs_json() {
    local notebook1="$1" notebook2="$2" comparison_file="$3"
    mkdir -p "$(dirname "$comparison_file")"
    if [ ! -f "$notebook2" ]; then
        log "[NOTEBOOK] Executed notebook missing: $notebook2 — skipping"
        return 0
    fi
    python3 -u "$PROJECT_ROOT/analysis/compare_notebook.py" \
        "$notebook1" "$notebook2" "$NOTEBOOK_PATH" "$REPO_ID" \
        --json "$comparison_file" 2>&1 | tee -a "$LOG_FILE"
    comparison_status "${PIPESTATUS[0]}"
}

# compare_notebook.py records every executed notebook, also when its outputs cannot be compared; a non-zero exit
# means the comparison step itself failed (study run 2026-10-11: the pipeline's Python could not import attrs) and
# the run is recorded as COMPARISON_FAILED (repo.sh), a pipeline error that the next top-up runs again.
comparison_status() {
    if [ "$1" -ne 0 ]; then
        log "[NOTEBOOK] Comparison failed for $NOTEBOOK_PATH (exit $1)"
        COMPARISON_FAILED="${COMPARISON_FAILED:+$COMPARISON_FAILED; }$NOTEBOOK_PATH (exit $1)"
    fi
}

compare_notebook_outputs() {
    log "[NOTEBOOK] Comparing outputs for: $REPO_NAME"
    # local: a failed read must not leave the previous resource's notebooks in the array (study run 2026-10-11)
    local -a NOTEBOOK_ARRAY=()
    IFS=";" read -ra NOTEBOOK_ARRAY <<<"$NOTEBOOK_PATHS"
    for NOTEBOOK_PATH in "${NOTEBOOK_ARRAY[@]}"; do
        NOTEBOOK_PATH=$(notebook_rel_path "$NOTEBOOK_PATH")
        [ -n "$NOTEBOOK_PATH" ] || continue
        if [ ! -f "$REPO_DIR/$NOTEBOOK_PATH" ]; then
            log "[NOTEBOOK] Not found: $REPO_DIR/$NOTEBOOK_PATH — skipping"
            continue
        fi
        local notebook_dir base_name original_notebook executed_notebook comparison_result_file
        notebook_dir=$(dirname "$NOTEBOOK_PATH")
        base_name=$(basename "$NOTEBOOK_PATH" .ipynb)
        original_notebook="$REPO_DIR/$NOTEBOOK_PATH"
        # executed copy and comparison live in the run's artefact folder (one file per
        # notebook path, so notebooks with the same name in different folders do not collide)
        executed_notebook="$RUN_ARTIFACT_DIR/executed/$NOTEBOOK_PATH"
        comparison_result_file="$RUN_ARTIFACT_DIR/comparisons/${NOTEBOOK_PATH%.ipynb}.json"
        # the run already knows its repository; the URL lookup is only a fallback
        [[ "${REPO_ID:-}" =~ ^[0-9]+$ ]] || REPO_ID=$(get_repo_id_from_db "$REPO_URL")
        NOTEBOOK_ID=$(get_notebook_id_from_db "$REPO_ID" "$NOTEBOOK_PATH")
        log "[NOTEBOOK] ID=$NOTEBOOK_ID  REPO_ID=$REPO_ID  path=$NOTEBOOK_PATH"
        if [ -z "$NOTEBOOK_ID" ]; then
            log "[NOTEBOOK] No DB record for $NOTEBOOK_PATH — skipping"
            continue
        fi
        if [ ! -f "$executed_notebook" ]; then
            log "[NOTEBOOK] Output missing for $NOTEBOOK_PATH — recording failure"
            mkdir -p "$(dirname "$comparison_result_file")"
            python3 -u "$PROJECT_ROOT/analysis/compare_notebook.py" "$original_notebook" "$executed_notebook" \
                "$NOTEBOOK_PATH" "$REPO_ID" --json "$comparison_result_file" --missing-output 2>&1 | tee -a "$LOG_FILE"
            comparison_status "${PIPESTATUS[0]}"
            cat <<EOF >"$comparison_result_file"
{
  "notebook": "$NOTEBOOK_PATH",
  "NOTEBOOK_ID": "$NOTEBOOK_ID",
  "REPO_ID": "$REPO_ID",
  "status": "failed",
  "reason": "output_notebook_not_created"
}
EOF
            continue
        fi
        compare_notebook_outputs_json "$original_notebook" "$executed_notebook" "$comparison_result_file"
    done
}
