#!/usr/bin/env python3
"""Merge per-task pipeline databases (e.g. one per SLURM array task) into one database.

Usage: merge_task_dbs.py --target merged.sqlite task-0.sqlite task-1.sqlite ...

Every task starts from an empty database, so row ids collide between tasks. Rows are
copied with new ids and every reference is remapped:
  repositories      matched by (platform, repository); created if new
  notebooks         matched by (repository, name); created if new
  repository_metadata  one row per repository; the most recently fetched one wins
  everything else   (sessions, runs, executions, metrics, cell comparisons, classifications,
                    acquisitions + files, licences, environments, dependency sources, imports,
                    packages, artefacts, stage and notebook usage, ReproScore readiness and outcome)
                    copied with new ids
Columns are matched by name (columns missing in the target are added). The target is
created with the schema of the first source if it does not exist. Each source is merged
in one transaction; a source already merged (same SHA-256) is refused. The tables
merge_sources and merge_run_map record what was merged and which task run became which
merged run. Tables not listed here are reported and skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# table -> {column: referenced table}
REFERENCES = {
    'pipeline_sessions': {},
    'repositories': {},
    'notebooks': {'repository_id': 'repositories'},
    'repository_metadata': {'repo_id': 'repositories'},
    'repository_runs': {'repository_id': 'repositories', 'session_id': 'pipeline_sessions'},
    'notebook_executions': {'repository_run_id': 'repository_runs', 'repository_id': 'repositories',
                            'notebook_id': 'notebooks'},
    'notebook_reproducibility_metrics': {'repository_run_id': 'repository_runs',
                                         'notebook_execution_id': 'notebook_executions',
                                         'repository_id': 'repositories', 'notebook_id': 'notebooks'},
    'notebook_cell_comparisons': {'repository_run_id': 'repository_runs',
                                  'notebook_execution_id': 'notebook_executions', 'notebook_id': 'notebooks'},
    'notebook_classifications': {'notebook_id': 'notebooks'},
    'resource_acquisitions': {'repository_run_id': 'repository_runs', 'repository_id': 'repositories'},
    'acquisition_files': {'acquisition_id': 'resource_acquisitions'},
    'run_stage_usage': {'repository_run_id': 'repository_runs'},
    'notebook_resource_usage': {'repository_run_id': 'repository_runs', 'notebook_id': 'notebooks'},
    'run_environments': {'repository_run_id': 'repository_runs'},
    'resource_licences': {'repository_run_id': 'repository_runs', 'repository_id': 'repositories'},
    'dependency_sources': {'repository_run_id': 'repository_runs'},
    'notebook_imports': {'repository_run_id': 'repository_runs', 'notebook_id': 'notebooks'},
    'run_packages': {'repository_run_id': 'repository_runs'},
    'run_artifacts': {'repository_run_id': 'repository_runs'},
    'resource_readiness': {'repository_run_id': 'repository_runs', 'repository_id': 'repositories'},
    'reproscore_outcome': {'repository_run_id': 'repository_runs', 'repository_id': 'repositories'},
    'metadata_snapshots': {'repository_run_id': 'repository_runs', 'repository_id': 'repositories'},
}
ORDER = list(REFERENCES)                      # parents before children
NATURAL_KEYS = {'repositories': ('platform', 'repository'), 'notebooks': ('repository_id', 'name'),
                'repository_metadata': ('repo_id',)}
BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS merge_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_path TEXT, sha256 TEXT UNIQUE, merged_at TEXT, rows_json TEXT);
CREATE TABLE IF NOT EXISTS merge_run_map (
    merge_source_id INTEGER, source_run_id INTEGER, run_id INTEGER,
    FOREIGN KEY (merge_source_id) REFERENCES merge_sources(id));
"""


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def columns(connection, table):
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def create_target(target, source):
    """Empty target with the schema (tables, indexes, views) of the source."""
    statements = [row[0] for row in source.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END")]
    for statement in statements:
        target.execute(statement)


def merge_source(target, path):
    digest = sha256(path)
    if target.execute('SELECT 1 FROM merge_sources WHERE sha256 = ?', (digest,)).fetchone():
        raise ValueError(f'{path} was already merged (same SHA-256)')
    source = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    present = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    skipped = sorted(present - set(REFERENCES) - {'sqlite_sequence', 'merge_sources', 'merge_run_map'})
    id_maps = {table: {} for table in REFERENCES}
    counts = {}
    for table in ORDER:
        if table not in present:
            continue
        target_columns = columns(target, table)
        if not target_columns:      # table new to the target: copy its definition
            target.execute(source.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                                          (table,)).fetchone()[0])
            target_columns = columns(target, table)
        for column in columns(source, table):
            if column not in target_columns:
                target.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}"')
                target_columns.append(column)
        refs = REFERENCES[table]
        copied = matched = 0
        for row in source.execute(f'SELECT * FROM "{table}" ORDER BY id'):
            values = dict(row)
            old_id = values.pop('id')
            for column, parent in refs.items():
                if values.get(column) is not None:
                    values[column] = id_maps[parent].get(values[column])     # None if the parent was not copied
            key = NATURAL_KEYS.get(table)
            if key:
                where = ' AND '.join(f'"{k}" IS ?' for k in key)
                existing = target.execute(f'SELECT id FROM "{table}" WHERE {where}',
                                          [values.get(k) for k in key]).fetchone()
                if existing:
                    id_maps[table][old_id] = existing[0]
                    matched += 1
                    if table == 'repository_metadata':
                        newer = target.execute("SELECT COALESCE(fetched_at, '') < COALESCE(?, '') FROM repository_metadata "
                                               'WHERE id = ?', (values.get('fetched_at'), existing[0])).fetchone()[0]
                        if newer:
                            assignments = ', '.join(f'"{c}" = ?' for c in values)
                            target.execute(f'UPDATE repository_metadata SET {assignments} WHERE id = ?',
                                           [*values.values(), existing[0]])
                    continue
            names = ', '.join(f'"{c}"' for c in values)
            marks = ', '.join('?' for _ in values)
            cursor = target.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks})', list(values.values()))
            id_maps[table][old_id] = cursor.lastrowid
            copied += 1
        counts[table] = dict(copied=copied, matched=matched)
    cursor = target.execute('INSERT INTO merge_sources (source_path, sha256, merged_at, rows_json) VALUES (?,?,?,?)',
                            (str(Path(path).resolve()), digest, datetime.now(timezone.utc).isoformat(timespec='seconds'),
                             json.dumps(counts)))
    target.executemany('INSERT INTO merge_run_map (merge_source_id, source_run_id, run_id) VALUES (?,?,?)',
                       [(cursor.lastrowid, old, new) for old, new in id_maps['repository_runs'].items()])
    source.close()
    return counts, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('sources', type=Path, nargs='+')
    parser.add_argument('--keep-going', action='store_true',
                        help='exit 0 although some sources could not be merged (they are reported); used by the study')
    args = parser.parse_args(argv)
    readable = [p for p in args.sources if readable_db(p)]
    for path in args.sources:
        if path not in readable:
            print(f'[MERGE] {path}: NOT merged: missing or unreadable database', file=sys.stderr)
    if not readable:
        print('[MERGE] no readable source database', file=sys.stderr)
        return 1
    new_target = not args.target.exists()
    target = sqlite3.connect(args.target, isolation_level=None)
    target.execute('PRAGMA foreign_keys = OFF')
    if new_target:
        first = sqlite3.connect(f'file:{readable[0]}?mode=ro', uri=True)
        target.execute('BEGIN')
        create_target(target, first)
        target.execute('COMMIT')
        first.close()
    target.executescript(BOOKKEEPING)
    status = 0 if len(readable) == len(args.sources) else 1
    for path in readable:
        try:
            target.execute('BEGIN')
            counts, skipped = merge_source(target, path)
            target.execute('COMMIT')
            summary = ', '.join(f"{t} {c['copied']}" + (f"(+{c['matched']} matched)" if c['matched'] else '')
                                for t, c in counts.items() if c['copied'] or c['matched'])
            print(f'[MERGE] {path}: {summary}' + (f'; skipped tables: {", ".join(skipped)}' if skipped else ''))
        except Exception as error:  # noqa: BLE001 - report and continue with the next source
            target.execute('ROLLBACK')
            print(f'[MERGE] {path}: NOT merged: {error}', file=sys.stderr)
            status = 1
    target.close()
    if status and args.keep_going:
        print('[MERGE] some sources were not merged (see above); continuing (--keep-going)', file=sys.stderr)
        return 0
    return status


def readable_db(path):
    """An existing SQLite file that opens and has the repositories table (a task that got that far)."""
    if not Path(path).is_file():
        return False
    try:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
            return db.execute("SELECT 1 FROM sqlite_master WHERE name = 'repositories'").fetchone() is not None
    except sqlite3.Error:
        return False


if __name__ == '__main__':
    sys.exit(main())
