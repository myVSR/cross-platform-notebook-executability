#!/bin/bash

command_exists () {
    command -v "$1" >/dev/null 2>&1
}

validate_repo() {
    # 0 = reachable; 1 = definitely not obtainable (not found, private: git asks for credentials);
    # 2 = could not be checked (network, proxy, server error) after NBF_VALIDATE_ATTEMPTS tries with backoff.
    # (Study run 2026-10: network failures here were recorded as INVALID_REPOSITORY_URL and replaced.)
    local repo_url="$1" attempt output wait=15
    log "[REPO] Validating repository URL: $repo_url"
    for attempt in $(seq 1 "${NBF_VALIDATE_ATTEMPTS:-5}"); do
        if output=$(LC_ALL=C GIT_TERMINAL_PROMPT=0 timeout 60 git ls-remote "$repo_url" 2>&1 >/dev/null); then
            log "[REPO] Repository URL is valid."
            return 0
        fi
        if grep -qiE 'terminal prompts disabled|could not read Username|repository .*not found|Repository not found|HTTP (404|410|451)|Authentication failed|does not appear to be a git repository' <<<"$output"; then
            log "[ERROR] Repository not found or not public - $repo_url ($(head -1 <<<"$output"))"
            return 1
        fi
        log "[REPO] Could not reach $repo_url (attempt $attempt: $(head -1 <<<"$output")); retrying in ${wait}s"
        [ "$attempt" -lt "${NBF_VALIDATE_ATTEMPTS:-5}" ] && sleep "$wait"
        wait=$((wait * 2))
    done
    log "[ERROR] Repository could not be checked (network): $repo_url"
    return 2
}

validate_zenodo() {
    local record_url="$1"
    local record_id
    local -a headers=(-H "Accept: application/json")

    if [[ ! "$record_url" =~ ^https?://(www\.)?zenodo\.org/records?/([0-9]+)([/?#].*)?$ ]]; then
        log "[ERROR] Invalid Zenodo record URL - $record_url"
        return 1
    fi

    record_id="${BASH_REMATCH[2]}"

    if [ -n "${ZENODO_TOKEN:-}" ]; then
        headers+=(-H "Authorization: Bearer $ZENODO_TOKEN")
    fi

    log "[REPO] Validating Zenodo record: $record_id"
    if curl -fsS \
        --retry 2 \
        --retry-all-errors \
        --retry-delay 1 \
        --connect-timeout 10 \
        --max-time 30 \
        "${headers[@]}" \
        "https://zenodo.org/api/records/$record_id" \
        >/dev/null 2>&1; then
        log "[REPO] Zenodo record is valid."
        return 0
    fi

    log "[ERROR] Zenodo record does not exist or is unreachable - $record_url"
    return 1
}
