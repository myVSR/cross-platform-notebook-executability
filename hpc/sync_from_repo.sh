#!/bin/bash
###############################################################################
# sync_from_repo.sh — copy the code a the cluster run needs from the repository into hpc/
#
# Only runtime files are copied, with their repository paths (so the pipeline runs
# unchanged with hpc/ as its project root). Edit code in the repository, never in
# hpc/, and run this again: copied files are overwritten, and MANIFEST.txt records
# the commit and the SHA-256 of every copied file. the cluster-only files in hpc/
# (prepare.sbatch, run_array.sbatch, merge.sh, sampling.sbatch, submit_sampling.sh, classify.sbatch, study.sh,
# study.py, README.md, data/,
# this script) are kept.
#   bash hpc/sync_from_repo.sh          # copy + MANIFEST.txt
#   bash hpc/sync_from_repo.sh --zip    # also hpc-<commit>.zip in the repository root, to copy to the HPC
# The zip leaves out results/, .env and data/sampling/. Commit first: with uncommitted changes in copied
# or the cluster-only files the zip is named hpc-<commit>-dirty.zip and MANIFEST.txt says so.
###############################################################################
set -euo pipefail
HPC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HPC/.." && pwd)"

FILES=(
    # configuration
    config/config.sh
    config/tool-requirements.txt
    requirements.txt
    # Bash pipeline (classification.sh is sourced; classification itself is off on the cluster)
    src/logging.sh src/checks.sh src/db.sh src/pyenv.sh src/requirements.sh src/notebooks.sh
    src/classification.sh src/repo.sh src/usage.sh
    # Python modules called by the pipeline
    src/zenodo_fetch.py src/provenance.py src/python_version.py src/licence.py src/dependencies.py
    src/install_env.py src/kernel_probe.py src/resource_files.py src/reproscore_stage.py src/metadata_snapshot.py
    src/data/pipreqs-mapping.txt src/data/import-overrides.txt src/data/NOTICE-pipreqs.txt
    # output comparison (thesis score + comparison version 2)
    analysis/__init__.py analysis/compare_notebook.py
    analysis/nbprocess/__init__.py analysis/nbprocess/diff.py analysis/nbprocess/loader.py
    analysis/nbprocess/summary.py analysis/nbprocess/filesystem.py analysis/nbprocess/outputs.py
    analysis/nbprocess/nondeterminism.py analysis/nbprocess/compare_cells.py analysis/nbprocess/error_context.py
    # classification stage after the run (classify.sbatch on the GPU partition)
    analysis/nbprocess/collector.py analysis/notebook_classification/__init__.py
    analysis/notebook_classification/categories.py analysis/notebook_classification/hybrid.py
    analysis/notebook_classification/local_llm.py analysis/notebook_classification/models.py
    analysis/notebook_classification/rules.py analysis/notebook_classification/storage.py
    analysis/notebook_classification/evaluation.py pipeline/classify_from_artifacts.py pipeline/install_ollama.sh
    # batch driver, HPC setup/check, merge tool (cohort lists are generated into data/ below)
    pipeline/run_full_sample.sh pipeline/hpc_setup_cluster.sh pipeline/hpc_preflight.sh
    pipeline/merge_task_dbs.py pipeline/build_licensee_sif.sh pipeline/topup_from_reserve.py pipeline/prepare_rerun.py pipeline/check_fixcheck.py data/cohort-fixcheck.csv
    pipeline/check_fixcheck_r3.py data/cohort-fixcheck-r3.csv
    # ReproScore (part of this repository, third_party/reproscore) and the pipeline's rubric
    config/reproscore_rubric.yaml third_party/reproscore/VERSION third_party/reproscore/LICENSE
    third_party/reproscore/config/default_rubric.yaml third_party/reproscore/src/__init__.py
    third_party/reproscore/src/scoring/__init__.py third_party/reproscore/src/scoring/rrs.py
    third_party/reproscore/src/scoring/ros.py third_party/reproscore/src/scoring/rcs.py
    third_party/reproscore/src/scoring/rubric.py third_party/reproscore/src/utils/__init__.py
    third_party/reproscore/src/utils/notebook_paths.py src/reproscore_rrs_worker.py
    third_party/reproscore-1.0/VERSION third_party/reproscore-1.0/LICENSE third_party/reproscore-1.0/config/default_rubric.yaml
    third_party/reproscore-1.0/src/__init__.py third_party/reproscore-1.0/src/scoring/__init__.py third_party/reproscore-1.0/src/scoring/rrs.py
    third_party/reproscore-1.0/src/scoring/ros.py third_party/reproscore-1.0/src/scoring/rcs.py third_party/reproscore-1.0/src/scoring/rubric.py
    third_party/reproscore-1.0/src/utils/__init__.py third_party/reproscore-1.0/src/utils/notebook_paths.py
    # sampling (notes/sampling-design.md): frames S0-S4; pilot input exported from the frozen DB
    analysis/sampling/README.md analysis/sampling/common.py analysis/sampling/probe_frames.py
    analysis/sampling/pilot_power.py analysis/sampling/codeberg_census.py analysis/sampling/github_id_sample.py
    analysis/sampling/zenodo_frame.py analysis/sampling/run_codeberg_census.sh analysis/sampling/run_zenodo_frame.sh
    analysis/sampling/run_github_pilot.sh analysis/sampling/draw_sample.py analysis/thesis/results/notebook-cohort.csv
    analysis/sampling/extend_zenodo_reserve.py
)

missing=0
for file in "${FILES[@]}"; do
    [ -f "$REPO/$file" ] || { echo "[SYNC] missing in the repository: $file"; missing=1; }
done
[ "$missing" -eq 0 ] || exit 1

for file in "${FILES[@]}"; do
    mkdir -p "$HPC/$(dirname "$file")"
    cp -p "$REPO/$file" "$HPC/$file"
done
chmod +x "$HPC"/pipeline/*.sh "$HPC"/pipeline/*.py "$HPC"/analysis/sampling/*.sh "$HPC"/analysis/sampling/*.py

# cohort lists (generated from data/evaluation-sample-*.csv)
python3 "$REPO/pipeline/make_cohort.py" 318 "$HPC/data/cohort-318.csv" >/dev/null
python3 "$REPO/pipeline/make_cohort.py" 326 "$HPC/data/cohort-326.csv" >/dev/null

# a draw folder prepared on the workstation for the round in study.env (NBF_DRAW_FROM; round 3: the cohort plus the
# Zenodo reserve extension). It goes into the bundle as NBF_DRAW_FROM, and study.py copies it once to NBF_DRAW_DIR
# (data/sampling/ is never in the zip, so the top-up log on the HPC is never overwritten).
draw_dir=$(sed -n 's/^NBF_DRAW_DIR=//p' "$HPC/study.env" | tr -d '"')
draw_from=$(sed -n 's/^NBF_DRAW_FROM=//p' "$HPC/study.env" | tr -d '"')
draw_files=()
if [ -n "$draw_from" ]; then
    [ -f "$REPO/$draw_dir/cohort-primary.csv" ] || { echo "[SYNC] study.env names $draw_dir, missing in the repository"; exit 1; }
    rm -rf "${HPC:?}/$draw_from"
    mkdir -p "$(dirname "$HPC/$draw_from")"
    cp -rp "$REPO/$draw_dir" "$HPC/$draw_from"
    while IFS= read -r f; do draw_files+=("$f"); done < <(cd "$HPC" && find "$draw_from" -type f | sort)
fi

commit=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)
HPC_ONLY=(hpc/prepare.sbatch hpc/run_array.sbatch hpc/merge.sh hpc/sampling.sbatch hpc/submit_sampling.sh hpc/classify.sbatch hpc/study.sh hpc/study.py hpc/study_driver.sbatch
            hpc/README.md hpc/sync_from_repo.sh hpc/study.env)
dirty=$(git -C "$REPO" status --porcelain --untracked-files=no -- "${FILES[@]}" "${HPC_ONLY[@]}" 2>/dev/null | wc -l)
{
    echo "source commit: $commit$([ "$dirty" -gt 0 ] && echo " + $dirty uncommitted change(s) in copied files")"
    echo "synced: $(date -Iseconds)"
    echo "files copied from the repository (do not edit them here):"
    (cd "$HPC" && sha256sum "${FILES[@]}" data/cohort-318.csv data/cohort-326.csv "${draw_files[@]}")
} > "$HPC/MANIFEST.txt"
echo "[SYNC] ${#FILES[@]} files + 2 cohort lists copied into $HPC (commit ${commit:0:7}$([ "$dirty" -gt 0 ] && echo ", with uncommitted changes"))"

if [ "${1:-}" = "--zip" ]; then
    name="hpc-${commit:0:7}$([ "$dirty" -gt 0 ] && echo "-dirty").zip"
    rm -f "$REPO/$name"
    (cd "$REPO" && zip -qr "$name" hpc -x 'hpc/results/*' 'hpc/.env' 'hpc/data/sampling/*' '*/__pycache__/*' \
        '*.pyc' 'hpc/*.zip')
    echo "[SYNC] $REPO/$name ($(du -h "$REPO/$name" | cut -f1)); unzip it on the HPC over the old hpc/ folder"
    [ "$dirty" -gt 0 ] && echo "[SYNC] WARNING: $dirty uncommitted change(s): commit, then run this again for a clean bundle"
fi
