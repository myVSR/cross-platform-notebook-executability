#!/usr/bin/env python3
"""Tests for src/resource_files.py and pipeline/licences_from_artifacts.py.
The licence detection test needs Docker and the licensee image (skipped otherwise).
Run: python3 tests/test_resource_files.py"""
import gzip
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import licence  # noqa: E402
import provenance  # noqa: E402
import resource_files as rf  # noqa: E402

MIT = """MIT License

Copyright (c) 2024 Example

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""


def make_repo(root):
    files = {
        'LICENSE': MIT, 'README.md': '# demo', 'requirements.txt': 'numpy==1.26.4\n', 'pyproject.toml': '[project]\n',
        'CITATION.cff': 'cff-version: 1.2.0\n', 'notebooks/README.md': 'nb readme', 'notebooks/a.ipynb': '{}',
        'other/README.md': 'not a notebook folder', 'other/environment.yml': 'name: x\n',
        'a/b/c/d/requirements.txt': 'too deep\n', 'node_modules/x/requirements.txt': 'skip\n',
        '.git/config': 'skip', 'big/requirements-big.txt': 'x' * (2 * 1024 * 1024),
        'Dockerfile': 'FROM python:3.11\n', 'binder/postBuild': 'pip install x\n',
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.repo, self.out = self.root / 'repo', self.root / 'artifacts'
        make_repo(self.repo)

    def test_selects_descriptive_and_dependency_files(self):
        summary = rf.store(self.repo, self.out, ['notebooks/a.ipynb'])
        stored = {s['path']: s['kind'] for s in summary['stored']}
        self.assertEqual(stored, {
            'CITATION.cff': 'descriptive', 'LICENSE': 'descriptive', 'README.md': 'descriptive',
            'notebooks/README.md': 'descriptive', 'other/environment.yml': 'dependency',
            'pyproject.toml': 'dependency', 'requirements.txt': 'dependency', 'Dockerfile': 'dependency',
            'binder/postBuild': 'dependency'})
        self.assertEqual([s['path'] for s in summary['skipped']], ['big/requirements-big.txt'])
        packed = self.out / 'resource' / 'LICENSE.gz'
        self.assertEqual(gzip.decompress(packed.read_bytes()).decode(), MIT)

    def test_recorded_as_resource_files(self):
        rf.store(self.repo, self.out, ['notebooks/a.ipynb'])
        connection = sqlite3.connect(':memory:')
        connection.executescript(
            'CREATE TABLE repositories (id INTEGER PRIMARY KEY, repository TEXT, platform TEXT);'
            'CREATE TABLE repository_runs (id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL, url TEXT,'
            ' run_status TEXT);'
            'CREATE TABLE repository_metadata (repo_id INTEGER UNIQUE, license TEXT);'
            "INSERT INTO repositories VALUES (1, 'demo', 'codeberg'); INSERT INTO repository_runs VALUES (5, 1, 'u', 'SUCCESS');")
        provenance.init(connection)
        provenance.record_artifacts(connection, 5, self.out)
        kinds = dict(connection.execute('SELECT kind, COUNT(*) FROM run_artifacts GROUP BY kind').fetchall())
        self.assertEqual(kinds, {'resource file': 9})
        self.connection = connection

    @unittest.skipUnless(licence.detect(Path(tempfile.gettempdir()))['status'] != 'unavailable',
                         'Docker or the licensee image is not available')
    def test_licence_detected_afterwards_from_stored_files(self):
        self.test_recorded_as_resource_files()
        db = self.root / 'run.sqlite'
        disk = sqlite3.connect(db)
        self.connection.backup(disk)
        disk.close()
        moved = self.root / 'moved'
        shutil.move(str(self.out), moved)                    # results brought back to another place
        output = subprocess.run([sys.executable, str(ROOT / 'pipeline' / 'licences_from_artifacts.py'), '--db', str(db),
                                 '--path-map', f'{self.out}={moved}'], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(output.stdout.splitlines()[-1]), {'runs': 1, 'status': {'ok': 1}})
        row = sqlite3.connect(db).execute('SELECT licence_file, status, detector FROM resource_licences').fetchone()
        self.assertEqual(row[:2], ('MIT', 'ok'))
        self.assertIn('from stored resource files', row[2])

    def test_frozen_database_is_refused(self):
        result = subprocess.run([sys.executable, str(ROOT / 'pipeline' / 'licences_from_artifacts.py'), '--db',
                                 str(ROOT / 'data' / 'output' / 'db' / 'db.sqlite')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('frozen', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=1)
