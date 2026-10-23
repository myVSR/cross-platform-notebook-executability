#!/usr/bin/env python3
"""Offline tests for pipeline/topup_from_reserve.py (D9 reserve use). Run: python3 tests/test_topup.py"""
import csv
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'pipeline' / 'topup_from_reserve.py'
FIELDS = ['platform', 'stratum', 'year', 'url', 'role', 'processing_order']


class TopupTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        rows = [
            # codeberg primary: 2 analysable, 1 not obtainable (2020), 1 ineligible (2021), 1 pipeline error
            ('codeberg', '', 2020, 'https://codeberg.org/a/ok1', 'primary', 1),
            ('codeberg', '', 2020, 'https://codeberg.org/a/ok2', 'primary', 2),
            ('codeberg', '', 2020, 'https://codeberg.org/a/gone', 'primary', 3),
            ('codeberg', '', 2021, 'https://codeberg.org/a/nonb', 'primary', 4),
            ('codeberg', '', 2021, 'https://codeberg.org/a/kernel', 'primary', 5),
            ('codeberg', '', 2021, 'https://codeberg.org/r/r2021', 'reserve', 1),
            ('codeberg', '', 2020, 'https://codeberg.org/r/r2020', 'reserve', 2),
            ('codeberg', '', 2019, 'https://codeberg.org/r/r2019', 'reserve', 3),
            # zenodo: Z2 record lost, reserve has Z1 first, then Z2
            ('zenodo', 'Z1', 2022, 'https://zenodo.org/records/1', 'primary', 6),
            ('zenodo', 'Z2', 2022, 'https://zenodo.org/records/2', 'primary', 7),
            ('zenodo', 'Z1', 2022, 'https://zenodo.org/records/91', 'reserve', 4),
            ('zenodo', 'Z2', 2023, 'https://zenodo.org/records/92', 'reserve', 5),
            # github: not run yet
            ('github', '', 2019, 'https://github.com/g/one', 'primary', 8),
            ('github', '', 2018, 'https://github.com/g/res', 'reserve', 6),
        ]
        with open(self.dir / 'draw.csv', 'w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(FIELDS)
            writer.writerows(rows)
        self.db = self.dir / 'merged.sqlite'
        db = sqlite3.connect(self.db)
        db.executescript("""
            CREATE TABLE repositories (id INTEGER PRIMARY KEY, platform TEXT, repository TEXT);
            CREATE TABLE repository_runs (id INTEGER PRIMARY KEY, repository_id INTEGER, run_status TEXT);
            CREATE TABLE notebooks (id INTEGER PRIMARY KEY, repository_id INTEGER, name TEXT, language TEXT);""")
        runs = [('codeberg', 'A/ok1', 'SUCCESS', 'python'), ('codeberg', 'a/ok2', 'ENV_RUN_FAIL', 'python'),
                ('codeberg', 'a/gone', 'GIT_CLONE_FAILED', None), ('codeberg', 'a/nonb', 'SUCCESS', 'R'),
                ('codeberg', 'a/kernel', 'KERNEL_START_FAIL', 'python'), ('zenodo', '1', 'SUCCESS', 'python'),
                ('zenodo', '2', 'NO_NOTEBOOKS', None)]
        for i, (platform, repo, status, language) in enumerate(runs, 1):
            db.execute('INSERT INTO repositories VALUES (?,?,?)', (i, platform, repo))
            db.execute('INSERT INTO repository_runs (repository_id, run_status) VALUES (?,?)', (i, status))
            if language:
                db.execute('INSERT INTO notebooks (repository_id, name, language) VALUES (?,?,?)', (i, 'x.ipynb', language))
        db.commit()
        db.close()

    def run_topup(self, target):
        out = subprocess.run([sys.executable, str(SCRIPT), '--draw-dir', str(self.dir), '--db', str(self.db),
                              '--target', str(target)], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def cohort(self, name):
        with open(self.dir / name) as stream:
            return [r['url'] for r in csv.DictReader(stream)]

    def test_replacements_by_stratum_and_year(self):
        summary = self.run_topup(target=4)
        cb = summary['platforms']['codeberg']
        self.assertEqual(cb['outcomes'], {'analysable': 2, 'not_obtainable': 1, 'ineligible': 1, 'pipeline_error': 1})
        self.assertEqual(cb['shortfall'], 1)                                   # 4 - (2 + 0 + 1)
        with open(self.dir / 'topup-log.csv') as stream:
            log = list(csv.DictReader(stream))
        replaced = {r['url']: (r['replaced_by'], r['match']) for r in log if r['action'] == 'replaced'}
        self.assertEqual(replaced['https://codeberg.org/a/gone'],
                         ('https://codeberg.org/r/r2020', 'same stratum and year'))
        self.assertEqual(replaced['https://zenodo.org/records/2'],
                         ('https://zenodo.org/records/92', 'same stratum'))      # Z2 kept, although Z1 comes first
        self.assertIn('https://codeberg.org/a/kernel', self.cohort('cohort-rerun-1.csv'))
        self.assertEqual(summary['platforms']['github']['outcomes'], {'not_run': 1})
        self.assertEqual(summary['platforms']['github']['replaced'], 0)
        self.assertIn('https://github.com/g/one', self.cohort('cohort-rerun-1.csv'))      # never ran -> re-run

    def test_second_topup_never_reuses_the_reserve(self):
        self.run_topup(target=4)
        summary = self.run_topup(target=5)                                     # nothing new has run
        self.assertEqual(summary['topup'], 2)
        new = self.cohort('cohort-topup-2.csv')
        self.assertNotIn('https://codeberg.org/r/r2020', new)
        self.assertIn('https://codeberg.org/r/r2021', new)                     # replaces a/nonb (2021)

    def test_reruns_are_capped(self):
        for _ in range(3):
            self.run_topup(target=4)
        log = list(csv.DictReader(open(self.dir / 'topup-log.csv')))
        self.assertEqual(sum(r['url'] == 'https://codeberg.org/a/kernel' and r['action'] == 're-run' for r in log), 2)

    def test_infrastructure_failures_are_rerun_not_replaced_or_counted(self):
        db = sqlite3.connect(self.db)
        for i, (status, language) in enumerate([('METADATA_FETCH_FAILED', None), ('PIPELINE_INTERRUPTED', 'python'),
                                                ('REPO_DIR_NOT_CLEAN', None), ('VENV_CREATE_FAIL', 'python')], 100):
            db.execute('INSERT INTO repositories VALUES (?,?,?)', (i, 'codeberg', f'infra/r{i}'))
            db.execute('INSERT INTO repository_runs (repository_id, run_status) VALUES (?,?)', (i, status))
            if language:
                db.execute('INSERT INTO notebooks (repository_id, name, language) VALUES (?,?,?)', (i, 'x.ipynb', language))
        db.commit()
        with open(self.dir / 'draw.csv', 'a', newline='') as stream:
            csv.writer(stream).writerows(('codeberg', '', 2020, f'https://codeberg.org/infra/r{i}', 'primary', 50 + i)
                                         for i in range(100, 104))
        summary = self.run_topup(target=4)
        self.assertEqual(summary['platforms']['codeberg']['outcomes']['pipeline_error'], 5)     # kernel + these 4
        rerun = self.cohort('cohort-rerun-1.csv')
        self.assertTrue(all(f'https://codeberg.org/infra/r{i}' in rerun for i in range(100, 104)))
        log = list(csv.DictReader(open(self.dir / 'topup-log.csv')))
        self.assertFalse(any(r['url'].startswith('https://codeberg.org/infra/') and r['action'] == 'replaced' for r in log))

    def test_frozen_database_is_refused(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--draw-dir', str(self.dir), '--db',
                                 str(ROOT / 'data' / 'output' / 'db' / 'db.sqlite')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main(verbosity=1)
