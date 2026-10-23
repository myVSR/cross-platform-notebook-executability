#!/bin/bash
###############################################################################
# hpc_preflight.sh — can the notebook pipeline run on this HPC system?
#
# Checks, without root and without installing anything:
#   1. node and SLURM context        5. Python build prerequisites (headers, libs)
#   2. internet access (APIs, PyPI,   6. Python packages the pipeline itself needs
#      git) incl. proxy settings      7. file systems: space, type, SQLite locking
#   3. command-line tools             8. SLURM limits and energy accounting
#   4. compilers and module system    9. energy counters, GPU/Ollama, containers
# Temporary files go to a private folder that is deleted at the end.
#
# Run it twice, because internet access often differs between node types:
#   login node:    bash pipeline/hpc_preflight.sh
#   compute node:  srun --time=00:30:00 --cpus-per-task=2 --mem=4G --pty bash pipeline/hpc_preflight.sh
#                  (or submit with sbatch; add --partition=... / --account=... as required)
# Optional, slower:
#   --build-python   also builds Python 3.11 with pyenv's python-build in the temp folder (5-15 min)
#   --container      also tries to run a container (apptainer/singularity, docker://ruby:3.3-slim)
#
# Output: printed and saved as hpc-preflight-<host>-<date>.txt in the current folder.
# Send that file back; nothing else is needed.
###############################################################################

BUILD_PYTHON=false
CONTAINER=false
for arg in "$@"; do
    case "$arg" in
        --build-python) BUILD_PYTHON=true ;;
        --container) CONTAINER=true ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    esac
done

REPORT="$PWD/hpc-preflight-$(hostname -s 2>/dev/null || hostname)-$(date +%Y%m%d-%H%M%S).txt"
TMPWORK=$(mktemp -d "${TMPDIR:-/tmp}/nbf-preflight.XXXXXX")
trap 'rm -rf "$TMPWORK"' EXIT
exec > >(tee "$REPORT") 2>&1

PASS=0; WARN=0; FAIL=0; BLOCKERS=()
pass() { PASS=$((PASS + 1)); printf '  [PASS] %s\n' "$*"; }
warn() { WARN=$((WARN + 1)); printf '  [WARN] %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$*"; }
blocker() { fail "$*"; BLOCKERS+=("$*"); }
info() { printf '  [INFO] %s\n' "$*"; }
section() { printf '\n== %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

echo "Notebook pipeline HPC preflight  $(date -Iseconds)"
if [ -n "${NBF_ENV_LOADED:-}" ]; then echo "Environment: $NBF_HOME/env.sh loaded"
elif [ -f "$HOME/.notebookfair/env.sh" ]; then echo "Note: ~/.notebookfair/env.sh exists but is not loaded; run 'source ~/.notebookfair/env.sh' first"; fi
echo "Report: $REPORT"

###############################################################################
section "1. Node and SLURM context"
info "host: $(hostname -f 2>/dev/null || hostname)"
info "OS: $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -s), kernel $(uname -r)"
info "CPU: $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | xargs), $(nproc 2>/dev/null) usable cores"
info "memory: $(awk '/MemTotal/ {printf "%.1f GiB", $2/1048576}' /proc/meminfo 2>/dev/null)"
info "user: $(id -un), shell bash ${BASH_VERSION}"
if [ -n "${SLURM_JOB_ID:-}" ]; then
    info "inside SLURM job $SLURM_JOB_ID (partition ${SLURM_JOB_PARTITION:-?}, node ${SLURMD_NODENAME:-?}, cpus ${SLURM_CPUS_ON_NODE:-?})"
    NODE_KIND=compute
else
    info "not inside a SLURM job (probably a login node)"
    NODE_KIND=login
fi
if [ "${BASH_VERSINFO[0]}" -ge 4 ]; then pass "bash >= 4 (associative arrays)"; else blocker "bash ${BASH_VERSION} is older than 4"; fi

###############################################################################
section "2. Internet access ($NODE_KIND node)"
for var in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY; do
    [ -n "${!var:-}" ] && info "$var=${!var}"
done
probe_url() {   # name url
    local name="$1" url="$2" code
    if have curl; then
        code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 10 --max-time 30 "$url" 2>/dev/null)
    elif have python3; then
        code=$(python3 -c "import urllib.request,sys
try: print(urllib.request.urlopen(sys.argv[1], timeout=30).status)
except Exception as e: print(getattr(e, 'code', '000'))" "$url" 2>/dev/null)
    else
        code=000
    fi
    # any HTTP answer means the host is reachable; 000 = no connection at all
    if [ "$code" = "407" ]; then blocker "$name: proxy requires authentication (HTTP 407) — $url"
    elif [ "$code" = "429" ]; then warn "$name reachable but rate-limited (HTTP 429)"
    elif [[ "$code" =~ ^[1-5][0-9][0-9]$ ]]; then pass "$name reachable (HTTP $code)"
    else blocker "$name NOT reachable from this node (no connection) — $url"; fi
}
probe_url "GitHub API"        "https://api.github.com/repos/octocat/Hello-World"
probe_url "Codeberg API"      "https://codeberg.org/api/v1/version"
probe_url "Zenodo API"        "https://zenodo.org/api/records/3362625"
probe_url "PyPI index"        "https://pypi.org/simple/pip/"
probe_url "PyPI files"        "https://files.pythonhosted.org/"
probe_url "Python sources (pyenv)" "https://www.python.org/ftp/python/"
if have git; then
    for repo in https://github.com/octocat/Hello-World https://codeberg.org/aiocoap/aiocoap; do
        if GIT_TERMINAL_PROMPT=0 timeout 60 git ls-remote --heads "$repo" >/dev/null 2>&1; then pass "git ls-remote $repo"
        else blocker "git cannot reach $repo"; fi
    done
fi

###############################################################################
section "3. Command-line tools"
need() {   # tool importance(blocker|warn) note
    local tool="$1" level="$2" note="$3" version
    if have "$tool"; then
        version=$("$tool" --version 2>&1 | head -1 | cut -c1-70)
        pass "$tool: $(command -v "$tool") ${version:+($version)}"
    elif [ "$level" = blocker ]; then blocker "$tool missing — $note"
    else warn "$tool missing — $note"; fi
}
need git blocker "cloning repositories"
need curl blocker "API calls, downloads"
need jq blocker "JSON in the bash pipeline"
need sqlite3 blocker "the pipeline database"
need python3 blocker "pipeline scripts"
need gzip blocker "artefact store"
need tar blocker "archives"
need unzip warn "ZIP archives (Python zipfile is used too)"
need bsdtar warn "Zenodo .rar/.7z archives (libarchive)"
need timeout blocker "per-notebook time limit (coreutils)"
need sha256sum blocker "checksums"
need awk blocker "bash pipeline"
need make blocker "building Python with pyenv"
need gcc blocker "building Python and source packages"
need g++ warn "C++ source packages"
need gfortran warn "Fortran source packages (old scipy etc.)"
need pkg-config warn "finding libraries for source builds"
need pyenv warn "installed by setup.sh into ~/.pyenv if missing (needs internet + compiler)"
if [ -x /usr/bin/time ] && /usr/bin/time -f '%e %M' true 2>/dev/null; then pass "GNU time (/usr/bin/time -f) for per-notebook usage"
else warn "GNU /usr/bin/time missing: per-notebook CPU/memory would not be recorded (wall time only)"; fi
if have python3; then
    pyv=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    if python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then pass "python3 $pyv >= 3.10"
    else blocker "python3 $pyv is older than 3.10 (pipeline scripts need 3.10+; 3.11+ preferred)"; fi
fi

###############################################################################
section "4. Compilers and module system"
if have module || type module >/dev/null 2>&1 || [ -n "${MODULESHOME:-}" ] || [ -n "${LMOD_CMD:-}" ]; then
    pass "environment modules available (${LMOD_VERSION:+Lmod $LMOD_VERSION}${MODULESHOME:+ $MODULESHOME})"
    avail=$( (module -t avail 2>&1 || true) | tr ' ' '\n' | grep -iE '^(python|gcc|openssl|sqlite|jq|git|apptainer|singularity|libffi|zlib|bzip2|xz|readline|tk|ncurses|libarchive|anaconda|miniconda|mamba|ruby|cuda)' | sort -u | head -80 | tr '\n' ' ')
    info "relevant modules: ${avail:-none listed}"
    info "loaded: $( (module -t list 2>&1 || true) | tr '\n' ' ' | cut -c1-300)"
else
    info "no module system found"
fi
if have gcc; then info "gcc: $(gcc -dumpfullversion 2>/dev/null || gcc -dumpversion)"; fi

###############################################################################
section "5. Python build prerequisites (pyenv compiles Python 3.8-3.14)"
compile_check() {   # name header lib(s) required(blocker|warn)
    local name="$1" header="$2" libs="$3" level="$4"
    have gcc || { warn "$name: no gcc to test"; return; }
    printf '#include <%s>\nint main(void){return 0;}\n' "$header" > "$TMPWORK/t.c"
    if gcc "$TMPWORK/t.c" -o "$TMPWORK/t" $libs >/dev/null 2>&1; then pass "$name ($header, $libs)"
    elif [ "$level" = blocker ]; then blocker "$name: $header / $libs not found (headers or -devel package / module needed)"
    else warn "$name: $header / $libs not found (Python builds without this module)"; fi
}
compile_check "OpenSSL (ssl, hashlib; pip needs it)" openssl/ssl.h "-lssl -lcrypto" blocker
compile_check "zlib" zlib.h "-lz" blocker
compile_check "libffi (ctypes)" ffi.h "-lffi" blocker
compile_check "bzip2" bzlib.h "-lbz2" warn
compile_check "xz/lzma" lzma.h "-llzma" warn
compile_check "sqlite3 (sqlite3 module)" sqlite3.h "-lsqlite3" warn
compile_check "readline" readline/readline.h "-lreadline" warn
compile_check "ncurses (curses)" ncurses.h "-lncurses" warn
compile_check "uuid" uuid/uuid.h "-luuid" warn
compile_check "tk (_tkinter; 4 thesis notebooks failed on it)" tk.h "-ltk -ltcl" warn
if have openssl; then info "openssl CLI: $(openssl version)"; fi
if [ "$BUILD_PYTHON" = true ]; then
    builder=""
    for candidate in "$HOME/.pyenv/plugins/python-build/bin/python-build" "$(command -v python-build 2>/dev/null)"; do
        [ -x "$candidate" ] && builder="$candidate" && break
    done
    if [ -z "$builder" ] && have git; then
        git clone -q --depth 1 https://github.com/pyenv/pyenv.git "$TMPWORK/pyenv" 2>/dev/null && builder="$TMPWORK/pyenv/plugins/python-build/bin/python-build"
    fi
    if [ -n "$builder" ]; then
        info "building Python 3.11 with $builder (log: $TMPWORK/build.log, shown below on failure) ..."
        start=$(date +%s)
        if "$builder" 3.11.9 "$TMPWORK/py311" > "$TMPWORK/build.log" 2>&1 && "$TMPWORK/py311/bin/python3" -c 'import ssl, ctypes, sqlite3, lzma, bz2, zlib'; then
            pass "Python 3.11.9 built in $(( $(date +%s) - start ))s; ssl/ctypes/sqlite3/lzma/bz2/zlib import"
        else
            blocker "building Python 3.11.9 failed or misses modules:"; tail -15 "$TMPWORK/build.log" | sed 's/^/        /'
        fi
    else
        warn "python-build not found and pyenv could not be cloned; build test skipped"
    fi
else
    info "test build skipped (run with --build-python to build Python 3.11 here)"
fi

###############################################################################
section "6. Python packages the pipeline itself needs (python3)"
if have python3; then
    for module in json sqlite3 tomllib nbformat nbconvert nbdime numpy PIL IPython; do
        if python3 -c "import $module" 2>/dev/null; then pass "import $module"
        else
            case "$module" in
                tomllib) warn "tomllib missing (python < 3.11): pyproject/Pipfile parsing is skipped" ;;
                nbformat|nbconvert|nbdime|numpy|PIL|IPython) warn "$module missing: install with 'python3 -m pip install --user -r requirements.txt' (or in a venv)" ;;
                *) blocker "standard module $module missing" ;;
            esac
        fi
    done
    pipv=$(python3 -m pip --version 2>/dev/null | awk '{print $2}')
    if [ -n "$pipv" ]; then info "pip $pipv (resource venvs upgrade pip; time travel needs pip >= 26 there)"
    else warn "pip not available for python3 (needed to install the pipeline's packages)"; fi
    if python3 -m venv "$TMPWORK/venv" >/dev/null 2>&1; then pass "python3 -m venv works"
    else blocker "python3 -m venv fails (python3-venv / ensurepip missing)"; fi
fi

###############################################################################
section "7. File systems"
check_fs() {   # label path
    local label="$1" path="$2" type free
    [ -n "$path" ] && [ -d "$path" ] || return
    type=$(stat -f -c %T "$path" 2>/dev/null)
    free=$(df -Ph "$path" 2>/dev/null | awk 'NR==2 {print $4 " free of " $2}')
    info "$label $path: ${type:-?}, $free"
    if [ -w "$path" ]; then
        local db="$path/.nbf-preflight-$$.sqlite"
        # two writers at once, waiting for the lock as the pipeline does (.timeout)
        if sqlite3 "$db" "PRAGMA journal_mode=WAL; CREATE TABLE t(x); INSERT INTO t VALUES (1);" >/dev/null 2>&1 &&
           ( sqlite3 -cmd ".timeout 20000" "$db" "BEGIN IMMEDIATE; INSERT INTO t VALUES (2); COMMIT;" >/dev/null 2>&1 &
             sqlite3 -cmd ".timeout 20000" "$db" "BEGIN IMMEDIATE; INSERT INTO t VALUES (3); COMMIT;" >/dev/null 2>&1 & wait ) &&
           [ "$(sqlite3 "$db" 'SELECT COUNT(*) FROM t;' 2>/dev/null)" = "3" ]; then
            pass "SQLite (WAL, two concurrent writers) works on $label"
        else
            warn "SQLite with concurrent writers fails on $label (${type:-?}): use one database per job there"
        fi
        rm -f "$db" "$db-wal" "$db-shm"
    else
        warn "$label not writable"
    fi
}
check_fs "home" "$HOME"
check_fs "TMPDIR" "${TMPDIR:-/tmp}"
for var in SCRATCH WORK WORKSPACE PROJECT DATA BEEGFS LUSTRE; do
    [ -n "${!var:-}" ] && check_fs "\$$var" "${!var}"
done
check_fs "current folder" "$PWD"
if have quota; then info "quota: $(quota -s 2>/dev/null | tail -n +3 | head -3 | tr -s ' ' | tr '\n' ';')"; fi
info "need: ~1-5 GB per resource venv (node-local scratch is best), pip cache, ~10-50 MB artefacts per resource"

###############################################################################
section "8. SLURM limits and energy accounting"
if have sinfo; then
    info "partitions (name, time limit, nodes, cpus/node, memory/node):"
    sinfo -h -o '%P %l %D %c %m' 2>/dev/null | head -15 | sed 's/^/         /'
else
    info "sinfo not available"
fi
if have scontrol; then
    energy=$(scontrol show config 2>/dev/null | grep -iE 'AcctGatherEnergyType|JobAcctGatherType' | tr -s ' ' | tr '\n' ';')
    info "accounting: ${energy:-unknown}"
    if echo "$energy" | grep -qiE 'AcctGatherEnergyType *= *acct_gather_energy/(rapl|ipmi|pm_counters|gpu|xcc)'; then
        pass "SLURM gathers energy per job (sacct ConsumedEnergy): footprint can be measured"
    else
        warn "SLURM energy gathering not configured: footprint will be estimated, not measured"
    fi
fi
if [ -n "${SLURM_JOB_ID:-}" ] && have sacct; then
    info "this job: $(sacct -j "$SLURM_JOB_ID" -n -X -o Timelimit,ConsumedEnergyRaw,AllocCPUS 2>/dev/null | head -1 | xargs)"
fi
if have sbatch; then pass "sbatch available (job arrays: one slice of resources per task)"; fi

###############################################################################
section "9. Energy counters, GPU/Ollama, containers"
if [ -r /sys/class/powercap/intel-rapl:0/energy_uj ]; then pass "RAPL energy counter readable"
elif [ -e /sys/class/powercap/intel-rapl:0 ]; then info "RAPL present but not readable (normal without root)"
else info "no RAPL interface"; fi
if have nvidia-smi; then info "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -2 | tr '\n' ';')"
else info "no GPU on this node"; fi
if have ollama; then info "ollama: $(ollama --version 2>&1 | head -1) (classifier gemma3:4b)"
else info "ollama not installed: run the notebook classifier separately (classify-only.sh) on the workstation"; fi
container=""
for tool in apptainer singularity docker podman; do
    if have "$tool"; then info "$tool: $($tool --version 2>&1 | head -1)"; container="${container:-$tool}"; fi
done
if [ -z "$container" ]; then
    warn "no container runtime: licence detection (licensee) would be 'unavailable' (already measured for all 318 resources on the workstation)"
elif [ "$CONTAINER" = true ] && [ "$container" != docker ]; then
    if timeout 900 "$container" exec docker://ruby:3.3-slim ruby -v >/dev/null 2>&1; then pass "$container can run a Docker image (licensee would work)"
    else warn "$container could not run docker://ruby:3.3-slim (registry access or permissions)"; fi
fi

###############################################################################
section "Summary"
echo "  PASS $PASS, WARN $WARN, FAIL $FAIL  ($NODE_KIND node)"
if [ "${#BLOCKERS[@]}" -eq 0 ]; then
    echo "  No blockers found on this node."
else
    echo "  Blockers:"
    printf '    - %s\n' "${BLOCKERS[@]}"
fi
[ "$NODE_KIND" = login ] && echo "  Run it also on a compute node (see the header): internet access often differs."
echo "  Report saved to $REPORT"
