#!/usr/bin/env python3
"""Top up the cohort from the reserve after a run pass (notes/sampling-design.md, section 6.4, D9).

Reads the draw (data/sampling/draw/<date>/: draw.csv, cohort-primary.csv, cohort-reserve.csv) and a
(merged) run database, classifies every drawn resource that was used so far, and writes the next
cohort with replacements for the platforms that are below the target of analysable resources.

Outcome per drawn resource (latest run of the repository in the database):
  analysable          acquired, at least one Python notebook (whatever happened afterwards:
                      environment or execution failures are results, Y_r = 0)
  not_obtainable      acquisition failed (clone/download/metadata/invalid record) -> replaced
  ineligible          acquired but no notebook or no Python notebook (D1)    -> replaced
  pipeline_error      the pipeline itself failed (tool environment, kernel start, classification,
                      dependency analysis, interrupted by a time limit or crash, metadata lookup)
                      -> NOT replaced: listed for a re-run (cohort-rerun-<n>.csv)
  not_run             in the cohort but no run in the database (e.g. a task that gave up at its time
                      limit) -> listed for a re-run as well; run this script only after the passes ended
Replacements are taken from the reserve in its seeded processing order, preferring a reserve resource
from the same stratum and creation year as the resource it replaces (Zenodo: same Z1/Z2/Z3 stratum;
then same stratum; then any of the platform); GitHub (self-weighting) takes the next in order.
Every decision is logged (topup-log.csv, appended), and reserve resources already used by an earlier
top-up are never used twice.

Usage:
  python3 pipeline/topup_from_reserve.py --draw-dir data/sampling/draw/2026-10-20 --db merged.sqlite [--target 400]
Output in the draw folder: cohort-topup-<n>.csv (category,platform,url; for run_array.sbatch via NBF_COHORT),
cohort-rerun-<n>.csv, topup-log.csv, topup-<n>-summary.json.
Never use the frozen evaluation database (data/output/db/db.sqlite): the script refuses it.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN = (ROOT / 'data' / 'output' / 'db' / 'db.sqlite').resolve()
NOT_OBTAINABLE = {'GIT_CLONE_FAILED', 'ZENODO_DOWNLOAD_FAILED', 'REPO_DIR_MISSING',
                  'INVALID_REPOSITORY_URL', 'INVALID_ZENODO_RECORD', 'UNSUPPORTED_PLATFORM'}
INELIGIBLE = {'NO_NOTEBOOKS', 'NO_PYTHON_NOTEBOOKS'}
# failures of the pipeline or the infrastructure, not outcomes of the resource: re-run (at most --max-reruns).
# METADATA_FETCH_FAILED comes after the resource was validated as reachable (git ls-remote / Zenodo record),
# so it is a transient API failure, not a missing resource.
PIPELINE_ERROR = {'TOOL_ENV_FAIL', 'KERNEL_START_FAIL', 'CLASSIFICATION_FAILED', 'DEPENDENCY_ANALYSIS_FAIL',
                  'RUNNING', 'INTERRUPTED', 'PIPELINE_INTERRUPTED', 'METADATA_FETCH_FAILED', 'REPO_DIR_NOT_CLEAN',
                  'PYTHON_BINARY_MISSING', 'VENV_CREATE_FAIL', 'NO_NOTEBOOK_PATHS', 'DEFERRED_LONG_REPO',
                  'INSTALL_NETWORK_FAIL', 'VALIDATION_UNREACHABLE', 'DISK_FULL', 'COMPARISON_FAILED'}
LOG_FIELDS = ['topup', 'logged_at', 'platform', 'stratum', 'year', 'url', 'role', 'outcome', 'run_status',
              'action', 'replaced_by', 'match']


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def key(platform, url):
    """(platform, repository name as stored by the pipeline), lower case."""
    url = url.strip().rstrip('/').removesuffix('.git')
    if platform == 'zenodo':
        return platform, url.rsplit('/', 1)[-1].lower()
    return platform, '/'.join(url.split('/')[-2:]).lower()


def read_csv(path):
    with open(path, newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def run_outcomes(db):
    """{(platform, repository): (outcome, run_status)} from the latest run of every repository."""
    connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    rows = connection.execute(
        'SELECT r.platform, r.repository, rr.run_status, '
        '  (SELECT COUNT(*) FROM notebooks n WHERE n.repository_id = r.id), '
        "  (SELECT COUNT(*) FROM notebooks n WHERE n.repository_id = r.id AND COALESCE(LOWER(n.language), 'python') LIKE 'python%') "
        'FROM repository_runs rr JOIN repositories r ON r.id = rr.repository_id '
        'WHERE rr.id = (SELECT MAX(id) FROM repository_runs WHERE repository_id = r.id)').fetchall()
    outcomes = {}
    for platform, repository, status, notebooks, python in rows:
        if status in NOT_OBTAINABLE:
            outcome = 'not_obtainable'
        elif status in PIPELINE_ERROR:                 # before the notebook test: an interrupted run is not a result
            outcome = 'pipeline_error'
        elif status in INELIGIBLE or (notebooks and not python):
            outcome = 'ineligible'
        elif python:
            outcome = 'analysable'
        else:
            outcome = 'ineligible'                 # acquired, no notebooks registered
        outcomes[(platform.lower(), str(repository).lower())] = (outcome, status)
    return outcomes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--draw-dir', type=Path, required=True)
    parser.add_argument('--db', type=Path, required=True, help='(merged) run database, read only')
    parser.add_argument('--target', type=int, default=400, help='analysable resources per platform')
    parser.add_argument('--max-reruns', type=int, default=2,
                        help='re-runs per resource for pipeline errors (analysis plan P4); then reported as is')
    args = parser.parse_args(argv)
    if args.db.resolve() == FROZEN:
        sys.exit('refusing the frozen evaluation database; use the database of the new runs')

    draw = read_csv(args.draw_dir / 'draw.csv')
    log_path = args.draw_dir / 'topup-log.csv'
    log = read_csv(log_path) if log_path.exists() else []
    topup = 1 + max([int(r['topup']) for r in log] or [0])
    used_reserve = {r['replaced_by'] for r in log if r['replaced_by']}
    outcomes = run_outcomes(args.db)

    # resources in the cohort so far: the primary sample plus every reserve resource already used
    in_cohort = [r for r in draw if r['role'] == 'primary' or (r['role'] == 'reserve' and r['url'] in used_reserve)]
    reserve = sorted((r for r in draw if r['role'] == 'reserve' and r['url'] not in used_reserve),
                     key=lambda r: int(r['processing_order'] or 0))
    status = {}
    for row in in_cohort:
        status[row['url']] = outcomes.get(key(row['platform'], row['url']), ('not_run', ''))

    entries, new_cohort, rerun, summary = [], [], [], dict(topup=topup, created_at=now(), target=args.target,
                                                           db=str(args.db), platforms={})
    by_platform = defaultdict(list)
    for row in in_cohort:
        by_platform[row['platform']].append(row)
    for platform, rows in sorted(by_platform.items()):
        counts = Counter(status[r['url']][0] for r in rows)
        shortfall = max(0, args.target - counts['analysable'] - counts['not_run'] - counts['pipeline_error'])
        lost = [r for r in rows if status[r['url']][0] in ('not_obtainable', 'ineligible')]
        already_replaced = {r['url'] for r in log if r['action'] == 'replaced'}
        to_replace = [r for r in lost if r['url'] not in already_replaced][:shortfall]
        pool = [r for r in reserve if r['platform'] == platform]
        for row in to_replace:
            def pick(match):
                for candidate in pool:
                    if platform == 'github' or match(candidate):
                        return candidate
                return None
            levels = [('same stratum and year', lambda c: c['stratum'] == row['stratum'] and c['year'] == row['year']),
                      ('same stratum', lambda c: c['stratum'] == row['stratum']), ('platform', lambda c: True)]
            chosen, how = None, None
            for label, match in levels:
                chosen = pick(match)
                if chosen:
                    how = 'next in order' if platform == 'github' else label
                    break
            outcome, run_status = status[row['url']]
            entries.append(dict(topup=topup, logged_at=now(), platform=platform, stratum=row['stratum'],
                                year=row['year'], url=row['url'], role=row['role'], outcome=outcome,
                                run_status=run_status, action='replaced' if chosen else 'reserve exhausted',
                                replaced_by=chosen['url'] if chosen else '', match=how or ''))
            if chosen:
                pool.remove(chosen)
                reserve.remove(chosen)
                new_cohort.append(chosen)
        reruns_so_far = Counter(r['url'] for r in log if r['action'] == 're-run')
        for row in rows:
            if status[row['url']][0] in ('pipeline_error', 'not_run'):
                if reruns_so_far[row['url']] >= args.max_reruns:
                    continue                    # reported with its last status (analysis plan P4)
                rerun.append(row)
                entries.append(dict(topup=topup, logged_at=now(), platform=platform, stratum=row['stratum'],
                                    year=row['year'], url=row['url'], role=row['role'],
                                    outcome=status[row['url']][0], run_status=status[row['url']][1],
                                    action='re-run', replaced_by='', match=''))
        summary['platforms'][platform] = dict(
            in_cohort=len(rows), outcomes=dict(counts), shortfall=shortfall,
            replaced=sum(e['action'] == 'replaced' and e['platform'] == platform for e in entries),
            reserve_left=len([r for r in reserve if r['platform'] == platform]),
            reached=counts['analysable'] >= args.target)

    def write(path, rows):
        with open(path, 'w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=['category', 'platform', 'url'])
            writer.writeheader()
            writer.writerows(dict(category='unclassified', platform=r['platform'], url=r['url']) for r in rows)

    if new_cohort:
        write(args.draw_dir / f'cohort-topup-{topup}.csv', new_cohort)
    if rerun:
        write(args.draw_dir / f'cohort-rerun-{topup}.csv', rerun)
    if entries:
        new_file = not log_path.exists()
        with open(log_path, 'a', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=LOG_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerows(entries)
    summary.update(topup_cohort=f'cohort-topup-{topup}.csv' if new_cohort else None,
                   rerun_cohort=f'cohort-rerun-{topup}.csv' if rerun else None,
                   replacements=len(new_cohort), reruns=len(rerun))
    (args.draw_dir / f'topup-{topup}-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
