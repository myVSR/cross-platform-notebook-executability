"""One read-only view of pipeline results, with an explicitly labelled CSV fallback."""
from pathlib import Path
import csv
import re
import sqlite3

import study_data

ROOT = Path(__file__).resolve().parents[1]
TABLES = ('repositories', 'notebooks', 'repository_metadata', 'repository_runs', 'notebook_executions', 'notebook_classifications', 'notebook_reproducibility_metrics')
# The thesis corpus: the evaluation sample and its extension, minus the resources left out for balance.
CORPUS_FILES = ('evaluation-sample-dataset.csv', 'evaluation-sample-extension.csv')
EXCLUSIONS_FILE = 'evaluation-sample-exclusions.csv'


def _read_csv(path):
    with path.open(newline='', encoding='utf-8-sig') as stream:
        return list(csv.DictReader(stream))


def _repository_url(row):
    platform, name = row.get('platform') or 'unknown', row['repository']
    if row.get('repository_url'):
        return row['repository_url']
    if platform == 'zenodo':
        return f'https://zenodo.org/records/{name}'
    if platform in ('github', 'codeberg', 'gitlab'):
        return 'https://' + platform + ('.org/' if platform == 'codeberg' else '.com/') + name
    return ''


def _canonical(url):
    url = re.sub(r'^https?://(www\.)?', '', url.strip().lower()).rstrip('/').removesuffix('.git')
    return url.replace('zenodo.org/record/', 'zenodo.org/records/')


def _thesis_corpus():
    folder = ROOT / 'data'
    urls = {_canonical(row['url']) for name in CORPUS_FILES for row in _read_csv(folder / name)}
    return urls - {_canonical(row['url']) for row in _read_csv(folder / EXCLUSIONS_FILE)}


def _snapshot_tables():
    """Saved thesis data only: the database export also holds smoke tests and resources outside the analysed corpus."""
    folder = ROOT / 'kg/graph/pipeline_data'
    tables = {name: _read_csv(folder / f'{name}.csv') for name in TABLES}
    corpus = _thesis_corpus()
    tables['repositories'] = [row for row in tables['repositories'] if _canonical(_repository_url(row)) in corpus]
    return tables


def _read_tables(connection):
    present = study_data.tables(connection)
    def source(name):
        # the classification view of the full pipeline holds one current row per notebook
        return 'notebook_classifications_current' if name == 'notebook_classifications' and 'notebook_classifications_current' in present else name
    return {name: [dict(row) for row in connection.execute(f'SELECT * FROM {source(name)}')] if source(name) in present else []
            for name in TABLES}


def _read_release(release):
    """A full-pipeline release (study, the cluster test): only its own database, ids '<release>.<id>'."""
    with study_data.connect(study_data.release_db(release)) as connection:
        rows = _normalise(_read_tables(connection), 'pipeline')
        for row in rows:
            row['id'] = study_data.website_id(release, row['id'])
            row['previousIds'] = []
            for notebook in row['notebooks']:
                notebook['id'] = study_data.website_id(release, notebook['id'])
                notebook['previousIds'] = []
        study_data.add_summaries(rows, connection, release)
    return {'repositories': rows, 'source': 'pipeline', 'release': release}


def read_pipeline_workspace(release=None):
    release = release or study_data.default_release()
    if release != study_data.THESIS:
        return _read_release(release)
    saved = _normalise(_snapshot_tables(), 'snapshot')
    db = ROOT / 'data/output/db/db.sqlite'
    if not db.is_file():
        return {'repositories': saved, 'source': 'snapshot', 'release': release}
    with sqlite3.connect(f'{db.resolve().as_uri()}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {name: [dict(row) for row in connection.execute(f'SELECT * FROM {name}')] for name in TABLES}
    current = _normalise(tables, 'pipeline')
    def key(row): return row['url'].rstrip('/').removesuffix('.git').lower()
    old_by_url = {key(row): row for row in saved}
    for row in current:
        previous = old_by_url.get(key(row))
        row['id'] = 'db-' + row['id']
        row['previousIds'] = [previous['id']] if previous else []
        previous_notebooks = {n['name']: n for n in previous['notebooks']} if previous else {}
        for notebook in row['notebooks']:
            old = previous_notebooks.get(notebook['name'])
            notebook['id'] = 'db-' + notebook['id']
            notebook['previousIds'] = [old['id']] if old else []
        if previous:
            dates = {r['startedAt'] for r in row['runs']}
            row['runs'].extend(r for r in previous['runs'] if r['startedAt'] not in dates)
    with sqlite3.connect(f'{db.resolve().as_uri()}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        study_data.add_summaries(current, connection, release)
    live_urls = {key(row) for row in current}
    # A first website run creates a small working database. Keep the saved
    # research records visible, replacing only sources that have newer runs.
    return {'repositories': current + [row for row in saved if key(row) not in live_urls], 'source': 'pipeline', 'release': release}


def _execution_note(execution):
    """The recorded execution error, e.g. "KeyError: 'Adj Close' (cell 12)"; classifier warnings are not execution notes."""
    message, kind = (execution.get('error_message') or '').strip(), (execution.get('error_type') or '').strip()
    if not message and not kind:
        return None
    note = f'{kind}: {message}' if kind and message and not message.startswith(kind) else message or kind
    cell = str(execution.get('error_cell_index') or '').strip()
    return f'{note} (cell {cell})' if cell.isdigit() else note


def _normalise(tables, source):
    def latest(name, key):
        result = {}
        for row in sorted(tables[name], key=lambda r: int(r.get('id') or 0)):
            result[str(row[key])] = row
        return result
    def number(value):
        try:
            return float(value) if value not in ('', None) else None
        except (TypeError, ValueError):
            return None
    def split(value):
        return [part.strip() for part in str(value or '').split(';') if part.strip()]

    metadata = {str(r['repo_id']): r for r in tables['repository_metadata']}
    executions = latest('notebook_executions', 'notebook_id')
    classifications = latest('notebook_classifications', 'notebook_id')
    metrics = latest('notebook_reproducibility_metrics', 'notebook_id')
    notebooks, runs = {}, {}
    for row in sorted(tables['repository_runs'], key=lambda r: int(r['id']), reverse=True):
        runs.setdefault(str(row['repository_id']), []).append({
            'id': str(row['id']), 'status': row['run_status'], 'startedAt': row.get('started_at'),
            'finishedAt': row.get('finished_at'), 'durationSeconds': number(row.get('duration_seconds')),
            'error': row.get('error_message') or None,
        })
    for row in tables['notebooks']:
        key = str(row['id'])
        e, c, m = executions.get(key, {}), classifications.get(key, {}), metrics.get(key, {})
        # A comparison score must belong to the latest execution, not an older successful run.
        score = number(m.get('reproducibility_score')) if str(m.get('notebook_execution_id')) == str(e.get('id')) else None
        notebooks.setdefault(str(row['repository_id']), []).append({
            'id': key, 'name': row['name'], 'language': row.get('language') or 'Unknown',
            'finalCategory': c.get('final_category') or c.get('effective_category') or None,
            'ruleCategory': c.get('rule_category') or None, 'llmCategory': c.get('llm_category') or None,
            'agreement': c.get('agreement_status') or None, 'needsHumanReview': str(c.get('needs_human_review')) == '1',
            'confidence': number(c.get('llm_confidence')) if c.get('llm_confidence') not in ('', None) else number(c.get('rule_confidence')),
            'executionStatus': e.get('execution_status') or None, 'executionDuration': number(e.get('execution_duration')),
            'executionNote': _execution_note(e),
            'reproducibilityScore': score, 'updatedAt': e.get('created_at') or c.get('created_at') or None,
        })
    repositories = []
    for row in tables['repositories']:
        key = str(row['id']); meta = metadata.get(key, {}); history = runs.get(key, []); items = notebooks.get(key, [])
        platform, name = row.get('platform') or 'unknown', row['repository']
        url = _repository_url(row)
        repositories.append({
            'id': key, 'name': name, 'platform': platform, 'url': url, 'title': meta.get('title') or name,
            'description': meta.get('description') or '', 'authors': split(meta.get('authors')), 'license': meta.get('license') or '',
            'doi': meta.get('doi') or None, 'keywords': split(meta.get('keywords')), 'notebookCount': len(items),
            'lastRunStatus': history[0]['status'] if history else None, 'lastRunStartedAt': history[0]['startedAt'] if history else None,
            'lastRunFinishedAt': history[0]['finishedAt'] if history else None, 'runCount': len(history),
            'notebooks': items, 'runs': history, 'source': source,
        })
    return repositories
