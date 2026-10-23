#!/bin/bash
#SBATCH --job-name=nbf-rerun
#SBATCH --partition=standard
#SBATCH --time=3-00:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --array=0-31%16
#SBATCH --output=results/%x-%A_%a.out
#SBATCH --signal=B:USR1@900
###############################################################################
# The re-run as a SLURM job array (from the hpc/ folder, after prepare.sbatch):
#   cd hpc && sbatch run_array.sbatch                      # cohort-326 (option A), 32 tasks
#   NBF_COHORT=data/cohort-318.csv sbatch run_array.sbatch   # thesis cohort
#   sbatch --array=0-3 run_array.sbatch                      # small test: 4 tasks
# Each task takes every N-th resource of the cohort (N = number of array tasks) and
# writes its own database and folders under results/<run>/task-<i>/ (logs, artefacts,
# progress); clones and venvs go to node-local scratch and are deleted at the end.
# Classification is off (done afterwards from the stored notebooks).
# Time limit: 15 minutes before it, a task submits itself again for the same task index and results
# folder (at most NBF_MAX_RESUBMITS, default 3); the new job marks the unfinished resource INTERRUPTED,
# retries it and skips the finished ones (the history stays in the database).
# Afterwards: bash merge.sh results/<run>
# Needs ~/.notebookfair/env.sh (pipeline/hpc_setup_cluster.sh) and a .env file in this
# folder with GITHUB_TOKEN=... (GitHub allows 60 unauthenticated requests per hour).
###############################################################################
set -uo pipefail
BUNDLE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
source "$HOME/.notebookfair/env.sh" || { echo "run pipeline/hpc_setup_cluster.sh first"; exit 1; }
[ -f "$BUNDLE/.env" ] && grep -q '^GITHUB_TOKEN=.' "$BUNDLE/.env" || echo "[WARN] no GITHUB_TOKEN in $BUNDLE/.env"

TASK="${SLURM_ARRAY_TASK_ID:-0}"
TASKS="${NBF_TASKS:-$(( ${SLURM_ARRAY_TASK_MAX:-0} + 1 ))}"     # NBF_TASKS keeps the split after a resubmit
RUN="${NBF_RUN_NAME:-run-${SLURM_ARRAY_JOB_ID:-local}}"
RESULTS="$BUNDLE/results/$RUN/task-$TASK"
SCRATCH="${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/nbf-${SLURM_JOB_ID:-$$}"
mkdir -p "$RESULTS" "$SCRATCH"
trap 'rm -rf "$SCRATCH"' EXIT

export DB_FILE="$RESULTS/db.sqlite" OUTPUT_DIR="$RESULTS" LOG_DIR="$RESULTS/logs" ARTIFACT_DIR="$RESULTS/artifacts" \
       COMP_DIR="$RESULTS/comparisons" REPOS_DIR="$SCRATCH/repos" VENV_BASE_DIR="$SCRATCH/venvs" \
       WORKER_COUNT="$TASKS" WORKER_INDEX="$TASK" SAMPLE_CSV="$BUNDLE/${NBF_COHORT:-data/cohort-326.csv}" \
       CLASSIFICATION_ENABLED=false NBF_PRIVATE_DB=1
echo "[TASK] $TASK of $TASKS, run $RUN, cohort $SAMPLE_CSV, node $(hostname), results $RESULTS"
{                                                   # appended: one block per job (resubmits included)
    echo "run=$RUN task=$TASK tasks=$TASKS cohort=$SAMPLE_CSV node=$(hostname) job=${SLURM_JOB_ID:-} resubmission=${NBF_RESUBMITS:-0} started=$(date -Iseconds)"
    cat "$BUNDLE/MANIFEST.txt" 2>/dev/null | head -2
} >> "$RESULTS/task.info"
resubmits="${NBF_RESUBMITS:-0}"
on_time_limit() {
    echo "[TASK] $(date -Iseconds) time limit near"
    if [ "$resubmits" -lt "${NBF_MAX_RESUBMITS:-3}" ]; then
        sbatch --job-name="${SLURM_JOB_NAME:-nbf-rerun}" --array="$TASK" \
            --export=ALL,NBF_TASKS="$TASKS",NBF_RUN_NAME="$RUN",NBF_RESUBMITS=$((resubmits + 1)) \
            "$BUNDLE/run_array.sbatch" && echo "[TASK] resubmitted task $TASK of $RUN ($((resubmits + 1)))"
    else
        echo "[TASK] not resubmitted (limit reached): unfinished resources show up in the top-up re-run list"
    fi
    echo "time_limit=$(date -Iseconds) resubmitted=$((resubmits < ${NBF_MAX_RESUBMITS:-3}))" >> "$RESULTS/task.info"
    kill -TERM -- "-$child" 2>/dev/null
    wait "$child" 2>/dev/null
    exit 0
}
# Pre-flight: the package index must be reachable through the cluster proxy before any resource runs
# (the 2026-10 study run lost half its package installs to proxy failures). Up to NBF_INDEX_CHECKS checks, 60 s apart;
# if it never answers, this task runs nothing: its resources are re-run by the next top-up.
INDEX_URL="${NBF_PIP_INDEX_URL:-https://pypi.org/simple}"
index_ok=0
for attempt in $(seq 1 "${NBF_INDEX_CHECKS:-10}"); do
    if python3 -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=30).read(1)' "$INDEX_URL/pip/" 2>/dev/null; then
        index_ok=1
        break
    fi
    echo "[TASK] $(date -Iseconds) package index $INDEX_URL not reachable (check $attempt); waiting 60 s"
    sleep 60
done
echo "index_check=$([ "$index_ok" = 1 ] && echo ok || echo failed) index=$INDEX_URL proxy=${https_proxy:+set} at=$(date -Iseconds)" >> "$RESULTS/task.info"
echo "disk=$(df -Pk "$SCRATCH" | awk 'NR==2 {print int($4/1048576) " GB free of " int($2/1048576) " GB"}') scratch=$SCRATCH" >> "$RESULTS/task.info"
if [ "$index_ok" != 1 ]; then
    echo "[TASK] the package index is unreachable: no resource is run in this task (the next top-up re-runs them)"
    echo "finished=$(date -Iseconds) exit=1 reason=index-unreachable" >> "$RESULTS/task.info"
    exit 1
fi

trap on_time_limit USR1
setsid --wait bash "$BUNDLE/pipeline/run_full_sample.sh" &
child=$!
wait "$child"
status=$?
echo "finished=$(date -Iseconds) exit=$status" >> "$RESULTS/task.info"
