#!/usr/bin/env python3
"""Pre-specified analysis of the cross-platform study (notes/analysis-plan.md, approved 2026-10-11).

Reads the merged run database and the draw, and writes one self-contained, timestamped output folder:

  results/analysis/<UTC time>-<commit>/
    analysis.log        everything the run did, with warnings and tracebacks
    provenance.json     git commit and dirty files, script hashes, Python and package versions, host,
                        SHA-256 of every input, parameters (seed, bootstrap sizes), timing per section,
                        failed sections, deviations from the plan, SHA-256 of every output
    results.json        all estimates (nested by section)
    tables/*.csv        one table per analysis
    MANIFEST.sha256     checksums of all files in the folder (sha256sum -c MANIFEST.sha256)
  results/analysis/latest -> the newest folder
  paper/msr2027/results/study.json   the paper macros (read by make_numbers.py), with the provenance summary
  paper/msr2027/tab-*.tex            generated tables (e.g. the sensitivity analyses)
The pipeline commit comes from MANIFEST.txt next to the database (copied from the the cluster bundle).

Sections (plan section numbers in brackets):
  cohort           in-cohort resources and outcome classes [1, 2, 7]; infrastructure failures listed separately
  install_quality  quality gate: package installs that failed for infrastructure reasons (network, index)
  availability     S6: obtainable share per platform and Zenodo stratum; reasons; eligibility by content
  primary          RQ1: mean Y_r per platform (Codeberg SRS+FPC, GitHub SRS, Zenodo two-phase bootstrap),
                   three pairwise differences with Holm [3, 10]; interim pooled SD
  notebook_model   S1: notebook-level mixed-effects logistic regression (random intercept per resource), ICC
  reproduction     S2: comparison-v2 normalised score (also strict, text/data, before-error) over assessable cells
  failures         S4/RQ3: first-error categories, type->context table, dependency causes, timeouts, chi-square
  models           S3/RQ2 + RQ4: fractional logit, unadjusted vs adjusted platform AMEs, RRS increment in R^2
  readiness        S5/RQ4: RRS 1.1/1.0 and dimensions vs Y_r, R_r and ROS (Spearman)
  licences         S7: licence file (licensee) vs platform API
  sensitivity      section 5 of the plan (+ infrastructure failures included, notebooks without an execution record)
  duplicates       identical notebooks within and across resources/platforms
  figures          Fig. 2: resources with no/some/all notebooks clean, mix of assessable cells (design-based)

A failing section is logged with its traceback and recorded in provenance.json; the others still run
(exit status 1 if any section failed).

Usage (use the analysis environment, analysis/paper/requirements.txt):
  analysis/paper/.venv/bin/python analysis/paper/run_analysis.py \
      [--db results/study/merged.sqlite] [--draw-dir data/sampling/draw/2026-10-09] \
      [--seed 20261011] [--bootstrap 2000] [--model-bootstrap 500] [--only-present] [--no-paper]
Never use the frozen evaluation database (data/output/db/db.sqlite): the script refuses it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import platform as host_platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import warnings
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / 'pipeline')]
import estimators as est                                      # noqa: E402
from make_numbers import fmt                                  # noqa: E402
from topup_from_reserve import key as resource_key, run_outcomes  # noqa: E402  (same outcome definition as the top-ups)

FROZEN = (ROOT / 'data' / 'output' / 'db' / 'db.sqlite').resolve()
PLATFORMS = ['codeberg', 'github', 'zenodo']
NAME = {'codeberg': 'Codeberg', 'github': 'Github', 'zenodo': 'Zenodo'}
LABEL = {'codeberg': 'Codeberg', 'github': 'GitHub', 'zenodo': 'Zenodo'}
PAIRS = [('codeberg', 'github'), ('codeberg', 'zenodo'), ('github', 'zenodo')]
STRATA = ['Z1', 'Z2', 'Z3']
STRATUM_WORD = {'Z1': 'ZOne', 'Z2': 'ZTwo', 'Z3': 'ZThree'}
CAUSE_TEXT = {'DEPENDENCY_ERROR': 'dependency errors', 'FILE_ERROR': 'missing files', 'CODE_ERROR': 'code errors',
              'OTHER_ERROR': 'other errors', 'EXTERNAL_SERVICE_ERROR': 'external services',
              'DATA_ERROR': 'data errors', 'EXECUTION_FAILURE': 'execution failures',
              'EXECUTION_ENVIRONMENT_ERROR': 'environment errors', 'NETWORK_ERROR': 'network errors'}
INPUT_FILES = ['notes/analysis-plan.md', 'notes/sampling-design.md', 'analysis/paper/infrastructure-failures.csv',
               'analysis/paper/requirements.txt', 'pipeline/topup_from_reserve.py', 'analysis/paper/make_numbers.py']
SCRIPT_FILES = ['analysis/paper/run_analysis.py', 'analysis/paper/estimators.py']
PACKAGES = ['numpy', 'pandas', 'scipy', 'statsmodels', 'patsy', 'matplotlib']
DEVIATIONS = [
    'Population (decision 2026-10-23): the pipeline registered every .ipynb as a Python notebook (src/repo.sh) and ran '
    'it with a Python kernel. --notebook-languages gives each original notebook\'s kernel language (read from its '
    'metadata, analysis/paper/notebook_languages.py); notebooks of other languages are removed, as the plan defines '
    'Y_r over Python notebooks, and a resource left without a Python notebook is ineligible (no replacement: domain '
    'estimation). Notebooks without language metadata stay in, as before.',
    'S3: the "archived" covariate is not recorded for all platforms (no Zenodo equivalent); it is left out.',
    'S1: the mixed model is fitted unweighted (variational Bayes, statsmodels BinomialBayesMixedGLM); '
    'the design-based platform estimates are the primary ones.',
    'S3/RQ4: the pooled fractional logit uses Zenodo weights normalised to mean 1 within the platform, so each '
    'platform contributes its sample size; it describes associations in the sample, not population effects.',
    'S4: chi-square tests use notebook counts and ignore the clustering in resources (descriptive only).',
    'Section 7: resources whose results were damaged by the infrastructure or the pipeline, not by the resource, are '
    'listed in --infrastructure, excluded from the primary analysis and included as recorded in sensitivity 8. '
    'Round 1 (decision 2026-10-11): 8 resources hit by the full node-local /tmp (infrastructure-failures.csv). '
    'Round 2 (decision 2026-10-20): 8 resources (infrastructure-failures-r2.csv): 5 hit by a full node-local /tmp during '
    'the run, 1 whose output comparison failed (pipeline Python could not import attrs), 2 pipeline path bugs.',
    'Y_r: Python notebooks of an analysable resource without an execution record count as not clean '
    '(sensitivity: excluded).',
]

log = logging.getLogger('analysis')


def sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def rel(path) -> str:
    path = Path(path).resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def git(*args) -> str | None:
    try:
        return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                    # noqa: BLE001 - provenance must not stop the run
        return None


def jsonable(value):
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, est.Estimate | est.Difference):
        return jsonable(value.as_dict())
    if isinstance(value, Path):
        return rel(value)
    return value


# --------------------------------------------------------------------------------------------- context

class Context:
    def __init__(self, args, out_dir: Path):
        self.args, self.out = args, out_dir
        self.tables_dir = out_dir / 'tables'
        self.tables_dir.mkdir(parents=True, exist_ok=True)
        self.results: dict = {}
        self.macros: dict = {}
        self.latex: dict = {}                            # name -> LaTeX source (paper/msr2027/<name>.tex)
        self.figures: dict = {}                          # name -> PDF in the output folder (paper/msr2027/<name>.pdf)
        self.seed, self.B, self.B_model = args.seed, args.bootstrap, args.model_bootstrap
        self.summary = json.loads((args.draw_dir / 'summary.json').read_text())
        self.codeberg_frame = int(self.summary['codeberg']['frame_screened_in'])
        self.strata = {h: est.Stratum(int(v['frame_N']), int(v['inspected_m']), int(v['pool']))
                       for h, v in self.summary['zenodo']['strata'].items()}

    def connect(self):
        return sqlite3.connect(f'file:{self.args.db}?mode=ro', uri=True)

    def table(self, name: str, frame: pd.DataFrame, index=False):
        path = self.tables_dir / f'{name}.csv'
        frame.to_csv(path, index=index)
        log.info('table %s: %d rows', path.name, len(frame))

    def rng(self, label):
        return est.child_rng(self.seed, label)

    def by_platform(self, units: pd.DataFrame, y: str, domain: pd.Series, label: str) -> dict:
        """Design-based domain mean per platform; units = the sample (all drawn resources of the variant)."""
        out = {}                                 # every outcome here is a share in [0, 1]: intervals are clipped
        for p in PLATFORMS:
            sub = units[units.platform == p]
            d = domain[sub.index].to_numpy(bool)
            values = sub[y].to_numpy(float)
            if p == 'codeberg':
                out[p] = est.srs_mean(values, d, frame_n=self.codeberg_frame)
            elif p == 'github':
                out[p] = est.srs_mean(values, d)
            else:
                out[p] = est.two_phase_mean(values, d, sub.stratum.to_numpy(object), self.strata,
                                            self.rng(f'{label}:{p}'), self.B)
            out[p].low, out[p].high = float(np.clip(out[p].low, 0, 1)), float(np.clip(out[p].high, 0, 1))
        return out

    @staticmethod
    def compare(estimates: dict) -> list[est.Difference]:
        diffs = [est.difference(a, estimates[a], b, estimates[b]) for a, b in PAIRS]
        for d, p in zip(diffs, est.holm([d.p for d in diffs])):
            d.p_holm = p
        return diffs


def estimate_rows(label, estimates: dict, diffs=None) -> list[dict]:
    rows = [dict(variant=label, quantity=p, **jsonable(e.as_dict())) for p, e in estimates.items()]
    for d in diffs or []:
        rows.append(dict(variant=label, quantity=f'{d.a}-{d.b}', mean=d.diff, se=d.se, low=d.low, high=d.high,
                         df=None if math.isinf(d.df) else d.df, p=d.p, p_holm=d.p_holm))
    for row in rows:
        row.pop('weights', None)
        row.pop('drawn', None)
    return rows


# --------------------------------------------------------------------------------------------- data

def load_infrastructure(path: Path | None) -> pd.DataFrame:
    if path is None:                                   # --infrastructure none
        return pd.DataFrame(columns=['platform', 'url', 'effect', 'evidence', 'decision', 'key'])
    frame = pd.read_csv(path, dtype=str).fillna('')
    frame['key'] = [resource_key(p, u) for p, u in zip(frame.platform, frame.url)]
    return frame


def python_notebook(language: str, kernel: str) -> bool:
    """The kernel language of a notebook is Python; notebooks without any language metadata count as Python."""
    language, kernel = language.strip().lower(), kernel.strip().lower()
    if language in ('', 'invalid'):
        return not kernel or kernel.startswith('python')
    return language == 'python'


def load_notebook_languages(path: Path, db: Path) -> dict:
    """(resource key, notebook path) -> Python or not, from notebook_languages.py output for the passes merged into db."""
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    source = {i: '/'.join(p.split('/')[-3:-1]) for i, p in con.execute('SELECT id, source_path FROM merge_sources')}
    merged = {(source[s], r): m for s, r, m in con.execute(
        'SELECT merge_source_id, source_run_id, run_id FROM merge_run_map')}
    key = {run: (platform, repository) for run, platform, repository in con.execute(
        'SELECT rr.id, LOWER(r.platform), LOWER(r.repository) FROM repository_runs rr '
        'JOIN repositories r ON r.id = rr.repository_id')}
    con.close()
    languages, unmatched = {}, 0
    for row in pd.read_csv(path, dtype=str).fillna('').rename(columns={'pass': 'pass_name'}).itertuples():
        run = merged.get((f'{row.pass_name}/{row.task}', int(row.run_id)))
        if run is None or run not in key:
            unmatched += 1
            continue
        languages[(key[run], row.notebook)] = python_notebook(row.language, row.kernel)
    log.info('notebook languages: %d notebooks, %d not Python, %d rows not in the database', len(languages),
             sum(not v for v in languages.values()), unmatched)
    return languages


def load_data(ctx: Context):
    """In-cohort units (one row per drawn resource used), resource outcomes and notebooks of the latest runs."""
    draw_dir, db = ctx.args.draw_dir, ctx.args.db
    draw = pd.read_csv(draw_dir / 'draw.csv', dtype=str).fillna('')
    topup = pd.read_csv(draw_dir / 'topup-log.csv', dtype=str).fillna('')
    used_reserve = set(topup.loc[topup.replaced_by != '', 'replaced_by'])
    cohort = draw[(draw.role == 'primary') | ((draw.role == 'reserve') & draw.url.isin(used_reserve))].copy()
    cohort['key'] = [resource_key(p, u) for p, u in zip(cohort.platform, cohort.url)]
    outcomes = run_outcomes(db)
    cohort['outcome'] = [outcomes.get(k, ('not_run', ''))[0] for k in cohort.key]
    cohort['run_status'] = [outcomes.get(k, ('not_run', ''))[1] for k in cohort.key]
    cohort['year'] = pd.to_numeric(cohort.year, errors='coerce')
    log.info('draw: %d rows; in cohort %d (primary %d, used reserve %d)', len(draw), len(cohort),
             int((cohort.role == 'primary').sum()), int((cohort.role == 'reserve').sum()))

    infra = load_infrastructure(ctx.args.infrastructure)
    cohort['infrastructure'] = cohort.key.isin(set(infra.key))
    missing = set(infra.key) - set(cohort.key)
    if missing:
        log.warning('infrastructure list entries not in the cohort: %s', sorted(missing))
    if ctx.args.only_present:
        before = len(cohort)
        cohort = cohort[cohort.outcome != 'not_run'].copy()
        log.warning('--only-present: kept %d of %d in-cohort resources that have a run (test mode)', len(cohort), before)

    con = ctx.connect()
    latest = pd.read_sql_query(
        'SELECT r.id AS repository_id, LOWER(r.platform) AS platform, LOWER(r.repository) AS repository, '
        '       rr.id AS run_id, rr.run_status, rr.error_message '
        'FROM repositories r JOIN repository_runs rr '
        '  ON rr.id = (SELECT MAX(id) FROM repository_runs WHERE repository_id = r.id)', con)
    latest['key'] = list(zip(latest.platform, latest.repository))
    cohort = cohort.merge(latest[['key', 'repository_id', 'run_id', 'error_message']], on='key', how='left')
    cohort.index = range(len(cohort))

    notebooks = pd.read_sql_query(
        'WITH latest AS (SELECT repository_id, MAX(id) AS run_id FROM repository_runs GROUP BY repository_id) '
        'SELECT n.repository_id, n.id AS notebook_id, n.name, l.run_id, ne.id AS execution_id, ne.execution_status, '
        '       ne.error_type, ne.error_category, ne.error_category_by_type, ne.dependency_cause, '
        '       m.score_normalised, m.score_strict, m.score_text_data, m.score_before_error, m.cells_assessable, '
        '       m.cells_identical, m.cells_reproduced_normalised, m.cells_image_only, m.cells_different, '
        '       m.cells_error, m.cells_missing, '
        '       (SELECT ra.sha256 FROM run_artifacts ra WHERE ra.repository_run_id = l.run_id '
        "          AND ra.kind = 'original notebook' AND ra.notebook_path = n.name ORDER BY ra.id DESC LIMIT 1) AS sha256 "
        'FROM notebooks n JOIN latest l ON l.repository_id = n.repository_id '
        'LEFT JOIN notebook_executions ne ON ne.id = (SELECT MAX(id) FROM notebook_executions '
        '       WHERE repository_run_id = l.run_id AND notebook_id = n.id) '
        'LEFT JOIN notebook_reproducibility_metrics m ON m.id = (SELECT MAX(id) FROM notebook_reproducibility_metrics '
        '       WHERE notebook_execution_id = ne.id AND comparison_version = 2) '
        "WHERE COALESCE(LOWER(n.language), 'python') LIKE 'python%'", con)
    con.close()
    if ctx.args.language_map is not None:               # the pipeline's language column says 'python' for every notebook
        keys = notebooks.repository_id.map(latest.set_index('repository_id').key)
        python = pd.Series([ctx.args.language_map.get((k, n), True) for k, n in zip(keys, notebooks.name)],
                           index=notebooks.index)
        log.info('removed %d notebooks of other languages', int((~python).sum()))
        remaining = set(notebooks.repository_id)
        notebooks_before = notebooks
        notebooks = notebooks[python].copy()
        none_left = cohort.repository_id.isin(remaining - set(notebooks.repository_id)) & (cohort.outcome == 'analysable')
        cohort.loc[none_left, 'outcome'] = 'ineligible'
        cohort.loc[none_left, 'run_status'] = 'NO_PYTHON_NOTEBOOKS'
        log.info('resources without a Python notebook (now ineligible): %s',
                 cohort[none_left].groupby('platform').size().to_dict())
        analysed = set(cohort.loc[(cohort.outcome == 'analysable') | none_left, 'repository_id'])
        removed = notebooks_before[~python & notebooks_before.repository_id.isin(analysed)]
        removed_platform = removed.repository_id.map(latest.set_index('repository_id').platform)
        no_python = cohort[none_left].groupby('platform').size()
        ctx.macros.update(OtherLanguageNotebooks=fmt(len(removed)), NoPythonResources=fmt(int(no_python.sum())))
        for p in PLATFORMS:
            ctx.macros[f'OtherLanguageNotebooks{p.capitalize()}'] = fmt(int((removed_platform == p).sum()))
            ctx.macros[f'NoPythonResources{p.capitalize()}'] = fmt(int(no_python.get(p, 0)))
    return cohort, notebooks, infra


def resource_covariates(ctx: Context, run_ids) -> pd.DataFrame:
    """Per run: dependency files, Python version, size, licence, readiness and outcome scores."""
    con = ctx.connect()
    q = lambda sql: pd.read_sql_query(sql, con)          # noqa: E731
    runs = pd.DataFrame({'run_id': sorted(set(int(r) for r in run_ids if pd.notna(r)))})

    deps = q('SELECT repository_run_id AS run_id, kind, used, pinned, specs_json FROM dependency_sources')
    deps['specs'] = [len(json.loads(s)) if s else 0 for s in deps.specs_json]
    used = deps[deps.used == 1]
    agg = pd.DataFrame({
        'dep_declared': used[used.kind != '%pip'].groupby('run_id').size().gt(0),
        'inline_pip': used[used.kind == '%pip'].groupby('run_id').size().gt(0),
        'pinned': used.groupby('run_id').pinned.sum(),
        'specs': used.groupby('run_id').specs.sum(),
        'dep_files_found': deps[deps.kind != '%pip'].groupby('run_id').size().gt(0)})
    runs = runs.merge(agg, left_on='run_id', right_index=True, how='left')
    for column in ('dep_declared', 'inline_pip', 'dep_files_found'):
        runs[column] = runs[column].astype('boolean').fillna(False).astype(bool)
    runs['pinned_share'] = np.where(runs.specs.fillna(0) > 0, runs.pinned.fillna(0) / runs.specs.replace(0, np.nan), 0.0)
    runs['pinned_share'] = runs.pinned_share.fillna(0.0)

    env = q('SELECT repository_run_id AS run_id, python_requested, python_source, python_selected, python_used, '
            'notebook_versions_json, notes_json, pip_uploaded_prior_to FROM run_environments '
            'WHERE id IN (SELECT MAX(id) FROM run_environments GROUP BY repository_run_id)')
    env['python_declared'] = env.python_source.fillna('default') != 'default'
    env['python_moved'] = [any('using' in note for note in json.loads(n or '[]')) for n in env.notes_json]
    env['no_cutoff'] = env.pip_uploaded_prior_to.isna() | (env.pip_uploaded_prior_to == '')
    env['declares_only_310'] = [set(json.loads(v or '{}')) == {'3.10'} for v in env.notebook_versions_json]
    runs = runs.merge(env.drop(columns=['notes_json']), on='run_id', how='left')

    acq = q('SELECT repository_run_id AS run_id, bytes_on_disk FROM resource_acquisitions '
            'WHERE id IN (SELECT MAX(id) FROM resource_acquisitions GROUP BY repository_run_id)')
    runs = runs.merge(acq, on='run_id', how='left')

    lic = q('SELECT repository_run_id AS run_id, licence_api, licence_file, status AS licence_status FROM resource_licences '
            'WHERE id IN (SELECT MAX(id) FROM resource_licences GROUP BY repository_run_id)')
    runs = runs.merge(lic, on='run_id', how='left')

    ready = q('SELECT repository_run_id AS run_id, rrs, score_e, score_a, score_d, score_c, score_s, rrs_v1_0, '
              'score_e_v1_0, score_a_v1_0, score_d_v1_0, score_c_v1_0, score_s_v1_0, status AS rrs_status '
              'FROM resource_readiness WHERE id IN (SELECT MAX(id) FROM resource_readiness GROUP BY repository_run_id)')
    score_columns = [c for c in ready.columns if c.startswith(('rrs', 'score_')) and c != 'rrs_status']
    ready.loc[ready.rrs_status != 'ok', score_columns] = np.nan
    runs = runs.merge(ready, on='run_id', how='left')

    packages = q("SELECT repository_run_id AS run_id, error FROM run_packages WHERE status = 'failed'")
    packages['kind'] = install_failure_kind(packages.error)
    kinds = packages.groupby(['run_id', 'kind']).size().unstack(fill_value=0)
    for kind in INSTALL_INFRASTRUCTURE:
        column = f'install_{kind}'
        runs[column] = runs.run_id.map(kinds[kind]) if kind in kinds else 0
        runs[column] = runs[column].fillna(0).astype(int)
    runs['install_failed'] = runs.run_id.map(packages.groupby('run_id').size()).fillna(0).astype(int)
    runs['install_infrastructure'] = runs[[f'install_{k}' for k in INSTALL_INFRASTRUCTURE]].sum(axis=1).gt(0)

    outcome = q('SELECT repository_run_id AS run_id, ros, rcs, status AS ros_status FROM reproscore_outcome '
                'WHERE id IN (SELECT MAX(id) FROM reproscore_outcome GROUP BY repository_run_id)')
    runs = runs.merge(outcome, on='run_id', how='left')
    con.close()
    return runs


# Package installs that failed because of the infrastructure, not the resource (found in the 2026-10 study run:
# the cluster's pip configuration pointed at a mirror behind a proxy that often answered 500, and the mirror has
# no upload-time metadata, so the time-travel cutoff fails there).
INSTALL_INFRASTRUCTURE = ['network', 'index_no_upload_time']


def install_failure_kind(errors: pd.Series) -> pd.Series:
    e = errors.fillna('')
    network = e.str.contains(r'Tunnel connection failed|connection broken|Read timed out|ProxyError|'
                             r'NewConnectionError|Temporary failure in name resolution|Max retries exceeded', regex=True)
    index = e.str.contains('does not provide upload-time metadata', regex=False)
    nomatch = e.str.contains('No matching distribution', regex=False)
    return pd.Series(np.select([network, index, nomatch], ['network', 'index_no_upload_time', 'no_matching'], 'other'),
                     index=errors.index)


def resource_outcomes(notebooks: pd.DataFrame, exclude=None, dedup=False) -> pd.DataFrame:
    """Y_r, R_r (and the other v2 scores) per repository from its notebooks.
    exclude(notebooks) -> boolean mask of notebooks removed from the denominator (sensitivity analyses)."""
    nb = notebooks.copy()
    if dedup:
        nb = nb.sort_values('notebook_id')
        has = nb.sha256.notna()
        nb = pd.concat([nb[~has], nb[has].drop_duplicates(['repository_id', 'sha256'])])
    if exclude is not None:
        nb = nb[~exclude(nb)]
    nb = nb.assign(clean=(nb.execution_status == 'SUCCESS').astype(float),
                   assessable=nb.cells_assessable.fillna(0) > 0)
    grouped = nb.groupby('repository_id')
    out = pd.DataFrame({'notebooks': grouped.size(), 'y': grouped.clean.mean(),
                        'executed': grouped.execution_id.count(),
                        'notebooks_assessable': grouped.assessable.sum()})
    scored = nb[nb.assessable]
    for column, name in (('score_normalised', 'r'), ('score_strict', 'r_strict'), ('score_text_data', 'r_text_data'),
                         ('score_before_error', 'r_before_error')):
        out[name] = scored.groupby('repository_id')[column].mean()
    return out


# --------------------------------------------------------------------------------------------- sections

def section_cohort(ctx, data):
    units, notebooks, infra = data['units'], data['notebooks'], data['infra']
    counts = (units.assign(sample=np.where(units.infrastructure, 'infrastructure', 'sample'))
              .groupby(['platform', 'sample', 'outcome']).size().rename('resources').reset_index())
    ctx.table('cohort_outcomes', counts)
    by_role = units.groupby(['platform', 'role', 'outcome']).size().rename('resources').reset_index()
    ctx.table('cohort_outcomes_by_role', by_role)
    zen = units[units.platform == 'zenodo'].groupby(['stratum', 'content', 'outcome']).size().rename('resources').reset_index()
    ctx.table('cohort_zenodo_by_stratum_content', zen)
    detail = infra.merge(units[['key', 'outcome', 'run_status', 'role', 'stratum', 'error_message']], on='key', how='left')
    ctx.table('infrastructure_failures', detail.drop(columns=['key']))
    pipeline = units[units.outcome.isin(['pipeline_error', 'not_run'])][['platform', 'url', 'outcome', 'run_status', 'error_message']]
    ctx.table('pipeline_errors', pipeline)

    sample = data['sample']
    analysable = sample[sample.outcome == 'analysable']
    nb = notebooks[notebooks.repository_id.isin(analysable.repository_id)]
    env_failed = nb.repository_id.isin(analysable[analysable.run_status == 'ENV_RUN_FAIL'].repository_id)
    no_record = int((nb.execution_id.isna() & ~env_failed).sum())
    per = {}
    for p in PLATFORMS:
        s = sample[sample.platform == p]
        c = Counter(s.outcome)
        per[p] = dict(in_cohort=int((units.platform == p).sum()), sample=len(s), **{k: int(v) for k, v in c.items()},
                      infrastructure=int(((units.platform == p) & units.infrastructure).sum()),
                      notebooks=int(nb.repository_id.isin(s[s.outcome == 'analysable'].repository_id).sum()))
    ctx.results['cohort'] = dict(platforms=per, notebooks_analysed=int(len(nb)),
                                 notebooks_environment_failed=int(env_failed.sum()),
                                 notebooks_without_execution_record=no_record,
                                 resources_environment_failed=int((analysable.run_status == 'ENV_RUN_FAIL').sum()),
                                 infrastructure=len(infra), target=400)
    for p in PLATFORMS:
        n = NAME[p]
        ctx.macros.update({f'Analysable{n}': fmt(per[p].get('analysable', 0)), f'InCohort{n}': fmt(per[p]['in_cohort']),
                           f'Ineligible{n}': fmt(per[p].get('ineligible', 0)),
                           f'NotObtainable{n}': fmt(per[p].get('not_obtainable', 0)),
                           f'PipelineError{n}': fmt(per[p].get('pipeline_error', 0) + per[p].get('not_run', 0)),
                           f'Infrastructure{n}': fmt(int(((units.platform == p) & units.infrastructure).sum())),
                           f'Notebooks{n}': fmt(per[p]['notebooks'])})
    cov = analysable.merge(data['covariates'][['run_id', 'no_cutoff', 'python_used', 'python_declared', 'python_moved',
                                               'dep_declared', 'inline_pip']], on='run_id', how='left')
    for p in PLATFORMS:
        ctx.macros[f'Replaced{NAME[p]}'] = fmt(int(((units.platform == p) & (units.role == 'reserve')).sum()))
        c = cov[cov.platform == p]
        ctx.macros[f'NoCutoff{NAME[p]}'] = fmt(float(c.no_cutoff.fillna(True).mean()), 1, True) if len(c) else None
        # what the reconstruction could use: a declared Python version, declared dependencies, inline installs
        for column, macro in (('python_declared', 'PythonDeclared'), ('python_moved', 'PythonMoved'),
                              ('dep_declared', 'DepDeclared'), ('inline_pip', 'InlinePip')):
            ctx.macros[f'{macro}{NAME[p]}'] = (fmt(float(c[column].astype('boolean').fillna(False).astype(float).mean()),
                                                   1, True) if len(c) else None)
    ctx.macros['NoCutoffShare'] = fmt(float(cov.no_cutoff.fillna(True).mean()), 1, True) if len(cov) else None
    ctx.macros['PythonEightShare'] = fmt(float(cov.python_used.fillna('').str.startswith('3.8').mean()), 1, True) if len(cov) else None
    ctx.macros.update(NotebooksAnalysed=fmt(len(nb)), InfrastructureFailures=fmt(len(infra)),
                      NotebooksNoExecutionRecord=fmt(no_record), NotebooksEnvFailed=fmt(int(env_failed.sum())),
                      ResourcesEnvFailed=fmt(int((analysable.run_status == 'ENV_RUN_FAIL').sum())),
                      ZenodoShortOfTarget=fmt(max(0, 400 - per['zenodo'].get('analysable', 0))))
    log.info('analysable: %s; notebooks analysed %d', {p: per[p].get('analysable', 0) for p in PLATFORMS}, len(nb))


def section_availability(ctx, data):
    sample = data['sample'].copy()
    sample['obtainable'] = (sample.outcome != 'not_obtainable').astype(float)
    domain = pd.Series(True, index=sample.index)
    estimates = ctx.by_platform(sample, 'obtainable', domain, 'availability')
    rows = estimate_rows('obtainable', estimates)
    strata_rows = []
    for h in STRATA:
        s = sample[(sample.platform == 'zenodo') & (sample.stratum == h)]
        k, n = int(s.obtainable.sum()), len(s)
        low, high = est.wilson(k, n)
        strata_rows.append(dict(stratum=h, drawn=n, obtainable=k, share=k / n if n else math.nan, low=low, high=high))
        if n:
            ctx.macros[f'Obtainable{STRATUM_WORD[h]}'] = fmt(k / n, 1, True)
    ctx.table('availability', pd.DataFrame(rows))
    ctx.table('availability_zenodo_strata', pd.DataFrame(strata_rows))
    eligible = sample[sample.outcome != 'not_obtainable']
    eligible = eligible.assign(eligible=(eligible.outcome != 'ineligible').astype(float))
    elig = ctx.by_platform(eligible, 'eligible', pd.Series(True, index=eligible.index), 'eligibility')
    ctx.table('eligibility_among_obtainable', pd.DataFrame(estimate_rows('eligible', elig)))
    content = (eligible[eligible.platform == 'zenodo'].groupby(['stratum', 'content'])
               .agg(obtained=('eligible', 'size'), with_notebooks=('eligible', 'sum')).reset_index())
    ctx.table('eligibility_zenodo_by_content', content)
    reasons = (sample[sample.outcome == 'not_obtainable']
               .assign(reason=lambda f: f.error_message.fillna('').str.replace(r'\d+', '#', regex=True).str.slice(0, 120))
               .groupby(['platform', 'run_status', 'reason']).size().rename('resources').reset_index()
               .sort_values(['platform', 'resources'], ascending=[True, False]))
    ctx.table('not_obtainable_reasons', reasons)
    zen = sample[(sample.outcome == 'not_obtainable') & (sample.platform == 'zenodo')].error_message.fillna('')
    ctx.macros.update(ZenodoOverLimits=fmt(int(zen.str.contains('every file was skipped').sum())),
                      ZenodoBrokenArchives=fmt(int(zen.str.contains('cannot read archive').sum())))
    ctx.results['availability'] = dict(obtainable=estimates, eligible_among_obtainable=elig, zenodo_strata=strata_rows)
    for p in PLATFORMS:
        e = estimates[p]
        ctx.macros.update({f'Obtainable{NAME[p]}': fmt(e.mean, 1, True), f'Obtainable{NAME[p]}Low': fmt(e.low, 1, True),
                           f'Obtainable{NAME[p]}High': fmt(e.high, 1, True),
                           f'Eligible{NAME[p]}': fmt(elig[p].mean, 1, True)})


def signed(value, digits=1):
    """Percentage points with a typeset minus sign."""
    if value is None or not math.isfinite(value):
        return None
    text = fmt(abs(100 * value), digits)
    return f'$-${text}' if value < 0 and text.strip('0.{,}') else text


def decimal(value, digits=2):
    """A plain number with a typeset minus sign (correlations)."""
    if value is None or not math.isfinite(value):
        return None
    text = fmt(abs(value), digits)
    return f'$-${text}' if value < 0 and text.strip('0.{,}') else text


def p_text(p):
    if p is None or not math.isfinite(p):
        return None
    return '<0.001' if p < 0.001 else f'={p:.3f}'           # with its relation: used as $p\num...$


def primary_estimates(ctx, sample, res, label):
    frame = sample.merge(res, left_on='repository_id', right_index=True, how='left')
    frame.index = sample.index
    domain = (frame.outcome == 'analysable') & frame.y.notna()
    estimates = ctx.by_platform(frame, 'y', domain, label)
    return estimates, ctx.compare(estimates), frame


def section_primary(ctx, data):
    estimates, diffs, frame = primary_estimates(ctx, data['sample'], data['res'], 'primary')
    ctx.table('primary_clean_execution', pd.DataFrame(estimate_rows('primary', estimates, diffs)))
    z = estimates['zenodo']
    strata_rows = []
    for h in STRATA:
        s = frame[(frame.platform == 'zenodo') & (frame.stratum == h) & (frame.outcome == 'analysable') & frame.y.notna()]
        if len(s):
            e = est.srs_mean(s.y, np.ones(len(s), bool))
            strata_rows.append(dict(stratum=h, weight=z.extra['weights'].get(h), drawn=z.extra['drawn'].get(h),
                                    **e.as_dict()))
    ctx.table('primary_zenodo_strata_unweighted', pd.DataFrame(strata_rows))
    analysable = frame[(frame.outcome == 'analysable') & frame.y.notna()]
    pooled_sd = float(analysable.y.std(ddof=1))
    ctx.results['primary'] = dict(estimates=estimates, differences=diffs, zenodo_strata=strata_rows,
                                  pooled_sd=pooled_sd, interim_rule_threshold=0.40,
                                  zenodo_weights=z.extra['weights'], zenodo_drawn=z.extra['drawn'],
                                  zenodo_kish_n=z.n_eff)
    for p in PLATFORMS:
        e, n = estimates[p], NAME[p]
        ctx.macros.update({f'Clean{n}': fmt(e.mean, 1, True), f'Clean{n}Low': fmt(e.low, 1, True),
                           f'Clean{n}High': fmt(e.high, 1, True), f'Clean{n}SE': fmt(e.se, 1, True)})
    for d in diffs:
        name = f'Diff{NAME[d.a]}{NAME[d.b]}'
        ctx.macros.update({name: signed(d.diff), f'{name}Low': signed(d.low), f'{name}High': signed(d.high),
                           f'{name}P': p_text(d.p), f'{name}PHolm': p_text(d.p_holm)})
    ctx.macros.update(ZenodoKishN=fmt(z.n_eff), PooledSD=fmt(pooled_sd, 2),
                      **{f'Weight{STRATUM_WORD[h]}': fmt(w, 1) for h, w in z.extra['weights'].items()})
    log.info('primary: %s', {p: round(e.mean, 4) for p, e in estimates.items()})
    log.info('differences: %s', [(d.a, d.b, round(d.diff, 4), round(d.p_holm, 4)) for d in diffs])


def section_notebook_model(ctx, data):
    from statsmodels.genmod.bayes_mixed_glm import BinomialBayesMixedGLM
    sample = data['sample']
    ids = sample[sample.outcome == 'analysable'][['repository_id', 'platform']]
    nb = data['notebooks'].merge(ids, on='repository_id')
    nb = nb.assign(clean=(nb.execution_status == 'SUCCESS').astype(int), resource=nb.repository_id.astype(str))
    rows = []
    for p in PLATFORMS:
        s = nb[nb.platform == p]
        k, n = int(s.clean.sum()), len(s)
        low, high = est.wilson(k, n)
        rows.append(dict(platform=p, notebooks=n, clean=k, share=k / n if n else math.nan, low=low, high=high))
        ctx.macros[f'NotebookClean{NAME[p]}'] = fmt(k / n, 1, True) if n else None
    ctx.table('notebook_clean_shares', pd.DataFrame(rows))
    model = BinomialBayesMixedGLM.from_formula("clean ~ C(platform, Treatment('github'))",
                                               {'resource': '0 + C(resource)'}, nb)
    fit = model.fit_vb()
    sd = float(np.exp(fit.vcp_mean[0]))
    icc = sd ** 2 / (sd ** 2 + math.pi ** 2 / 3)
    fixed = pd.DataFrame({'term': model.exog_names, 'mean': fit.fe_mean, 'sd': fit.fe_sd})
    fixed['odds_ratio'] = np.exp(fixed['mean'])
    fixed['or_low'] = np.exp(fixed['mean'] - est.Z95 * fixed['sd'])
    fixed['or_high'] = np.exp(fixed['mean'] + est.Z95 * fixed['sd'])
    ctx.table('notebook_mixed_model', fixed)
    ctx.results['notebook_model'] = dict(shares=rows, fixed=fixed.to_dict('records'), resource_sd=sd, icc=icc,
                                         notebooks=len(nb), resources=int(nb.resource.nunique()))
    ctx.macros['NotebookICC'] = fmt(icc, 2)
    for p in ('codeberg', 'zenodo'):
        row = fixed[fixed.term.str.contains(f'T.{p}')]
        if len(row):
            ctx.macros[f'NotebookOR{NAME[p]}'] = fmt(float(row.odds_ratio.iloc[0]), 2)
    log.info('notebook model: ICC %.3f, resource SD %.3f', icc, sd)


def section_reproduction(ctx, data):
    sample = data['sample']
    frame = sample.merge(data['res'], left_on='repository_id', right_index=True, how='left')
    frame.index = sample.index
    analysable = frame.outcome == 'analysable'
    out, rows = {}, []
    for column in ('r', 'r_strict', 'r_text_data', 'r_before_error'):
        estimates = ctx.by_platform(frame, column, analysable & frame[column].notna(), f'reproduction:{column}')
        diffs = ctx.compare(estimates)
        out[column] = dict(estimates=estimates, differences=diffs)
        rows += estimate_rows(column, estimates, diffs)
    frame['any_assessable'] = (frame.notebooks_assessable.fillna(0) > 0).astype(float)
    frame['share_assessable'] = frame.notebooks_assessable / frame.notebooks
    for column in ('any_assessable', 'share_assessable'):
        estimates = ctx.by_platform(frame, column, analysable & frame[column].notna(), f'reproduction:{column}')
        out[column] = dict(estimates=estimates)
        rows += estimate_rows(column, estimates)
    ctx.table('reproduction', pd.DataFrame(rows))
    sample_ids = frame.loc[analysable, ['repository_id', 'platform']]
    nb = data['notebooks'].merge(sample_ids, on='repository_id')
    nb_rows = []
    for p in PLATFORMS:
        s = nb[nb.platform == p]
        nb_rows.append(dict(platform=p, notebooks=len(s), executed=int(s.execution_id.notna().sum()),
                            assessable=int((s.cells_assessable.fillna(0) > 0).sum()),
                            no_stored_output=int((s.execution_id.notna() & (s.cells_assessable.fillna(0) == 0)).sum())))
    ctx.table('reproduction_notebook_coverage', pd.DataFrame(nb_rows))
    ctx.results['reproduction'] = dict(out, notebook_coverage=nb_rows)
    for p in PLATFORMS:
        n = NAME[p]
        e = out['r']['estimates'][p]
        ctx.macros.update({f'ReproNorm{n}': fmt(e.mean, 1, True), f'ReproNorm{n}Low': fmt(e.low, 1, True),
                           f'ReproNorm{n}High': fmt(e.high, 1, True), f'ReproNorm{n}N': fmt(e.n_domain),
                           f'ReproStrict{n}': fmt(out['r_strict']['estimates'][p].mean, 1, True),
                           f'ReproStrict{n}Low': fmt(out['r_strict']['estimates'][p].low, 1, True),
                           f'ReproStrict{n}High': fmt(out['r_strict']['estimates'][p].high, 1, True),
                           f'ReproAssessable{n}': fmt(out['share_assessable']['estimates'][p].mean, 1, True),
                           f'ReproNone{n}': fmt(1 - out['any_assessable']['estimates'][p].mean, 1, True)})
    for d in out['r']['differences']:
        name = f'ReproDiff{NAME[d.a]}{NAME[d.b]}'
        ctx.macros.update({name: signed(d.diff), f'{name}P': p_text(d.p), f'{name}Low': signed(d.low),
                           f'{name}High': signed(d.high)})


def failing_notebooks(data):
    sample = data['sample']
    ids = sample[sample.outcome == 'analysable'][['repository_id', 'platform', 'run_status']]
    nb = data['notebooks'].merge(ids, on='repository_id')
    category = nb.error_category.copy()
    category[nb.execution_id.isna() & (nb.run_status == 'ENV_RUN_FAIL')] = 'ENVIRONMENT_FAILURE'
    category[nb.execution_id.isna() & (nb.run_status != 'ENV_RUN_FAIL')] = 'NO_EXECUTION_RECORD'
    category[nb.execution_status == 'SUCCESS'] = None
    return nb.assign(category=category)


def section_failures(ctx, data):
    nb = failing_notebooks(data)
    failing = nb[nb.category.notna()]
    counts = failing.groupby(['platform', 'category']).size().unstack(0, fill_value=0)
    rows = []
    for p in PLATFORMS:
        total = int(counts[p].sum()) if p in counts else 0
        for c in counts.index:
            k = int(counts.at[c, p]) if p in counts else 0
            low, high = est.wilson(k, total)
            rows.append(dict(platform=p, category=c, notebooks=k, failing=total, share=k / total if total else math.nan,
                             low=low, high=high))
    ctx.table('failure_categories_notebooks', pd.DataFrame(rows))

    # resource level: prevalence of each category (design-based, like the primary outcome)
    sample = data['sample']
    prevalence_rows, prevalence = [], {}
    for c in counts.index:
        has = failing[failing.category == c].groupby('repository_id').size().gt(0)
        frame = sample.assign(v=sample.repository_id.map(has).fillna(False).astype(float))
        e = ctx.by_platform(frame, 'v', frame.outcome == 'analysable', f'prevalence:{c}')
        prevalence[c] = e
        prevalence_rows += estimate_rows(c, e)
    ctx.table('failure_categories_resources', pd.DataFrame(prevalence_rows))

    chi_rows = []
    for a, b in PAIRS:
        table = counts[[a, b]]
        table = table[table.sum(axis=1) > 0]
        chi2, p, dof, _ = stats.chi2_contingency(table.to_numpy())
        chi_rows.append(dict(a=a, b=b, chi2=chi2, dof=dof, p=p))
    for row, p in zip(chi_rows, est.holm([r['p'] for r in chi_rows])):
        row['p_holm'] = p
    ctx.table('failure_categories_chi2', pd.DataFrame(chi_rows))

    executed = nb[nb.execution_id.notna()]
    both = executed[executed.error_category.notna() & executed.error_category_by_type.notna()
                    & (executed.execution_status != 'SUCCESS')]
    changed = both[both.error_category != both.error_category_by_type]
    to_external = changed[changed.error_category == 'EXTERNAL_SERVICE_ERROR']
    top_change = (changed.groupby(['error_category_by_type', 'error_category']).size().sort_values(ascending=False))
    ctx.macros.update(ReclassifiedNotebooks=fmt(len(changed)),
                      ReclassifiedShare=fmt(len(changed) / len(both), 1, True) if len(both) else None,
                      ReclassifiedToExternal=fmt(len(to_external)),
                      ReclassifiedTopFrom=CAUSE_TEXT.get(top_change.index[0][0], top_change.index[0][0]) if len(top_change) else None,
                      ReclassifiedTopTo=CAUSE_TEXT.get(top_change.index[0][1], top_change.index[0][1]) if len(top_change) else None,
                      ReclassifiedTopCount=fmt(int(top_change.iloc[0])) if len(top_change) else None)
    transition = pd.crosstab(executed.error_category_by_type.fillna('(none)'), executed.error_category.fillna('(none)'))
    ctx.table('failure_type_to_context', transition, index=True)
    causes = (failing[failing.dependency_cause.notna() & (failing.dependency_cause != '')]
              .assign(cause=lambda f: f.dependency_cause.str.replace(r'\(.*\)', '(...)', regex=True))
              .groupby(['platform', 'cause']).size().rename('notebooks').reset_index()
              .sort_values(['platform', 'notebooks'], ascending=[True, False]))
    ctx.table('dependency_causes', causes)
    groups = {'not installable': ('failed to install', 'not installable', 'no install attempt'),
              'not declared': ('imported indirectly', 'local module'),
              'version': ('version incompatibility', 'submodule missing', 'installed but module not importable',
                          'standard-library')}
    for p in PLATFORMS:
        c = causes[causes.platform == p]
        total = c.notebooks.sum()
        for group, words in groups.items():
            n = c[c.cause.apply(lambda t: any(w in t for w in words))].notebooks.sum()
            macro = 'Dep' + ''.join(w.capitalize() for w in group.split()) + NAME[p]
            ctx.macros[macro] = fmt(n / total, 1, True) if total else None
    install = failing[failing.dependency_cause.fillna('').str.contains('failed to install (', regex=False)]
    packages = (install.assign(package=install.dependency_cause.str.extract(r'failed to install \(([^)]*)\)')[0],
                               declared=install.dependency_cause.str.startswith('declared'))
                .groupby(['package', 'declared']).size().rename('notebooks').reset_index()
                .sort_values('notebooks', ascending=False).head(40))
    ctx.table('dependency_failed_packages', packages)
    timeouts = executed.assign(timeout=executed.error_type == 'NOTEBOOK_TIMEOUT').groupby('platform').agg(
        executed=('timeout', 'size'), timeouts=('timeout', 'sum')).reset_index()
    ctx.table('timeouts', timeouts)
    run_status = sample[sample.outcome == 'analysable'].groupby(['platform', 'run_status']).size().rename('resources').reset_index()
    ctx.table('analysable_run_status', run_status)

    overall = failing.category.value_counts()
    top = overall.index[0]
    ctx.results['failures'] = dict(notebook_shares=rows, prevalence=prevalence, chi2=chi_rows,
                                   overall=overall.to_dict(), failing_notebooks=int(len(failing)),
                                   timeouts=timeouts.to_dict('records'))
    ctx.macros.update(TopFailureCause=CAUSE_TEXT.get(top, top.lower().replace('_', ' ')),
                      TopFailureCauseShare=fmt(overall.iloc[0] / overall.sum(), 1, True),
                      FailingNotebooks=fmt(len(failing)), TimeoutNotebooks=fmt(int(timeouts.timeouts.sum())))
    for p in PLATFORMS:
        if 'DEPENDENCY_ERROR' in prevalence:
            ctx.macros[f'DependencyResources{NAME[p]}'] = fmt(prevalence['DEPENDENCY_ERROR'][p].mean, 1, True)
        if 'EXTERNAL_SERVICE_ERROR' in prevalence:
            ctx.macros[f'ExternalResources{NAME[p]}'] = fmt(prevalence['EXTERNAL_SERVICE_ERROR'][p].mean, 1, True)
        if 'FILE_ERROR' in prevalence:
            ctx.macros[f'FileResources{NAME[p]}'] = fmt(prevalence['FILE_ERROR'][p].mean, 1, True)
        t = timeouts[timeouts.platform == p]
        ctx.macros[f'Timeouts{NAME[p]}'] = fmt(int(t.timeouts.sum())) if len(t) else None
        share = next((r['share'] for r in rows if r['platform'] == p and r['category'] == 'DEPENDENCY_ERROR'), None)
        ctx.macros[f'DependencyShare{NAME[p]}'] = fmt(share, 1, True)
    log.info('failures: %d failing notebooks; top %s', len(failing), top)


# Zenodo licence ids -> SPDX (lower case), for the file-vs-API agreement only
LICENCE_ALIASES = {'mit-license': 'mit', 'apache2.0': 'apache-2.0', 'cc-zero': 'cc0-1.0', 'gpl-3.0-or-later': 'gpl-3.0',
                   'gpl-3.0-only': 'gpl-3.0', 'gpl-2.0-or-later': 'gpl-2.0', 'bsd-license': 'bsd-3-clause'}
MODEL_TERMS = ['year_c', 'dep_declared', 'inline_pip', 'pinned_share', 'python_declared', 'python_moved',
               'log_notebooks', 'log_size', 'licence']


def model_frame(ctx, data):
    sample = data['sample']
    frame = sample[sample.outcome == 'analysable'].merge(data['res'], left_on='repository_id', right_index=True)
    frame = frame.merge(data['covariates'], on='run_id', how='left')
    frame = frame[frame.y.notna()].copy()
    frame['year_c'] = frame.year - 2023
    frame['log_notebooks'] = np.log(frame.notebooks)
    frame['log_size'] = np.log1p(frame.bytes_on_disk.fillna(0) / 1e6)
    frame['licence'] = frame.licence_file.fillna('').ne('') | frame.licence_api.fillna('').ne('')
    for column in ('dep_declared', 'inline_pip', 'python_declared', 'python_moved', 'licence'):
        frame[column] = frame[column].astype('boolean').fillna(False).astype(float)
    weights = pd.Series(1.0, index=frame.index)
    z = frame.platform == 'zenodo'
    if z.any():
        w = frame.loc[z, 'stratum'].map(ctx.results['primary']['zenodo_weights']).astype(float)
        weights[z] = w / w.mean()
    frame['model_weight'] = weights
    frame['rrs10'] = frame.rrs / 10
    frame['rrs10_v1_0'] = frame.rrs_v1_0 / 10
    return frame


def fit_glm(y, X, weights):
    import statsmodels.api as sm
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        return sm.GLM(y, X, family=sm.families.Binomial(), var_weights=weights).fit(cov_type='HC0')


def pseudo_r2(fit):
    return 1 - fit.deviance / fit.null_deviance


def section_models(ctx, data):
    import patsy
    frame = model_frame(ctx, data)
    platform = "C(platform, Treatment('github'))"
    specs = {
        'unadjusted': f'y ~ {platform}',
        'adjusted': f'y ~ {platform} + ' + ' + '.join(MODEL_TERMS),
        'adjusted_rrs': f'y ~ {platform} + ' + ' + '.join(MODEL_TERMS) + ' + rrs10',
        'adjusted_dimensions': f'y ~ {platform} + ' + ' + '.join(MODEL_TERMS) + ' + score_e + score_a + score_d + score_c + score_s',
        'adjusted_rrs_v1_0': f'y ~ {platform} + ' + ' + '.join(MODEL_TERMS) + ' + rrs10_v1_0',
        'adjusted_dimensions_v1_0': f'y ~ {platform} + ' + ' + '.join(MODEL_TERMS) + ' + score_e_v1_0 + score_a_v1_0 + score_d_v1_0 + score_c_v1_0 + score_s_v1_0',
    }
    needed = MODEL_TERMS + ['rrs10', 'rrs10_v1_0', 'score_e', 'score_a', 'score_d', 'score_c', 'score_s',
                            'score_e_v1_0', 'score_a_v1_0', 'score_d_v1_0', 'score_c_v1_0', 'score_s_v1_0']
    complete = frame.dropna(subset=needed).copy()
    complete.index = range(len(complete))
    log.info('models: %d analysable resources, %d complete cases (dropped %d without readiness scores or covariates)',
             len(frame), len(complete), len(frame) - len(complete))
    designs, fits, coef_rows = {}, {}, []
    for name, formula in specs.items():
        y, X = patsy.dmatrices(formula, complete, return_type='dataframe')
        fit = fit_glm(y.iloc[:, 0].to_numpy(), X, complete.model_weight.to_numpy())
        designs[name], fits[name] = (y.iloc[:, 0].to_numpy(), X), fit
        ci = fit.conf_int()
        for term in X.columns:
            coef_rows.append(dict(model=name, term=term, coef=fit.params[term], se=fit.bse[term], p=fit.pvalues[term],
                                  low=ci.loc[term, 0], high=ci.loc[term, 1], odds_ratio=math.exp(fit.params[term])))
    ctx.table('model_coefficients', pd.DataFrame(coef_rows))
    # odds ratios of the adjusted model (fractional logit: ratio of the odds of the clean share)
    term_names = {'year_c': 'Year', 'dep_declared': 'DepDeclared', 'inline_pip': 'InlinePip', 'pinned_share': 'Pinned',
                  'python_declared': 'PythonDeclared', 'python_moved': 'PythonMoved', 'log_notebooks': 'Notebooks',
                  'log_size': 'Size', 'licence': 'Licence'}
    for row in coef_rows:
        if row['model'] == 'adjusted' and row['term'] in term_names:
            n = term_names[row['term']]
            ctx.macros.update({f'OR{n}': fmt(row['odds_ratio'], 2), f'OR{n}Low': fmt(math.exp(row['low']), 2),
                               f'OR{n}High': fmt(math.exp(row['high']), 2), f'OR{n}P': p_text(row['p'])})

    def counterfactual(X, level):
        Xc = X.copy()
        for p in ('codeberg', 'zenodo'):
            column = f"C(platform, Treatment('github'))[T.{p}]"
            Xc[column] = 1.0 if p == level else 0.0
        return Xc

    w = complete.model_weight.to_numpy()

    def ame(fit, X):
        """Average marginal effect of the platform vs GitHub: mean prediction with every resource set to it."""
        base = np.average(fit.predict(counterfactual(X, 'github')), weights=w)
        return {p: float(np.average(fit.predict(counterfactual(X, p)), weights=w) - base)
                for p in ('codeberg', 'zenodo')}

    point = {name: dict(r2=pseudo_r2(fit), ame=ame(fit, designs[name][1]), n=int(fit.nobs)) for name, fit in fits.items()}
    rng = ctx.rng('models')
    groups = [np.flatnonzero(complete.platform.to_numpy() == p) for p in PLATFORMS]
    boot = {name: dict(r2=[], codeberg=[], zenodo=[]) for name in specs}
    for _ in range(ctx.B_model):
        rows = np.concatenate([g[rng.integers(0, len(g), len(g))] for g in groups if len(g)])
        for name in specs:
            y, X = designs[name]
            try:
                fit = fit_glm(y[rows], X.iloc[rows], w[rows])
            except Exception:                         # noqa: BLE001 - a degenerate replicate is skipped
                continue
            Xr, wr = X.iloc[rows].reset_index(drop=True), w[rows]
            base = np.average(fit.predict(counterfactual(Xr, 'github')), weights=wr)
            for p in ('codeberg', 'zenodo'):
                boot[name][p].append(float(np.average(fit.predict(counterfactual(Xr, p)), weights=wr) - base))
            boot[name]['r2'].append(pseudo_r2(fit))
    summary_rows = []
    ci = lambda v: (float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))) if len(v) > 10 else (math.nan, math.nan)  # noqa: E731
    for name in specs:
        b = boot[name]
        r2_low, r2_high = ci(b['r2'])
        row = dict(model=name, n=point[name]['n'], pseudo_r2=point[name]['r2'], r2_low=r2_low, r2_high=r2_high,
                   replicates=len(b['r2']))
        for p in ('codeberg', 'zenodo'):
            low, high = ci(b[p])
            row.update({f'ame_{p}_vs_github': point[name]['ame'][p], f'ame_{p}_low': low, f'ame_{p}_high': high})
        summary_rows.append(row)
    increments = []
    for base, extended in (('adjusted', 'adjusted_rrs'), ('adjusted', 'adjusted_dimensions'),
                           ('adjusted', 'adjusted_rrs_v1_0'), ('adjusted', 'adjusted_dimensions_v1_0'),
                           ('unadjusted', 'adjusted')):
        delta = np.array(boot[extended]['r2'][:min(len(boot[extended]['r2']), len(boot[base]['r2']))]) - \
            np.array(boot[base]['r2'][:min(len(boot[extended]['r2']), len(boot[base]['r2']))])
        low, high = ci(delta)
        increments.append(dict(base=base, extended=extended, delta_r2=point[extended]['r2'] - point[base]['r2'],
                               low=low, high=high))
    ctx.table('model_summary', pd.DataFrame(summary_rows))
    ctx.table('model_r2_increments', pd.DataFrame(increments))
    ctx.results['models'] = dict(complete_cases=len(complete), analysable=len(frame), summary=summary_rows,
                                 increments=increments, bootstrap=ctx.B_model, formulas=specs)
    get = {r['model']: r for r in summary_rows}
    for label, model in (('Unadj', 'unadjusted'), ('Adj', 'adjusted')):
        for p in ('codeberg', 'zenodo'):
            r = get[model]
            ctx.macros.update({f'Ame{label}{NAME[p]}': signed(r[f'ame_{p}_vs_github']),
                               f'Ame{label}{NAME[p]}Low': signed(r[f'ame_{p}_low']),
                               f'Ame{label}{NAME[p]}High': signed(r[f'ame_{p}_high'])})
    inc = {(r['base'], r['extended']): r for r in increments}

    def small(value):                   # an increment that rounds to 0.000 is shown as "<0.001", not as zero
        return '$<$0.001' if value is not None and 0 <= value < 0.0005 else fmt(value, 3)
    ctx.macros.update(PseudoRTwoBase=fmt(get['adjusted']['pseudo_r2'], 3),
                      PseudoRTwoRRS=fmt(get['adjusted_rrs']['pseudo_r2'], 3),
                      DeltaRTwoRRS=small(inc[('adjusted', 'adjusted_rrs')]['delta_r2']),
                      DeltaRTwoRRSLow=fmt(inc[('adjusted', 'adjusted_rrs')]['low'], 3),
                      DeltaRTwoRRSHigh=fmt(inc[('adjusted', 'adjusted_rrs')]['high'], 3),
                      DeltaRTwoDims=fmt(inc[('adjusted', 'adjusted_dimensions')]['delta_r2'], 3),
                      DeltaRTwoRRSOld=small(inc[('adjusted', 'adjusted_rrs_v1_0')]['delta_r2']),
                      ModelResources=fmt(len(complete)))


def section_readiness(ctx, data):
    frame = model_frame(ctx, data)
    scores = ['rrs', 'rrs_v1_0', 'score_e', 'score_a', 'score_d', 'score_c', 'score_s', 'ros']
    rows = []
    for group, sub in [('all', frame)] + [(p, frame[frame.platform == p]) for p in PLATFORMS]:
        for score in scores:
            for outcome in ('y', 'r', 'ros'):
                if score == outcome:
                    continue
                pair = sub[[score, outcome]].dropna()
                if len(pair) < 3 or pair[score].nunique() < 2 or pair[outcome].nunique() < 2:
                    continue
                rho, p = stats.spearmanr(pair[score], pair[outcome])
                rows.append(dict(group=group, score=score, outcome=outcome, n=len(pair), spearman=rho, p=p))
    table = pd.DataFrame(rows)
    ctx.table('readiness_correlations', table)
    sample = data['sample'].merge(frame[['repository_id', 'rrs', 'rrs_v1_0', 'ros']], on='repository_id', how='left')
    means = {}
    for column in ('rrs', 'rrs_v1_0', 'ros'):
        sample_c = sample.assign(v=sample[column] / 100)
        sample_c.index = range(len(sample_c))
        means[column] = ctx.by_platform(sample_c, 'v', (sample_c.outcome == 'analysable') & sample_c.v.notna(),
                                        f'readiness:{column}')
    ctx.table('readiness_means', pd.DataFrame(sum((estimate_rows(c, e) for c, e in means.items()), [])))
    ctx.results['readiness'] = dict(correlations=rows, means=means)
    pick = lambda g, s, o: table[(table.group == g) & (table.score == s) & (table.outcome == o)]  # noqa: E731
    for g, label in [('all', '')] + [(p, NAME[p]) for p in PLATFORMS]:
        r = pick(g, 'rrs', 'y')
        if len(r):
            ctx.macros[f'SpearmanRRSClean{label}'] = decimal(float(r.spearman.iloc[0]))
    r = pick('all', 'rrs', 'ros')
    if len(r):
        ctx.macros['SpearmanRRSROS'] = decimal(float(r.spearman.iloc[0]))
    # the five dimensions (environment, data accessibility, documentation, code portability, signals) vs Y_r
    for score, label in (('score_e', 'Env'), ('score_a', 'Access'), ('score_d', 'Docs'), ('score_c', 'Code'),
                         ('score_s', 'Signals')):
        r = pick('all', score, 'y')
        if len(r):
            ctx.macros[f'Spearman{label}Clean'] = decimal(float(r.spearman.iloc[0]))
            ctx.macros[f'Spearman{label}CleanP'] = p_text(float(r.p.iloc[0]))
    for p in PLATFORMS:
        ctx.macros[f'RRSMean{NAME[p]}'] = fmt(means['rrs'][p].mean * 100, 1)
        ctx.macros[f'ROSMean{NAME[p]}'] = fmt(means['ros'][p].mean * 100, 1)


def section_licences(ctx, data):
    sample = data['sample'].merge(data['covariates'][['run_id', 'licence_api', 'licence_file', 'licence_status']],
                                  on='run_id', how='left')
    sample.index = range(len(sample))
    file_ = sample.licence_file.fillna('').str.strip()
    api = sample.licence_api.fillna('').str.strip()
    sample['file_present'] = file_.ne('').astype(float)
    sample['file_identified'] = (file_.ne('') & file_.str.upper().ne('NOASSERTION') & file_.str.lower().ne('other')).astype(float)
    sample['api_present'] = (api.ne('') & api.str.upper().ne('NOASSERTION')).astype(float)
    analysable = (sample.outcome == 'analysable') & sample.licence_status.eq('ok')
    estimates, rows = {}, []
    for column in ('file_present', 'file_identified', 'api_present'):
        estimates[column] = ctx.by_platform(sample, column, analysable, f'licences:{column}')
        rows += estimate_rows(column, estimates[column])
    ctx.table('licences', pd.DataFrame(rows))
    a = sample[analysable]
    norm = lambda s: s.fillna('').str.strip().str.lower().replace(LICENCE_ALIASES)  # noqa: E731
    f, p_ = norm(a.licence_file), norm(a.licence_api)
    agreement = np.select([f.eq('') & p_.eq(''), f.eq('') & p_.ne(''), f.ne('') & p_.eq(''), f.eq(p_)],
                          ['neither', 'api only', 'file only', 'both same'], 'both different')
    agree = pd.crosstab(a.platform, agreement)
    ctx.table('licences_agreement', agree, index=True)
    top = a.assign(licence=f.where(f.ne(''), '(none)')).groupby(['platform', 'licence']).size().rename('resources') \
        .reset_index().sort_values(['platform', 'resources'], ascending=[True, False]).groupby('platform').head(10)
    ctx.table('licences_top', top)
    ctx.results['licences'] = dict(estimates=estimates, agreement=agree.to_dict())
    for p in PLATFORMS:
        ctx.macros[f'LicenceFile{NAME[p]}'] = fmt(estimates['file_present'][p].mean, 1, True)
        ctx.macros[f'LicenceAPI{NAME[p]}'] = fmt(estimates['api_present'][p].mean, 1, True)


# Files the environment step reads at the resource root (src/requirements stage); an archive that unpacks into one
# folder put them one level down, where rounds 2 and 3 did not look (round 1 stripped such a folder).
ROOT_ENV_FILES = {'requirements.txt', 'pyproject.toml', 'setup.py', 'environment.yml', 'environment.yaml',
                  'runtime.txt', '.python-version', 'Pipfile'}


def wrapped_runs(ctx, run_ids) -> set:
    """Runs whose extracted archive members all sit in one top-level folder that holds an environment file."""
    runs = sorted(set(int(r) for r in run_ids if pd.notna(r)))
    if not runs:
        return set()
    con = ctx.connect()
    members = pd.read_sql_query(
        "SELECT a.repository_run_id AS run_id, f.path FROM acquisition_files f "
        "JOIN resource_acquisitions a ON a.id = f.acquisition_id "
        "WHERE f.origin = 'archive member' AND f.action = 'extracted' "
        f"AND a.repository_run_id IN ({','.join(map(str, runs))})", con)
    con.close()
    out = set()
    for run, paths in members.groupby('run_id').path:
        tops = {p.split('/', 1)[0] if '/' in p else None for p in paths}
        if len(tops) == 1 and None not in tops and {p.split('/', 1)[1] for p in paths if p.count('/') == 1} & ROOT_ENV_FILES:
            out.add(int(run))
    return out


def section_sensitivity(ctx, data):
    nb = data['notebooks']
    variants = {}
    primary = ctx.results.get('primary')
    base_sample, res = data['sample'], data['res']

    def run(label, sample, res_variant, domain_extra=None, y_zero_outside=None):
        frame = sample.merge(res_variant, left_on='repository_id', right_index=True, how='left')
        frame.index = range(len(frame))
        domain = (frame.outcome == 'analysable') & frame.y.notna()
        if y_zero_outside is not None:                     # outcome classes counted with Y_r = 0
            extra = frame.outcome.isin(y_zero_outside)
            frame.loc[extra, 'y'] = 0.0
            domain = domain | extra
        if domain_extra is not None:
            domain = domain & domain_extra(frame)
        estimates = ctx.by_platform(frame, 'y', domain, f'sensitivity:{label}')
        variants[label] = (estimates, ctx.compare(estimates))

    run('1 acquisition failures as Y=0', base_sample, res, y_zero_outside=['not_obtainable'])
    run('2 deduplicated notebooks', base_sample, resource_outcomes(nb, dedup=True))
    declares = data['covariates'].set_index('run_id').declares_only_310
    run('3 Python 3.10 declared only', base_sample, res,
        domain_extra=lambda f: f.run_id.map(declares).astype('boolean').fillna(False).astype(bool))
    run('4 timeouts as missing', base_sample, resource_outcomes(nb, exclude=lambda n: n.error_type == 'NOTEBOOK_TIMEOUT'))
    run('5 external services excluded', base_sample,
        resource_outcomes(nb, exclude=lambda n: n.error_category == 'EXTERNAL_SERVICE_ERROR'))
    run('7 Zenodo unknown content excluded', base_sample, res,
        domain_extra=lambda f: ~((f.platform == 'zenodo') & (f.content == 'unknown')))
    run('8 infrastructure failures included as recorded', data['units'], res)
    run('9 notebooks without execution record excluded', base_sample,
        resource_outcomes(nb, exclude=lambda n: n.execution_id.isna() & n.repository_id.isin(
            base_sample[base_sample.run_status != 'ENV_RUN_FAIL'].repository_id)))
    infra_runs = data['covariates'].set_index('run_id').install_infrastructure
    run('10 runs with infrastructure install failures excluded', base_sample, res,
        domain_extra=lambda f: ~f.run_id.map(infra_runs).astype('boolean').fillna(False).astype(bool))
    # exploratory (notes/analysis-plan.md section 11, added before round 3; marked in the table)
    covariates = data['covariates'].set_index('run_id')
    for label, column in (('E1 Python version not moved', 'python_moved'),
                          ('E2 package-date cutoff applied (no Python 3.8 runs)', 'no_cutoff')):
        flag = covariates[column]
        run(label, base_sample, res,
            domain_extra=lambda f, flag=flag: f.run_id.isin(flag.index) & ~f.run_id.map(flag).astype('boolean')
            .fillna(True).astype(bool))
    analysable_runs = base_sample.loc[base_sample.outcome == 'analysable', 'run_id']
    wrapped = wrapped_runs(ctx, analysable_runs)
    ctx.macros['WrappedZenodo'] = fmt(int(base_sample[base_sample.run_id.isin(wrapped) & (base_sample.platform == 'zenodo')
                                                  & (base_sample.outcome == 'analysable')].shape[0]))
    run('E3 archives unpacked into one folder with environment files excluded', base_sample, res,
        domain_extra=lambda f: ~f.run_id.isin(wrapped))
    if ctx.args.previous_db:                            # the same comparison on the previous round (its own design files)
        prev_args = argparse.Namespace(**{**vars(ctx.args), 'db': ctx.args.previous_db,
                                          'draw_dir': ctx.args.previous_draw_dir,
                                          'infrastructure': ctx.args.previous_infrastructure})
        prev = Context(prev_args, ctx.out / 'previous-round')
        prev_units, prev_nb, _ = load_data(prev)
        prev_sample = prev_units[~prev_units.infrastructure].copy()
        prev_sample.index = range(len(prev_sample))
        estimates, diffs, _ = primary_estimates(prev, prev_sample, resource_outcomes(prev_nb), 'sensitivity:previous')
        variants['R previous round'] = (estimates, diffs)
    if ctx.args.language_map is not None:               # as run: every notebook counted as Python, as the pipeline did
        as_run = Context(argparse.Namespace(**{**vars(ctx.args), 'language_map': None}), ctx.out / 'as-run')
        run_units, run_nb, _ = load_data(as_run)
        run_sample = run_units[~run_units.infrastructure].copy()
        run_sample.index = range(len(run_sample))
        estimates, diffs, _ = primary_estimates(as_run, run_sample, resource_outcomes(run_nb), 'sensitivity:as-run')
        variants['L other-language notebooks included (as run)'] = (estimates, diffs)
    rows = []
    if primary:
        rows += estimate_rows('0 primary', primary['estimates'], primary['differences'])
    for label, (e, d) in variants.items():
        rows += estimate_rows(label, e, d)
    table = pd.DataFrame(rows)
    ctx.table('sensitivity', table)
    shifts = []
    if primary:
        base = {f'{d.a}-{d.b}': d.diff for d in primary['differences']}
        for label, (_, diffs) in variants.items():
            for d in diffs:
                shifts.append(dict(variant=label, pair=f'{d.a}-{d.b}', shift=d.diff - base[f'{d.a}-{d.b}'],
                                   p_holm=d.p_holm, primary_significant=None))
        sig = {f'{d.a}-{d.b}': d.p_holm < 0.05 for d in primary['differences']}
        for s in shifts:
            s['primary_significant'] = sig[s['pair']]
            s['same_conclusion'] = (s['p_holm'] < 0.05) == sig[s['pair']]
            s['prespecified'] = s['variant'][0].isdigit()
        ctx.table('sensitivity_shifts', pd.DataFrame(shifts))
        ctx.latex['tab-sensitivity'] = sensitivity_latex(primary, variants)
        pre = [s for s in shifts if s['prespecified']]                 # the text's figures: pre-specified variants
        ctx.macros['SensMaxShift'] = fmt(max(abs(s['shift']) for s in pre) * 100, 1)
        ctx.macros['SensConclusionChanges'] = fmt(sum(not s['same_conclusion'] for s in pre))
        for code, macro in (('E1', 'SensPythonNotMoved'), ('E2', 'SensCutoffApplied'), ('R', 'SensPreviousRound'),
                            ('L', 'SensAsRun'), ('E3', 'SensWrapped')):
            rows_ = [s for s in shifts if s['variant'].startswith(code + ' ')]
            if rows_:
                ctx.macros[f'{macro}MaxShift'] = fmt(max(abs(s['shift']) for s in rows_) * 100, 1)
                ctx.macros[f'{macro}Changes'] = fmt(sum(not s['same_conclusion'] for s in rows_))
        for label, (e, _) in variants.items():
            if label.startswith(('E1 ', 'E2 ')):
                macro = 'SensPythonNotMoved' if label.startswith('E1') else 'SensCutoffApplied'
                ctx.macros[f'{macro}N'] = fmt(sum(e[p].n_domain for p in PLATFORMS))
    ctx.results['sensitivity'] = dict(variants={k: dict(estimates=e, differences=d) for k, (e, d) in variants.items()},
                                      shifts=shifts)
    ctx.results['sensitivity']['note_6'] = 'RRS 1.0 instead of 1.1: see models (adjusted_rrs_v1_0) and readiness (rrs_v1_0).'


# Fig. 2 (paper/msr2027/fig-outcomes.pdf): a diverging scheme, blue = clean / reproduced, red = failing / different,
# gray midpoint; each arm checked as an ordinal ramp with the dataviz validator (light surface).
FIG_BLUE = ('#1c5cab', '#86b6ef')
FIG_RED = ('#ee8a83', '#b3261e')
FIG_GRAY = '#b5b4ae'
CELL_GROUPS = [('identical', 'identical'), ('normalised', 'same after normalisation'),
               ('different', 'different'), ('failed', 'error or missing')]


def section_figures(ctx, data):
    """Fig. 2: (a) resources with no, some or all notebooks clean; (b) mix of the assessable cells. Design-based means
    per platform (Zenodo weighted); the figure goes to the output folder and to the paper."""
    sample, res, nb = data['sample'], data['res'], data['notebooks']
    frame = sample.merge(res, left_on='repository_id', right_index=True, how='left')
    frame.index = sample.index
    analysable = (frame.outcome == 'analysable') & frame.y.notna()
    frame['none_clean'] = (frame.y == 0).astype(float)
    frame['all_clean'] = (frame.y == 1).astype(float)
    frame['some_clean'] = ((frame.y > 0) & (frame.y < 1)).astype(float)
    clean = {k: ctx.by_platform(frame, k, analysable, f'figure:{k}') for k in ('none_clean', 'some_clean', 'all_clean')}

    cells = nb[nb.cells_assessable.fillna(0) > 0].copy()
    total = cells.cells_assessable.astype(float)
    cells['identical'] = cells.cells_identical.fillna(0) / total
    cells['normalised'] = (cells.cells_reproduced_normalised.fillna(0) - cells.cells_identical.fillna(0)) / total
    cells['failed'] = (cells.cells_error.fillna(0) + cells.cells_missing.fillna(0)) / total
    cells['different'] = 1 - cells.identical - cells.normalised - cells.failed     # different, image only
    per_resource = cells.groupby('repository_id')[[k for k, _ in CELL_GROUPS]].mean()
    mix_frame = sample.merge(per_resource, left_on='repository_id', right_index=True, how='left')
    mix_frame.index = sample.index
    has_cells = (mix_frame.outcome == 'analysable') & mix_frame.identical.notna()
    mix = {k: ctx.by_platform(mix_frame, k, has_cells, f'figure:cells:{k}') for k, _ in CELL_GROUPS}

    rows = [dict(panel='a', platform=p, part=k, mean=e[p].mean, low=e[p].low, high=e[p].high)
            for k, e in clean.items() for p in PLATFORMS]
    rows += [dict(panel='b', platform=p, part=k, mean=e[p].mean, low=e[p].low, high=e[p].high)
             for k, e in mix.items() for p in PLATFORMS]
    ctx.table('figure_outcomes', pd.DataFrame(rows))
    for p in PLATFORMS:
        n = NAME[p]
        ctx.macros.update({f'ResNoneClean{n}': fmt(clean['none_clean'][p].mean, 1, True),
                           f'ResAllClean{n}': fmt(clean['all_clean'][p].mean, 1, True),
                           f'CellsIdentical{n}': fmt(mix['identical'][p].mean, 1, True),
                           f'CellsNormalised{n}': fmt(mix['normalised'][p].mean, 1, True),
                           f'CellsDifferent{n}': fmt(mix['different'][p].mean, 1, True),
                           f'CellsFailed{n}': fmt(mix['failed'][p].mean, 1, True)})

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size': 7, 'font.family': 'serif', 'axes.linewidth': 0.5, 'pdf.fonttype': 42})
    fig, axes = plt.subplots(2, 1, figsize=(3.45, 2.35), sharex=True, gridspec_kw=dict(hspace=0.95))
    order = list(reversed(PLATFORMS))                                # Codeberg on top
    panels = [(axes[0], [(clean['all_clean'], 'all notebooks clean', FIG_BLUE[0]),
                         (clean['some_clean'], 'some', FIG_GRAY),
                         (clean['none_clean'], 'none', FIG_RED[1])], '(a) Resources by share of clean notebooks'),
              (axes[1], [(mix[k], label, color) for (k, label), color in
                         zip(CELL_GROUPS, (FIG_BLUE[0], FIG_BLUE[1], FIG_RED[0], FIG_RED[1]))],
               '(b) Assessable cells by comparison outcome')]
    for ax, parts, title in panels:
        left = np.zeros(len(order))
        for estimates, label, color in parts:
            width = np.array([estimates[p].mean for p in order]) * 100
            ax.barh(range(len(order)), width, left=left, height=0.62, color=color, edgecolor='white', linewidth=0.8,
                    label=label)
            for i, (x0, w) in enumerate(zip(left, width)):             # direct labels where they fit
                if w >= 9:
                    dark = color in (FIG_BLUE[0], FIG_RED[1])
                    ax.text(x0 + w / 2, i, f'{w:.0f}', ha='center', va='center', fontsize=6,
                            color='white' if dark else '#1f1f1f')
            left += width
        ax.set_yticks(range(len(order)), [LABEL[p] for p in order])
        ax.set_xlim(0, 100)
        ax.set_title(title, loc='left', fontsize=7, pad=3)
        ax.tick_params(length=2, width=0.5)
        for side in ('top', 'right', 'left'):
            ax.spines[side].set_visible(False)
        ax.legend(ncol=len(parts), loc='upper left', bbox_to_anchor=(-0.02, -0.12 if ax is axes[0] else -0.26),
                  frameon=False, fontsize=6, handlelength=1, handletextpad=0.4, columnspacing=0.9)
    figures = ctx.out / 'figures'
    figures.mkdir(exist_ok=True)
    fig.savefig(figures / 'fig-outcomes.pdf', bbox_inches='tight', pad_inches=0.01)
    fig.savefig(figures / 'fig-outcomes.png', bbox_inches='tight', pad_inches=0.01, dpi=300)
    plt.close(fig)
    ctx.figures['fig-outcomes'] = figures / 'fig-outcomes.pdf'


SENSITIVITY_LABELS = {
    '0 primary': 'Primary analysis', '1 acquisition failures as Y=0': 'Unobtainable as $\\Yr=0$',
    '2 deduplicated notebooks': 'Duplicate notebooks once', '3 Python 3.10 declared only': 'Declared Python 3.10 only',
    '4 timeouts as missing': 'Timeouts as missing', '5 external services excluded': 'External services excluded',
    '7 Zenodo unknown content excluded': 'Zenodo unknown content excl.',
    '8 infrastructure failures included as recorded': 'Infrastructure failures incl.',
    '9 notebooks without execution record excluded': 'Notebooks w/o record excluded',
    '10 runs with infrastructure install failures excluded': 'Network install failures excluded',
    'E1 Python version not moved': 'Python version not moved$^{\\dagger}$',
    'E2 package-date cutoff applied (no Python 3.8 runs)': 'Package-date cutoff applied$^{\\dagger}$',
    'E3 archives unpacked into one folder with environment files excluded': 'Unpacked-folder archives excl.$^{\\dagger}$',
    'R previous round': 'Previous round (round 2)',
    'L other-language notebooks included (as run)': 'Other-language notebooks incl.$^{\\dagger}$'}


def sensitivity_latex(primary, variants):
    """Rows: variants; columns: the three primary differences (percentage points), * = Holm-adjusted p < .05."""
    def cell(d):
        star = '$^{*}$' if d.p_holm is not None and d.p_holm < 0.05 else ''
        return f'{signed(d.diff)}{star}'
    rows = [('0 primary', primary['differences'])] + [(k, d) for k, (_, d) in variants.items()]
    lines = ['% Generated by analysis/paper/run_analysis.py - do not edit.',
             '\\begin{tabular}{lrrr}', '\\toprule',
             'Variant & C--G & C--Z & G--Z \\\\', '\\midrule']
    separated = False
    for label, diffs in rows:
        if not label[0].isdigit() and not separated:          # exploratory variants and the previous round
            lines.append('\\midrule')
            separated = True
        by = {(d.a, d.b): d for d in diffs}
        lines.append(f"{SENSITIVITY_LABELS.get(label, label)} & " + ' & '.join(cell(by[pair]) for pair in PAIRS) + ' \\\\')
        if label == '0 primary':
            lines.append('\\midrule')
    lines += ['\\bottomrule', '\\end{tabular}']
    return '\n'.join(lines) + '\n'


def section_duplicates(ctx, data):
    sample = data['sample']
    ids = sample[sample.outcome == 'analysable'][['repository_id', 'platform']]
    nb = data['notebooks'].merge(ids, on='repository_id')
    hashed = nb[nb.sha256.notna()]
    within = hashed.duplicated(['repository_id', 'sha256']).groupby(hashed.platform).sum()
    platforms_per_hash = hashed.groupby('sha256').platform.nunique()
    cross = hashed[hashed.sha256.map(platforms_per_hash) > 1]
    rows = [dict(platform=p, notebooks=int((nb.platform == p).sum()), hashed=int((hashed.platform == p).sum()),
                 duplicates_within_resource=int(within.get(p, 0)),
                 resources_sharing_a_notebook_with_another_platform=int(cross[cross.platform == p].repository_id.nunique()))
            for p in PLATFORMS]
    ctx.table('duplicates', pd.DataFrame(rows))
    ctx.results['duplicates'] = rows
    for row in rows:                    # Threats: the same project on several platforms (identical notebook files)
        ctx.macros[f'SharedAcross{NAME[row["platform"]]}'] = fmt(row['resources_sharing_a_notebook_with_another_platform'])


def section_install_quality(ctx, data):
    """Quality gate: package installs that failed for infrastructure reasons (network/proxy, an index without
    upload-time metadata). These make Y_r too low for the resource, so they must be near zero for valid results."""
    sample = data['sample']
    frame = sample[sample.outcome == 'analysable'].merge(data['covariates'], on='run_id', how='left')
    rows = []
    for p in PLATFORMS:
        f = frame[frame.platform == p]
        rows.append(dict(platform=p, runs=len(f), runs_with_failed_installs=int((f.install_failed > 0).sum()),
                         runs_infrastructure=int(f.install_infrastructure.sum()),
                         runs_network=int((f.install_network > 0).sum()),
                         runs_index_no_upload_time=int((f.install_index_no_upload_time > 0).sum()),
                         packages_failed=int(f.install_failed.sum()),
                         packages_network=int(f.install_network.sum()),
                         packages_index_no_upload_time=int(f.install_index_no_upload_time.sum())))
    table = pd.DataFrame(rows)
    ctx.table('install_quality', table)
    con = ctx.connect()
    examples = pd.read_sql_query("SELECT DISTINCT substr(error, 1, 400) AS error FROM run_packages WHERE status = 'failed' "
                                 "AND (error LIKE '%Tunnel connection failed%' OR error LIKE '%upload-time metadata%') LIMIT 20", con)
    con.close()
    ctx.table('install_quality_examples', examples)
    share = float(frame.install_infrastructure.mean()) if len(frame) else 0.0
    ctx.results['install_quality'] = dict(platforms=rows, share_runs_affected=share, gate=0.01, passed=share <= 0.01)
    ctx.macros.update(InstallInfraRuns=fmt(int(frame.install_infrastructure.sum())), InstallInfraShare=fmt(share, 1, True))
    if share > 0.01:
        log.warning('QUALITY GATE FAILED: %.1f%% of analysable runs (%d) had package installs fail for infrastructure '
                    'reasons (network/proxy or an index without upload-time metadata); Y_r is biased low for them. '
                    'See tables/install_quality.csv.', 100 * share, int(frame.install_infrastructure.sum()))


SECTIONS = [('cohort', section_cohort), ('install_quality', section_install_quality), ('availability', section_availability), ('primary', section_primary),
            ('notebook_model', section_notebook_model), ('reproduction', section_reproduction),
            ('failures', section_failures), ('models', section_models), ('readiness', section_readiness),
            ('licences', section_licences), ('sensitivity', section_sensitivity), ('duplicates', section_duplicates),
            ('figures', section_figures)]


# --------------------------------------------------------------------------------------------- provenance

def provenance_base(args, started):
    status = git('status', '--porcelain') or ''
    dirty = [line[3:] for line in status.splitlines()]
    relevant = [p for p in dirty if p.startswith(('analysis/paper/', 'pipeline/topup_from_reserve.py', 'notes/analysis-plan.md'))]
    packages = {}
    for name in PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    inputs = {rel(args.db): dict(sha256=sha256(args.db), bytes=args.db.stat().st_size)}
    for path in sorted(args.draw_dir.iterdir()):
        if path.is_file():
            inputs[rel(path)] = dict(sha256=sha256(path), bytes=path.stat().st_size)
    for name in INPUT_FILES:
        path = ROOT / name
        if path.is_file():
            inputs[name] = dict(sha256=sha256(path), bytes=path.stat().st_size,
                                last_commit=git('log', '-1', '--format=%H %cI', '--', name) or None,
                                dirty=name in dirty)
    if args.infrastructure is not None and args.infrastructure != (ROOT / INPUT_FILES[2]).resolve():
        inputs[rel(args.infrastructure)] = dict(sha256=sha256(args.infrastructure))
    if args.notebook_languages:
        inputs[rel(args.notebook_languages.resolve())] = dict(sha256=sha256(args.notebook_languages))
    pipeline = {}
    for name in ('MANIFEST.txt', 'state.json'):       # the the cluster bundle has no .git: its manifest names the commit
        path = args.db.parent / name
        if path.is_file():
            inputs[rel(path)] = dict(sha256=sha256(path), bytes=path.stat().st_size)
            if name == 'MANIFEST.txt':
                first = path.read_text().splitlines()[:2]
                pipeline.update(line.split(': ', 1) for line in first if ': ' in line)
            else:
                state = json.loads(path.read_text())
                pipeline.update(study_created=state.get('created'), study_complete=state.get('complete'),
                                passes=len(state.get('passes') or []))
    if not pipeline:
        log.warning('no MANIFEST.txt/state.json next to the database: the pipeline commit is not recorded')
    con = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    try:
        sessions = [dict(zip(('pipeline_commit', 'pipeline_dirty', 'sessions', 'first', 'last'), row)) for row in con.execute(
            'SELECT pipeline_commit, pipeline_dirty, COUNT(*), MIN(started_at), MAX(started_at) FROM pipeline_sessions '
            'GROUP BY 1, 2')]
        merges = con.execute('SELECT COUNT(*), MIN(merged_at), MAX(merged_at) FROM merge_sources').fetchone()
    except sqlite3.Error as error:
        sessions, merges = [f'unavailable: {error}'], None
    con.close()
    return dict(
        analysis='cross-platform study, pre-specified analysis (notes/analysis-plan.md)',
        started_at=started, command=[sys.executable, *sys.argv],
        git=dict(commit=git('rev-parse', 'HEAD'), branch=git('rev-parse', '--abbrev-ref', 'HEAD'),
                 dirty_files=len(dirty), dirty_relevant=relevant),
        scripts={name: sha256(ROOT / name) for name in SCRIPT_FILES},
        python=dict(version=sys.version, executable=sys.executable, packages=packages),
        host=dict(hostname=socket.gethostname(), platform=host_platform.platform(), machine=host_platform.machine(),
                  cpus=os.cpu_count(), user=os.environ.get('USER')),
        parameters=dict(infrastructure_list=rel(args.infrastructure) if args.infrastructure else 'none',
                        notebook_languages=rel(args.notebook_languages.resolve()) if args.notebook_languages else 'none', seed=args.seed, bootstrap=args.bootstrap, model_bootstrap=args.model_bootstrap,
                        only_present=args.only_present, codeberg_fpc=True, alpha=0.05, multiplicity='Holm (3)',
                        target_per_platform=400),
        inputs=inputs, pipeline=pipeline, pipeline_sessions=sessions,
        merge=dict(sources=merges[0], first=merges[1], last=merges[2]) if merges else None,
        deviations_from_plan=DEVIATIONS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, default=ROOT / 'results' / 'study' / 'merged.sqlite')
    parser.add_argument('--draw-dir', type=Path, default=ROOT / 'data' / 'sampling' / 'draw' / '2026-10-09')
    parser.add_argument('--infrastructure', default=str(ROOT / INPUT_FILES[2]),
                        help="CSV of resources hit by infrastructure failures (excluded from the primary analysis), "
                             "or 'none'; the default list belongs to round 1 (results/study)")
    parser.add_argument('--out-root', type=Path, default=ROOT / 'results' / 'analysis')
    parser.add_argument('--notebook-languages', type=Path, default=None,
                        help='CSV of notebook_languages.py for the passes merged into --db; notebooks of other '
                             'languages are removed (keyed by resource and path, so it also applies to --previous-db)')
    parser.add_argument('--paper-json', type=Path, default=ROOT / 'paper' / 'msr2027' / 'results' / 'study.json')
    parser.add_argument('--no-paper', action='store_true', help='do not write the paper macros (tests)')
    parser.add_argument('--macro-prefix', default='',
                        help='prefix for every paper macro (e.g. RoundOne with --paper-json .../round1.json), so that '
                             'an earlier round can be cited next to the main results; no LaTeX tables then')
    parser.add_argument('--seed', type=int, default=20261011)
    parser.add_argument('--bootstrap', type=int, default=2000, help='Zenodo two-phase bootstrap replicates')
    parser.add_argument('--model-bootstrap', type=int, default=500, help='replicates for model AMEs and R^2')
    parser.add_argument('--only-present', action='store_true',
                        help='test mode: keep only drawn resources that have a run in the database')
    parser.add_argument('--sections', default=','.join(n for n, _ in SECTIONS))
    parser.add_argument('--previous-db', type=Path, default=None,
                        help='merged database of the previous round: its primary comparison becomes a sensitivity row')
    parser.add_argument('--previous-draw-dir', type=Path, default=None, help='draw folder of the previous round')
    parser.add_argument('--previous-infrastructure', default='none',
                        help="exclusion list of the previous round (CSV or 'none')")
    args = parser.parse_args(argv)
    if args.previous_db:
        if not args.previous_draw_dir:
            sys.exit('--previous-db needs --previous-draw-dir')
        args.previous_db, args.previous_draw_dir = args.previous_db.resolve(), args.previous_draw_dir.resolve()
        args.previous_infrastructure = (None if args.previous_infrastructure.lower() == 'none'
                                        else Path(args.previous_infrastructure).resolve())
    args.db, args.draw_dir = args.db.resolve(), args.draw_dir.resolve()
    args.language_map = None                           # loaded once logging is set up
    args.infrastructure = None if args.infrastructure.lower() == 'none' else Path(args.infrastructure).resolve()
    if args.db == FROZEN:
        sys.exit('refusing the frozen evaluation database; use the merged database of the study')

    started = datetime.now(timezone.utc)
    commit = (git('rev-parse', '--short', 'HEAD') or 'nogit')
    out_dir = args.out_root / f'{started:%Y%m%dT%H%M%SZ}-{commit}{"-test" if args.only_present else ""}'
    out_dir.mkdir(parents=True, exist_ok=False)
    handlers = [logging.FileHandler(out_dir / 'analysis.log', encoding='utf-8'), logging.StreamHandler(sys.stderr)]
    logging.basicConfig(level=logging.INFO, handlers=handlers, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.captureWarnings(True)
    log.info('output: %s', rel(out_dir))
    provenance = provenance_base(args, started.isoformat(timespec='seconds'))
    log.info('inputs: db %s sha256 %s; git %s (%d dirty files)', rel(args.db), provenance['inputs'][rel(args.db)]['sha256'],
             provenance['git']['commit'], provenance['git']['dirty_files'])
    if provenance['git']['dirty_relevant']:
        log.warning('uncommitted analysis files (pinned by their SHA-256 in provenance.json): %s',
                    provenance['git']['dirty_relevant'])

    if args.notebook_languages:
        args.language_map = load_notebook_languages(args.notebook_languages, args.db)
    ctx = Context(args, out_dir)
    t0 = time.monotonic()
    units, notebooks, infra = load_data(ctx)
    sample = units[~units.infrastructure].copy()
    sample.index = range(len(sample))
    data = dict(units=units, sample=sample, notebooks=notebooks, infra=infra, res=resource_outcomes(notebooks),
                covariates=resource_covariates(ctx, units.run_id))
    ctx.table('resources', sample.drop(columns=['key']).merge(data['res'], left_on='repository_id', right_index=True, how='left'))
    timings = {'load': round(time.monotonic() - t0, 2)}
    failed = {}
    wanted = set(args.sections.split(','))
    for name, function in SECTIONS:
        if name not in wanted:
            continue
        t = time.monotonic()
        log.info('section %s ...', name)
        try:
            function(ctx, data)
        except Exception as error:                     # noqa: BLE001 - one failing analysis must not stop the others
            failed[name] = f'{type(error).__name__}: {error}'
            log.error('section %s failed:\n%s', name, traceback.format_exc())
        timings[name] = round(time.monotonic() - t, 2)
        log.info('section %s done in %.1f s', name, timings[name])

    (out_dir / 'results.json').write_text(json.dumps(jsonable(ctx.results), indent=1) + '\n')
    macros = {f'{args.macro_prefix}{k}': v for k, v in sorted(ctx.macros.items()) if v is not None}
    (out_dir / 'macros.json').write_text(json.dumps(macros, indent=1) + '\n')
    finished = datetime.now(timezone.utc)
    outputs = {rel(p): sha256(p) for p in sorted(out_dir.rglob('*')) if p.is_file() and p.name != 'analysis.log'}
    provenance.update(finished_at=finished.isoformat(timespec='seconds'),
                      seconds=round((finished - started).total_seconds(), 1), timings=timings,
                      failed_sections=failed, macros=len(macros), outputs=outputs, output_dir=rel(out_dir))
    if not args.no_paper:
        if args.only_present:
            log.warning('--only-present: the paper macros are not written (test mode)')
        else:
            args.paper_json.parent.mkdir(parents=True, exist_ok=True)
            args.paper_json.write_text(json.dumps(dict(
                generated_by='analysis/paper/run_analysis.py', generated_at=provenance['finished_at'],
                analysis_output=rel(out_dir), git_commit=provenance['git']['commit'],
                pipeline_commit=provenance['pipeline'].get('source commit'), macro_prefix=args.macro_prefix,
                scripts=provenance['scripts'], db_sha256=provenance['inputs'][rel(args.db)]['sha256'],
                failed_sections=failed, quality_gate=ctx.results.get('install_quality', {}).get('passed'),
                macros=macros), indent=1) + '\n')
            log.info('paper macros: %s (%d)', rel(args.paper_json), len(macros))
            for name, source in ({} if args.macro_prefix else ctx.latex).items():
                target = args.paper_json.parent.parent / f'{name}.tex'
                target.write_text(source)
                (out_dir / 'tables' / f'{name}.tex').write_text(source)
                provenance.setdefault('paper_tex', {})[rel(target)] = sha256(target)
                log.info('paper table: %s', rel(target))
            for name, source in ({} if args.macro_prefix else ctx.figures).items():
                target = args.paper_json.parent.parent / f'{name}.pdf'
                shutil.copyfile(source, target)
                provenance.setdefault('paper_figures', {})[rel(target)] = sha256(target)
                log.info('paper figure: %s', rel(target))
            provenance['paper_json'] = dict(path=rel(args.paper_json), sha256=sha256(args.paper_json))
    log.info('finished in %.1f s; failed sections: %s', provenance['seconds'], failed or 'none')
    (out_dir / 'provenance.json').write_text(json.dumps(jsonable(provenance), indent=1) + '\n')
    logging.shutdown()
    lines = [f'{sha256(p)}  {p.relative_to(out_dir)}' for p in sorted(out_dir.rglob('*'))
             if p.is_file() and p.name != 'MANIFEST.sha256']
    (out_dir / 'MANIFEST.sha256').write_text('\n'.join(lines) + '\n')
    latest = args.out_root / 'latest'
    if latest.is_symlink() or not latest.exists():
        latest.unlink(missing_ok=True)
        latest.symlink_to(out_dir.name)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
