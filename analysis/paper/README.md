# Analysis for the MSR 2027 paper

Pre-specified analysis of the cross-platform study (`notes/analysis-plan.md`, committed in `169202e`
before the first outcome analysis ran).

| File | Purpose |
|---|---|
| `run_analysis.py` | the analysis: all sections of the plan; writes a timestamped output folder with log and provenance |
| `estimators.py` | design-based estimators (Codeberg SRS + FPC, GitHub SRS, Zenodo two-phase bootstrap, Holm, Wilson); tests in `tests/test_estimators.py` |
| `infrastructure-failures.csv` | round 1: the 8 resources hit by the full node-local `/tmp` (decision 2026-10-11): excluded from the primary analysis, included in sensitivity 8 |
| `infrastructure-failures-r2.csv` | round 2: the 8 resources damaged by the run, not by the resource (5 full node-local `/tmp`, 1 failed output comparison, 2 path bugs; decision 2026-10-20): excluded from the primary analysis, included in sensitivity 8 |
| `make_numbers.py` | writes `paper/msr2027/numbers.tex` from the sampling summaries and `paper/msr2027/results/*.json` |
| `requirements.txt` | pinned analysis environment |

## Run
```
python3 -m venv analysis/paper/.venv && analysis/paper/.venv/bin/pip install -r analysis/paper/requirements.txt
# round 2, the reported results (~3 min on a laptop; the mixed model takes most)
analysis/paper/.venv/bin/python analysis/paper/run_analysis.py --db results/study-r2/merged.sqlite \
    --draw-dir data/sampling/draw/2026-10-09-r2 --infrastructure analysis/paper/infrastructure-failures-r2.csv
python3 analysis/paper/make_numbers.py --draw-dir data/sampling/draw/2026-10-09-r2 --study-db results/study-r2/merged.sqlite
```
Without options the script analyses round 1 (`results/study/merged.sqlite`, `data/sampling/draw/2026-10-09/`,
`infrastructure-failures.csv`). Inputs:
- the merged database, plus `MANIFEST.txt` (pipeline commit) and `state.json` from the the cluster run next to it;
- the draw folder (draw, top-up log);
- the infrastructure list.

## Output: `results/analysis/<UTC time>-<commit>/` (`latest` links to the newest)
- `analysis.log`: every step, warnings, tracebacks of failed sections (the other sections still run; exit status 1).
- `provenance.json`:
  - git commit, dirty files, SHA-256 of the analysis scripts;
  - Python and package versions, host;
  - SHA-256 of every input (database, draw files, plan, infrastructure list, pipeline manifest);
  - pipeline commit, parameters (seed 20261011, B = 2000 / 500);
  - timings, failed sections, deviations from the plan, SHA-256 of every output.
- `results.json`, `macros.json`, `tables/*.csv`.
- `MANIFEST.sha256`: check with `sha256sum -c MANIFEST.sha256` inside the folder.

The paper macros go to `paper/msr2027/results/study.json`. They carry the output folder, commits, script hashes and database SHA-256.
