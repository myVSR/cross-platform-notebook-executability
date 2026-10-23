#!/bin/bash

log() {
    local msg="$(date '+%Y-%m-%d %H:%M:%S') - $*"

    if [ -n "$LOG_FILE" ]; then
        echo "$msg" | tee -a "$LOG_FILE"
    else
        echo "$msg"
    fi
}


now_sec() {
    date +%s
}

elapsed_sec() {
    local start="$1"
    local end
    end=$(now_sec)
    echo $((end - start))
}
