#!/usr/bin/env python3
"""ReproScore (https://anonymous.example/reproscore; Samuel, TPDL 2026) as a pipeline stage.

  rrs      Reproducibility Readiness Score of the acquired resource folder: 26 static sub-metrics in
           5 categories (E environment, A data, D documentation, C code portability, S signals).
           Runs on the fresh clone, before anything is installed or executed (the clone is deleted
           after the run, so this cannot be done later). Stored in resource_readiness with every
           sub-metric's evidence.
  outcome  Reproducibility Outcome Score and Composite Score from this pipeline's own execution
           records (no second execution). Mapping version 2:
             I  install success      the environment stage succeeded and every package the pipeline
                                     decided to install was installed (run_packages)
             N  notebook exec rate   notebooks with execution_status SUCCESS / executed notebooks
             E  import success rate  executed notebooks without DEPENDENCY_ERROR / executed notebooks
             R  output reproduction  mean comparison-v2 score (score_normalised) of the notebooks
                                     with assessable cells (stored outputs to compare with)
             X, delta, T             not measured (no entry point, one execution, no tests) -> None
           ROS is normalised over the available components and RCS blends it with RRS by coverage
           (ReproScore's ROSScorer / RCSScorer) with the rubric REPROSCORE_RUBRIC
           (config/reproscore_rubric.yaml: default weights x 0.85 plus R 0.15). Stored in
           reproscore_outcome; recomputable on any database (e.g. after a merge). The same evidence
           is also scored with ReproScore's default rubric (ros_default, rcs_default; R has no weight
           there and is ignored), comparable with the ReproScore paper.
RRS is computed twice, each time with that version's default rubric and in its own process
(src/reproscore_rrs_worker.py; both versions are packages called `src`):
  rrs, score_*, evidence_json     ReproScore 1.1 (third_party/reproscore; fixes from the code review)
  rrs_v1_0, score_*_v1_0          ReproScore 1.0 (third_party/reproscore-1.0; the TPDL 2026 paper version)
ROS/RCS use 1.1's scorers; rcs_v1_0 = RRS 1.0 with the default-rubric ROS (fully paper-comparable).
REPROSCORE_BASELINE_DIR='' switches the 1.0 score off.

ReproScore is part of this repository (third_party/reproscore; upstream commit and patches in its
VENDORED.md and VERSION) and is imported with its own folder first on the path (both projects have
a top-level package called `src`). VERSION, rubric names and SHA-256 are stored with every score.
Never fails a run: problems are recorded as status 'error' / 'unavailable'.

Usage:
  reproscore_stage.py --db DB rrs --run-id N --repo-dir DIR
  reproscore_stage.py --db DB outcome [--run-id N]          (all runs without an outcome if omitted)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAPPING_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_readiness (
    id INTEGER PRIMARY KEY AUTOINCREMENT, repository_run_id INTEGER NOT NULL UNIQUE, repository_id INTEGER,
    tool TEXT, tool_commit TEXT, rubric_name TEXT, rubric_sha256 TEXT,
    rrs REAL, score_e REAL, score_a REAL, score_d REAL, score_c REAL, score_s REAL,
    penalties_json TEXT, submetrics_json TEXT, evidence_json TEXT, seconds REAL, status TEXT, error TEXT,
    created_at TEXT, FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id));
CREATE TABLE IF NOT EXISTS reproscore_outcome (
    id INTEGER PRIMARY KEY AUTOINCREMENT, repository_run_id INTEGER NOT NULL UNIQUE, repository_id INTEGER,
    mapping_version INTEGER, tool_commit TEXT,
    install_success INTEGER, execution_success INTEGER, output_determinism REAL, notebook_exec_rate REAL,
    import_success_rate REAL, test_pass_rate REAL, output_reproduction REAL, notebooks_executed INTEGER,
    notebooks_compared INTEGER, rubric_name TEXT, rubric_sha256 TEXT,
    ros REAL, ros_components_json TEXT, coverage_weight_sum REAL, rrs REAL, rcs REAL, alpha REAL,
    coverage_level TEXT, ros_default REAL, ros_default_components_json TEXT, coverage_weight_sum_default REAL,
    rcs_default REAL, alpha_default REAL, rubric_default_sha256 TEXT, status TEXT, error TEXT, created_at TEXT,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id));
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


READINESS_COLUMNS = {'rrs_v1_0': 'REAL', 'score_e_v1_0': 'REAL', 'score_a_v1_0': 'REAL', 'score_d_v1_0': 'REAL',
                     'score_c_v1_0': 'REAL', 'score_s_v1_0': 'REAL', 'tool_commit_v1_0': 'TEXT',
                     'penalties_v1_0_json': 'TEXT', 'submetrics_v1_0_json': 'TEXT', 'status_v1_0': 'TEXT',
                     'error_v1_0': 'TEXT', 'seconds_v1_0': 'REAL'}
OUTCOME_COLUMNS = {'output_reproduction': 'REAL', 'notebooks_compared': 'INTEGER', 'rubric_name': 'TEXT',
                   'rubric_sha256': 'TEXT', 'ros_default': 'REAL', 'ros_default_components_json': 'TEXT',
                   'coverage_weight_sum_default': 'REAL', 'rcs_default': 'REAL', 'alpha_default': 'REAL',
                   'rubric_default_sha256': 'TEXT', 'rcs_v1_0': 'REAL', 'alpha_v1_0': 'REAL'}


def tool_dir():
    return Path(os.environ.get('REPROSCORE_DIR') or PROJECT_ROOT / 'third_party' / 'reproscore')


def outcome_rubric_path():
    return Path(os.environ.get('REPROSCORE_RUBRIC') or PROJECT_ROOT / 'config' / 'reproscore_rubric.yaml')


def load_reproscore():
    """Import reproscore's scorers from REPROSCORE_DIR; returns (module dict, commit)."""
    folder = tool_dir()
    if not (folder / 'src' / 'scoring' / 'rrs.py').is_file():
        raise FileNotFoundError(f'reproscore not found in {folder} (expected third_party/reproscore or REPROSCORE_DIR)')
    own = {str(PROJECT_ROOT), str(PROJECT_ROOT / 'src'), ''}
    sys.path[:] = [str(folder)] + [p for p in sys.path if os.path.abspath(p or '.') not in own and p not in own]
    for name in [m for m in sys.modules if m == 'src' or m.startswith('src.')]:
        del sys.modules[name]
    from src.scoring import rcs, ros, rrs, rubric  # noqa: E402  (reproscore's package)
    version = folder / 'VERSION'                       # copy in this repository: upstream commit + patches
    if version.is_file():
        commit = version.read_text().strip()
    else:                                              # a separate clone (REPROSCORE_DIR)
        try:
            commit = subprocess.run(['git', '-C', str(folder), 'rev-parse', 'HEAD'], capture_output=True,
                                    text=True, timeout=30).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            commit = None
    return dict(rrs=rrs, ros=ros, rcs=rcs, rubric=rubric, folder=folder), commit


# reproscore (commit 1d7a559) tests every component of the full path, not only the part inside the
# repository: a component starting with "." (e.g. "../repo", "~/.cache/...") drops all files of some
# detectors (expected_outputs: 40 -> 0 for carate), and env/build/dist/venv/... drop all notebooks.
# The resource is therefore always scored through a symlink at a clean temporary path.
EXCLUDED_PARTS = {'.ipynb_checkpoints', '.reproscore', 'site-packages', 'venv', '.venv', 'env', 'node_modules',
                  '__pycache__', '.git', '.tox', '.nox', 'dist', 'build', 'eggs', '.eggs', 'lib'}


def clean_prefix(path):
    return not any(part.startswith('.') or part in EXCLUDED_PARTS for part in Path(path).parts[1:])


def scoring_path(repo_dir):
    """(path to score, temporary folder to remove or None): a symlink /tmp/rrs-XXXX/resource -> repo."""
    for base in (tempfile.gettempdir(), '/tmp'):
        if clean_prefix(base):
            folder = Path(tempfile.mkdtemp(prefix='rrs-', dir=base))
            link = folder / 'resource'
            link.symlink_to(Path(repo_dir).resolve(), target_is_directory=True)
            return link, folder
    return Path(repo_dir).resolve(), None


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None


def connect(db):
    connection = sqlite3.connect(db, timeout=60)
    connection.executescript(SCHEMA)
    for table, wanted in (('reproscore_outcome', OUTCOME_COLUMNS), ('resource_readiness', READINESS_COLUMNS)):
        present = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        for column, kind in wanted.items():
            if column not in present:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
    return connection


def repository_id(connection, run_id):
    row = connection.execute('SELECT repository_id FROM repository_runs WHERE id = ?', (run_id,)).fetchone()
    return row[0] if row else None


def baseline_dir():
    """ReproScore 1.0 (paper version) for the second RRS; empty REPROSCORE_BASELINE_DIR switches it off."""
    value = os.environ.get('REPROSCORE_BASELINE_DIR')
    if value is not None:
        return Path(value) if value else None
    return PROJECT_ROOT / 'third_party' / 'reproscore-1.0'


def tool_version(folder):
    version = Path(folder) / 'VERSION'
    if version.is_file():
        return version.read_text().strip()
    try:
        return subprocess.run(['git', '-C', str(folder), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                              timeout=30).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def score_with(folder, target):
    """RRS of `target` with the ReproScore copy in `folder` and its default rubric (separate process)."""
    folder = Path(folder)
    if not (folder / 'src' / 'scoring' / 'rrs.py').is_file():
        raise FileNotFoundError(f'reproscore not found in {folder}')
    rubric = folder / 'config' / 'default_rubric.yaml'
    done = subprocess.run([sys.executable, str(PROJECT_ROOT / 'src' / 'reproscore_rrs_worker.py'), str(folder),
                           str(target), str(rubric)], capture_output=True, text=True, timeout=1800,
                          env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})
    if done.returncode != 0:
        lines = [line for line in done.stderr.strip().splitlines() if line.strip()]
        raise RuntimeError(lines[-1][:400] if lines else f'worker exit {done.returncode}')
    result = json.loads(done.stdout)
    result.update(tool_commit=tool_version(folder), rubric_sha256=sha256(rubric))
    return result


def record_rrs(connection, run_id, repo_dir):
    """RRS with ReproScore 1.1 (rrs, score_*, evidence) and 1.0 (rrs_v1_0, ...) from one scoring path."""
    values = dict(status='error', error=None, tool='reproscore')
    target, temporary = scoring_path(repo_dir)
    try:
        for suffix, folder in (('', tool_dir()), ('_v1_0', baseline_dir())):
            if folder is None:
                continue
            started = time.monotonic()
            status, error = 'error', None
            try:
                r = score_with(folder, target)
                c = r['categories']
                values.update({f'rrs{suffix}': r['rrs'], f'score_e{suffix}': c.get('E'),
                               f'score_a{suffix}': c.get('A'), f'score_d{suffix}': c.get('D'),
                               f'score_c{suffix}': c.get('C'), f'score_s{suffix}': c.get('S'),
                               f'tool_commit{suffix}': r['tool_commit'],
                               f'penalties{suffix}_json': json.dumps(r['penalties']),
                               f'submetrics{suffix}_json': json.dumps(r['submetrics'])})
                if not suffix:
                    values.update(rubric_name=r['rubric_name'], rubric_sha256=r['rubric_sha256'],
                                  evidence_json=json.dumps(r['evidence'], default=str))
                status = 'ok'
            except FileNotFoundError as exc:
                status, error = 'unavailable', str(exc)
            except Exception as exc:  # noqa: BLE001 - a scoring problem must not fail the run
                error = f'{type(exc).__name__}: {exc}'[:500]
            values.update({f'status{suffix}': status, f'error{suffix}': error,
                           f'seconds{suffix}': round(time.monotonic() - started, 3)})
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)
    if temporary is None and values.get('status') == 'ok':
        values['error'] = f'scored at {target}: the path may affect some sub-metrics (no clean temporary folder)'
    values.update(repository_run_id=run_id, repository_id=repository_id(connection, run_id), created_at=now())
    connection.execute('DELETE FROM resource_readiness WHERE repository_run_id = ?', (run_id,))
    columns = ', '.join(values)
    connection.execute(f'INSERT INTO resource_readiness ({columns}) VALUES ({", ".join("?" for _ in values)})',
                       list(values.values()))
    connection.commit()
    return values


def evidence_from_db(connection, run_id):
    """ExecutionEvidence fields (mapping version 1) from this run's records; None where not measured."""
    environment = connection.execute("SELECT exit_status FROM run_stage_usage WHERE repository_run_id = ? AND "
                                     "stage = 'environment' ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    install = None
    if environment is not None:
        failed = connection.execute("SELECT COUNT(*) FROM run_packages WHERE repository_run_id = ? AND decision = "
                                    "'install' AND status != 'installed'", (run_id,)).fetchone()[0]
        install = environment[0] == 0 and failed == 0
    rows = connection.execute('SELECT execution_status, error_category FROM notebook_executions '
                              'WHERE repository_run_id = ?', (run_id,)).fetchall()
    executed = len(rows)
    scores = [r[0] for r in connection.execute(
        'SELECT score_normalised FROM notebook_reproducibility_metrics WHERE repository_run_id = ? AND '
        'comparison_version = 2 AND cells_assessable > 0 AND score_normalised IS NOT NULL', (run_id,))] \
        if 'notebook_reproducibility_metrics' in tables(connection) else []
    return dict(
        install_success=install,
        notebook_exec_rate=(sum(s == 'SUCCESS' for s, _ in rows) / executed) if executed else None,
        import_success_rate=(sum(c != 'DEPENDENCY_ERROR' for _, c in rows) / executed) if executed else None,
        output_reproduction=(sum(scores) / len(scores)) if scores else None,
        execution_success=None, output_determinism=None, test_pass_rate=None), executed, len(scores)


def tables(connection):
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def record_outcome(connection, run_id, modules=None, commit=None):
    values = dict(repository_run_id=run_id, repository_id=repository_id(connection, run_id),
                  mapping_version=MAPPING_VERSION, status='error', error=None, created_at=now())
    try:
        if modules is None:
            modules, commit = load_reproscore()
        fields, executed, compared = evidence_from_db(connection, run_id)
        rubric_path = outcome_rubric_path()
        rubric = modules['rubric'].load_rubric(rubric_path)
        evidence = modules['ros'].ExecutionEvidence(**fields)
        ros = modules['ros'].ROSScorer(rubric=rubric).score(evidence)
        readiness = connection.execute("SELECT rrs, rrs_v1_0 FROM resource_readiness WHERE repository_run_id = ? "
                                       "AND status = 'ok'", (run_id,)).fetchone()
        values.update({k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items()},
                      notebooks_executed=executed, notebooks_compared=compared, tool_commit=commit,
                      rubric_name=rubric.name, rubric_sha256=sha256(rubric_path), ros=ros.ros,
                      ros_components_json=json.dumps(ros.component_scores),
                      coverage_weight_sum=ros.coverage_weight_sum, status='ok')
        # the same evidence with ReproScore's default rubric (no weight for R, so R is ignored):
        # comparable with the ReproScore paper
        default_path = modules['folder'] / 'config' / 'default_rubric.yaml'
        default = modules['rubric'].load_rubric(default_path)
        ros_default = modules['ros'].ROSScorer(rubric=default).score(evidence)
        values.update(ros_default=ros_default.ros, ros_default_components_json=json.dumps(ros_default.component_scores),
                      coverage_weight_sum_default=ros_default.coverage_weight_sum,
                      rubric_default_sha256=sha256(default_path))
        if readiness:
            composite = modules['rcs'].RCSScorer(rubric=rubric).score(rrs=readiness[0], ros=ros.ros,
                                                         coverage_weight_sum=ros.coverage_weight_sum)
            values.update(rrs=composite.rrs, rcs=composite.rcs, alpha=composite.alpha,
                          coverage_level=composite.coverage_level)
            composite = modules['rcs'].RCSScorer(rubric=default).score(
                rrs=readiness[0], ros=ros_default.ros, coverage_weight_sum=ros_default.coverage_weight_sum)
            values.update(rcs_default=composite.rcs, alpha_default=composite.alpha)
            if readiness[1] is not None:            # paper version: RRS 1.0 + default-rubric ROS
                composite = modules['rcs'].RCSScorer(rubric=default).score(
                    rrs=readiness[1], ros=ros_default.ros, coverage_weight_sum=ros_default.coverage_weight_sum)
                values.update(rcs_v1_0=composite.rcs, alpha_v1_0=composite.alpha)
    except FileNotFoundError as error:
        values.update(status='unavailable', error=str(error))
    except Exception as error:  # noqa: BLE001
        values['error'] = f'{type(error).__name__}: {error}'[:500]
    connection.execute('DELETE FROM reproscore_outcome WHERE repository_run_id = ?', (run_id,))
    connection.execute(f'INSERT INTO reproscore_outcome ({", ".join(values)}) VALUES '
                       f'({", ".join("?" for _ in values)})', list(values.values()))
    connection.commit()
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    rrs = commands.add_parser('rrs')
    rrs.add_argument('--run-id', type=int, required=True)
    rrs.add_argument('--repo-dir', type=Path, required=True)
    outcome = commands.add_parser('outcome')
    outcome.add_argument('--run-id', type=int)
    outcome.add_argument('--all', action='store_true', help='recompute every run (default: runs without an outcome)')
    args = parser.parse_args(argv)
    connection = connect(args.db)
    if args.command == 'rrs':
        values = record_rrs(connection, args.run_id, args.repo_dir)
        print(json.dumps({k: values.get(k) for k in ('status', 'rrs', 'score_e', 'score_a', 'score_d', 'score_c',
                                                     'score_s', 'seconds', 'error', 'status_v1_0', 'rrs_v1_0',
                                                     'error_v1_0')}))
        return 0
    if args.run_id is not None:
        runs = [args.run_id]
    else:
        runs = [r[0] for r in connection.execute(
            'SELECT id FROM repository_runs' + ('' if args.all else
                                                ' WHERE id NOT IN (SELECT repository_run_id FROM reproscore_outcome)'))]
    try:
        modules, commit = load_reproscore()
    except FileNotFoundError:
        modules, commit = None, None
    for run_id in runs:
        values = record_outcome(connection, run_id, modules, commit)
        if args.run_id is not None:
            print(json.dumps({k: values.get(k) for k in ('status', 'ros', 'rrs', 'rcs', 'alpha', 'coverage_level',
                                                         'ros_default', 'rcs_default', 'rcs_v1_0',
                                                         'install_success', 'notebook_exec_rate',
                                                         'import_success_rate', 'output_reproduction', 'error')}))
    if args.run_id is None:
        print(f'{len(runs)} runs scored')
    return 0


if __name__ == '__main__':
    sys.exit(main())
