#!/bin/bash
###############################################################################
# usage.sh — resource-usage measurement for footprint estimates
#
#   usage_begin <stage>
#   ...stage work...
#   usage_end <run_id> <stage> [disk_path] [exit_status]
#
# CPU time comes from the shell builtin `times` (this shell plus all reaped
# descendants, e.g. pip, jupyter and the kernel), so it is read without a
# subshell. Energy is the RAPL package counter (whole host) when readable;
# network bytes are host-wide deltas over all interfaces except lo. Values
# are stored by src/provenance.py in run_stage_usage. Stages may be nested
# only if they have different names. Numbers are formatted in the C locale.
###############################################################################

declare -gA USAGE_START=()

# Seconds elapsed since a `date +%s.%N` timestamp, in the C locale (a decimal
# comma would be rejected by provenance.py). Usage: usage_elapsed <start>
usage_elapsed() {
    LC_ALL=C awk -v a="$1" -v b="$(date +%s.%N)" 'BEGIN { printf "%.3f", b - a }'
}

# Sets USAGE_CPU="<user> <sys>": CPU seconds of this shell + reaped children.
# Must not be called in a subshell ($(...)): `times` would then report the
# subshell's own, empty counters.
usage_cpu_seconds() {
    local -x LC_ALL=C
    local file="${TMPDIR:-/tmp}/usage-times.$$"
    times > "$file"
    USAGE_CPU=$(awk '
        function secs(t,  m, s) { gsub(/,/, ".", t); m = t; sub(/m.*/, "", m); s = t; sub(/^[0-9]+m/, "", s); sub(/s$/, "", s); return m * 60 + s }
        { user += secs($1); sys += secs($2) }
        END { printf "%.3f %.3f\n", user, sys }' "$file")
    rm -f "$file"
}

# RAPL package energy in microjoules, or "NA" if not readable.
usage_energy_uj() {
    local counter=/sys/class/powercap/intel-rapl:0/energy_uj
    if [ -r "$counter" ] && cat "$counter" 2>/dev/null; then
        return 0
    fi
    echo NA
}

usage_energy_range_uj() {
    cat /sys/class/powercap/intel-rapl:0/max_energy_range_uj 2>/dev/null || echo 0
}

# "<rx_bytes> <tx_bytes>" summed over all interfaces except lo.
usage_net_bytes() {
    local -x LC_ALL=C
    # "iface: rx_bytes ..." - large counters may follow the colon without a space
    awk 'NR > 2 { i = index($0, ":"); name = substr($0, 1, i - 1); gsub(/ /, "", name)
                  if (name == "lo") next; split(substr($0, i + 1), f, " "); rx += f[1]; tx += f[9] }
         END { printf "%.0f %.0f\n", rx, tx }' /proc/net/dev 2>/dev/null ||
        echo "0 0"
}

usage_begin() {
    local -x LC_ALL=C
    local stage="$1" net
    usage_cpu_seconds
    net=$(usage_net_bytes)
    USAGE_START[$stage]="$(date +%s.%N) $USAGE_CPU $(usage_energy_uj) $net $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

usage_end() {
    local -x LC_ALL=C
    local run_id="$1" stage="$2" disk_path="${3:-}" exit_status="${4:-}"
    local start="${USAGE_START[$stage]:-}"
    [ -n "$start" ] && [[ "$run_id" =~ ^[0-9]+$ ]] || return 0
    unset "USAGE_START[$stage]"

    local t0 u0 s0 e0 rx0 tx0 started t1 u1 s1 e1 rx1 tx1 energy="NA" disk=""
    read -r t0 u0 s0 e0 rx0 tx0 started <<<"$start"
    t1=$(date +%s.%N)
    usage_cpu_seconds
    read -r u1 s1 <<<"$USAGE_CPU"
    e1=$(usage_energy_uj)
    read -r rx1 tx1 <<<"$(usage_net_bytes)"
    if [ "$e0" != "NA" ] && [ "$e1" != "NA" ]; then
        energy=$(awk -v a="$e0" -v b="$e1" -v r="$(usage_energy_range_uj)" \
            'BEGIN { d = b - a; if (d < 0 && r > 0) d += r; printf "%.3f", d / 1e6 }')
    fi
    if [ -n "$disk_path" ] && [ -e "$disk_path" ]; then
        disk=$(du -sb "$disk_path" 2>/dev/null | cut -f1)
    fi

    python3 "$PROJECT_ROOT/src/provenance.py" --db "$DB_FILE" stage \
        --run-id "$run_id" --stage "$stage" --started-at "$started" \
        --wall "$(awk -v a="$t0" -v b="$t1" 'BEGIN { printf "%.3f", b - a }')" \
        --user "$(awk -v a="$u0" -v b="$u1" 'BEGIN { printf "%.3f", b - a }')" \
        --sys "$(awk -v a="$s0" -v b="$s1" 'BEGIN { printf "%.3f", b - a }')" \
        --energy-joules "$energy" --net-rx $((rx1 - rx0)) --net-tx $((tx1 - tx0)) \
        --disk-bytes "$disk" --exit-status "$exit_status" ||
        log "[USAGE] Could not record usage for run $run_id stage $stage"
}

# Run a command under GNU time and store per-notebook usage.
# Usage: usage_notebook <run_id> <repository_id> <notebook_path> <command...>
# Returns the command's exit code.
usage_notebook() {
    local -x LC_ALL=C
    local run_id="$1" repository_id="$2" notebook_path="$3"
    shift 3
    local stats="${TMPDIR:-/tmp}/usage-notebook.$$" e0 e1 energy="NA" code
    e0=$(usage_energy_uj)
    if [ -x /usr/bin/time ]; then
        /usr/bin/time -f '%e %U %S %M %I %O' -o "$stats" "$@"
        code=$?
    else
        local t0=$(date +%s.%N)
        "$@"
        code=$?
        echo "$(awk -v a="$t0" -v b="$(date +%s.%N)" 'BEGIN { printf "%.2f", b - a }') NA NA NA NA NA" > "$stats"
    fi
    e1=$(usage_energy_uj)
    if [ "$e0" != "NA" ] && [ "$e1" != "NA" ]; then
        energy=$(awk -v a="$e0" -v b="$e1" -v r="$(usage_energy_range_uj)" \
            'BEGIN { d = b - a; if (d < 0 && r > 0) d += r; printf "%.3f", d / 1e6 }')
    fi
    local wall user sys rss fs_in fs_out
    # GNU time prepends "Command exited with non-zero status N" on failure.
    read -r wall user sys rss fs_in fs_out < <(tail -n 1 "$stats" 2>/dev/null)
    rm -f "$stats"
    if [[ "$run_id" =~ ^[0-9]+$ ]]; then
        python3 "$PROJECT_ROOT/src/provenance.py" --db "$DB_FILE" notebook-usage \
            --run-id "$run_id" ${repository_id:+--repository-id "$repository_id"} --notebook-path "$notebook_path" \
            --wall "${wall:-NA}" --user "${user:-NA}" --sys "${sys:-NA}" --max-rss-kb "${rss:-NA}" \
            --fs-in "${fs_in:-NA}" --fs-out "${fs_out:-NA}" --energy-joules "$energy" --exit-code "$code" ||
            log "[USAGE] Could not record usage for notebook $notebook_path"
    fi
    return "$code"
}
