# ReproScore 1.1 (development) in this repository

Copied from https://anonymous.example/reproscore, commit `1d7a5594fe6c97a82a73e0c80d066ef11d5e2998`
(2026-09-30), by its author the authors, for the pipeline stage `src/reproscore_stage.py`.
Licence GPL-3.0 (`LICENSE`; this repository is GPL-3.0 as well). Note: `CITATION.cff` upstream says
`license: MIT`, which does not match `LICENSE`.

Copied: `src/`, `tests/`, `config/default_rubric.yaml`, `LICENSE`, `CITATION.cff`, `README.md`.
Not copied: the evaluation data (`data/ablation/`), analysis scripts and notebooks.

`VERSION` is stored with every score (`tool_commit`). The paper version (1.0 = upstream 1d7a559 +
patches 1–3) is kept unchanged in `../reproscore-1.0`; the pipeline scores RRS with both.
`notebookfair.patch` is the diff of patches 1–3 against the upstream commit (`git apply` in the reproscore repository
with `-p1`), so the fixes can be taken upstream.

## Patches
1. **Path checks relative to the repository** (`src/utils/notebook_paths.py`, `src/scoring/rrs.py`).
   `is_excluded_notebook` and the hidden-folder checks tested every component of the full path, so a
   folder above the repository changed the score: `../carate` dropped all figures from
   `expected_outputs` (RRS 49.21 instead of 49.69), and a clone under `.../build/` or `.../env/` would
   lose all notebooks. Now `is_excluded_notebook(path, repo)` and the hidden-part checks use
   `path.relative_to(repo)` (29 calls + 2 checks).
2. **Crash in `inline_explanation_density`** (`src/scoring/rrs.py`): notebooks without code cells and no
   script lines raised `TypeError: type NoneType doesn't define __round__`. Now scored 50, like the
   existing no-code case.
3. **ROS component R, output reproduction** (`src/scoring/ros.py`): `ExecutionEvidence.output_reproduction`
   (0–1), counted only when the rubric weights `"R"`, so the default rubric gives the same ROS as before.
   The pipeline's rubric `config/reproscore_rubric.yaml` (notebookfair-v1) weights it 0.15.

Upstream tests (`python3 -m pytest third_party/reproscore/tests`) pass after the patches (31/31).
To update: copy the new upstream version, re-apply the patches that upstream does not have yet,
update `VERSION` and this file.

## ReproScore 1.1 fixes (2026-10-05)

From the code review in notebook-study (`notes/reproscore-review.md`). `reproscore-1.1.patch` is
the diff from `../reproscore-1.0` (applies with `git apply -p1`). Each fix has tests in
`tests/test_fixes_1_1.py` (86 tests); the original 31 tests still pass.

| Review | Fix |
|---|---|
| A1 | README found in any letter case (`readme.md`, `README.markdown`, …); Markdown preferred |
| A2 | Python version: pyproject `requires-python`, unquoted setup.cfg `python_requires`, all conda operators, Pipfile `[requires]`, binder/ folders |
| A3 | `data_pointer`: only links in a data context (data section or a line about data), badges/images ignored |
| A4 | Seeds: word-bounded random calls; seeding via `default_rng(seed)`, `RandomState(seed)`, `random_state=<int>`, `seed=<int>`, CUDA/JAX/Lightning/Transformers seeds |
| A5 | `import_resolvability`: imports first (stdlib/local-only → 100), `sys.stdlib_module_names`, local modules, binder/.binder/requirements/ files, larger import→distribution table |
| A6 | `dep_pinning`: per-format parsing (requirements, conda incl. `name=ver` and pip section, pyproject/Poetry, setup.cfg/setup.py, Pipfile), Python/R lockfiles only, binder/ folders |
| A7 | `container_spec`: `.def` needs `Bootstrap:`; Containerfile, Dockerfile.*, *.Dockerfile; digest and versioned/date tags count as pinned |
| A8 | `except: pass` in one-line and `...` forms |
| B1 | Usage examples: commands at line start inside code blocks (not prose/data blocks), notebook and workflow runners; linear time |
| B2 | Docstring coverage with `ast`: share of public functions/methods/classes; none → neutral 50 |
| B3 | README headings outside code blocks, whole words, reST underlines, numbering/links/emoji removed |
| B5 | Notebook downloads (`read_csv("https://…")`, `urlretrieve`, `load_dataset`, `download=True`, `fetch_*`, `gdown`, Keras `get_file` / `datasets.*.load_data`, `kagglehub`); `curl … \| sh` installers excluded |
| B7 | CLI detection only by import/decorator (`click`, `typer`, `fire`, `docopt`) |
| B8 | Licence file names in any case/suffix; `.zenodo.json` as metadata |
| B9 | GPU package regex (`tensorflow[-_]gpu`) |
| C1 | `load_rubric(path)` raises for a missing/unreadable explicit path |
| C2 | `ROSScorer` tolerates rubrics without some components |
| — | `CITATION.cff`: licence GPL-3.0 (matches LICENSE) |

Not changed (model decisions, open): B4 stored notebook outputs as expected outputs, B6 the A < 10 penalty for
self-contained notebooks, B10 file sampling (first 20–30 files).
