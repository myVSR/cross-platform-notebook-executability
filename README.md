# Cross-platform notebook executability: replication package

This repository accompanies the MSR 2027 submission *Preserved but Not Runnable? A Probability-Sample Study of
Jupyter Notebooks on GitHub, Codeberg and Zenodo*. It is anonymised for double-anonymous review.

## What the study does

The study measures whether Jupyter notebooks that are shared on three kinds of platform can still be run, and
whether they reproduce the outputs stored in them:
- **GitHub**, the dominant forge;
- **Codeberg**, a community-run forge;
- **Zenodo**, a research archive.

Instead of a convenience sample from GitHub search, each platform gets a probability sample with known inclusion
probabilities:
- **Codeberg:** a census of all public repositories, screened for notebooks;
- **GitHub:** uniformly random repository IDs;
- **Zenodo:** every record version with a notebook or an archive, in three strata, with random inspection of
  archive contents.

Every drawn resource goes through the same pipeline:
1. **Acquire** the repository or the exact Zenodo record version.
2. **Rebuild the environment:** the Python version from the resource's own evidence, and packages limited to
   releases older than the resource.
3. **Execute** every notebook headlessly, with a 30-minute limit.
4. **Compare** each cell with the stored output, recording every normalisation rule applied.
5. **Classify** the first error of each failing notebook, using its context.
6. **Score** the resource's readiness before execution and its outcome after.

Every decision is recorded as provenance. The analysis is pre-specified (`notes/analysis-plan.md`). It estimates
clean execution and output reproduction per platform, the characteristics associated with them and the causes of
failure. Two blind manual audits check the output normalisation and the failure categories.

The repository contains:
- the sampling code, frames and request logs;
- the draw;
- the pipeline;
- the analysis code and the reference analysis run behind the paper;
- the manual audits;
- the design and analysis plans.

**Merged databases:** the merged results databases of the three study rounds (`results/study*/merged.sqlite`,
0.7–1 GB each) and the two largest frame databases are not in this repository because of their size. They will be
added in the DOI version of the package. `LARGE-FILES.txt` lists each of them with its SHA-256. Until then, the
reference analysis run in `results/analysis/` holds every table, figure and number of the paper.

## Layout

| Path | Content |
|---|---|
| `notes/sampling-design.md`, `notes/analysis-plan.md` | the pre-specified sampling design and analysis plan, with dated amendments (section 11: round 3) |
| `data/sampling/codeberg`, `github`, `zenodo` | frames (SQLite + CSV exports with SHA-256), request logs of every API call, summaries |
| `data/sampling/draw/2026-10-09*` | the draw (`draw.csv`: strata, roles, processing order, version checks), cohorts, top-up logs per study round; `-r3`: with the Zenodo reserve extension (`analysis/sampling/extend_zenodo_reserve.py`) |
| `results/study/`, `results/study-r2/`, `results/study-r3/` | per study round (1, 2, 3): its manifest and state; the merged database (every run, environment, package, execution, cell comparison, failure, readiness score, provenance) follows in the DOI version |
| `results/analysis/<run>/` | the analysis run behind the paper: `tables/*.csv`, `figures/`, `macros.json` (every number of the text), `results.json`, `provenance.json` (input and output SHA-256, versions, seeds), log |
| `results/audit-*/` | manual audits: keys (pipeline labels), the annotators' answers, `scores.json`, the annotation guideline. The audit pages themselves contain third-party notebook content and are not included. |
| `src/`, `pipeline/`, `config/` | the pipeline (Bash + Python) |
| `hpc/` | the SLURM driver used to run the study on an HPC cluster |
| `analysis/` | output comparison (`nbprocess/`, `compare_notebook.py`), sampling, paper analysis, audits |
| `third_party/reproscore*` | the readiness scorer, the corrected version used in the study and the published version 1.0 (GPL-3.0) |
| `notebooks/` (if present) | original and executed notebooks of resources whose detected licence allows redistribution; all other resources are referenced by URL, version and checksum in the database |
| `tests/` | the test suite |

## Path 1: regenerate every number, table and figure

This path needs the merged databases (DOI version).

```
python3 -m venv analysis/paper/.venv && analysis/paper/.venv/bin/pip install -r analysis/paper/requirements.txt
analysis/paper/.venv/bin/python analysis/paper/run_analysis.py --db results/study-r3/merged.sqlite \
    --draw-dir data/sampling/draw/2026-10-09-r3 --infrastructure analysis/paper/infrastructure-failures-r3.csv \
    --previous-db results/study-r2/merged.sqlite --previous-draw-dir data/sampling/draw/2026-10-09-r2 \
    --previous-infrastructure analysis/paper/infrastructure-failures-r2.csv \
    --notebook-languages analysis/paper/notebook-languages-r3.csv --no-paper
```

- **Checking a run:** the analysis writes a new folder under `results/analysis/`. Compare its `macros.json` with
  the one shipped. The seeds are fixed, so the numbers are identical.
- **Notebook languages:** the pipeline ran every notebook with a Python kernel. `notebook-languages-r3.csv` gives
  each original notebook's kernel language (from its metadata, `analysis/paper/notebook_languages.py`), and the
  analysis removes the other languages. Without the option, every notebook counts as Python, which is the as-run
  row of the sensitivity table.
- **Rounds:** the paper reports round 3 (`results/study-r3`). Round 2 is its "previous round" sensitivity row, and
  round 1 (`results/study`) is described only in the section on the study rounds. The exact command of the
  reference run is in its `provenance.json`.
- **Audits:** `python3 analysis/audit/score_audits.py results/audit-r2 --no-paper` recomputes Cohen's κ. On the
  items where both annotators agree, it also recomputes the pipeline's accuracy and precision; add
  `--db results/study-r3/merged.sqlite` for the weighted rates. Disagreements are left out. The annotators are
  pseudonymised as A1 and A2.

## Path 2: re-run the pipeline on a few resources (no HPC cluster needed)

Requirements: Linux (Debian/Ubuntu, or WSL), about 20 GB of disk and a network connection.

```
bash setup.sh                                   # pyenv, Pythons, tool environment, SQLite (once)
TARGET_COUNT=1 bash run.sh                      # one resource end to end, results in data/output/
```

- **Larger runs:** `hpc/README.md` documents the full study on a SLURM cluster: `study.sh` passes, top-ups from the
  reserve, re-runs, merging.
- **Results differ between runs:** packages, remote services and repositories change over time, so a re-run
  measures the resources as they are now. The database records the exact environment of every original run.

## Licences

- **Code:** GPL-3.0 (`LICENSE`).
- **Data and measurements:** CC-BY 4.0.
- **Third-party notebooks:** included only where their licence allows redistribution, under that licence.
