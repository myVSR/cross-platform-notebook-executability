#!/usr/bin/env python3
"""Check a small test run of the 2026-10-11 pipeline fixes (data/cohort-fixcheck.csv) before the main run.

Each resource of the cohort had one problem in round 1 (results/study); the run passes when every fix shows:
  fixcheck-network     packages failed through the cluster proxy ("Tunnel connection failed: 500")
  fixcheck-mirror      the cluster's pip mirror had no upload-time metadata for the time travel
  fixcheck-all-failed  no notebook left an executed copy: recorded as ENV_RUN_FAIL without executions
  fixcheck-archive     the Zenodo archive a notebook opens (risis_dataset.zip) was deleted after extraction
  fixcheck-tar-link    a tar archive with links was refused (pipeline Python 3.11.0 has no tarfile data filter)
  fixcheck-validation  a public record was recorded as INVALID_ZENODO_RECORD (network/rate limit at validation)
and, for every resource that installs packages:
  - the install used PyPI with the machine's pip configuration switched off (install-report.json: index);
  - no package failed for network or index reasons;
  - the time travel is active (except Python 3.8, which pip >= 26 does not support);
  - every task checked the index before starting (task.info: index_check=ok) and logged its free disk space.

Usage (stdlib only, runs on the HPC login node):
  python3 pipeline/check_fixcheck.py results/fixcheck [--baseline results/study/merged.sqlite]
  python3 pipeline/check_fixcheck.py results/study/merged.sqlite --results-root <copy of the HPC results/>   (round 1)
RUN is a results folder with task-*/db.sqlite or a single database. Artefact paths stored by the HPC run are found
under --results-root (default: the folder that contains RUN). Exit status 0 if every check passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COHORT = ROOT / 'data' / 'cohort-fixcheck.csv'
PYPI = 'https://pypi.org/simple'
NETWORK = re.compile(r'Tunnel connection failed|connection broken|ProxyError|Max retries exceeded|Read timed out', re.I)
INDEX = 'does not provide upload-time metadata'


def key(platform, url):
    url = url.strip().rstrip('/').removesuffix('.git')
    return platform, (url.rsplit('/', 1)[-1] if platform == 'zenodo' else '/'.join(url.split('/')[-2:])).lower()


def databases(run: Path):
    return [run] if run.is_file() else sorted(run.glob('task-*/db.sqlite'))


def local(path, results_root: Path):
    """An artefact path as stored on the HPC (…/results/<run>/…) under the local results folder."""
    p = Path(path)
    if p.exists():
        return p
    marker = '/results/'
    return results_root / path.split(marker, 1)[1] if marker in path else p


def latest_runs(run: Path):
    """{(platform, repository): (db path, run id, run status)} of the latest run per resource."""
    found = {}
    for db in databases(run):
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        for platform, repository, run_id, status in con.execute(
                'SELECT LOWER(r.platform), LOWER(r.repository), rr.id, rr.run_status FROM repositories r '
                'JOIN repository_runs rr ON rr.id = (SELECT MAX(id) FROM repository_runs WHERE repository_id = r.id)'):
            found[(platform, repository)] = (db, run_id, status)
        con.close()
    return found


def check_resource(category, db, run_id, status, results_root):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    q = lambda sql, *a: con.execute(sql, a).fetchall()           # noqa: E731
    checks = []

    def check(name, ok, detail=''):
        checks.append((name, bool(ok), detail))

    if category in ('fixcheck-tar-link', 'fixcheck-validation'):
        # obtained now; with or without a Python notebook (both are results)
        check('resource obtained and classified', status in ('SUCCESS', 'NO_NOTEBOOKS', 'NO_PYTHON_NOTEBOOKS'), status)
        if category == 'fixcheck-tar-link':
            links = q("SELECT f.action, COUNT(*) FROM acquisition_files f JOIN resource_acquisitions a ON a.id = f.acquisition_id "
                      "WHERE a.repository_run_id = ? AND f.action LIKE 'link%' OR (a.repository_run_id = ? AND f.reason LIKE "
                      "'%outside the archive%') GROUP BY 1", run_id, run_id)
            check('tar links extracted or recorded', links, ', '.join(f'{a}: {n}' for a, n in links))
        if status != 'SUCCESS':
            con.close()
            return checks, dict(notebooks=0, clean=0, failed_packages=0, python='-')
    else:
        check('run finished as a result (not a pipeline/environment error)', status == 'SUCCESS', status)
    report_path = q("SELECT path FROM run_artifacts WHERE repository_run_id = ? AND kind = 'install' "
                    "AND path LIKE '%install-report.json'", run_id)
    report = None
    if report_path and local(report_path[0][0], results_root).is_file():
        report = json.loads(local(report_path[0][0], results_root).read_text())
    index = (report or {}).get('index') or {}
    check('install used PyPI, machine pip config off', index.get('index_url') == PYPI and index.get('config_file') == '/dev/null',
          f"index={index.get('index_url')} config={index.get('config_file')}" if report else 'no install-report.json found')
    failed = q("SELECT name, error FROM run_packages WHERE repository_run_id = ? AND status = 'failed'", run_id)
    network = [n for n, e in failed if e and NETWORK.search(e)]
    mirror = [n for n, e in failed if e and INDEX in e]
    check('no package failed for network reasons', not network, ', '.join(network[:6]))
    check('no package failed for index (upload-time) reasons', not mirror, ', '.join(mirror[:6]))
    python = (q('SELECT python_used FROM run_environments WHERE repository_run_id = ?', run_id) or [(None,)])[0][0] or ''
    cutoff = (report or {}).get('uploaded_prior_to')
    check('time travel active (not required for Python 3.8)', cutoff or python.startswith('3.8'),
          f'python {python}, cutoff {cutoff}')
    notebooks = q("SELECT COUNT(*) FROM notebooks n JOIN repository_runs rr ON rr.repository_id = n.repository_id "
                  "WHERE rr.id = ? AND COALESCE(LOWER(n.language), 'python') LIKE 'python%'", run_id)[0][0]
    executions = q('SELECT execution_status, error_type, error_message FROM notebook_executions WHERE repository_run_id = ?',
                   run_id)
    if category == 'fixcheck-all-failed':
        check('every notebook has an execution record', notebooks and len(executions) == notebooks,
              f'{len(executions)} of {notebooks}: ' + ', '.join(f'{s}:{t}' for s, t, _ in executions))
    if category == 'fixcheck-archive':
        kept = q("SELECT f.path FROM acquisition_files f JOIN resource_acquisitions a ON a.id = f.acquisition_id "
                 "WHERE a.repository_run_id = ? AND f.origin = 'record file' AND f.reason LIKE '%archive kept%'", run_id)
        check('extracted archive kept next to its contents', kept, ', '.join(p for (p,) in kept))
        missing_zip = [m for _, _, m in executions if m and re.search(r"\.zip'", m)]
        check('no notebook misses an archive of the record', not missing_zip, '; '.join(missing_zip)[:200])
    clean = sum(s == 'SUCCESS' for s, _, _ in executions)
    con.close()
    return checks, dict(notebooks=notebooks, clean=clean, failed_packages=len(failed), python=python)


def baseline_summary(db, resource):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    row = con.execute(
        'SELECT rr.id, rr.run_status FROM repositories r JOIN repository_runs rr ON rr.id = '
        '(SELECT MAX(id) FROM repository_runs WHERE repository_id = r.id) WHERE LOWER(r.platform) = ? AND LOWER(r.repository) = ?',
        resource).fetchone()
    if not row:
        return None
    clean = con.execute("SELECT COUNT(*) FROM notebook_executions WHERE repository_run_id = ? AND execution_status = 'SUCCESS'",
                        (row[0],)).fetchone()[0]
    failed = con.execute("SELECT COUNT(*) FROM run_packages WHERE repository_run_id = ? AND status = 'failed'",
                         (row[0],)).fetchone()[0]
    con.close()
    return dict(status=row[1], clean=clean, failed_packages=failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run', type=Path, help='results/<run> folder (task-*/db.sqlite) or one database')
    parser.add_argument('--baseline', type=Path, help='round-1 database for a before/after comparison')
    parser.add_argument('--results-root', type=Path, help='local copy of the HPC results/ folder (artefacts)')
    parser.add_argument('--cohort', type=Path, default=COHORT)
    args = parser.parse_args(argv)
    results_root = args.results_root or (args.run.parent if args.run.is_dir() else args.run.parent.parent)
    with open(args.cohort, newline='', encoding='utf-8') as stream:
        cohort = list(csv.DictReader(stream))
    runs = latest_runs(args.run)
    all_ok = True
    for row in cohort:
        resource = key(row['platform'], row['url'])
        print(f"\n== {row['category']}: {row['url']}")
        if resource not in runs:
            print('  FAIL  no run of this resource')
            all_ok = False
            continue
        db, run_id, status = runs[resource]
        checks, summary = check_resource(row['category'], db, run_id, status, results_root)
        for name, ok, detail in checks:
            print(f"  {'ok  ' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail and not ok or detail and name.startswith('time') else ''}")
            all_ok &= ok
        before = baseline_summary(args.baseline, resource) if args.baseline else None
        print(f"  now:    {status}, clean {summary['clean']}/{summary['notebooks']} notebooks, "
              f"{summary['failed_packages']} failed packages, Python {summary['python']}")
        if before:
            print(f"  before: {before['status']}, clean {before['clean']} notebooks, {before['failed_packages']} failed packages")
    if args.run.is_dir():
        infos = sorted(args.run.glob('task-*/task.info'))
        texts = [p.read_text() for p in infos]
        index_ok = bool(infos) and all('index_check=ok' in t for t in texts)
        disk = bool(infos) and all('disk=' in t for t in texts)
        print(f"\n== tasks ({len(infos)})\n  {'ok  ' if index_ok else 'FAIL'}  package index reachable at the start of every task"
              f"\n  {'ok  ' if disk else 'FAIL'}  free disk space logged by every task")
        for t in texts:
            for line in t.splitlines():
                if line.startswith(('index_check=', 'disk=')):
                    print('        ' + line)
        all_ok &= index_ok and disk
    print(f"\n{'PASSED: the fixes work; start the main run (bash study.sh autopilot)' if all_ok else 'FAILED: see above'}")
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
