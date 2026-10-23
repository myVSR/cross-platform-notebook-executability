#!/bin/bash
#SBATCH --job-name=nbf-sampling
#SBATCH --partition=standard
#SBATCH --time=3-00:00:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --output=results/%x-%j.out
#SBATCH --signal=B:USR1@900
###############################################################################
# Sampling steps (notes/sampling-design.md, analysis/sampling/README.md) as SLURM jobs.
# Usually submitted by submit_sampling.sh; one step per job:
#   sbatch --job-name=nbf-sampling-probe    sampling.sbatch probe      # S0 probes + S1 pilot power (minutes)
#   sbatch --job-name=nbf-sampling-codeberg sampling.sbatch codeberg   # S2 census (~2 days)
#   sbatch --job-name=nbf-sampling-zenodo   sampling.sbatch zenodo     # S4 frame + inspections (~1 day)
#   sbatch --job-name=nbf-sampling-github   sampling.sbatch github     # S3 pilot (1,000 IDs) + extension to 650 screened-in
#   sbatch --job-name=nbf-sampling-draw     sampling.sbatch draw       # S5 draw, after S2-S4 (~1 hour: Zenodo version checks)
# The jobs only make API requests (1 CPU). Every step is resumable: 15 minutes before the time
# limit the job submits itself again (at most NBF_MAX_RESUBMITS times, default 3) and the new job
# continues where the old one stopped. Results: data/sampling/<platform>/ in this folder.
# Needs ~/.notebookfair/env.sh (pipeline/hpc_setup_cluster.sh) and .env in this folder with
# GITHUB_TOKEN (github, probe) and ZENODO_TOKEN (zenodo; optional, 4x fewer requests).
###############################################################################
set -uo pipefail
STEP="${1:?usage: sbatch sampling.sbatch probe|codeberg|zenodo|github|draw}"
BUNDLE="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
source "$HOME/.notebookfair/env.sh" || { echo "run pipeline/hpc_setup_cluster.sh first"; exit 1; }
cd "$BUNDLE" || exit 1

case "$STEP" in
    probe)    command=(bash -c 'python3 analysis/sampling/probe_frames.py && python3 analysis/sampling/pilot_power.py') ;;
    codeberg) command=(bash analysis/sampling/run_codeberg_census.sh) ;;
    zenodo)   command=(bash analysis/sampling/run_zenodo_frame.sh) ;;
    github)   command=(bash analysis/sampling/run_github_pilot.sh) ;;
    draw)     command=(python3 analysis/sampling/draw_sample.py) ;;
    *) echo "unknown step: $STEP"; exit 2 ;;
esac

resubmits="${NBF_RESUBMITS:-0}"
on_time_limit() {
    echo "[SAMPLING] $(date -Iseconds) time limit near (job ${SLURM_JOB_ID:-})"
    if [ "$resubmits" -lt "${NBF_MAX_RESUBMITS:-3}" ]; then
        sbatch --job-name="${SLURM_JOB_NAME:-nbf-sampling}" --export=ALL,NBF_RESUBMITS=$((resubmits + 1)) \
            "$BUNDLE/sampling.sbatch" "$STEP" && echo "[SAMPLING] resubmitted ($((resubmits + 1)))"
    else
        echo "[SAMPLING] not resubmitted: limit of ${NBF_MAX_RESUBMITS:-3} reached; submit again by hand to continue"
    fi
    kill -TERM -- "-$child" 2>/dev/null       # the whole process group: runner script and its python
    wait "$child" 2>/dev/null
    exit 0
}
trap on_time_limit USR1

echo "[SAMPLING] step $STEP, job ${SLURM_JOB_ID:-local}, node $(hostname), resubmission $resubmits, $(date -Iseconds)"
head -2 "$BUNDLE/MANIFEST.txt" 2>/dev/null
setsid --wait "${command[@]}" &              # own process group, so the handler can stop all of it
child=$!
wait "$child"
status=$?
echo "[SAMPLING] step $STEP finished with exit $status at $(date -Iseconds)"
exit $status
