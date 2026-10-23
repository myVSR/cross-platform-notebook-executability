#!/bin/bash

# =============================================================================
# pyenv.sh — Python version isolation via pyenv + venv
# Replaces the Docker build/run cycle from docker.sh
#
# Assumes pyenv is installed and available at $PYENV_ROOT (default: ~/.pyenv)
# Each repo gets its own venv under $VENV_BASE_DIR/<REPO_NAME>
# =============================================================================

PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
export PYENV_ROOT
export PATH="$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH"
eval "$(pyenv init -)"
VENV_BASE_DIR="${VENV_BASE_DIR:-$HOME/.repo_venvs}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-$PYENV_ROOT/versions/.notebookfair-pip-cache}"
export PIP_CACHE_DIR
NOTEBOOK_TIMEOUT_SECONDS="${NOTEBOOK_TIMEOUT_SECONDS:-1800}"
export NOTEBOOK_TIMEOUT_SECONDS
# messages of a full disk in nbconvert's output (ENOSPC, errno 28)
ENOSPC_PATTERN='No space left on device|\[Errno 28\]|Disk quota exceeded'
export NOTEBOOK_TIMEOUT_SECONDS

# Error state — mirrors DOCKER_ERROR_TYPE / DOCKER_ERROR_MESSAGE in docker.sh
ENV_ERROR_TYPE=""
ENV_ERROR_MESSAGE=""
REPO_VENV_DIR=""
REPO_PYTHON=""
REPO_PIP=""


# -----------------------------------------------------------------------------
# detect_python_version()
# Prints the full decision of src/python_version.py as JSON: evidence from
# runtime.txt, .python-version, environment.yml, Pipfile, pyproject.toml,
# setup.cfg/setup.py and the notebooks' language_info, the requested version
# and its source, and the version to use ("use"). See that file for the rules.
# Usage: detect_python_version <repo_dir> [notebook paths separated by ;]
# -----------------------------------------------------------------------------
detect_python_version() {
    python3 "$PROJECT_ROOT/src/python_version.py" "$1" --notebooks "${2:-}" --json
}

# -----------------------------------------------------------------------------
# ensure_tool_env()
# The Jupyter tool environment (nbconvert/nbclient/jupyter_client) that drives the
# kernels: pinned in config/tool-requirements.txt, built once with Python
# $TOOL_ENV_PYTHON, rebuilt when the pins change. Kept apart from every resource
# venv so that a resource's pins (tornado, traitlets, ...) can never break it.
# -----------------------------------------------------------------------------
ensure_tool_env() {
    local requirements="$PROJECT_ROOT/config/tool-requirements.txt" stamp="$TOOL_ENV_DIR/.requirements.sha256" want
    want=$(sha256sum "$requirements" | cut -d' ' -f1)
    if [ -x "$TOOL_ENV_DIR/bin/jupyter-nbconvert" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$want" ]; then
        return 0
    fi
    log "[TOOLS] Creating the Jupyter tool environment at $TOOL_ENV_DIR (Python $TOOL_ENV_PYTHON)..."
    local version
    version=$(ensure_pyenv_version "$TOOL_ENV_PYTHON") || return 1
    rm -rf "$TOOL_ENV_DIR"
    mkdir -p "$(dirname "$TOOL_ENV_DIR")"
    "$PYENV_ROOT/versions/$version/bin/python" -m venv "$TOOL_ENV_DIR" >> "${LOG_FILE:-/dev/null}" 2>&1 || return 1
    "$TOOL_ENV_DIR/bin/pip" install --disable-pip-version-check -q -r "$requirements" >> "${LOG_FILE:-/dev/null}" 2>&1 || return 1
    echo "$want" > "$stamp"
}

# Versions of the tool environment, as JSON (recorded per run).
tool_env_json() {
    "$TOOL_ENV_DIR/bin/python" - "$TOOL_ENV_DIR" <<'PY' 2>/dev/null || echo '{}'
import importlib.metadata as m, json, platform, sys
print(json.dumps(dict(path=sys.argv[1], python=platform.python_version(),
                      **{p: m.version(p) for p in ('nbconvert', 'nbclient', 'jupyter_client', 'nbformat')})))
PY
}

# Kernelspec of this run in $RUN_ARTIFACT_DIR/kernels/: the resource venv's python
# running ipykernel, with the resource's local-module folders on PYTHONPATH (from
# the dependency analysis). Sets RUN_KERNEL_NAME. Usage: write_run_kernelspec <repo_dir> <plan.json>
write_run_kernelspec() {
    local repo_dir="$1" plan="$2"
    RUN_KERNEL_NAME="nbf-run-${RUN_ID:-$$}"
    local dir="$RUN_ARTIFACT_DIR/kernels/$RUN_KERNEL_NAME"
    mkdir -p "$dir"
    python3 - "$REPO_PYTHON" "$repo_dir" "$plan" "$dir/kernel.json" "$RUN_KERNEL_NAME" "${REPO_VENV_DIR:-}" <<'PY'
import json, os, sys
python, repo, plan, target, name, venv = sys.argv[1:7]
paths = json.load(open(plan)).get('import_path', []) if os.path.exists(plan) else []
spec = dict(argv=[python, '-m', 'ipykernel_launcher', '-f', '{connection_file}'], display_name=name, language='python',
            metadata=dict(debugger=False))
# Always set PYTHONPATH: the kernel would otherwise inherit the pipeline's own PYTHONPATH
# (the project root), letting notebooks import the pipeline's modules (src, analysis, config).
env = dict(PYTHONPATH=os.pathsep.join(os.path.normpath(os.path.join(repo, p)) for p in paths))
if venv:
    # The venv is the active environment: shell commands in the notebook (!pip install, !python) use it. Without this
    # they ran the pipeline's own Python through the pyenv shims on PATH: in the 2026-10 study run 311 notebooks
    # (142 runs) installed into or upgraded the shared pipeline interpreter, which once broke the output comparison.
    # pip refuses to install outside a virtual environment, and installs keep the run's time-travel cutoff.
    env.update(PATH=os.pathsep.join([os.path.join(venv, 'bin'), os.environ.get('PATH', '')]), VIRTUAL_ENV=venv,
               PIP_REQUIRE_VIRTUALENV='true')
    report = os.path.join(os.path.dirname(plan), 'install-report.json')
    cutoff = json.load(open(report)).get('uploaded_prior_to') if os.path.exists(report) else None
    if cutoff:
        env['PIP_UPLOADED_PRIOR_TO'] = cutoff
spec['env'] = env
json.dump(spec, open(target, 'w'), indent=1)
PY
    export RUN_KERNEL_NAME
}

# -----------------------------------------------------------------------------
# pyenv_installed VERSION
# True if the interpreter that the pipeline uses ($PYENV_ROOT/versions/VERSION/bin/python)
# exists and runs. Deliberately not `pyenv versions --bare | grep`: on the cluster that check
# missed every installed version (run-9150494: 318/326 runs PYTHON_INSTALL_FAIL with
# "pyenv: .../versions/3.8.20 already exists"), while the interpreters themselves were fine.
# -----------------------------------------------------------------------------
pyenv_installed() {
    local python="$PYENV_ROOT/versions/$1/bin/python"
    [ -n "$1" ] && [ -x "$python" ] && "$python" -c 'import sys' >/dev/null 2>&1
}

# -----------------------------------------------------------------------------
# ensure_pyenv_version()
# Installs the requested Python version via pyenv if not already present.
# Accepts partial versions like "3.8" — resolves to latest patch.
# -----------------------------------------------------------------------------
ensure_pyenv_version() {
    local requested="$1"

    if pyenv_installed "$requested"; then
        log "[PYENV] Python $requested already installed." >&2
        echo "$requested"
        return 0
    fi

    # "3.8" -> newest 3.8.x; "3.8.10" -> exactly 3.8.10 if pyenv offers it,
    # otherwise the newest 3.8.x (an exact patch request used to fail here).
    local resolved minor_version available
    available=$(pyenv install --list 2>/dev/null | grep -E "^\s+[0-9]+\.[0-9]+\.[0-9]+$" | xargs -n1)
    if [[ "$requested" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && grep -qx "$requested" <<<"$available"; then
        resolved="$requested"
    else
        minor_version=$(cut -d. -f1-2 <<<"$requested")
        resolved=$(grep -E "^${minor_version//./\\.}\.[0-9]+$" <<<"$available" | sort -t. -k3,3n | tail -1)
        if [ -n "$resolved" ] && [ "$resolved" != "$requested" ]; then
            log "[PYENV] Python $requested resolved to $resolved" >&2
        fi
    fi

    if [ -z "$resolved" ]; then
        log "[ERROR] [PYENV] No installable Python version matches: $requested" >&2
        return 1
    fi

    if pyenv_installed "$resolved"; then
        log "[PYENV] Python $resolved already installed (resolved from $requested)." >&2
        echo "$resolved"
        return 0
    fi

    log "[PYENV] Installing Python $resolved (resolved from $requested)... (this can take several minutes the first time)" >&2
    local install_output
    if ! install_output=$(pyenv install "$resolved" 2>&1); then
        printf '%s\n' "$install_output" >> "$LOG_FILE"
        # another task may have finished the same build in the meantime
        if pyenv_installed "$resolved"; then
            log "[PYENV] Python $resolved is installed (pyenv install reported: $(printf '%s' "$install_output" | tail -1 | xargs))." >&2
            echo "$resolved"
            return 0
        fi
        local reason
        reason=$(printf '%s\n' "$install_output" | grep -iE 'permission denied|no space left|cannot|error:|failed' | tail -2 | xargs)
        [ -z "$reason" ] && reason=$(printf '%s\n' "$install_output" | grep -v '^$' | tail -2 | xargs)
        log "[ERROR] [PYENV] Failed to install Python $resolved — ${reason:-see build log}" >&2
        # ensure_pyenv_version is invoked via $(...) command substitution,
        # which forks a subshell. A plain variable assignment made in that
        # subshell (like the one below) never reaches the caller once the
        # subshell exits, so we also hand the reason back through a file
        # path the caller gave us in PYENV_INSTALL_FAILURE_REASON_FILE.
        if [ -n "${PYENV_INSTALL_FAILURE_REASON_FILE:-}" ]; then
            printf '%s' "$reason" > "$PYENV_INSTALL_FAILURE_REASON_FILE"
        fi
        PYENV_INSTALL_FAILURE_REASON="$reason"
        return 1
    fi
    printf '%s\n' "$install_output" >> "$LOG_FILE"

    log "[PYENV] Python $resolved installed successfully." >&2
    echo "$resolved"
}

# -----------------------------------------------------------------------------
# setup_pyenv_env()
# Creates a fresh venv for the repo using the correct Python version,
# installs requirements and any setup.py packages.
#
# Sets globals:
#   REPO_VENV_DIR  — path to the venv for this repo
#   REPO_PYTHON    — path to the python binary inside the venv
#   REPO_PIP       — path to pip inside the venv
# -----------------------------------------------------------------------------
setup_pyenv_env() {
    local repo_dir="$1"         # e.g. ./MyRepo
    local requirements_file="$2"  # e.g. ./MyRepo/requirements.txt
    local setup_paths="$3"       # semicolon-separated, relative to repo_dir

    ENV_ERROR_TYPE=""
    ENV_ERROR_MESSAGE=""

    # --- 1. Detect + install Python version ---
    local requested_version
    PYTHON_DECISION_JSON=$(detect_python_version "$repo_dir" "${NOTEBOOK_PATHS:-}")
    requested_version=$(jq -r '.use // empty' <<<"$PYTHON_DECISION_JSON" 2>/dev/null)
    if [ -z "$requested_version" ]; then
        log "[PYENV] Python version detection failed; using ${PYTHON_DEFAULT_VERSION:-3.10}"
        requested_version="${PYTHON_DEFAULT_VERSION:-3.10}"
        PYTHON_DECISION_JSON=""
    fi
    log "[PYENV] Python $requested_version ($(jq -r '"requested \(.requested) from \(.source)"' <<<"$PYTHON_DECISION_JSON" 2>/dev/null))"
    while IFS= read -r note; do
        [ -n "$note" ] && log "[PYENV] $note"
    done < <(jq -r '.notes[]?' <<<"$PYTHON_DECISION_JSON" 2>/dev/null)

    local python_version
    local reason_file
    reason_file=$(mktemp "${TMPDIR:-/tmp}/pyenv_install_reason.XXXXXX")
    PYENV_INSTALL_FAILURE_REASON_FILE="$reason_file"
    PYENV_INSTALL_FAILURE_REASON=""
    python_version=$(ensure_pyenv_version "$requested_version")
    local ensure_status=$?
    if [ -s "$reason_file" ]; then
        PYENV_INSTALL_FAILURE_REASON=$(cat "$reason_file")
    fi
    rm -f "$reason_file"
    unset PYENV_INSTALL_FAILURE_REASON_FILE
    if [ $ensure_status -ne 0 ] || [ -z "$python_version" ]; then
        ENV_ERROR_TYPE="PYTHON_INSTALL_FAIL"
        ENV_ERROR_MESSAGE="Could not install Python $requested_version inside the isolated container.${PYENV_INSTALL_FAILURE_REASON:+ Reason: $PYENV_INSTALL_FAILURE_REASON}"
        return 1
    fi

    local python_bin
    python_bin="$PYENV_ROOT/versions/$python_version/bin/python"

    if [ ! -x "$python_bin" ]; then
        log "[ERROR] [PYENV] Python binary not found at: $python_bin"
        ENV_ERROR_TYPE="PYTHON_BINARY_MISSING"
        ENV_ERROR_MESSAGE="Python binary missing: $python_bin"
        return 1
    fi

    # --- 2. Create fresh venv ---
    mkdir -p "$VENV_BASE_DIR"
    REPO_VENV_DIR="$VENV_BASE_DIR/$REPO_NAME"

    # Always start clean
    if [ -d "$REPO_VENV_DIR" ]; then
        log "[PYENV] Removing existing venv at $REPO_VENV_DIR"
        rm -rf "$REPO_VENV_DIR"
    fi

    log "[PYENV] Creating venv at $REPO_VENV_DIR using Python $python_version"
    if ! "$python_bin" -m venv "$REPO_VENV_DIR" >> "$LOG_FILE" 2>&1; then
        log "[ERROR] [PYENV] Failed to create venv"
        ENV_ERROR_TYPE="VENV_CREATE_FAIL"
        ENV_ERROR_MESSAGE="Failed to create venv using Python $python_version"
        return 1
    fi

    REPO_PYTHON="$REPO_VENV_DIR/bin/python"
    REPO_PIP="$REPO_VENV_DIR/bin/pip"
    export REPO_VENV_DIR REPO_PYTHON REPO_PIP

    # Record the version decision and the interpreter actually used.
    if [[ "${RUN_ID:-}" =~ ^[0-9]+$ ]]; then
        python3 "$PROJECT_ROOT/src/provenance.py" --db "$DB_FILE" environment --run-id "$RUN_ID" \
            --python-decision "${PYTHON_DECISION_JSON:-}" \
            --python-used "$("$REPO_PYTHON" -c 'import platform; print(platform.python_version())' 2>/dev/null)" \
            >>"$LOG_FILE" 2>&1 || log "[PROVENANCE] Could not record the environment of run $RUN_ID"
    fi

    # --- 3. Dependencies: evidence and plan (src/dependencies.py), installation (src/install_env.py) ---
    # Nothing is written into the resource: plan, install lists, freeze and logs go to the
    # run's artefact folder. Jupyter itself is NOT installed here (see ensure_tool_env).
    # pip itself is today's version (needed for --uploaded-prior-to); setuptools and
    # wheel are installed by src/install_env.py as of the resource's date.
    log "[PYENV] Upgrading pip in venv..."
    "$REPO_PIP" install --upgrade pip >> "$LOG_FILE" 2>&1
    local resource_date=""
    if [ "${PIP_TIME_TRAVEL:-true}" = "true" ] && [[ "${RUN_ID:-}" =~ ^[0-9]+$ ]]; then
        resource_date=$(sqlite3 "$DB_FILE" "SELECT version_date FROM resource_acquisitions WHERE repository_run_id=$RUN_ID;" 2>/dev/null)
    fi
    local install_dir="$RUN_ARTIFACT_DIR/install"
    mkdir -p "$install_dir"
    if [ -n "$setup_paths" ]; then
        log "[PYENV] Supplied setup paths ($setup_paths) are covered by dependency discovery (setup.py/setup.cfg/pyproject)"
    fi
    if ! python3 "$PROJECT_ROOT/src/dependencies.py" "$repo_dir" --notebooks "${NOTEBOOK_PATHS:-}" \
            --python "$REPO_PYTHON" --requirements "${REQUIREMENT_PATHS:-}" --json "$install_dir/dependencies.json" \
            >> "$LOG_FILE" 2>&1; then
        ENV_ERROR_TYPE="DEPENDENCY_ANALYSIS_FAIL"
        ENV_ERROR_MESSAGE="Could not analyse the resource's dependencies (see log)"
        return 1
    fi
    log "[PYENV] Installing dependencies (declared jointly, then inferred extras, then ipykernel)..."
    python3 "$PROJECT_ROOT/src/install_env.py" --python "$REPO_PYTHON" --plan "$install_dir/dependencies.json" \
        --out-dir "$install_dir" --timeout "${PIP_INSTALL_TIMEOUT:-3600}" \
        ${resource_date:+--resource-date "$resource_date"} \
        --python-selected "$("$REPO_PYTHON" -c 'import platform; print(platform.python_version())')" \
        --python-requested "$(jq -r '.requested // empty' <<<"${PYTHON_DECISION_JSON:-"{}"}" 2>/dev/null)" \
        2>&1 | tee -a "$LOG_FILE"
    local install_status=${PIPESTATUS[0]}
    if [[ "${RUN_ID:-}" =~ ^[0-9]+$ ]]; then
        python3 "$PROJECT_ROOT/src/provenance.py" --db "$DB_FILE" dependencies --run-id "$RUN_ID" \
            --plan "$install_dir/dependencies.json" --install "$install_dir/install-report.json" >> "$LOG_FILE" 2>&1 ||
            log "[PROVENANCE] Could not record the dependencies of run $RUN_ID"
    fi
    if [ "$install_status" -eq 75 ]; then     # the package index could not be reached: not a result of the resource
        ENV_ERROR_TYPE="INSTALL_NETWORK_FAIL"
        ENV_ERROR_MESSAGE="The package index ($PIP_INDEX_URL) could not be reached while installing dependencies (see install-report.json)"
        return 1
    fi

    # --- 4. Kernel for this run: a kernelspec pointing at the venv's python, probed once ---
    if ! ensure_tool_env; then
        ENV_ERROR_TYPE="TOOL_ENV_FAIL"
        ENV_ERROR_MESSAGE="Could not create the Jupyter tool environment at $TOOL_ENV_DIR"
        return 1
    fi
    write_run_kernelspec "$repo_dir" "$install_dir/dependencies.json"
    local probe
    probe=$(env -u PYTHONPATH JUPYTER_PATH="$RUN_ARTIFACT_DIR" "$TOOL_ENV_DIR/bin/python" "$PROJECT_ROOT/src/kernel_probe.py" \
        "$RUN_KERNEL_NAME" 2>>"$LOG_FILE")
    local probe_status=$? kernel_fallback=""
    if [ "$probe_status" -ne 0 ]; then
        # The ipykernel of the resource's date may not start with today's tools: retry with today's.
        log "[PYENV] Kernel did not start ($(jq -r '.error // ""' <<<"$probe" 2>/dev/null)); retrying with the current ipykernel"
        kernel_fallback="ipykernel $(jq -r '.ipykernel_version // "as of the cutoff"' <<<"$probe" 2>/dev/null) failed to start; upgraded to the current ipykernel"
        env -u PYTHONPATH "$REPO_PIP" install --upgrade ipykernel >> "$LOG_FILE" 2>&1
        probe=$(env -u PYTHONPATH JUPYTER_PATH="$RUN_ARTIFACT_DIR" "$TOOL_ENV_DIR/bin/python" "$PROJECT_ROOT/src/kernel_probe.py" \
            "$RUN_KERNEL_NAME" 2>>"$LOG_FILE")
        probe_status=$?
    fi
    if [[ "${RUN_ID:-}" =~ ^[0-9]+$ ]]; then
        python3 "$PROJECT_ROOT/src/provenance.py" --db "$DB_FILE" kernel --run-id "$RUN_ID" \
            --kernel-name "$RUN_KERNEL_NAME" --probe-json "${probe:-"{}"}" --tool-env-json "$(tool_env_json)" \
            --fallback "$kernel_fallback" >> "$LOG_FILE" 2>&1 || log "[PROVENANCE] Could not record the kernel of run $RUN_ID"
    fi
    if [ "$probe_status" -ne 0 ]; then
        ENV_ERROR_TYPE="KERNEL_START_FAIL"
        ENV_ERROR_MESSAGE="The notebook kernel could not be started: $(jq -r '.error // "see log"' <<<"$probe" 2>/dev/null)"
        return 1
    fi
    log "[PYENV] Kernel $RUN_KERNEL_NAME runs $(jq -r '"\(.executable) (Python \(.python_version), ipykernel \(.ipykernel_version))"' <<<"$probe" 2>/dev/null)"

    log "[PYENV] Environment ready. Python: $("$REPO_PYTHON" --version 2>&1)"
    return 0
}


# -----------------------------------------------------------------------------
# run_in_pyenv_env()
# Executes all notebooks in NOTEBOOK_PATHS using the venv set up by
# setup_pyenv_env(). Writes results to the exec log in the same format
# as entrypoint.sh so that notebooks.sh / compare_notebook_outputs is unaffected.
# -----------------------------------------------------------------------------
run_in_pyenv_env() {
    local repo_dir="$1"   # e.g. ./MyRepo

    ENV_ERROR_TYPE=""
    ENV_ERROR_MESSAGE=""

    # one execution log per run (a shared log let notebooks with the same path in
    # different resources pick up each other's status and duration)
    local EXEC_LOG="${RUN_ARTIFACT_DIR:-$LOG_DIR}/execution.log"
    export NOTEBOOK_EXEC_LOG="$EXEC_LOG"
    if [ -z "$NOTEBOOK_PATHS" ]; then
        log "[PYENV] No NOTEBOOK_PATHS provided, nothing to execute."
        ENV_ERROR_TYPE="NO_NOTEBOOK_PATHS"
        ENV_ERROR_MESSAGE="NOTEBOOK_PATHS was empty"
        return 1
    fi

    local any_executed=false

    IFS=';' read -ra NOTEBOOKS <<< "$NOTEBOOK_PATHS"
    for NOTEBOOK_PATH in "${NOTEBOOKS[@]}"; do
        NOTEBOOK_PATH=$(notebook_rel_path "$NOTEBOOK_PATH")
        local full_path="$repo_dir/$NOTEBOOK_PATH"

        if [ ! -f "$full_path" ]; then
            log "[PYENV] Notebook not found: $full_path"
            echo "EXEC_FAIL|$REPO_NAME|$NOTEBOOK_PATH|0|NOTEBOOK_NOT_FOUND" | tee -a "$EXEC_LOG"
            continue
        fi

        local notebook_dir
        notebook_dir=$(dirname "$full_path")
        local base_name
        base_name=$(basename "$NOTEBOOK_PATH" .ipynb)
        # The executed copy goes to the run's artefact folder, never into the resource.
        local output_dir="$RUN_ARTIFACT_DIR/executed/$(dirname "$NOTEBOOK_PATH")"
        local output_nb="$output_dir/${base_name}.ipynb"
        mkdir -p "$output_dir"

        log "[PYENV] Executing notebook: $NOTEBOOK_PATH"
        local start_ts log_offset
        start_ts=$(date +%s)
        log_offset=$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)

        # Run nbconvert from within the notebook's directory so relative
        # file paths inside the notebook resolve correctly
        (
            cd "$notebook_dir"
            # usage_notebook (src/usage.sh) runs the command under GNU time and
            # stores wall/CPU time, peak memory and I/O per notebook.
            usage_notebook "${RUN_ID:-}" "${REPO_ID:-}" "$NOTEBOOK_PATH" \
            timeout "$NOTEBOOK_TIMEOUT_SECONDS" env -u PYTHONPATH -u GITHUB_TOKEN -u CODEBERG_TOKEN -u ZENODO_TOKEN -u GITLAB_TOKEN JUPYTER_PATH="$RUN_ARTIFACT_DIR" \
                "$TOOL_ENV_DIR/bin/jupyter-nbconvert" \
                --to notebook \
                --execute \
                --allow-errors \
                --ExecutePreprocessor.kernel_name="$RUN_KERNEL_NAME" \
                "$(basename "$NOTEBOOK_PATH")" \
                --output-dir "$output_dir" \
                --output "${base_name}.ipynb" \
                >> "$LOG_FILE" 2>&1
        )
        local exit_code=$?
        local end_ts
        end_ts=$(date +%s)
        local duration=$(( end_ts - start_ts ))
        # the node-local disk ran full while the notebook ran (repo.sh: disk_check, recorded as DISK_FULL)
        disk_check "notebook $NOTEBOOK_PATH" || true
        if [ -z "${DISK_LOW:-}" ] && tail -c +"$((log_offset + 1))" "$LOG_FILE" 2>/dev/null | grep -qE "$ENOSPC_PATTERN"; then
            DISK_LOW="notebook $NOTEBOOK_PATH: no space left on device"
            log "[DISK] $NOTEBOOK_PATH ran out of disk space"
        fi

        if [ "$exit_code" -eq 124 ]; then
            log "[PYENV] Notebook timed out after ${NOTEBOOK_TIMEOUT_SECONDS}s: $NOTEBOOK_PATH"
            echo "EXEC_FAIL|$REPO_NAME|$NOTEBOOK_PATH|$duration|NOTEBOOK_TIMEOUT" | tee -a "$EXEC_LOG"
            continue
        fi

        if [ ! -f "$output_nb" ]; then
            log "[PYENV] Output notebook not created for $NOTEBOOK_PATH (exit $exit_code)"
            echo "EXEC_FAIL|$REPO_NAME|$NOTEBOOK_PATH|$duration" | tee -a "$EXEC_LOG"
            continue
        fi

        any_executed=true

        if grep -q '"output_type": "error"' "$output_nb"; then
            log "[PYENV] Notebook executed with errors: $NOTEBOOK_PATH"
            echo "SUCCESS_WITH_ERRORS|$REPO_NAME|$NOTEBOOK_PATH|$duration" | tee -a "$EXEC_LOG"
        else
            log "[PYENV] Notebook executed successfully: $NOTEBOOK_PATH"
            echo "SUCCESS|$REPO_NAME|$NOTEBOOK_PATH|$duration" | tee -a "$EXEC_LOG"
        fi
    done

    if [ "$any_executed" = false ]; then
        ENV_ERROR_TYPE="NOTEBOOK_EXECUTION_ERROR"
        ENV_ERROR_MESSAGE="No notebooks were successfully executed"
        return 1
    fi

    return 0
}


# -----------------------------------------------------------------------------
# cleanup_pyenv_env()
# Removes the repo's venv. Call after compare_notebook_outputs to keep
# disk usage in check.
# -----------------------------------------------------------------------------
cleanup_pyenv_env() {
    if [ -n "$REPO_VENV_DIR" ] && [ -d "$REPO_VENV_DIR" ]; then
        log "[PYENV] Cleaning up venv: $REPO_VENV_DIR"
        rm -rf "$REPO_VENV_DIR"
    fi
}


# -----------------------------------------------------------------------------
# analyze_env_error()  — mirrors analyze_container_error() in docker.sh
# Scans LOG_FILE for known error patterns and populates
# ENV_ERROR_TYPE / ENV_ERROR_MESSAGE.
# -----------------------------------------------------------------------------
analyze_env_error() {
    local log_file="$1"
    local error_type="ENV_RUN_FAIL"
    local error_message="Environment execution failed"

    if grep -qi "NoSuchKernel\|No such kernel" "$log_file"; then
        error_type="KERNEL_NOT_FOUND"
        error_message="Jupyter kernel not found"
    elif grep -qi "nbconvert.*failed\|Error executing notebook" "$log_file"; then
        error_type="NOTEBOOK_EXECUTION_ERROR"
        error_message="Failed to execute notebook"
    elif grep -qi "ModuleNotFoundError" "$log_file"; then
        error_type="MODULE_NOT_FOUND"
        missing=$(grep -oP "ModuleNotFoundError: No module named ['\"]?\K[^'\" \n]+" "$log_file" | head -1)
        error_message="Missing Python module: ${missing:-unknown}"
    elif grep -qi "ImportError" "$log_file"; then
        error_type="IMPORT_ERROR"
        error_message=$(grep -oP "ImportError.*" "$log_file" | head -1 | cut -c1-200)
    elif grep -qi "SyntaxError" "$log_file"; then
        error_type="SYNTAX_ERROR"
        error_message="Python syntax error in notebook"
    elif grep -qi "MemoryError" "$log_file"; then
        error_type="MEMORY_ERROR"
        error_message="Out of memory error"
    elif grep -qi "TimeoutError\|timeout" "$log_file"; then
        error_type="TIMEOUT_ERROR"
        error_message="Execution timed out"
    elif grep -qi "ConnectionError\|URLError" "$log_file"; then
        error_type="NETWORK_ERROR"
        error_message="Network connection error"
    fi

    ENV_ERROR_TYPE="$error_type"
    ENV_ERROR_MESSAGE="$error_message"
}
