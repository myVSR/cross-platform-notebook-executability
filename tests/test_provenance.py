#!/usr/bin/env python3
"""Offline tests for src/provenance.py. Run: python3 tests/test_provenance.py"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import provenance  # noqa: E402


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = self.tmp / 'db.sqlite'
        self.connection = provenance.connect(self.db)
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE repositories (id INTEGER PRIMARY KEY, repository TEXT, platform TEXT);"
            "CREATE TABLE notebooks (id INTEGER PRIMARY KEY, repository_id INTEGER, name TEXT);"
            "CREATE TABLE repository_runs (id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL, url TEXT,"
            " run_status TEXT NOT NULL, error_message TEXT, started_at TEXT, finished_at TEXT,"
            " duration_seconds FLOAT, created_at TEXT);"
            "INSERT INTO repositories VALUES (1, 'owner/repo', 'github'), (2, '123', 'zenodo');"
            "INSERT INTO repository_runs (id, repository_id, run_status) VALUES (10, 1, 'RUNNING'), (20, 2, 'RUNNING');")
        provenance.init(self.connection)
        provenance.init(self.connection)  # idempotent

    def test_git_version_and_tree_are_recorded(self):
        repo = self.tmp / 'clone'
        repo.mkdir()
        git = lambda *a: subprocess.run(['git', *a], cwd=repo, check=True, capture_output=True)
        git('init', '-q', '-b', 'main')
        (repo / 'nb.ipynb').write_text('{}')
        (repo / 'data').mkdir()
        (repo / 'data' / 'x.csv').write_text('1,2\n')
        git('add', '.')
        git('-c', 'user.name=t', '-c', 'user.email=t@example.org', 'commit', '-q', '-m', 'first')
        head = git('rev-parse', 'HEAD').stdout.decode().strip()

        provenance.record_acquisition(self.connection, 10, 1, 'github', 'https://github.com/owner/repo', str(repo),
                                      wall_seconds=1.5)
        row = self.connection.execute('SELECT * FROM v_resource_versions').fetchone()
        self.assertEqual(row[5], head)                    # version_id
        self.assertEqual(row[6], 'main')                  # version_label
        files = dict(self.connection.execute('SELECT path, size_bytes FROM acquisition_files'))
        self.assertEqual(files, {'nb.ipynb': 2, 'data/x.csv': 4})
        checksum = self.connection.execute("SELECT checksum FROM acquisition_files WHERE path='nb.ipynb'").fetchone()[0]
        self.assertTrue(checksum.startswith('git-blob-sha1:'))

        # recording the same run again replaces, not duplicates
        provenance.record_acquisition(self.connection, 10, 1, 'github', 'u', str(repo))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM acquisition_files').fetchone()[0], 2)

    def test_zenodo_manifest_is_recorded_with_members_and_skips(self):
        manifest = dict(record_id='123', concept_record_id='100', record_version='v2.1', record_revision=4,
                        record_updated='2025-01-01T00:00:00+00:00', duplicate_notebooks=[],
                        files=[dict(key='a.ipynb', size=10, checksum='md5:abc', md5_verified=True, action='downloaded',
                                    reason='notebook', seconds=0.2, bytes_transferred=10),
                               dict(key='big.zip', size=10**10, checksum='md5:def', action='skip',
                                    reason='archive without notebooks larger than 256 MiB'),
                               dict(key='code.zip', size=50, checksum='md5:123', md5_verified=True, action='extracted',
                                    reason='archive with notebooks', seconds=0.5, bytes_transferred=50,
                                    members=[dict(path='pkg/n.ipynb', size=300, crc=255)])])
        path = self.tmp / 'manifest.json'
        path.write_text(json.dumps(manifest))
        provenance.record_acquisition(self.connection, 20, 2, 'zenodo', 'https://zenodo.org/records/123',
                                      str(self.tmp / 'missing'), manifest=str(path))
        version = self.connection.execute(
            'SELECT version_id, version_label, concept_id, record_revision, files_downloaded, files_skipped, '
            'bytes_transferred FROM resource_acquisitions').fetchone()
        self.assertEqual(version, ('123', 'v2.1', '100', 4, 2, 1, 60))
        member = self.connection.execute(
            "SELECT origin, archive, checksum FROM acquisition_files WHERE path='pkg/n.ipynb'").fetchone()
        self.assertEqual(member, ('archive member', 'code.zip', 'crc32:000000ff'))

    def test_session_records_host_and_hides_secrets(self):
        with mock.patch.dict(os.environ, {'ZENODO_MAX_FILE_BYTES': '1', 'ZENODO_TOKEN': 'secret',
                                          'NOTEBOOK_TIMEOUT_SECONDS': '1800'}, clear=True):
            config = provenance.config_from_environment()
        self.assertEqual(config, {'NOTEBOOK_TIMEOUT_SECONDS': '1800', 'ZENODO_MAX_FILE_BYTES': '1'})
        session = provenance.create_session(self.connection, config)
        row = self.connection.execute('SELECT logical_cpus, memory_bytes, python_version, energy_counter '
                                      'FROM pipeline_sessions WHERE id=?', (session,)).fetchone()
        self.assertGreater(row[0], 0)
        self.assertGreater(row[1], 0)
        self.assertTrue(row[3])
        self.assertIn('session_id', {r[1] for r in self.connection.execute('PRAGMA table_info(repository_runs)')})

    def test_environment_decision_is_recorded(self):
        decision = dict(requested='3.0', source='setup.py', use='3.10', default='3.10', python2=False,
                        constraints='>=3.0', notebook_versions={'3.8': 2}, evidence=[{'kind': 'constraint'}], notes=[])
        provenance.main(['--db', str(self.db), 'environment', '--run-id', '10',
                         '--python-decision', json.dumps(decision), '--python-used', '3.10.14'])
        provenance.main(['--db', str(self.db), 'environment', '--run-id', '10',
                         '--python-decision', json.dumps(decision), '--python-used', '3.10.15'])  # replaces
        rows = sqlite3.connect(self.db).execute(
            'SELECT python_requested, python_source, python_selected, python_used, constraints FROM run_environments').fetchall()
        self.assertEqual(rows, [('3.0', 'setup.py', '3.10', '3.10.15', '>=3.0')])

    def test_licence_is_stored_next_to_the_api_value(self):
        self.connection.executescript(
            "CREATE TABLE repository_metadata (repo_id INTEGER UNIQUE, license TEXT);"
            "INSERT INTO repository_metadata VALUES (1, '');")
        result = dict(status='ok', detector='licensee (test)', licence_file='MIT', licence_files=['MIT'],
                      licence_any=['MIT'], matched_files=[{'filename': 'LICENSE', 'kind': 'licence file'}])
        provenance.main(['--db', str(self.db), 'licence', '--run-id', '10', '--result-json', json.dumps(result)])
        row = sqlite3.connect(self.db).execute(
            'SELECT repository_id, licence_api, licence_file, status FROM resource_licences').fetchone()
        self.assertEqual(row, (1, '', 'MIT', 'ok'))

    def test_dependencies_and_error_causes(self):
        self.connection.executescript(
            "INSERT INTO notebooks VALUES (7, 1, 'nb.ipynb');"
            "CREATE TABLE notebook_executions (id INTEGER PRIMARY KEY, repository_run_id INTEGER, notebook_name TEXT, "
            "error_type TEXT, error_message TEXT, error_category TEXT);")
        provenance.init(self.connection)                                    # adds dependency_cause
        plan = dict(sources=[dict(path='requirements.txt', kind='requirements', used=True, reason='requirements file',
                                  specs=['pandas==1.0'], pinned=1, notes=[])],
                    imports=[dict(notebook='nb.ipynb', module='pandas', top='pandas', cells=[0], classification='third-party',
                                  distribution='pandas', mapping='same name'),
                             dict(notebook='nb.ipynb', module='sklearn.cross_validation', top='sklearn', cells=[1],
                                  classification='third-party', distribution='scikit-learn', mapping='override'),
                             dict(notebook='nb.ipynb', module='helpers', top='helpers', cells=[0], classification='local',
                                  local_path='lib'),
                             dict(notebook='nb.ipynb', module='urllib2', top='urllib2', cells=[2], classification='python2-stdlib')],
                    requirements=[], import_path=['lib'], python2_notebooks=[])
        install = dict(packages=[dict(name='pandas', spec='pandas==1.0', origin='declared', origin_kind='requirements',
                                      origin_files=['requirements.txt'], imports=['pandas'], decision='install',
                                      reason='requirements file', method='joint', status='failed', error='no wheel'),
                                 dict(name='scikit-learn', spec='scikit-learn', origin='inferred', origin_kind='notebook imports',
                                      origin_files=['nb.ipynb'], imports=['sklearn.cross_validation'], decision='install',
                                      reason='imported', method='one by one', status='installed', installed_version='1.5.0')],
                       modules_available=dict(pandas=False, sklearn=True), kernel=dict(status='installed', version='6.29',
                       changed=['tornado 4.5 -> 6.4']), pip_version='24.0', pip_check_ok=True, pip_check='', freeze='x==1',
                       summary=dict(declared=1))
        p, i = self.tmp / 'plan.json', self.tmp / 'install.json'
        p.write_text(json.dumps(plan)); i.write_text(json.dumps(install))
        provenance.main(['--db', str(self.db), 'dependencies', '--run-id', '10', '--plan', str(p), '--install', str(i)])
        db = sqlite3.connect(self.db)
        self.assertEqual(db.execute("SELECT notebook_id, importable FROM notebook_imports WHERE module='pandas'").fetchone(), (7, 0))
        self.assertEqual(sorted(r[0] for r in db.execute('SELECT origin FROM run_packages')), ['declared', 'inferred', 'kernel'])
        self.assertEqual(json.loads(db.execute('SELECT kernel_install_changes_json FROM run_environments').fetchone()[0]),
                         ['tornado 4.5 -> 6.4'])
        db.executemany('INSERT INTO notebook_executions (repository_run_id, notebook_name, error_type, error_message) '
                       'VALUES (10, ?, ?, ?)', [('nb.ipynb', 'ModuleNotFoundError', "No module named 'pandas'"),
                                                ('nb.ipynb', 'ModuleNotFoundError', "No module named 'sklearn.cross_validation'"),
                                                ('nb.ipynb', 'ModuleNotFoundError', "No module named 'helpers'"),
                                                ('nb.ipynb', 'ModuleNotFoundError', "No module named 'urllib2'"),
                                                ('nb.ipynb', 'ModuleNotFoundError', "No module named 'mpmath'"),
                                                ('nb.ipynb', 'ImportError', "cannot import name 'x' from 'y'")])
        db.commit()
        provenance.main(['--db', str(self.db), 'classify-dependency-errors', '--run-id', '10'])
        causes = [r[0] for r in sqlite3.connect(self.db).execute('SELECT dependency_cause FROM notebook_executions ORDER BY id')]
        self.assertEqual(causes, ['declared package failed to install (pandas)',
                                  'submodule missing in the installed version (version incompatibility or extra package)',
                                  'local module of the resource not importable', 'Python 2 module',
                                  'imported indirectly (missing transitive dependency, or by local code)',
                                  'version incompatibility: name not found in the installed package (cannot import name)'])

    def test_measurements_tolerate_decimal_comma(self):
        # bash/awk under a German locale print "0,543"
        self.assertEqual(provenance.number('0,543'), 0.543)
        self.assertEqual(provenance.number('12', int), 12)
        self.assertIsNone(provenance.number('NA'))

    def test_cli_stage_and_notebook_usage(self):
        self.connection.execute("INSERT INTO notebooks VALUES (5, 1, 'nb.ipynb')")
        self.connection.commit()
        provenance.main(['--db', str(self.db), 'stage', '--run-id', '10', '--stage', 'execute', '--wall', '2.5',
                         '--user', '2.0', '--sys', '0.1', '--energy-joules', 'NA', '--net-rx', '100', '--net-tx', '5',
                         '--disk-bytes', '', '--exit-status', '0'])
        provenance.main(['--db', str(self.db), 'notebook-usage', '--run-id', '10', '--repository-id', '1',
                         '--notebook-path', 'nb.ipynb', '--wall', '2.4', '--user', '1.9', '--sys', '0.1',
                         '--max-rss-kb', '1000', '--fs-in', '0', '--fs-out', '8', '--energy-joules', 'NA',
                         '--exit-code', '0'])
        footprint = sqlite3.connect(self.db).execute(
            'SELECT wall_seconds, cpu_user_seconds, energy_pkg_joules, max_rss_kb FROM v_run_footprint_inputs '
            'WHERE repository_run_id=10').fetchone()
        self.assertEqual(footprint, (2.5, 2.0, None, 1000))
        notebook = sqlite3.connect(self.db).execute('SELECT notebook_id FROM notebook_resource_usage').fetchone()
        self.assertEqual(notebook, (5,))


if __name__ == '__main__':
    unittest.main(verbosity=1)
