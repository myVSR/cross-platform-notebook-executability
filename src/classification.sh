#!/bin/bash

classify_repository_notebooks() {
    local repository_id="$1"
    local repository_dir="$2"
    local notebook_paths="$3"
    local notebook_path
    local notebook_id
    local notebook_file
    local classification_output
    local -a notebook_array
    local -a command
    local any_attempted=false
    local any_succeeded=false

    if [ "$CLASSIFICATION_ENABLED" != "true" ]; then
        log "[CLASSIFICATION] Classification is disabled"
        return 0
    fi

    [ -z "$notebook_paths" ] && return 0

    if ! [[ "$repository_id" =~ ^[1-9][0-9]*$ ]]; then
        log "[CLASSIFICATION] Invalid repository ID: $repository_id"
        return 1
    fi

    IFS=';' read -ra notebook_array <<<"$notebook_paths"

    for notebook_path in "${notebook_array[@]}"; do
        notebook_path=$(trim_whitespace "$notebook_path")
        [ -z "$notebook_path" ] && continue

        any_attempted=true
        notebook_id=$(get_notebook_id_from_db "$repository_id" "$notebook_path")

        if ! [[ "$notebook_id" =~ ^[1-9][0-9]*$ ]]; then
            log "[CLASSIFICATION] Skipping $notebook_path — it is not registered in the database (non-fatal)"
            continue
        fi

        notebook_file="$repository_dir/$notebook_path"

        if [ ! -f "$notebook_file" ]; then
            log "[CLASSIFICATION] Skipping $notebook_path — file not found in the repository, check the path for typos (non-fatal)"
            continue
        fi

        command=(
            python3
            -m analysis.notebook_classification
            classify
            "$notebook_file"
            --db-file "$DB_FILE"
            --notebook-id "$notebook_id"
            --model "$CLASSIFICATION_MODEL"
            --ollama-url "$CLASSIFICATION_OLLAMA_URL"
            --timeout "$CLASSIFICATION_TIMEOUT"
            --confidence-threshold "$CLASSIFICATION_CONFIDENCE_THRESHOLD"
        )

        if [ "$CLASSIFICATION_RULE_ONLY" = "true" ]; then
            command+=(--rule-only)
        elif [ "$CLASSIFICATION_LLM_ONLY" = "true" ]; then
            command+=(--llm-only)
        fi

        log "[CLASSIFICATION] Classifying: $notebook_path"

        if ! classification_output=$("${command[@]}" 2>&1); then
            log "[CLASSIFICATION] Failed: $notebook_path (skipping, non-fatal)"
            log "$classification_output"
            continue
        fi

        any_succeeded=true
        log "[CLASSIFICATION] Result stored for notebook ID $notebook_id"
    done

    if [ "$any_attempted" = true ] && [ "$any_succeeded" = false ]; then
        log "[CLASSIFICATION] No notebooks could be classified"
        return 1
    fi

    return 0
}