#!/bin/bash
#SBATCH --job-name=nbf-prepare
#SBATCH --partition=standard
#SBATCH --time=04:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=16G
#SBATCH --output=results/%x-%j.out
###############################################################################
# Run once before the array (from the hpc/ folder): builds every Python the
# notebooks may need (3.8-3.14) with pyenv, and the pinned Jupyter tool
# environment, so that array tasks never build interpreters at the same time.
#   cd hpc && mkdir -p results && sbatch prepare.sbatch
###############################################################################
set -uo pipefail
BUNDLE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
source "$HOME/.notebookfair/env.sh" || { echo "run pipeline/hpc_setup_cluster.sh first"; exit 1; }
export PROJECT_ROOT="$BUNDLE" LOG_FILE="$BUNDLE/results/prepare-${SLURM_JOB_ID:-local}.log"
export MAKE_OPTS="-j${SLURM_CPUS_PER_TASK:-4}"
source "$BUNDLE/config/config.sh"
source "$BUNDLE/src/logging.sh"
source "$BUNDLE/src/pyenv.sh"
status=0
for version in 3.8 3.9 3.10 3.11 3.12 3.13 3.14; do
    if resolved=$(ensure_pyenv_version "$version"); then
        "$PYENV_ROOT/versions/$resolved/bin/python" -c 'import ssl, sqlite3, ctypes, lzma, bz2, zlib' \
            && echo "[PREPARE] Python $resolved ok" || { echo "[PREPARE] Python $resolved misses modules"; status=1; }
    else
        echo "[PREPARE] Python $version could not be built (see $LOG_FILE)"; status=1
    fi
done
if ensure_tool_env; then echo "[PREPARE] tool environment: $(tool_env_json)"; else echo "[PREPARE] tool environment FAILED"; status=1; fi
# licensee for licence detection (no Docker on the cluster): SIF from the image exported on the workstation
SIF="${LICENSEE_SIF:-$HOME/.notebookfair/licensee-10.1.0.sif}"
if [ -f "$SIF" ]; then
    echo "[PREPARE] licensee SIF present: $SIF"
elif [ -f "$HOME/.notebookfair/licensee-10.1.0.docker.tar" ]; then
    bash "$BUNDLE/pipeline/build_licensee_sif.sh" build || { echo "[PREPARE] licensee SIF build FAILED"; status=1; }
else
    echo "[PREPARE] no licensee image: licences are recorded as unavailable (copy licensee-10.1.0.docker.tar, see README)"
fi
if [ "$status" -eq 0 ]; then                          # marker for study.sh
    { echo "prepared=$(date -Iseconds) job=${SLURM_JOB_ID:-local}"; head -1 "$BUNDLE/MANIFEST.txt"; } > "$HOME/.notebookfair/prepare.ok"
fi
exit $status
