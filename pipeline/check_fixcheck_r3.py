#!/usr/bin/env python3
"""Check the trial run of the round-3 pipeline (data/cohort-fixcheck-r3.csv) before the whole cohort runs again.

The cohort holds the resources that showed the round-2 faults:
  r3-damaged     the 8 resources excluded from round 2 (analysis/paper/infrastructure-failures-r2.csv): full
                 node-local disk, a crashed output comparison (attrs), a notebook name with a leading space,
                 a repository folder named like a notebook
  r3-shell-pip   resources whose notebooks ran `!pip install` against the pipeline's own Python in round 2
                 (notes/round2-shell-pip.csv); five of them failed on a module their own line names
The run passes when, for every resource:
  - the latest run ended as a result of the resource, not as a pipeline error (DISK_FULL, COMPARISON_FAILED, ...);
  - every Python notebook has an execution record;
and, for r3-shell-pip, no executed notebook shows pip working on the pipeline interpreter (its site-packages path,
or "Uninstalling attrs").

Usage (stdlib only, runs on the HPC login node):
  python3 pipeline/check_fixcheck_r3.py results/fixcheck-r3 [--pipeline-python 3.11.0]
Exit status 0 if every check passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COHORT = ROOT / 'data' / 'cohort-fixcheck-r3.csv'
PIPELINE_ERROR = {'TOOL_ENV_FAIL', 'KERNEL_START_FAIL', 'CLASSIFICATION_FAILED', 'DEPENDENCY_ANALYSIS_FAIL',
                  'RUNNING', 'INTERRUPTED', 'PIPELINE_INTERRUPTED', 'METADATA_FETCH_FAILED', 'REPO_DIR_NOT_CLEAN',
                  'PYTHON_BINARY_MISSING', 'VENV_CREATE_FAIL', 'NO_NOTEBOOK_PATHS', 'DEFERRED_LONG_REPO',
                  'INSTALL_NETWORK_FAIL', 'VALIDATION_UNREACHABLE', 'DISK_FULL', 'COMPARISON_FAILED'}


def key(platform, url):
    url = url.strip().rstrip('/').removesuffix('.git')
    return platform, (url.rsplit('/', 1)[-1] if platform == 'zenodo' else '/'.join(url.split('/')[-2:])).lower()


def latest_runs(run: Path):
    found = {}
    for db in sorted(run.glob('task-*/db.sqlite')):
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        for platform, repository, run_id, status in con.execute(
                'SELECT LOWER(r.platform), LOWER(r.repository), rr.id, rr.run_status FROM repositories r '
                'JOIN repository_runs rr ON rr.id = (SELECT MAX(id) FROM repository_runs WHERE repository_id = r.id)'):
            found[(platform, repository)] = (db, run_id, status)
        con.close()
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run', type=Path, help='results folder of the trial run (task-*/db.sqlite)')
    parser.add_argument('--pipeline-python', default='3.11.0')
    parser.add_argument('--cohort', type=Path, default=COHORT)
    args = parser.parse_args(argv)
    minor = '.'.join(args.pipeline_python.split('.')[:2])
    marker = f'versions/{args.pipeline_python}/lib/python{minor}/site-packages'
    runs = latest_runs(args.run)
    with open(args.cohort, newline='', encoding='utf-8') as stream:
        cohort = list(csv.DictReader(stream))
    failed = 0
    for row in cohort:
        name = f"{row['category']:13} {row['platform']:8} {row['url']}"
        found = runs.get(key(row['platform'], row['url']))
        if not found:
            print(f'FAIL  {name}\n        no run in {args.run}')
            failed += 1
            continue
        db, run_id, status = found
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        notebooks = con.execute(
            "SELECT COUNT(*) FROM notebooks n JOIN repository_runs rr ON rr.repository_id = n.repository_id "
            "WHERE rr.id = ? AND COALESCE(LOWER(n.language), 'python') LIKE 'python%'", (run_id,)).fetchone()[0]
        executions = con.execute('SELECT COUNT(DISTINCT notebook_id) FROM notebook_executions WHERE repository_run_id = ?',
                                 (run_id,)).fetchone()[0]
        clean = con.execute("SELECT COUNT(*) FROM notebook_executions WHERE repository_run_id = ? "
                            "AND execution_status = 'SUCCESS'", (run_id,)).fetchone()[0]
        con.close()
        checks = [('result, not a pipeline error', status not in PIPELINE_ERROR, status)]
        if status == 'SUCCESS':
            checks.append(('every Python notebook has an execution record', notebooks and executions >= notebooks,
                           f'{executions} of {notebooks}'))
        else:                       # a resource outcome without execution (e.g. not obtainable): look at it by hand
            print(f'WARN  {name}\n        {status}: no execution; check the run log that it is the resource, not the node')
        if row['category'] == 'r3-shell-pip':
            executed = sorted(db.parent.glob(f'artifacts/run-{run_id}/executed/**/*.ipynb.gz'))
            hits = []
            for path in executed:
                try:
                    text = gzip.open(path, 'rt', encoding='utf-8', errors='replace').read()
                except OSError:
                    continue
                if marker in text or 'Uninstalling attrs' in text:
                    hits.append(path.name)
            checks.append(('no !pip on the pipeline interpreter', bool(executed) and not hits,
                           f'{len(executed)} executed notebooks' + (f'; hits: {", ".join(hits[:4])}' if hits else '')))
        ok = all(c[1] for c in checks)
        failed += not ok
        print(f"{'ok   ' if ok else 'FAIL '} {name}   ({clean} of {notebooks} clean)")
        for label, passed, detail in checks:
            print(f"        {'ok  ' if passed else 'FAIL'} {label}: {detail}")
    print('\nPASSED' if not failed else f'\nFAILED ({failed} of {len(cohort)} resources)')
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
