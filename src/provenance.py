#!/usr/bin/env python3
"""Provenance and resource-usage records for pipeline runs (SQLite, stdlib only).

Commands (all take --db):
  init                      create the tables and views (idempotent)
  session                   record host and software of this pipeline process; prints its id
  acquisition               record which version of a resource was acquired, and every file
  stage                     record resource usage of one stage of a run
  notebook-usage            record resource usage of one notebook execution
  environment               record the Python version decision and the interpreter used
  licence                   record the file-based licence (src/licence.py) next to the API value
  dependencies              record dependency evidence and install results (src/dependencies.py,
                            src/install_env.py)
  kernel                    record the kernel that ran the notebooks (src/kernel_probe.py) and the tool env
  artifacts                 record the files stored for a run (original/executed notebooks, install files)
  classify-dependency-errors  explain each ModuleNotFoundError/ImportError of a run from the records

Tables
  pipeline_sessions         host, CPU, memory, OS, Python, pipeline commit, limits, energy counter
  resource_acquisitions     one per run: version id (commit SHA / Zenodo record id), version label
                            and date, Zenodo concept id and revision, bytes transferred and on disk
  acquisition_files         every file: path, size, checksum (+ verified), action, reason,
                            seconds and bytes transferred
  run_stage_usage           per run and stage: wall, CPU user/sys, energy (RAPL, if readable),
                            host network bytes, disk bytes
  notebook_resource_usage   per notebook execution: wall, CPU user/sys, peak RSS, file-system I/O,
                            energy (RAPL, if readable), exit code
  run_environments          per run: Python requested (and from which file), chosen, actually used,
                            Python 2 flag, constraints, notebook versions, all evidence and notes
  resource_licences         per run: licence from the platform API and from the files (licensee),
                            with detector, status and every matched file
  dependency_sources        per run and dependency file: kind, used or not and why, specs, pins, notes
  notebook_imports          per run, notebook and imported module: cells, classification (stdlib,
                            python2-stdlib, local, not-installable, third-party), distribution and
                            mapping source, local folder, importable after installation
  run_packages              per run and package: spec, origin (declared / inferred / kernel),
                            source files, imports it serves, decision and reason, install method,
                            status, installed version, error, seconds, conflicting pins
  run_artifacts             per run and stored file: kind, notebook, path in the artefact store,
                            SHA-256 and size of the original content, stored size
  notebook_executions.dependency_cause   added column: why a ModuleNotFoundError/ImportError
                            happened, derived from the records above (error_category is unchanged)
Views
  v_resource_versions       repository, platform, run, version id/label/date, acquired_at
  v_run_footprint_inputs    per run: summed stage usage and bytes transferred (input to
                            analysis/footprint.py)
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    hostname TEXT, os TEXT, kernel TEXT, cpu_model TEXT, logical_cpus INTEGER, memory_bytes INTEGER,
    python_version TEXT, pipeline_commit TEXT, pipeline_dirty INTEGER,
    energy_counter TEXT,          -- e.g. 'intel-rapl:0 (package-0)' or 'unavailable: ...'
    config_json TEXT              -- limits, timeouts, model names in force for this session
);
CREATE TABLE IF NOT EXISTS resource_acquisitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER NOT NULL UNIQUE,
    repository_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    source_url TEXT,
    method TEXT,                  -- 'git clone --depth 1' | 'zenodo api'
    version_id TEXT,              -- Git commit SHA | Zenodo record id (version-specific)
    version_label TEXT,           -- Git branch | Zenodo metadata.version
    version_date TEXT,            -- commit date (ISO) | Zenodo: deposit date of this version (created), else last update
    concept_id TEXT,              -- Zenodo concept record id (all versions)
    record_revision INTEGER,      -- Zenodo revision_id
    files_total INTEGER, files_downloaded INTEGER, files_skipped INTEGER,
    bytes_transferred INTEGER,    -- Git pack size | sum of downloaded bytes
    bytes_on_disk INTEGER,
    wall_seconds REAL,
    acquired_at TEXT NOT NULL,
    details_json TEXT,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id),
    FOREIGN KEY (repository_id) REFERENCES repositories(id)
);
CREATE TABLE IF NOT EXISTS acquisition_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    acquisition_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    origin TEXT NOT NULL,         -- 'record file' | 'archive member' | 'git tree'
    archive TEXT,                 -- record file the member came from
    size_bytes INTEGER,
    checksum TEXT,                -- 'md5:..' | 'crc32:..' | 'git-blob-sha1:..'
    checksum_verified INTEGER,    -- 1 verified, 0 mismatch, NULL not checked
    action TEXT,                  -- downloaded | extracted | extracted_partially | skip | cloned | ...
    reason TEXT,
    seconds REAL,
    bytes_transferred INTEGER,
    FOREIGN KEY (acquisition_id) REFERENCES resource_acquisitions(id)
);
CREATE INDEX IF NOT EXISTS idx_acquisition_files_acq ON acquisition_files(acquisition_id);
CREATE TABLE IF NOT EXISTS run_stage_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER NOT NULL,
    stage TEXT NOT NULL,          -- acquire | classify | environment | execute | compare
    started_at TEXT,
    wall_seconds REAL, cpu_user_seconds REAL, cpu_sys_seconds REAL,
    energy_pkg_joules REAL,       -- RAPL package energy, whole host (NULL if not readable)
    net_rx_bytes_host INTEGER, net_tx_bytes_host INTEGER,   -- whole host, all interfaces but lo
    disk_bytes INTEGER,           -- size of the stage's output folder, if given
    exit_status INTEGER,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_run_stage_usage_run ON run_stage_usage(repository_run_id);
CREATE TABLE IF NOT EXISTS notebook_resource_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER NOT NULL,
    notebook_id INTEGER,
    notebook_path TEXT NOT NULL,
    wall_seconds REAL, cpu_user_seconds REAL, cpu_sys_seconds REAL,
    max_rss_kb INTEGER,           -- largest single process (normally the kernel)
    fs_inputs INTEGER, fs_outputs INTEGER,   -- 512-byte blocks, from GNU time
    energy_pkg_joules REAL,
    exit_code INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id),
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id)
);
CREATE INDEX IF NOT EXISTS idx_notebook_resource_usage_run ON notebook_resource_usage(repository_run_id);
CREATE TABLE IF NOT EXISTS run_environments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER NOT NULL UNIQUE,
    python_requested TEXT,        -- as found (pin, notebook metadata, or default)
    python_source TEXT,           -- file or rule it came from
    python_selected TEXT,         -- after mapping into the supported range
    python_used TEXT,             -- interpreter actually used (platform.python_version())
    python_default TEXT,
    python2 INTEGER,              -- Python 2 evidence found
    constraints TEXT,
    notebook_versions_json TEXT,  -- {"3.8": 4, ...} from language_info
    evidence_json TEXT,
    notes_json TEXT,
    created_at TEXT,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id)
);
CREATE TABLE IF NOT EXISTS resource_licences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER NOT NULL UNIQUE,
    repository_id INTEGER NOT NULL,
    licence_api TEXT,             -- repository_metadata.license at detection time (platform API)
    licence_file TEXT,            -- licensee, licence files only: SPDX id, NOASSERTION, or '' (none)
    licence_files_json TEXT,      -- every licence named by a licence file
    licence_any_json TEXT,        -- also README and package manifests
    matched_files_json TEXT,
    detector TEXT,                -- e.g. licensee (notebookfair/licensee:10.1.0)
    status TEXT,                  -- ok | unavailable | error
    error TEXT,
    created_at TEXT,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id),
    FOREIGN KEY (repository_id) REFERENCES repositories(id)
);
CREATE TABLE IF NOT EXISTS dependency_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER NOT NULL,
    path TEXT, kind TEXT, used INTEGER, reason TEXT,
    specs_json TEXT, pinned INTEGER, notes_json TEXT,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_dependency_sources_run ON dependency_sources(repository_run_id);
CREATE TABLE IF NOT EXISTS notebook_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER NOT NULL,
    notebook_id INTEGER,
    notebook_path TEXT, module TEXT, top_level TEXT, cells_json TEXT,
    classification TEXT,          -- stdlib | python2-stdlib | local | not-installable | third-party | unreadable notebook
    distribution TEXT, mapping_source TEXT,   -- override | pipreqs | same name
    local_path TEXT,
    importable INTEGER,           -- third-party: importable after installation (find_spec), else NULL
    note TEXT,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id),
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id)
);
CREATE INDEX IF NOT EXISTS idx_notebook_imports_run ON notebook_imports(repository_run_id);
CREATE TABLE IF NOT EXISTS run_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER NOT NULL,
    name TEXT, spec TEXT,
    origin TEXT,                  -- declared | inferred | kernel
    origin_kind TEXT,             -- requirements | conda environment | pyproject.toml | ... | %pip | notebook imports
    origin_files_json TEXT, imports_json TEXT,
    decision TEXT, reason TEXT, method TEXT,
    status TEXT,                  -- installed | failed | skipped
    installed_version TEXT, error TEXT, seconds REAL, conflicts_json TEXT, note TEXT,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_run_packages_run ON run_packages(repository_run_id);
CREATE TABLE IF NOT EXISTS run_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER NOT NULL,
    kind TEXT,                    -- original notebook | executed notebook | comparison | install | kernelspec | resource file | log
    notebook_path TEXT, path TEXT, sha256 TEXT, bytes INTEGER, stored_bytes INTEGER, created_at TEXT,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_run_artifacts_run ON run_artifacts(repository_run_id);
CREATE TABLE IF NOT EXISTS notebook_cell_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER NOT NULL,
    notebook_execution_id INTEGER NOT NULL,
    notebook_id INTEGER,
    cell_index INTEGER,
    outcome TEXT,                 -- empty | not stored | identical | identical normalised | identical tolerance |
                                  -- image similar | image only | error | missing | different  (analysis/nbprocess/compare_cells.py)
    rules_json TEXT, notes_json TEXT, stored_outputs INTEGER, executed_outputs INTEGER,
    error_name TEXT, source_changed INTEGER,
    FOREIGN KEY (notebook_execution_id) REFERENCES notebook_executions(id)
);
CREATE INDEX IF NOT EXISTS idx_notebook_cell_comparisons_execution ON notebook_cell_comparisons(notebook_execution_id);
DROP VIEW IF EXISTS v_dependency_provenance;
CREATE VIEW v_dependency_provenance AS
SELECT rr.repository_id, r.platform, r.repository, p.repository_run_id, p.name AS package, p.spec, p.origin,
       p.origin_kind, p.origin_files_json, p.imports_json, p.decision, p.status, p.installed_version, p.error
FROM run_packages p JOIN repository_runs rr ON rr.id = p.repository_run_id
JOIN repositories r ON r.id = rr.repository_id;
DROP VIEW IF EXISTS v_resource_versions;
CREATE VIEW v_resource_versions AS
SELECT r.id AS repository_id, r.platform, r.repository, a.repository_run_id, a.method,
       a.version_id, a.version_label, a.version_date, a.concept_id, a.record_revision,
       a.files_downloaded, a.files_skipped, a.bytes_transferred, a.acquired_at
FROM resource_acquisitions a JOIN repositories r ON r.id = a.repository_id;
DROP VIEW IF EXISTS v_run_footprint_inputs;
CREATE VIEW v_run_footprint_inputs AS
SELECT rr.id AS repository_run_id, rr.repository_id, rr.session_id, rr.run_status,
       SUM(u.wall_seconds) AS wall_seconds, SUM(u.cpu_user_seconds) AS cpu_user_seconds,
       SUM(u.cpu_sys_seconds) AS cpu_sys_seconds, SUM(u.energy_pkg_joules) AS energy_pkg_joules,
       COUNT(u.energy_pkg_joules) AS stages_with_energy, COUNT(u.id) AS stages,
       (SELECT bytes_transferred FROM resource_acquisitions a WHERE a.repository_run_id = rr.id) AS bytes_transferred,
       (SELECT MAX(max_rss_kb) FROM notebook_resource_usage n WHERE n.repository_run_id = rr.id) AS max_rss_kb
FROM repository_runs rr LEFT JOIN run_stage_usage u ON u.repository_run_id = rr.id
GROUP BY rr.id;
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect(path):
    connection = sqlite3.connect(path, timeout=30)
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


ENVIRONMENT_COLUMNS = {
    'kernel_name': 'TEXT', 'kernel_status': 'TEXT', 'kernel_executable': 'TEXT', 'kernel_python_version': 'TEXT',
    'ipykernel_version': 'TEXT', 'kernel_sys_path_json': 'TEXT', 'kernel_error': 'TEXT', 'tool_env_json': 'TEXT',
    'pip_version': 'TEXT', 'pip_freeze': 'TEXT', 'pip_check_ok': 'INTEGER', 'pip_check': 'TEXT',
    'kernel_install_changes_json': 'TEXT', 'import_path_json': 'TEXT', 'python2_notebooks_json': 'TEXT',
    'install_summary_json': 'TEXT', 'pip_uploaded_prior_to': 'TEXT', 'pip_cutoff_reason': 'TEXT', 'kernel_fallback': 'TEXT'}


# Comparison version 2 (analysis/nbprocess/compare_cells.py) next to the thesis score (reproducibility_score)
METRIC_COLUMNS = {
    'comparison_version': 'INTEGER', 'score_strict': 'REAL', 'score_normalised': 'REAL', 'score_text_data': 'REAL',
    'score_before_error': 'REAL', 'cells_assessable': 'INTEGER', 'cells_empty': 'INTEGER', 'cells_not_stored': 'INTEGER',
    'cells_identical': 'INTEGER', 'cells_reproduced_normalised': 'INTEGER', 'cells_image_similar': 'INTEGER',
    'cells_image_only': 'INTEGER',
    'cells_error': 'INTEGER', 'cells_missing': 'INTEGER', 'cells_different': 'INTEGER'}


def init(connection):
    existing = {row[1] for row in connection.execute('PRAGMA table_info(repository_runs)')}
    if not existing:
        connection.execute('CREATE TABLE IF NOT EXISTS repository_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                           'repository_id INTEGER NOT NULL, url TEXT, run_status TEXT NOT NULL, error_message TEXT, '
                           'started_at TEXT, finished_at TEXT, duration_seconds FLOAT, '
                           'created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        existing = {row[1] for row in connection.execute('PRAGMA table_info(repository_runs)')}
    if 'session_id' not in existing:
        connection.execute('ALTER TABLE repository_runs ADD COLUMN session_id INTEGER REFERENCES pipeline_sessions(id)')
    connection.executescript(SCHEMA)
    existing = {row[1] for row in connection.execute('PRAGMA table_info(run_environments)')}
    for column, kind in ENVIRONMENT_COLUMNS.items():
        if column not in existing:
            connection.execute(f'ALTER TABLE run_environments ADD COLUMN {column} {kind}')
    metrics = {row[1] for row in connection.execute('PRAGMA table_info(notebook_reproducibility_metrics)')}
    for column, kind in METRIC_COLUMNS.items():
        if metrics and column not in metrics:
            connection.execute(f'ALTER TABLE notebook_reproducibility_metrics ADD COLUMN {column} {kind}')
    executions = {row[1] for row in connection.execute('PRAGMA table_info(notebook_executions)')}
    if executions and 'dependency_cause' not in executions:
        connection.execute('ALTER TABLE notebook_executions ADD COLUMN dependency_cause TEXT')
    connection.commit()


def run(command, cwd=None):
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return result.stdout.strip() if result.returncode == 0 else ''


def energy_counter():
    base = Path('/sys/class/powercap/intel-rapl:0')
    if not base.exists():
        return 'unavailable: no RAPL powercap interface'
    name = (base / 'name').read_text().strip() if (base / 'name').exists() else '?'
    try:
        (base / 'energy_uj').read_text()
    except OSError:
        return f'unavailable: intel-rapl:0 ({name}) not readable by {getpass.getuser()}'
    return f'intel-rapl:0 ({name})'


def host_info():
    cpu_model = ''
    for line in Path('/proc/cpuinfo').read_text().splitlines() if Path('/proc/cpuinfo').exists() else []:
        if line.startswith('model name'):
            cpu_model = line.split(':', 1)[1].strip()
            break
    memory = 0
    if Path('/proc/meminfo').exists():
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemTotal:'):
                memory = int(line.split()[1]) * 1024
    os_name = ''
    if Path('/etc/os-release').exists():
        for line in Path('/etc/os-release').read_text().splitlines():
            if line.startswith('PRETTY_NAME='):
                os_name = line.split('=', 1)[1].strip('"')
    return dict(hostname=socket.gethostname(), os=os_name or platform.system(), kernel=platform.release(),
                cpu_model=cpu_model or platform.processor(), logical_cpus=os.cpu_count(), memory_bytes=memory,
                python_version=sys.version.split()[0],
                pipeline_commit=run(['git', 'rev-parse', 'HEAD'], cwd=ROOT) or None,
                pipeline_dirty=int(bool(run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT))),
                energy_counter=energy_counter())


CONFIG_PREFIXES = ('ZENODO_MAX_', 'NOTEBOOK_', 'CLASSIFICATION_', 'REPO_TIMEOUT', 'FAST_FIRST', 'PYTHON_',
                   'PIP_', 'PYENV_VERSION')
SECRET_MARKERS = ('TOKEN', 'SECRET', 'PASSWORD', 'KEY', 'AUTH')


def config_from_environment():
    """Pipeline settings in force, without anything that looks like a secret."""
    return {name: value for name, value in sorted(os.environ.items())
            if name.startswith(CONFIG_PREFIXES) and not any(m in name for m in SECRET_MARKERS)}


def create_session(connection, config):
    info = host_info()
    cursor = connection.execute(
        'INSERT INTO pipeline_sessions (started_at, hostname, os, kernel, cpu_model, logical_cpus, memory_bytes, '
        'python_version, pipeline_commit, pipeline_dirty, energy_counter, config_json) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (now(), info['hostname'], info['os'], info['kernel'], info['cpu_model'], info['logical_cpus'],
         info['memory_bytes'], info['python_version'], info['pipeline_commit'], info['pipeline_dirty'],
         info['energy_counter'], json.dumps(config, sort_keys=True)))
    connection.commit()
    return cursor.lastrowid


def directory_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def git_acquisition(directory):
    """Version and file list of a Git working copy."""
    head = run(['git', 'rev-parse', 'HEAD'], cwd=directory)
    if not head:
        raise ValueError(f'not a Git working copy: {directory}')
    files = []
    for line in run(['git', 'ls-tree', '-r', '-l', '--full-tree', 'HEAD'], cwd=directory).splitlines():
        meta, path = line.split('\t', 1)
        mode, kind, blob, size = meta.split()
        files.append(dict(path=path, origin='git tree', size_bytes=None if size == '-' else int(size),
                          checksum=f'git-{kind}-sha1:{blob}', checksum_verified=None, action='cloned',
                          reason='submodule (not fetched)' if kind == 'commit' else None))
    git_dir = Path(directory) / '.git'
    pack_bytes = sum(p.stat().st_size for p in (git_dir / 'objects').rglob('*') if p.is_file()) if git_dir.is_dir() else None
    details = dict(remote=run(['git', 'remote', 'get-url', 'origin'], cwd=directory),
                   shallow=(git_dir / 'shallow').exists(),
                   commit_subject=run(['git', 'log', '-1', '--format=%s'], cwd=directory),
                   author_date=run(['git', 'log', '-1', '--format=%aI'], cwd=directory))
    return dict(method='git clone --depth 1', version_id=head,
                version_label=run(['git', 'symbolic-ref', '--short', '-q', 'HEAD'], cwd=directory) or None,
                version_date=run(['git', 'log', '-1', '--format=%cI'], cwd=directory) or None,
                concept_id=None, record_revision=None, files=files, bytes_transferred=pack_bytes, details=details)


def zenodo_acquisition(manifest_path):
    """Version and file list from the manifest written by src/zenodo_fetch.py."""
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    files = []
    for entry in manifest.get('files', []):
        checksum = entry.get('checksum')
        verified = entry.get('md5_verified')
        reason = entry.get('reason')
        if entry.get('archive_kept'):                 # src/zenodo_fetch.py keeps the archive next to its contents
            reason = '; '.join(x for x in (reason, 'archive kept next to its contents') if x)
        files.append(dict(path=entry['key'], origin='record file', size_bytes=entry.get('size'), checksum=checksum,
                          checksum_verified=None if verified is None else int(verified), action=entry.get('action'),
                          reason=reason, seconds=entry.get('seconds'),
                          bytes_transferred=entry.get('bytes_transferred')))
        for member in entry.get('members') or []:
            files.append(dict(path=member['path'], origin='archive member', archive=entry['key'],
                              size_bytes=member.get('size'),
                              checksum=f"crc32:{member['crc']:08x}" if member.get('crc') is not None else None,
                              checksum_verified=member.get('verified'), action=member.get('action', 'extracted'),
                              reason=member.get('reason'), seconds=member.get('seconds'),
                              bytes_transferred=member.get('bytes_transferred')))
    # record-file rows already include their members' transfers (partial archives)
    transferred = sum(f.get('bytes_transferred') or 0 for f in files if f['origin'] == 'record file')
    details = {k: manifest.get(k) for k in ('limits', 'flattened', 'duplicate_notebooks', 'error',
                                            'api_requests', 'started_at', 'finished_at')}
    return dict(method='zenodo api', version_id=manifest.get('record_id'), version_label=manifest.get('record_version'),
                version_date=manifest.get('record_created') or manifest.get('record_updated'), concept_id=manifest.get('concept_record_id'),
                record_revision=manifest.get('record_revision'), files=files, bytes_transferred=transferred,
                details=details)


def record_acquisition(connection, run_id, repository_id, platform_name, source_url, directory,
                       manifest=None, wall_seconds=None):
    data = zenodo_acquisition(manifest) if manifest else git_acquisition(directory)
    files = data.pop('files')
    downloaded = sum(f['action'] not in ('skip', None) for f in files if f['origin'] != 'archive member')
    skipped = sum(f['action'] == 'skip' for f in files)
    connection.execute('DELETE FROM acquisition_files WHERE acquisition_id IN '
                       '(SELECT id FROM resource_acquisitions WHERE repository_run_id = ?)', (run_id,))
    connection.execute('DELETE FROM resource_acquisitions WHERE repository_run_id = ?', (run_id,))
    cursor = connection.execute(
        'INSERT INTO resource_acquisitions (repository_run_id, repository_id, platform, source_url, method, version_id, '
        'version_label, version_date, concept_id, record_revision, files_total, files_downloaded, files_skipped, '
        'bytes_transferred, bytes_on_disk, wall_seconds, acquired_at, details_json) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (run_id, repository_id, platform_name, source_url, data['method'], data['version_id'], data['version_label'],
         data['version_date'], data['concept_id'], data['record_revision'], len(files), downloaded, skipped,
         data['bytes_transferred'], directory_bytes(directory) if directory and Path(directory).is_dir() else None,
         wall_seconds, now(), json.dumps(data['details'], sort_keys=True)))
    acquisition_id = cursor.lastrowid
    connection.executemany(
        'INSERT INTO acquisition_files (acquisition_id, path, origin, archive, size_bytes, checksum, checksum_verified, '
        'action, reason, seconds, bytes_transferred) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        [(acquisition_id, f['path'], f['origin'], f.get('archive'), f.get('size_bytes'), f.get('checksum'),
          f.get('checksum_verified'), f.get('action'), f.get('reason'), f.get('seconds'), f.get('bytes_transferred'))
         for f in files])
    connection.commit()
    return acquisition_id


def record_environment(connection, run_id, decision_json, python_used):
    decision = json.loads(decision_json) if decision_json else {}
    connection.execute('DELETE FROM run_environments WHERE repository_run_id = ?', (run_id,))
    connection.execute(
        'INSERT INTO run_environments (repository_run_id, python_requested, python_source, python_selected, '
        'python_used, python_default, python2, constraints, notebook_versions_json, evidence_json, notes_json, '
        'created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (run_id, decision.get('requested'), decision.get('source'), decision.get('use'), python_used or None,
         decision.get('default'), int(bool(decision.get('python2'))) if decision else None,
         decision.get('constraints'), json.dumps(decision.get('notebook_versions', {})),
         json.dumps(decision.get('evidence', [])), json.dumps(decision.get('notes', [])), now()))
    connection.commit()


def record_licence(connection, run_id, result_json):
    result = json.loads(result_json) if result_json else dict(status='error', error='no detector output')
    row = connection.execute('SELECT repository_id FROM repository_runs WHERE id = ?', (run_id,)).fetchone()
    if row is None:
        raise ValueError(f'unknown run {run_id}')
    api = connection.execute('SELECT license FROM repository_metadata WHERE repo_id = ?', (row[0],)).fetchone()
    connection.execute('DELETE FROM resource_licences WHERE repository_run_id = ?', (run_id,))
    connection.execute(
        'INSERT INTO resource_licences (repository_run_id, repository_id, licence_api, licence_file, '
        'licence_files_json, licence_any_json, matched_files_json, detector, status, error, created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (run_id, row[0], api[0] if api else None, result.get('licence_file'),
         json.dumps(result.get('licence_files', [])), json.dumps(result.get('licence_any', [])),
         json.dumps(result.get('matched_files', [])), result.get('detector'), result.get('status'),
         result.get('error'), now()))
    connection.commit()


def ensure_environment_row(connection, run_id):
    if connection.execute('SELECT 1 FROM run_environments WHERE repository_run_id = ?', (run_id,)).fetchone() is None:
        connection.execute('INSERT INTO run_environments (repository_run_id, created_at) VALUES (?, ?)', (run_id, now()))


def notebook_ids(connection, run_id):
    row = connection.execute('SELECT repository_id FROM repository_runs WHERE id = ?', (run_id,)).fetchone()
    if row is None:
        return {}
    return {name: nid for nid, name in connection.execute('SELECT id, name FROM notebooks WHERE repository_id = ?', (row[0],))}


def record_dependencies(connection, run_id, plan, install):
    ids = notebook_ids(connection, run_id)
    available = (install or {}).get('modules_available', {})
    for table in ('dependency_sources', 'notebook_imports', 'run_packages'):
        connection.execute(f'DELETE FROM {table} WHERE repository_run_id = ?', (run_id,))
    connection.executemany(
        'INSERT INTO dependency_sources (repository_run_id, path, kind, used, reason, specs_json, pinned, notes_json) '
        'VALUES (?,?,?,?,?,?,?,?)',
        [(run_id, s['path'], s['kind'], int(bool(s['used'])), s['reason'], json.dumps(s['specs']), s.get('pinned'),
          json.dumps(s.get('notes', []))) for s in plan.get('sources', [])])
    connection.executemany(
        'INSERT INTO notebook_imports (repository_run_id, notebook_id, notebook_path, module, top_level, cells_json, '
        'classification, distribution, mapping_source, local_path, importable, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        [(run_id, ids.get(i['notebook']), i['notebook'], i.get('module'), i.get('top'), json.dumps(i.get('cells', [])),
          i.get('classification'), i.get('distribution'), i.get('mapping'), i.get('local_path'),
          int(available[i['top']]) if i.get('classification') == 'third-party' and i.get('top') in available else None,
          i.get('note')) for i in plan.get('imports', [])])
    packages = list((install or {}).get('packages') or [])
    if not install:   # installation did not run: record the plan only
        packages = [dict(r, status='not attempted', imports=[]) for r in plan.get('requirements', [])]
    kernel = (install or {}).get('kernel')
    if kernel and kernel.get('status') != 'already installed':
        packages.append(dict(name='ipykernel', spec='ipykernel', origin='kernel', origin_kind='pipeline',
                             origin_files=[], imports=[], decision='install', reason='kernel for executing the notebooks',
                             method='after the resource packages', status=kernel.get('status'),
                             installed_version=kernel.get('version'), error=kernel.get('error'),
                             seconds=kernel.get('seconds'), conflicts=[]))
    connection.executemany(
        'INSERT INTO run_packages (repository_run_id, name, spec, origin, origin_kind, origin_files_json, imports_json, '
        'decision, reason, method, status, installed_version, error, seconds, conflicts_json, note) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        [(run_id, p.get('name'), p.get('spec'), p.get('origin'), p.get('origin_kind'), json.dumps(p.get('origin_files', [])),
          json.dumps(p.get('imports', [])), p.get('decision'), p.get('reason'), p.get('method'), p.get('status'),
          p.get('installed_version'), p.get('error'), p.get('seconds'), json.dumps(p.get('conflicts', [])),
          p.get('note')) for p in packages])
    ensure_environment_row(connection, run_id)
    connection.execute(
        'UPDATE run_environments SET pip_version = ?, pip_freeze = ?, pip_check_ok = ?, pip_check = ?, '
        'kernel_install_changes_json = ?, import_path_json = ?, python2_notebooks_json = ?, install_summary_json = ?, '
        'pip_uploaded_prior_to = ?, pip_cutoff_reason = ? WHERE repository_run_id = ?',
        ((install or {}).get('pip_version'), (install or {}).get('freeze'),
         None if not install else int(bool(install.get('pip_check_ok'))), (install or {}).get('pip_check'),
         json.dumps((kernel or {}).get('changed', [])), json.dumps(plan.get('import_path', [])),
         json.dumps(plan.get('python2_notebooks', [])), json.dumps((install or {}).get('summary', {})),
         (install or {}).get('uploaded_prior_to'), (install or {}).get('cutoff_reason'), run_id))
    connection.commit()


def record_kernel(connection, run_id, kernel_name, probe, tool_env, fallback=''):
    ensure_environment_row(connection, run_id)
    connection.execute(
        'UPDATE run_environments SET kernel_name = ?, kernel_status = ?, kernel_executable = ?, kernel_python_version = ?, '
        'ipykernel_version = ?, kernel_sys_path_json = ?, kernel_error = ?, tool_env_json = ?, kernel_fallback = ? '
        'WHERE repository_run_id = ?',
        (kernel_name, probe.get('status'), probe.get('executable'), probe.get('python_version'),
         probe.get('ipykernel_version'), json.dumps(probe.get('sys_path_head', [])), probe.get('error'),
         json.dumps(tool_env), fallback or None, run_id))
    connection.commit()


def record_artifacts(connection, run_id, directory):
    """Every file below the run's artefact folder; .gz files are hashed on their decompressed content."""
    import gzip
    import hashlib
    root = Path(directory)
    rows = []
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        kind = {'original': 'original notebook', 'executed': 'executed notebook', 'comparisons': 'comparison',
                'install': 'install', 'kernels': 'kernelspec', 'resource': 'resource file',
                'execution.log': 'log'}.get(relative.parts[0], 'other')
        data = gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_bytes()
        notebook = str(PurePosixPath(*relative.parts[1:])).removesuffix('.gz') if kind.endswith('notebook') else None
        rows.append((run_id, kind, notebook, str(path), hashlib.sha256(data).hexdigest(), len(data),
                     path.stat().st_size, now()))
    connection.execute('DELETE FROM run_artifacts WHERE repository_run_id = ?', (run_id,))
    connection.executemany('INSERT INTO run_artifacts (repository_run_id, kind, notebook_path, path, sha256, bytes, '
                           'stored_bytes, created_at) VALUES (?,?,?,?,?,?,?,?)', rows)
    connection.commit()
    return len(rows)


def dependency_cause(error_type, message, imports, packages):
    """Why a ModuleNotFoundError / ImportError happened, from the run's records (None if not a dependency error)."""
    message = message or ''
    if error_type == 'ImportError':
        if 'cannot import name' in message:
            return 'version incompatibility: name not found in the installed package (cannot import name)'
        return 'import error (other)'
    if error_type != 'ModuleNotFoundError':
        return None
    import re as _re
    m = _re.search(r"No module named '([^']+)'", message)
    module = m.group(1) if m else ''
    top = module.split('.')[0]
    direct = [i for i in imports if i['top_level'] == top]
    kinds = {i['classification'] for i in direct}
    if 'local' in kinds:
        return 'local module of the resource not importable'
    if 'python2-stdlib' in kinds:
        return 'Python 2 module'
    if 'stdlib' in kinds:
        return 'standard-library module missing in this interpreter build (e.g. _tkinter, distutils)'
    if 'not-installable' in kinds:
        return 'runtime-only module, not installable (e.g. google.colab)'
    serving = [p for p in packages if top in {x.split('.')[0] for x in json.loads(p['imports_json'] or '[]')}
               or (direct and p['name'] == _re.sub(r'[-_.]+', '-', (direct[0]['distribution'] or '')).lower())]
    if serving:
        package = serving[0]
        if package['status'] == 'failed':
            return f"{package['origin']} package failed to install ({package['name']})"
        if package['status'] == 'installed' and '.' in module:
            return 'submodule missing in the installed version (version incompatibility or extra package)'
        if package['status'] == 'installed':
            return 'package installed but module not importable (wrong package for this import)'
    if not direct:
        return 'imported indirectly (missing transitive dependency, or by local code)'
    return 'third-party import with no install attempt'


def classify_dependency_errors(connection, run_id):
    connection.row_factory = sqlite3.Row
    executions = connection.execute(
        'SELECT id, notebook_name, error_type, error_message FROM notebook_executions WHERE repository_run_id = ? '
        "AND error_type IN ('ModuleNotFoundError', 'ImportError')", (run_id,)).fetchall()
    packages = connection.execute('SELECT * FROM run_packages WHERE repository_run_id = ?', (run_id,)).fetchall()
    counts = {}
    for execution in executions:
        imports = connection.execute('SELECT * FROM notebook_imports WHERE repository_run_id = ? AND notebook_path = ?',
                                     (run_id, execution['notebook_name'])).fetchall()
        cause = dependency_cause(execution['error_type'], execution['error_message'], imports, packages)
        connection.execute('UPDATE notebook_executions SET dependency_cause = ? WHERE id = ?', (cause, execution['id']))
        counts[cause] = counts.get(cause, 0) + 1
    connection.commit()
    connection.row_factory = None
    return counts


def number(value, cast=float):
    """Parse a measurement; tolerate a decimal comma from a non-C locale."""
    if value in (None, '', 'NA'):
        return None
    value = str(value).replace(',', '.')
    return cast(float(value)) if cast is int else cast(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    session = commands.add_parser('session')
    session.add_argument('--config-json', help='default: pipeline settings from the environment')
    acquisition = commands.add_parser('acquisition')
    for name in ('--run-id', '--repository-id'):
        acquisition.add_argument(name, type=int, required=True)
    acquisition.add_argument('--platform', required=True)
    acquisition.add_argument('--url', required=True)
    acquisition.add_argument('--dir', required=True)
    acquisition.add_argument('--zenodo-manifest')
    acquisition.add_argument('--wall-seconds', type=number)
    stage = commands.add_parser('stage')
    stage.add_argument('--run-id', type=int, required=True)
    stage.add_argument('--stage', required=True)
    for name in ('--started-at', '--wall', '--user', '--sys', '--energy-joules', '--net-rx', '--net-tx',
                 '--disk-bytes', '--exit-status'):
        stage.add_argument(name)
    usage = commands.add_parser('notebook-usage')
    usage.add_argument('--run-id', type=int, required=True)
    usage.add_argument('--repository-id', type=int)
    usage.add_argument('--notebook-path', required=True)
    for name in ('--wall', '--user', '--sys', '--max-rss-kb', '--fs-in', '--fs-out', '--energy-joules', '--exit-code'):
        usage.add_argument(name)
    environment = commands.add_parser('environment')
    environment.add_argument('--run-id', type=int, required=True)
    environment.add_argument('--python-decision', default='', help='JSON from src/python_version.py --json')
    environment.add_argument('--python-used', default='')
    licence_parser = commands.add_parser('licence')
    licence_parser.add_argument('--run-id', type=int, required=True)
    licence_parser.add_argument('--result-json', default='', help='JSON from src/licence.py --json')
    deps = commands.add_parser('dependencies')
    deps.add_argument('--run-id', type=int, required=True)
    deps.add_argument('--plan', type=Path, required=True, help='JSON from src/dependencies.py')
    deps.add_argument('--install', type=Path, help='install-report.json from src/install_env.py')
    kernel_parser = commands.add_parser('kernel')
    kernel_parser.add_argument('--run-id', type=int, required=True)
    kernel_parser.add_argument('--kernel-name', required=True)
    kernel_parser.add_argument('--probe-json', default='{}')
    kernel_parser.add_argument('--tool-env-json', default='{}')
    kernel_parser.add_argument('--fallback', default='')
    artifacts = commands.add_parser('artifacts')
    artifacts.add_argument('--run-id', type=int, required=True)
    artifacts.add_argument('--dir', type=Path, required=True)
    classify = commands.add_parser('classify-dependency-errors')
    classify.add_argument('--run-id', type=int, required=True)
    args = parser.parse_args(argv)

    connection = connect(args.db)
    try:
        init(connection)
        if args.command == 'session':
            config = json.loads(args.config_json) if args.config_json else config_from_environment()
            print(create_session(connection, config))
        elif args.command == 'acquisition':
            record_acquisition(connection, args.run_id, args.repository_id, args.platform, args.url, args.dir,
                               manifest=args.zenodo_manifest, wall_seconds=args.wall_seconds)
        elif args.command == 'stage':
            connection.execute(
                'INSERT INTO run_stage_usage (repository_run_id, stage, started_at, wall_seconds, cpu_user_seconds, '
                'cpu_sys_seconds, energy_pkg_joules, net_rx_bytes_host, net_tx_bytes_host, disk_bytes, exit_status) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (args.run_id, args.stage, args.started_at, number(args.wall), number(args.user), number(args.sys),
                 number(args.energy_joules), number(args.net_rx, int), number(args.net_tx, int),
                 number(args.disk_bytes, int), number(args.exit_status, int)))
            connection.commit()
        elif args.command == 'dependencies':
            install = json.loads(args.install.read_text()) if args.install and args.install.is_file() else None
            record_dependencies(connection, args.run_id, json.loads(args.plan.read_text()), install)
        elif args.command == 'kernel':
            record_kernel(connection, args.run_id, args.kernel_name, json.loads(args.probe_json or '{}'),
                          json.loads(args.tool_env_json or '{}'), args.fallback)
        elif args.command == 'artifacts':
            print(record_artifacts(connection, args.run_id, args.dir))
        elif args.command == 'classify-dependency-errors':
            print(json.dumps(classify_dependency_errors(connection, args.run_id)))
        elif args.command == 'licence':
            record_licence(connection, args.run_id, args.result_json)
        elif args.command == 'environment':
            record_environment(connection, args.run_id, args.python_decision, args.python_used)
        elif args.command == 'notebook-usage':
            notebook_id = None
            if args.repository_id is not None:
                row = connection.execute('SELECT id FROM notebooks WHERE repository_id = ? AND name = ?',
                                         (args.repository_id, args.notebook_path)).fetchone()
                notebook_id = row[0] if row else None
            connection.execute(
                'INSERT INTO notebook_resource_usage (repository_run_id, notebook_id, notebook_path, wall_seconds, '
                'cpu_user_seconds, cpu_sys_seconds, max_rss_kb, fs_inputs, fs_outputs, energy_pkg_joules, exit_code) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (args.run_id, notebook_id, args.notebook_path, number(args.wall), number(args.user), number(args.sys),
                 number(args.max_rss_kb, int), number(args.fs_in, int), number(args.fs_out, int),
                 number(args.energy_joules), number(args.exit_code, int)))
            connection.commit()
    finally:
        connection.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
