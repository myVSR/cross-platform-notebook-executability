"""Read-only view of the full-pipeline results for the website (releases, repository and notebook detail).

A *release* is one pipeline database:
  thesis-2026   data/output/db/db.sqlite (frozen thesis database; shown as before by workspace_data.py)
  study-2026    $NBF_STUDY_DB, else results/study/merged.sqlite (the merged the cluster study database)
  more          NBF_WEB_RELEASES="name=path;name=path" (e.g. a the cluster test database during development)
The default release is $NBF_WEB_DEFAULT_RELEASE, else thesis-2026.

Website ids carry the release: thesis rows keep "db-<id>" (existing links), other releases use
"<release>.<id>", so a detail page can always find its database. Every table beyond the seven thesis tables
is optional: a database without it simply has no such section.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THESIS = 'thesis-2026'
NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')


def releases():
    found = {THESIS: dict(label='Thesis 2026 (frozen)', path=ROOT / 'data/output/db/db.sqlite', frozen=True),
             'study-2026': dict(label='Cross-platform study 2026',
                                path=Path(os.environ.get('NBF_STUDY_DB') or ROOT / 'results/study/merged.sqlite'),
                                frozen=False)}
    for item in filter(None, (os.environ.get('NBF_WEB_RELEASES') or '').split(';')):
        name, _, path = item.partition('=')
        name = name.strip()
        if NAME.match(name) and path.strip():
            found[name] = dict(label=name, path=Path(path.strip()).expanduser(), frozen=False)
    return found


def default_release():
    wanted = os.environ.get('NBF_WEB_DEFAULT_RELEASE') or THESIS
    return wanted if wanted in releases() else THESIS


def list_releases():
    default = default_release()
    return [dict(name=name, label=info['label'], available=info['path'].is_file(), frozen=info['frozen'],
                 default=name == default) for name, info in releases().items()]


def release_db(name):
    info = releases().get(name)
    if not info:
        raise ValueError(f'Unknown release: {name}')
    if not info['path'].is_file():
        raise FileNotFoundError(f'The database of release {name} is not available.')
    return info['path']


def connect(path):
    connection = sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def tables(connection):
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}


def rows(connection, have, table, sql, *params):
    if table not in have:
        return []
    return [dict(r) for r in connection.execute(sql, params)]


def columns(connection, table):
    return {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}


def parse_id(website_id):
    """'db-12' -> (thesis, 12); 'study-2026.12' -> ('study-2026', 12)."""
    text = str(website_id)
    if text.startswith('db-') and text[3:].isdigit():
        return THESIS, int(text[3:])
    name, _, number = text.rpartition('.')
    if name in releases() and number.isdigit():
        return name, int(number)
    raise ValueError('This record has no pipeline database detail (saved thesis snapshot).')


def website_id(release, number):
    return f'db-{number}' if release == THESIS else f'{release}.{number}'


def number(value):
    try:
        return None if value in (None, '') else float(value)
    except (TypeError, ValueError):
        return None


def flag(value):
    return None if value in (None, '') else str(value).lower() in ('1', 'true', 'yes', 'ok')


def loads(value, default=None):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def version_text(label, version_id):
    """'main @ aca6305' for a commit, else the record version label or id."""
    if version_id and re.fullmatch(r'[0-9a-f]{40}', str(version_id)):
        return f'{label} @ {version_id[:7]}' if label else str(version_id)[:7]
    return label or (str(version_id) if version_id else None)


def licence_files(row):
    """Matched licence files as display strings: 'LICENSE (AGPL-3.0, exact 100%)'."""
    out = []
    for item in loads(row.get('matched_files_json'), []) or loads(row.get('licence_files_json'), []) or []:
        if isinstance(item, dict):
            name = item.get('filename') or item.get('path') or '?'
            details = [str(item[k]) for k in ('licence', 'matcher') if item.get(k)]
            if item.get('confidence') is not None:
                details.append(f"{round(float(item['confidence']))}%")
            out.append(f"{name} ({', '.join(details)})" if details else name)
        else:
            out.append(str(item))
    return out


# --- workspace summaries -----------------------------------------------------------------------------
def summaries(connection):
    """Per notebook (latest execution) and per repository (latest run) values of the new pipeline tables."""
    have = tables(connection)
    notebooks, repositories = {}, {}
    if 'notebook_reproducibility_metrics' in have and 'score_normalised' in columns(connection, 'notebook_reproducibility_metrics'):
        for m in connection.execute(
                'SELECT m.notebook_id, m.score_strict, m.score_normalised, m.cells_assessable, e.error_category '
                'FROM notebook_reproducibility_metrics m JOIN notebook_executions e ON e.id = m.notebook_execution_id '
                'WHERE e.id IN (SELECT MAX(id) FROM notebook_executions GROUP BY notebook_id)'):
            notebooks[m['notebook_id']] = dict(scoreStrict=number(m['score_strict']),
                                               scoreNormalised=number(m['score_normalised']),
                                               cellsAssessable=number(m['cells_assessable']),
                                               errorCategory=m['error_category'] or None)
    elif 'notebook_executions' in have and 'error_category' in columns(connection, 'notebook_executions'):
        for e in connection.execute('SELECT notebook_id, error_category FROM notebook_executions '
                                    'WHERE id IN (SELECT MAX(id) FROM notebook_executions GROUP BY notebook_id)'):
            notebooks[e['notebook_id']] = dict(errorCategory=e['error_category'] or None)
    latest = {r['repository_id']: r['id'] for r in connection.execute(
        'SELECT repository_id, MAX(id) AS id FROM repository_runs GROUP BY repository_id')}
    by_run = {run: repo for repo, run in latest.items()}
    for table, fields in (('resource_readiness', ('rrs', 'rrs_v1_0')), ('reproscore_outcome', ('ros', 'rcs')),
                          ('resource_licences', ('licence_file',)), ('resource_acquisitions', ('version_label', 'version_id'))):
        if table not in have:
            continue
        present = [f for f in fields if f in columns(connection, table)]
        for r in connection.execute(f'SELECT repository_run_id, {", ".join(present)} FROM {table} ORDER BY id'):
            repo = by_run.get(r['repository_run_id'])
            if repo is not None:
                entry = repositories.setdefault(repo, {})
                for f in present:
                    entry[f] = r[f]
    return notebooks, {repo: dict(rrs=number(v.get('rrs')), rrsV10=number(v.get('rrs_v1_0')), ros=number(v.get('ros')),
                                  rcs=number(v.get('rcs')), licenceDetected=v.get('licence_file') or None,
                                  version=version_text(v.get('version_label'), v.get('version_id')))
                       for repo, v in repositories.items()}


def add_summaries(repositories, connection, release):
    """Attach summaries() to the workspace rows built by workspace_data._normalise (ids already prefixed)."""
    notebooks, repos = summaries(connection)
    prefix = 'db-' if release == THESIS else f'{release}.'
    for repo in repositories:
        if not str(repo['id']).startswith(prefix):
            continue
        repo['release'] = release
        repo.update(repos.get(int(str(repo['id'])[len(prefix):]), {}))
        for notebook in repo['notebooks']:
            notebook.update(notebooks.get(int(str(notebook['id'])[len(prefix):]), {}))
    return repositories


# --- repository detail -------------------------------------------------------------------------------
def repository_detail(website_repository_id):
    release, repo_id = parse_id(website_repository_id)
    with connect(release_db(release)) as connection:
        have = tables(connection)
        if not connection.execute('SELECT 1 FROM repositories WHERE id = ?', (repo_id,)).fetchone():
            raise LookupError('Repository not found in this release.')
        runs = rows(connection, have, 'repository_runs', 'SELECT * FROM repository_runs WHERE repository_id = ? ORDER BY id DESC', repo_id)
        ids = [r['id'] for r in runs]

        def per_run(table, order='id'):
            if table not in have or not ids:
                return {}
            out = {}
            marks = ','.join('?' * len(ids))
            for r in connection.execute(f'SELECT * FROM {table} WHERE repository_run_id IN ({marks}) ORDER BY {order}', ids):
                out.setdefault(r['repository_run_id'], []).append(dict(r))
            return out

        sessions = {r['id']: r for r in rows(connection, have, 'pipeline_sessions', 'SELECT * FROM pipeline_sessions')}
        acquisitions, environments = per_run('resource_acquisitions'), per_run('run_environments')
        packages, sources = per_run('run_packages', 'name'), per_run('dependency_sources', 'path')
        licences, readiness, outcomes = per_run('resource_licences'), per_run('resource_readiness'), per_run('reproscore_outcome')
        snapshots, stages, artifacts = per_run('metadata_snapshots'), per_run('run_stage_usage'), per_run('run_artifacts')
        out_runs = []
        for run in runs:
            rid = run['id']
            session = sessions.get(run.get('session_id')) or {}
            acq = (acquisitions.get(rid) or [None])[-1]
            env = (environments.get(rid) or [None])[-1]
            lic = (licences.get(rid) or [None])[-1]
            ready = (readiness.get(rid) or [None])[-1]
            outcome = (outcomes.get(rid) or [None])[-1]
            arts = artifacts.get(rid) or []
            out_runs.append(dict(
                id=str(rid), status=run['run_status'], error=run.get('error_message') or None,
                startedAt=run.get('started_at'), finishedAt=run.get('finished_at'),
                durationSeconds=number(run.get('duration_seconds')),
                session=session and dict(hostname=session.get('hostname'), os=session.get('os'),
                                         cpu=session.get('cpu_model'), logicalCpus=session.get('logical_cpus'),
                                         memoryBytes=number(session.get('memory_bytes')),
                                         python=session.get('python_version'), pipelineCommit=session.get('pipeline_commit'),
                                         pipelineDirty=flag(session.get('pipeline_dirty'))) or None,
                acquisition=acq and dict(method=acq.get('method'), sourceUrl=acq.get('source_url'),
                                         versionId=acq.get('version_id'), versionLabel=acq.get('version_label'),
                                         versionDate=acq.get('version_date'), recordRevision=acq.get('record_revision'),
                                         filesTotal=number(acq.get('files_total')), filesDownloaded=number(acq.get('files_downloaded')),
                                         filesSkipped=number(acq.get('files_skipped')),
                                         bytesTransferred=number(acq.get('bytes_transferred')),
                                         bytesOnDisk=number(acq.get('bytes_on_disk')), wallSeconds=number(acq.get('wall_seconds')),
                                         acquiredAt=acq.get('acquired_at')) or None,
                environment=env and dict(pythonRequested=env.get('python_requested'), pythonSource=env.get('python_source'),
                                         pythonSelected=env.get('python_selected'), pythonUsed=env.get('python_used'),
                                         kernelName=env.get('kernel_name'), kernelStatus=env.get('kernel_status'),
                                         kernelPythonVersion=env.get('kernel_python_version'),
                                         ipykernelVersion=env.get('ipykernel_version'), kernelError=env.get('kernel_error'),
                                         kernelFallback=env.get('kernel_fallback'), pipVersion=env.get('pip_version'),
                                         pipCheckOk=flag(env.get('pip_check_ok')), pipCheck=(env.get('pip_check') or '')[:4000] or None,
                                         packagesUploadedBefore=env.get('pip_uploaded_prior_to'),
                                         packageCutoffReason=env.get('pip_cutoff_reason'),
                                         installSummary=loads(env.get('install_summary_json'))) or None,
                packages=[dict(name=p['name'], spec=p.get('spec'), origin=p.get('origin'), originKind=p.get('origin_kind'),
                               decision=p.get('decision'), reason=p.get('reason'), method=p.get('method'),
                               status=p.get('status'), installedVersion=p.get('installed_version'),
                               error=(p.get('error') or '')[:600] or None) for p in packages.get(rid, [])],
                dependencySources=[dict(path=s['path'], kind=s.get('kind'), used=flag(s.get('used')), reason=s.get('reason'),
                                        pinned=number(s.get('pinned'))) for s in sources.get(rid, [])],
                licence=lic and dict(platform=lic.get('licence_api') or None, detected=lic.get('licence_file') or None,
                                     detector=lic.get('detector'), status=lic.get('status'), error=lic.get('error'),
                                     files=licence_files(lic)) or None,
                readiness=ready and dict(
                    status=ready.get('status'), error=ready.get('error'), rubric=ready.get('rubric_name'),
                    versions=[dict(version=label, rrs=number(ready.get(f'rrs{s}')),
                                   dimensions={k: number(ready.get(f'score_{k}{s}')) for k in 'eadcs'},
                                   toolCommit=ready.get(f'tool_commit{s}'),
                                   penalties=loads(ready.get(f'penalties{s}_json'), []))
                              for s, label in (('', '1.1'), ('_v1_0', '1.0')) if ready.get(f'rrs{s}') is not None]) or None,
                outcome=outcome and dict(
                    status=outcome.get('status'), error=outcome.get('error'), mappingVersion=outcome.get('mapping_version'),
                    rubric=outcome.get('rubric_name'), ros=number(outcome.get('ros')), rcs=number(outcome.get('rcs')),
                    rosDefault=number(outcome.get('ros_default')), rcsDefault=number(outcome.get('rcs_default')),
                    rcsV10=number(outcome.get('rcs_v1_0')), rrs=number(outcome.get('rrs')), alpha=number(outcome.get('alpha')),
                    coverageLevel=outcome.get('coverage_level'),
                    components=dict(installSuccess=number(outcome.get('install_success')),
                                    executionSuccess=number(outcome.get('execution_success')),
                                    outputDeterminism=number(outcome.get('output_determinism')),
                                    notebookExecutionRate=number(outcome.get('notebook_exec_rate')),
                                    importSuccessRate=number(outcome.get('import_success_rate')),
                                    testPassRate=number(outcome.get('test_pass_rate')),
                                    outputReproduction=number(outcome.get('output_reproduction'))),
                    notebooksExecuted=number(outcome.get('notebooks_executed')),
                    notebooksCompared=number(outcome.get('notebooks_compared'))) or None,
                snapshots=[dict(platform=s.get('platform'), purpose=s.get('purpose'), url=s.get('url'),
                                requestedAt=s.get('requested_at'), respondedAt=s.get('responded_at'),
                                httpStatus=s.get('http_status'), attempts=s.get('attempts'), tokenUsed=flag(s.get('token_used')),
                                bytes=number(s.get('response_bytes')), sha256=s.get('response_sha256'), tool=s.get('tool'))
                           for s in snapshots.get(rid, [])],
                stages=[dict(stage=s['stage'], startedAt=s.get('started_at'), wallSeconds=number(s.get('wall_seconds')),
                             cpuSeconds=(number(s.get('cpu_user_seconds')) or 0) + (number(s.get('cpu_sys_seconds')) or 0),
                             energyJoules=number(s.get('energy_pkg_joules')), diskBytes=number(s.get('disk_bytes')),
                             exitStatus=s.get('exit_status')) for s in stages.get(rid, [])],
                artifacts=dict(count=len(arts), bytes=sum(number(a.get('bytes')) or 0 for a in arts),
                               storedBytes=sum(number(a.get('stored_bytes')) or 0 for a in arts)) if arts else None,
            ))
    return dict(release=release, repositoryId=website_repository_id, runs=out_runs,
                sections=sorted(t for t in have if t in {
                    'resource_acquisitions', 'run_environments', 'run_packages', 'dependency_sources', 'resource_licences',
                    'resource_readiness', 'reproscore_outcome', 'metadata_snapshots', 'run_stage_usage', 'run_artifacts'}))


# --- notebook detail ---------------------------------------------------------------------------------
def notebook_detail(website_notebook_id):
    release, notebook_id = parse_id(website_notebook_id)
    with connect(release_db(release)) as connection:
        have = tables(connection)
        executions = rows(connection, have, 'notebook_executions',
                          'SELECT e.*, r.started_at AS run_started_at, r.run_status FROM notebook_executions e '
                          'LEFT JOIN repository_runs r ON r.id = e.repository_run_id WHERE e.notebook_id = ? ORDER BY e.id DESC',
                          notebook_id)
        metrics = {m['notebook_execution_id']: m for m in rows(
            connection, have, 'notebook_reproducibility_metrics',
            'SELECT * FROM notebook_reproducibility_metrics WHERE notebook_id = ? ORDER BY id', notebook_id)}
        cells = {}
        for c in rows(connection, have, 'notebook_cell_comparisons',
                      'SELECT * FROM notebook_cell_comparisons WHERE notebook_id = ? ORDER BY cell_index', notebook_id):
            cells.setdefault(c['notebook_execution_id'], []).append(c)
        usage = {u['repository_run_id']: u for u in rows(
            connection, have, 'notebook_resource_usage',
            'SELECT * FROM notebook_resource_usage WHERE notebook_id = ? ORDER BY id', notebook_id)}
        imports = {}
        for i in rows(connection, have, 'notebook_imports', 'SELECT * FROM notebook_imports WHERE notebook_id = ? ORDER BY module',
                      notebook_id):
            imports.setdefault(i['repository_run_id'], []).append(i)
        out = []
        for e in executions:
            m = metrics.get(e['id'])
            u = usage.get(e['repository_run_id'])
            out.append(dict(
                id=str(e['id']), runId=str(e['repository_run_id']), runStartedAt=e.get('run_started_at'),
                runStatus=e.get('run_status'), status=e.get('execution_status'), durationSeconds=number(e.get('execution_duration')),
                totalCodeCells=number(e.get('total_code_cells')), executedCells=number(e.get('executed_cells')),
                error=dict(type=e.get('error_type'), message=e.get('error_message'), cellIndex=e.get('error_cell_index'),
                           count=e.get('error_count'), category=e.get('error_category'),
                           categoryByType=e.get('error_category_by_type'), evidence=e.get('error_evidence'),
                           dependencyCause=e.get('dependency_cause'))
                if e.get('error_type') or e.get('error_message') or e.get('error_category') else None,
                metrics=m and dict(
                    comparisonVersion=m.get('comparison_version'), thesisScore=number(m.get('reproducibility_score')),
                    strict=number(m.get('score_strict')), normalised=number(m.get('score_normalised')),
                    textData=number(m.get('score_text_data')), beforeError=number(m.get('score_before_error')),
                    counts={k: number(m.get(c)) for k, c in (
                        ('assessable', 'cells_assessable'), ('identical', 'cells_identical'),
                        ('normalised', 'cells_reproduced_normalised'), ('imageSimilar', 'cells_image_similar'),
                        ('imageOnly', 'cells_image_only'), ('different', 'cells_different'), ('error', 'cells_error'),
                        ('missing', 'cells_missing'), ('empty', 'cells_empty'), ('notStored', 'cells_not_stored'))}) or None,
                cells=[dict(index=c['cell_index'], outcome=c.get('outcome'), stored=c.get('stored_outputs'),
                            executed=c.get('executed_outputs'), errorName=c.get('error_name'),
                            sourceChanged=flag(c.get('source_changed')), notes=loads(c.get('notes_json'), []))
                       for c in cells.get(e['id'], [])],
                usage=u and dict(wallSeconds=number(u.get('wall_seconds')),
                                 cpuSeconds=(number(u.get('cpu_user_seconds')) or 0) + (number(u.get('cpu_sys_seconds')) or 0),
                                 maxRssKb=number(u.get('max_rss_kb')), energyJoules=number(u.get('energy_pkg_joules'))) or None,
                imports=[dict(module=i['module'], distribution=i.get('distribution'), classification=i.get('classification'),
                              importable=flag(i.get('importable')), source=i.get('mapping_source'))
                         for i in imports.get(e['repository_run_id'], [])],
            ))
    return dict(release=release, notebookId=website_notebook_id, executions=out)
