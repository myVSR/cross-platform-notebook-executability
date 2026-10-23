"""Build a separate website graph from the current unified workspace snapshot.

This never rewrites the thesis CSV files or the thesis's materialised graph.
"""
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data/output/kg'
STATE = OUTPUT / 'workspace-build-state.json'
_lock = threading.Lock()
_running = False


def now(): return datetime.now(timezone.utc).isoformat()


def _save(value):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(value), encoding='utf-8')
    temp.replace(STATE)


def status():
    with _lock:
        try: value = json.loads(STATE.read_text(encoding='utf-8'))
        except (OSError, ValueError): return {'status': 'idle', 'message': 'Ready to rebuild from the current workspace.'}
        if value.get('status') == 'running' and not _running:
            value.update(status='failed', message='The graph build was interrupted. Please rebuild again.')
        return value


def start(payload):
    global _running
    if not isinstance(payload, dict) or not isinstance(payload.get('repositories'), list) or not isinstance(payload.get('notebooks'), list):
        raise ValueError('A complete workspace snapshot is required.')
    if len(payload['repositories']) > 20000 or len(payload['notebooks']) > 100000:
        raise ValueError('This workspace is too large for the local graph builder.')
    from rdflib import Graph  # Fail early if graph support is not installed.
    with _lock:
        if _running: return {'status': 'running', 'message': 'A graph build is already running.'}
        value = {'status': 'running', 'message': 'Building connections from saved workspace records…', 'startedAt': now()}
        _save(value); _running = True
    threading.Thread(target=_build, args=(payload,), daemon=True).start()
    return value


def _build(payload):
    global _running
    try:
        from rdflib import Graph, Namespace, RDF, RDFS, Literal, URIRef
        graph = Graph(); nf = Namespace('https://w3id.org/notebookfair#'); dct = Namespace('http://purl.org/dc/terms/'); schema = Namespace('https://schema.org/')
        base = 'https://w3id.org/notebookfair/workspace/'
        def entity(kind, key, name):
            uri = URIRef(base + kind + '/' + quote(str(key), safe=''))
            graph.add((uri, RDF.type, nf[kind])); graph.add((uri, RDFS.label, Literal(str(name))))
            return uri
        def prop(uri, predicate, value):
            if value is not None and value != '': graph.add((uri, predicate, Literal(value)))
        repos = {}
        for row in payload['repositories']:
            repo = entity('RepositoryResource', row['id'], row['name']); repos[row['id']] = repo
            platform = entity('RepositoryPlatform', row['platform'], row['platform'])
            graph.add((repo, nf.hostedOnPlatform, platform))
            prop(repo, schema.url, row.get('url')); prop(repo, dct.description, row.get('description')); prop(repo, dct.license, row.get('license')); prop(repo, dct.identifier, row.get('doi'))
            prop(repo, dct.source, row.get('source'))
            for author in row.get('authors', []): prop(repo, dct.creator, author)
            for keyword in row.get('keywords', []): prop(repo, dct.subject, keyword)
            for index, recorded in enumerate(row.get('runs', [])):
                if recorded['status'] == 'Imported': continue
                run = entity('RepositoryRun', f"{row['id']}-{index}", f"{recorded['date']} · {recorded['status']}")
                graph.add((repo, nf.hasRun, run)); graph.add((run, nf.runOfRepository, repo))
                prop(run, nf.runStatus, recorded['status']); prop(run, dct.description, recorded.get('note')); prop(run, dct.date, recorded.get('date'))
        for row in payload['notebooks']:
            repo = repos.get(row['repositoryId'])
            if repo is None: raise ValueError('A notebook references a missing repository.')
            notebook = entity('NotebookResource', row['id'], row['title'])
            graph.add((repo, nf.containsNotebook, notebook)); graph.add((notebook, nf.isNotebookOf, repo))
            prop(notebook, nf.notebookLanguage, row.get('language')); prop(notebook, dct.identifier, row.get('sourcePath'))
            if row.get('ruleCategory') or row.get('aiCategory') or row.get('reviewedAt'):
                classification = entity('NotebookClassificationActivity', row['id'], f"Classification · {row['title']}")
                graph.add((notebook, nf.hasClassification, classification)); graph.add((classification, nf.classifiedNotebook, notebook))
                for key, predicate in [('ruleCategory', nf.ruleCategory), ('aiCategory', nf.llmCategory), ('finalCategory', nf.finalCategory), ('humanReview', nf.needsHumanReview), ('agreement', nf.classificationAgreementStatus), ('reviewedAt', dct.modified), ('reviewNote', dct.description)]: prop(classification, predicate, row.get(key))
            if row.get('executionStatus') in ('Success', 'Partial', 'Failed'):
                execution = entity('NotebookExecution', row['id'], f"Execution · {row['title']}")
                graph.add((execution, nf.executedNotebook, notebook)); prop(execution, nf.executionStatus, row['executionStatus']); prop(execution, dct.description, row.get('executionNote')); prop(execution, dct.date, row.get('updatedAt'))
                if row.get('score') is not None:
                    result = entity('ReproducibilityResult', row['id'], f"{row['score']}% · {row['title']}")
                    graph.add((execution, nf.hasReproducibilityResult, result)); prop(result, nf.reproducibilityScore, row['score'] / 100)
        target = OUTPUT / 'notebookfair-workspace.nt'; temporary = target.with_suffix('.tmp')
        graph.serialize(destination=str(temporary), format='nt'); temporary.replace(target)
        summary = {'built_at': now(), 'data_source': 'workspace', 'total_unique_triples': len(graph), 'pipeline_source': payload.get('pipelineSource'), 'records_loaded_at': payload.get('loadedAt'), 'content_digest': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}
        meta = OUTPUT / 'workspace-build-summary.json'; meta.write_text(json.dumps(summary), encoding='utf-8')
        value = {'status': 'succeeded', 'message': f"Graph updated: {len(graph):,} statements.", 'finishedAt': now()}
    except Exception as error:
        value = {'status': 'failed', 'message': f'The graph could not be rebuilt: {error}', 'finishedAt': now()}
    finally:
        with _lock:
            _save(value); _running = False
