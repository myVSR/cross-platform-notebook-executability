#!/usr/bin/env python3
"""Classify notebook purpose after a run, from the artefact store (Step 6).

The re-run deletes every clone, so classification works on what the run stored: the original
notebook (artifacts/run-<id>/original/<path>.gz) and the resource's README (artifacts/run-<id>/
resource/README*.gz). Every notebook of the latest run of each repository is classified with the
hybrid classifier (rules + local LLM through Ollama) in one consistent *pass*: same model, model
digest, prompt version (local_llm.PROMPT_VERSION) and context window (num_ctx). The row stores the
run, the pass, the digest, num_ctx, the prompt token count and whether the prompt filled the window
(truncated), and whether a README was used. A pass can be resumed: notebooks already classified in
it are skipped.

Usage (on the cluster: hpc/classify.sbatch; needs a running Ollama for the LLM):
  python3 pipeline/classify_from_artifacts.py --db results/<run>/merged.sqlite \\
      [--pass NAME] [--model gemma3:4b] [--ollama-url http://127.0.0.1:11434] [--num-ctx 8192] \\
      [--rules-only] [--limit N] [--path-map OLD=NEW]
--path-map rewrites the stored artefact paths (e.g. after copying the cluster results to the workstation).
Never use the frozen evaluation database (data/output/db/db.sqlite): the script refuses it.
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.notebook_classification import local_llm  # noqa: E402
from analysis.notebook_classification.hybrid import classify_notebook  # noqa: E402
from analysis.notebook_classification.storage import ensure_classification_table, save_classification  # noqa: E402

FROZEN = (ROOT / 'data' / 'output' / 'db' / 'db.sqlite').resolve()
README_ORDER = ['.md', '.markdown', '.rst', '.txt', '']


def mapped(path, maps):
    for old, new in maps:
        if path.startswith(old):
            return new + path[len(old):]
    return path


def model_digest(base_url, model, requester=None):
    """Digest of the model in Ollama (exact weights), or None."""
    import requests
    try:
        get = requester or requests.get
        tags = get(f"{base_url.rstrip('/')}/api/tags", timeout=30).json().get('models', [])
    except Exception:  # noqa: BLE001 - provenance only
        return None
    names = {model, model if ':' in model else f'{model}:latest'}
    for entry in tags:
        if entry.get('name') in names or entry.get('model') in names:
            return entry.get('digest')
    return None


def ollama_version(base_url, requester=None):
    import requests
    try:
        return (requester or requests.get)(f"{base_url.rstrip('/')}/api/version", timeout=30).json().get('version')
    except Exception:  # noqa: BLE001 - provenance only
        return None


def readme_of(files):
    """The root README among a run's stored resource files: (relative path, stored path) or None."""
    candidates = []
    for stored in files:
        relative = stored.split('/resource/', 1)[-1].removesuffix('.gz')
        stem, dot, suffix = relative.partition('.')
        if '/' not in relative and stem.lower() == 'readme' and (dot + suffix).lower() in README_ORDER:
            candidates.append((README_ORDER.index((dot + suffix).lower()), relative, stored))
    return sorted(candidates)[0][1:] if candidates else None


def work_items(connection, maps):
    """(run id, notebook id, notebook path, stored gz path, README stored path) for the latest run of
    every repository that has stored original notebooks."""
    runs = connection.execute(
        'SELECT rr.id, rr.repository_id FROM repository_runs rr '
        'WHERE rr.id = (SELECT MAX(id) FROM repository_runs WHERE repository_id = rr.repository_id) '
        'ORDER BY rr.id').fetchall()
    for run_id, repository_id in runs:
        artefacts = connection.execute(
            "SELECT kind, notebook_path, path FROM run_artifacts WHERE repository_run_id = ? "
            "AND kind IN ('original notebook', 'resource file') ORDER BY path", (run_id,)).fetchall()
        readme = readme_of([mapped(p, maps) for kind, _, p in artefacts if kind == 'resource file'])
        for kind, notebook_path, stored in artefacts:
            if kind != 'original notebook':
                continue
            row = connection.execute('SELECT id FROM notebooks WHERE repository_id = ? AND name = ?',
                                     (repository_id, notebook_path)).fetchone()
            yield run_id, (row[0] if row else None), notebook_path, mapped(stored, maps), readme


def classify(db, pass_name=None, model=local_llm.DEFAULT_MODEL, base_url=local_llm.DEFAULT_BASE_URL,
             num_ctx=local_llm.DEFAULT_NUM_CTX, rules_only=False, limit=None, maps=(), requester=None,
             tags_requester=None, timeout=local_llm.DEFAULT_TIMEOUT, log=print):
    connection = sqlite3.connect(db, timeout=120)
    connection.row_factory = sqlite3.Row
    ensure_classification_table(connection)
    digest = None if rules_only else model_digest(base_url, model, tags_requester)
    server = None if rules_only else ollama_version(base_url, tags_requester)
    pass_name = pass_name or (f"rules-{date.today().isoformat()}" if rules_only else
                              f"{model}-p{local_llm.PROMPT_VERSION}-ctx{num_ctx}-{date.today().isoformat()}")
    done = {(r[0], r[1]) for r in connection.execute(
        'SELECT repository_run_id, notebook_path FROM notebook_classifications WHERE classification_pass = ?',
        (pass_name,))}
    items = [it for it in work_items(connection, maps) if (it[0], it[2]) not in done]
    if limit:
        items = items[:limit]
    log(f'[CLASSIFY] pass {pass_name}: {len(items)} notebooks to classify ({len(done)} already in this pass); '
        + ('rules only' if rules_only else f'model {model} digest {(digest or "unknown")[:19]} num_ctx {num_ctx}'))
    counts = dict(classified=0, llm_failed=0, truncated=0, missing_file=0, readme_used=0)
    started = time.monotonic()
    folder = Path(tempfile.mkdtemp(prefix='nbf-classify-'))
    try:
        for index, (run_id, notebook_id, notebook_path, stored, readme) in enumerate(items, 1):
            try:
                content = gzip.decompress(Path(stored).read_bytes())
            except OSError:
                counts['missing_file'] += 1
                log(f'[CLASSIFY] missing stored notebook: {stored}')
                continue
            target = folder / Path(notebook_path).name
            target.write_bytes(content)
            readme_text = None
            if readme:
                try:
                    readme_text = gzip.decompress(Path(readme[1]).read_bytes()).decode('utf-8', 'replace')
                except OSError:
                    readme_text = None
            result = classify_notebook(target, use_rules=True, use_llm=not rules_only, model=model,
                                       base_url=base_url, timeout=timeout, llm_requester=requester,
                                       readme=readme_text, num_ctx=num_ctx)
            result.notebook_path = notebook_path
            save_classification(connection, result, notebook_id=notebook_id, repository_run_id=run_id,
                                classification_pass=pass_name, model_digest=digest,
                                readme_used=bool(readme_text) and not rules_only, source='artifact')
            target.unlink()
            counts['classified'] += 1
            counts['llm_failed'] += int(not rules_only and result.llm_result is None)
            counts['truncated'] += int(bool(result.llm_result and result.llm_result.usage.get('truncated')))
            counts['readme_used'] += int(bool(readme_text) and not rules_only)
            if index % 50 == 0:
                rate = (time.monotonic() - started) / index
                log(f'[CLASSIFY] {index}/{len(items)} ({rate:.1f} s per notebook)')
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    summary = dict(pass_name=pass_name, model=None if rules_only else model, model_digest=digest,
                   ollama_version=server,
                   prompt_version=local_llm.PROMPT_VERSION, num_ctx=None if rules_only else num_ctx,
                   seconds=round(time.monotonic() - started, 1), finished_at=datetime.now(timezone.utc)
                   .isoformat(timespec='seconds'), **counts)
    log(json.dumps(summary))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--pass', dest='pass_name')
    parser.add_argument('--model', default=local_llm.DEFAULT_MODEL)
    parser.add_argument('--ollama-url', default=local_llm.DEFAULT_BASE_URL)
    parser.add_argument('--num-ctx', type=int, default=local_llm.DEFAULT_NUM_CTX)
    parser.add_argument('--timeout', type=int, default=local_llm.DEFAULT_TIMEOUT)
    parser.add_argument('--rules-only', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--path-map', action='append', default=[], help='OLD=NEW prefix of stored artefact paths')
    parser.add_argument('--summary-json', type=Path, help='write the pass summary here (appended as JSON lines)')
    args = parser.parse_args(argv)
    if args.db.resolve() == FROZEN:
        sys.exit('refusing the frozen evaluation database; use the database of the new runs')
    summary = classify(args.db, args.pass_name, args.model, args.ollama_url, args.num_ctx, args.rules_only,
                       args.limit, [tuple(m.split('=', 1)) for m in args.path_map], timeout=args.timeout)
    if args.summary_json:
        with open(args.summary_json, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(summary) + '\n')
    sys.exit(0 if summary['missing_file'] == 0 else 1)


if __name__ == '__main__':
    main()
