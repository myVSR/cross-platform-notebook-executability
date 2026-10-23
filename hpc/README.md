# Running the re-run on the cluster

This folder contains only what a the cluster run needs. Most files are **copies** from the
repository (listed with their SHA-256 and source commit in `MANIFEST.txt`), made by
`sync_from_repo.sh`. Change code in the repository, never here, then run
`bash hpc/sync_from_repo.sh` again. The the cluster-only files are this README,
`prepare.sbatch`, `run_array.sbatch`, `merge.sh`, `sampling.sbatch`, `submit_sampling.sh` and `data/` (cohort lists).

Not included, because it does not run on the cluster: the knowledge graph (built after merging),
the website, tests and the thesis. Licence detection runs with Singularity (step 2b); without the
image it records "unavailable" and `pipeline/licences_from_artifacts.py` detects the licences afterwards
on the workstation from the stored files.

## The whole study in one command

`bash study.sh next` submits every stage that is due and does nothing else. Run it again whenever you like:
once a day, after a job has finished, or from `scrontab`. It checks what is finished or still running first,
so nothing is ever submitted twice. `bash study.sh status` only shows where every stage stands.

| Stage | Submitted job(s) | Done when |
|---|---|---|
| 1 prepare | `prepare.sbatch` (Pythons, tool env, licensee SIF) | `~/.notebookfair/prepare.ok` |
| 2 sampling | `sampling.sbatch probe/codeberg/zenodo/github` | frames finished + summaries exported |
| 3 draw | `sampling.sbatch draw` | `data/sampling/draw/<date>/cohort-primary.csv` |
| 4 passes | `run_array.sbatch` (job name `nbf-study`, run `study-<n>-<kind>`) | a top-up needs nothing more |

After each pass, `study.sh next` merges **all** passes into `results/study/merged.sqlite` (rebuilt, oldest first).
`pipeline/topup_from_reserve.py` then forms the next pass:
- **replacements** from the reserve, for resources that could not be obtained or have no Python notebook;
- **re-runs**, for pipeline errors and interrupted or never-run resources (at most 2 per resource).

State: `results/study/state.json`; every replacement and re-run: `data/sampling/draw/<date>/topup-log.csv`.
Separately: `bash study.sh pilot` (thesis cohort 318, method comparison) and `bash study.sh classify`
(after the study, GPU partition, see "Classification").

Before the first `next`, once:
1. `.env` in this folder with `GITHUB_TOKEN` (needed) and `ZENODO_TOKEN` (optional).
2. `~/.notebookfair/licensee-10.1.0.docker.tar` copied from the workstation (step 2b).

**When something stops:**
- **Sampling jobs** resubmit themselves before the time limit and continue from their SQLite state.
- **Run tasks** resubmit themselves too: the unfinished resource is marked `INTERRUPTED` (never deleted) and retried, and finished resources are skipped.
- **A crash** without a resubmit leaves the resource `INTERRUPTED` or never run; the next top-up re-runs it.
- **All provenance stays:** every attempt is a row in `repository_runs`, with its acquisitions, stage usage, licences, ReproScore and artefacts. Analyses use the latest run per resource.

## Round 3: the whole cohort again, Zenodo to 400 (2026-10-21)

Why (notes/analysis-plan.md, section 11, written before the run):
- in round 2 the kernel ran without the resource's venv active, so `!pip install` in a notebook used the pipeline's
  Python (311 notebooks, 142 runs);
- 8 resources were damaged by full node-local disks, a crashed comparison and two path bugs;
- the Zenodo reserve ran out at 355 of 400 analysable records.

All three are fixed. `study.env` selects round 3:
- `NBF_STUDY=study-r3`: everything goes to `results/study-r3/`; rounds 1 and 2 are not touched;
- pass 1 is the whole cohort of round 2 (1,670 resources, `cohort-all.csv`);
- the draw folder was prepared on the workstation, with the Zenodo reserve extended along the same seed sequence
  (`analysis/sampling/extend_zenodo_reserve.py`). The zip carries it as `data/draw-2026-10-09-r3/`, and the first
  `study.sh next` copies it once to `data/sampling/draw/2026-10-09-r3/`. A later unzip never touches that copy.

**1. Trial run first** (15 resources, about 1–2 h): the 8 damaged resources and 7 with `!pip` lines.
```
cd ~/hpc && unzip -o ../hpc-<commit>.zip -d .. && head -2 MANIFEST.txt
sbatch --job-name=nbf-fixcheck --array=0-14 --time=04:00:00 \
       --export=ALL,NBF_COHORT=data/cohort-fixcheck-r3.csv,NBF_RUN_NAME=fixcheck-r3 run_array.sbatch
squeue -u $USER                                      # wait until nbf-fixcheck has finished
python3 pipeline/check_fixcheck_r3.py results/fixcheck-r3
```
It must end with `PASSED` (on round 2 it fails for all 15). A `WARN` line asks you to look at a resource that ended
without execution. Send me the output.

**2. Then the whole round:**
```
bash study.sh status                                 # shows: round study-r3, draw data/sampling/draw/2026-10-09-r3
bash study.sh autopilot                              # pass 1 (~17 h), merge, top-ups (Zenodo extension), re-runs
```
Bring back `results/study-r3/` (with `MANIFEST.txt` and `state.json`) and `data/sampling/draw/2026-10-09-r3/`
(its `topup-log.csv` records every replacement).

## Round 2: the whole cohort again (2026-10-11)

Round 1 (`results/study/`) lost packages in 51% of its runs, for two reasons:
- the cluster's pip configuration used the mirror `mirrors.bfsu.edu.cn` plus an NVIDIA extra index;
- that mirror sat behind a proxy that often answered 500, and it has no upload-time metadata for the time travel.

The pipeline now:
- ignores the machine's pip configuration and uses PyPI only (`config/config.sh`, `src/install_env.py`);
- retries network failures, and records a persistent one as `INSTALL_NETWORK_FAIL` (a pipeline error, re-run);
- checks that the index is reachable before a task starts, and waits after a network failure;
- skips a resource without a run record when the node-local disk has less than 12 GB free.

**Before round 2: test the fixes on 6 resources** (about 1 h). Each resource had one round-1 problem:
- proxy failures;
- the mirror without upload-time metadata;
- all notebooks failed (`ENV_RUN_FAIL` without executions);
- a Zenodo archive deleted after extraction;
- a tar archive with links refused (Python 3.11.0 has no data filter);
- a public record recorded as invalid (network at validation).
```
sbatch --job-name=nbf-fixcheck --array=0-5 --time=04:00:00 \
       --export=ALL,NBF_COHORT=data/cohort-fixcheck.csv,NBF_RUN_NAME=fixcheck run_array.sbatch
squeue -u $USER                                      # wait until nbf-fixcheck has finished
python3 pipeline/check_fixcheck.py results/fixcheck --baseline results/study/merged.sqlite
```
It must end with `PASSED`; on round 1 the same check fails for every fix. Send me the output, or copy `results/fixcheck/` back.
The job name is not `nbf-study`, so `study.sh` is not affected; delete `results/fixcheck/` afterwards if you like.

`study.env` selects round 2:
- `NBF_STUDY=study-r2`: everything goes to `results/study-r2/`; round 1 is not touched;
- pass 1 is the whole cohort of round 1: 1,670 resources, primary plus used reserve (`cohort-all.csv`);
- the draw folder `data/sampling/draw/2026-10-09-r2/` is prepared automatically from `2026-10-09/` on the first `next` (`pipeline/prepare_rerun.py`, see its `RERUN.md`).

```
# on your machine
bash hpc/sync_from_repo.sh --zip                   # after committing
# on the cluster (keep results/, data/sampling/ and .env when unzipping)
cd ~/hpc && unzip -o ../hpc-<commit>.zip -d .. && cat MANIFEST.txt | head -2
python3 -c "import urllib.request; urllib.request.urlopen('https://pypi.org/simple/pip/', timeout=30); print('pypi ok')"
bash study.sh status                                 # shows: round study-r2
bash study.sh autopilot                              # pass 1 (~1.5-2 days), merge, top-ups, re-runs; stops when complete
```
Progress: `bash study.sh status`, `tail results/study-r2/driver.log`, `grep -h index_check results/study-r2-*/task-*/task.info`.
When it is complete, copy `results/study-r2/` (with `MANIFEST.txt` and `state.json`) and `data/sampling/draw/2026-10-09-r2/` back. Then run the analysis:
`run_analysis.py --db results/study-r2/merged.sqlite --draw-dir data/sampling/draw/2026-10-09-r2 --infrastructure none`.

## Unattended: `bash study.sh autopilot`

`bash study.sh autopilot` submits `study_driver.sbatch` (job `nbf-driver`, 1 CPU, 2 GB). The driver:
- runs `study.py next` every 30 minutes (`NBF_DRIVER_INTERVAL`, in seconds) until the study is complete;
- submits the draw and each pass when it is due, and merges and tops up after each pass;
- never stops on an error. A failed round (sbatch refused, merge or top-up error) goes to `results/study/driver-errors.log` and is tried again in the next round;
- resubmits itself before its 3-day limit;
- writes `results/study/COMPLETE` and stops when the study is complete.

It is safe to combine with manual `study.sh next` calls: nothing is submitted twice.

Also robust against:
- a task database that cannot be read: the merge reports it and continues;
- a pass whose submission failed after its top-up: submitted in the next round;
- a pass that produced no database at all: reported in every round, nothing resubmitted until you look.

Watch from anywhere:
- `bash study.sh status` (passes, driver, number of failed rounds);
- `tail results/study/driver.log`;
- `results/study/driver-errors.log`.

Stop: `scancel --name=nbf-driver`.

## Steps

1. **Copy the folder to the cluster** (or clone the repository there and use its `hpc/` folder; a clone
   lets runs record the pipeline commit).
2. **Setup** (login node, once, ~20 min):
   `bash pipeline/hpc_setup_cluster.sh` — jq, SQLite, pyenv and the pipeline's Python in
   `~/.notebookfair/`, plus `~/.notebookfair/env.sh`.
2b. **Licence detector** (once): the cluster has no Docker, so the same licensee image runs with Singularity.
   On the workstation: `bash pipeline/build_licensee_sif.sh export` (writes `licensee-10.1.0.docker.tar`
   + `.json`, ~110 MB), then `scp licensee-10.1.0.docker.tar* <hpc>:~/.notebookfair/`.
   `prepare.sbatch` builds `~/.notebookfair/licensee-10.1.0.sif` from it and tests it (or by hand:
   `bash pipeline/build_licensee_sif.sh build`).
3. **GitHub token:** create `.env` in this folder with `GITHUB_TOKEN=...` (a token without
   scopes; unauthenticated GitHub allows only 60 requests per hour, shared by all jobs).
   `.env` is ignored by git.
4. **Build the Pythons and the Jupyter tool environment** (once):
   `mkdir -p results && sbatch prepare.sbatch`
5. **Test with a few tasks first**, e.g. `sbatch --array=0-1 run_array.sbatch` with
   `NBF_COHORT=data/cohort-test.csv` (4 small resources), then the full run:
   `sbatch run_array.sbatch` (cohort option A, `data/cohort-326.csv`, 32 tasks, at most 16 at a time) or
   `NBF_COHORT=data/cohort-318.csv sbatch run_array.sbatch` (thesis cohort).
   Each task writes `results/<run>/task-<i>/` (its own database, logs, artefacts, progress file,
   `task.info`); clones and venvs live on node-local scratch and are deleted at the end.
6. **Merge:** `bash merge.sh results/<run>` → `results/<run>/merged.sqlite`, plus a status summary.
7. **Bring back** `results/<run>/` (merged database and artefacts) to the workstation for
   classification, analysis and the knowledge graph.

## Classification (after a run, GPU partition)

Notebook purpose is classified after the run from the stored notebooks and READMEs (the runs keep
`CLASSIFICATION_ENABLED=false`): rules + local LLM (Ollama, `gemma3:4b`, context 8,192 tokens, prompt 1.2.0),
one consistent pass per run, written into `merged.sqlite`.

1. Once, login node: `bash pipeline/install_ollama.sh` — Ollama v0.32.15 (same as the workstation) in
   `~/.notebookfair/ollama`, model pulled, versions and model digest in `~/.notebookfair/ollama/install.json`
   (~1.4 GB + 3.3 GB).
2. After `merge.sh`: `sbatch classify.sbatch results/<run>` (1 GPU, resumable, resubmits itself before the
   time limit). Result: view `notebook_classifications_current` in `merged.sqlite`, each row with run, pass,
   model digest, prompt tokens, truncation flag and README use; pass summaries in
   `results/<run>/classification-passes.jsonl`.
3. Rules only (no GPU, quick check): `python3 pipeline/classify_from_artifacts.py --db results/<run>/merged.sqlite --rules-only`.

## Sampling (frames for the new probability sample)

Steps S0–S4 of `notes/sampling-design.md` (scripts: `analysis/sampling/`, see its README). They only make
API requests, so each runs as a 1-CPU job; all are resumable and resubmit themselves before the time limit.

1. `.env` in this folder: `GITHUB_TOKEN=...` (needed for S3) and `ZENODO_TOKEN=...` (optional: 100 instead
   of 25 hits per page). Codeberg needs no token (2,000 requests per 10 minutes either way).
2. `bash submit_sampling.sh` — checks both tokens, then submits four jobs:

   | Job | Step | Duration (estimate) |
   |---|---|---|
   | `nbf-sampling-probe` | S0 frame probes + S1 pilot power | minutes |
   | `nbf-sampling-codeberg` | S2 census: listing (2 passes), `/languages` for ~350k repositories, tree check, export | ~2 days |
   | `nbf-sampling-zenodo` | S4 Z1/Z2/Z3 frames, 1,000 tar + 20,000 ZIP inspections, export | ~1 day with token |
   | `nbf-sampling-github` | S3 exact-ID pilot (1,000 IDs, report), then the same sequence extended to 650 repositories with notebooks | pilot ~15 min, extension hours to ~1 day; skipped if the token is rejected |

   Single steps: `bash submit_sampling.sh github`. Codeberg checks repositories in a seeded random order,
   so stopping early still leaves a random sample. Use `NBF_CODEBERG_STOP_AFTER=1000 bash submit_sampling.sh codeberg`
   to stop once 1,000 repositories with notebooks are found (default: full census). `scancel` at any time
   works too; then run `python3 analysis/sampling/codeberg_census.py export`.
3. **Draw (S5)** when the jobs are done: `bash submit_sampling.sh draw` (it refuses while a frame is unfinished) →
   `data/sampling/draw/<date>/cohort-primary.csv` (~1,500 resources: 500 per platform, random processing order),
   `cohort-reserve.csv`, `draw.csv` (strata, weights, version checks), `summary.json`. Then the pipeline:
   `NBF_COHORT=data/sampling/draw/<date>/cohort-primary.csv sbatch run_array.sbatch`.
   After the run and `merge.sh`: `python3 pipeline/topup_from_reserve.py --draw-dir data/sampling/draw/<date>
   --db results/<run>/merged.sqlite` → `cohort-topup-<n>.csv` (replacements for resources that could not be
   obtained or have no Python notebook, same stratum/year first) and `cohort-rerun-<n>.csv` (pipeline errors);
   run both, merge, repeat until every platform has 400 analysable resources or the reserve is used up
   (`topup-log.csv` records every replacement).
4. Progress: `squeue -u $USER`, `results/nbf-sampling-<step>-<job>.out`, `data/sampling/<platform>/run.log`,
   `python3 analysis/sampling/codeberg_census.py status`, `python3 analysis/sampling/zenodo_frame.py status`.
5. Results: `data/sampling/` in this folder. Bring the whole folder back to the workstation as the repository's
   `data/sampling/`: frames, summaries, crawl state and request logs.

## Notes

- Settings for a run: `NBF_COHORT`, `NBF_RUN_NAME` (default `run-<array job id>`), and the usual
  pipeline variables (e.g. `NOTEBOOK_TIMEOUT_SECONDS`, `PIP_TIME_TRAVEL`).
- Re-running failed resources: a task skips resources that already have a run in its own
  database; delete those rows (or the task folder) to retry.
- Footprint: SLURM does not gather energy on the cluster; usage is recorded per stage and notebook
  and the energy is estimated (`analysis/footprint.py` in the repository).
