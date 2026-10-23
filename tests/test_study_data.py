#!/usr/bin/env python3
"""Website data layer for full-pipeline releases (pipeline/study_data.py, workspace_data.read_pipeline_workspace).

A small synthetic study database + the frozen thesis database (read only):
  - releases from NBF_WEB_RELEASES; ids '<release>.<id>' vs thesis 'db-<id>'
  - workspace summaries (comparison v2, error category, RRS/ROS/RCS, licence files, version 'main @ abc1234')
  - repository and notebook detail; licence match objects become strings; missing tables are optional
  - the thesis workspace is unchanged (same repositories and ids) and the frozen database is untouched
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipeline'))
FROZEN = ROOT / 'data/output/db/db.sqlite'
FROZEN_SHA = '50660f38bf9f4aac8ec583049582dfd0ab395ad93856d94f0a4b12d32c8a2546'

SCHEMA = """
CREATE TABLE repositories (id INTEGER PRIMARY KEY, repository TEXT, platform TEXT, notebooks_count INTEGER);
CREATE TABLE repository_metadata (id INTEGER PRIMARY KEY, repo_id INTEGER, title TEXT, description TEXT, authors TEXT, license TEXT, doi TEXT, keywords TEXT);
CREATE TABLE repository_runs (id INTEGER PRIMARY KEY, repository_id INTEGER, run_status TEXT, error_message TEXT, started_at TEXT, finished_at TEXT, duration_seconds REAL, session_id INTEGER);
CREATE TABLE notebooks (id INTEGER PRIMARY KEY, repository_id INTEGER, name TEXT, language TEXT);
CREATE TABLE notebook_executions (id INTEGER PRIMARY KEY, repository_run_id INTEGER, repository_id INTEGER, notebook_id INTEGER, notebook_name TEXT,
  execution_status TEXT, execution_duration REAL, total_code_cells INTEGER, executed_cells INTEGER, error_type TEXT, error_category TEXT,
  error_message TEXT, error_cell_index INTEGER, error_count INTEGER, created_at TEXT, dependency_cause TEXT, error_category_by_type TEXT, error_evidence TEXT);
CREATE TABLE notebook_classifications (id INTEGER PRIMARY KEY, notebook_id INTEGER, rule_category TEXT, llm_category TEXT, final_category TEXT,
  agreement_status TEXT, needs_human_review INTEGER, rule_confidence REAL, llm_confidence REAL, created_at TEXT);
CREATE TABLE notebook_reproducibility_metrics (id INTEGER PRIMARY KEY, repository_run_id INTEGER, notebook_execution_id INTEGER, notebook_id INTEGER,
  reproducibility_score REAL, comparison_version INTEGER, score_strict REAL, score_normalised REAL, score_text_data REAL, score_before_error REAL,
  cells_assessable INTEGER, cells_empty INTEGER, cells_not_stored INTEGER, cells_identical INTEGER, cells_different INTEGER);
CREATE TABLE notebook_cell_comparisons (id INTEGER PRIMARY KEY, repository_run_id INTEGER, notebook_execution_id INTEGER, notebook_id INTEGER,
  cell_index INTEGER, outcome TEXT, rules_json TEXT, notes_json TEXT, stored_outputs INTEGER, executed_outputs INTEGER, error_name TEXT, source_changed INTEGER);
CREATE TABLE resource_readiness (id INTEGER PRIMARY KEY, repository_run_id INTEGER, rrs REAL, score_e REAL, score_a REAL, score_d REAL, score_c REAL, score_s REAL,
  rrs_v1_0 REAL, score_e_v1_0 REAL, score_a_v1_0 REAL, score_d_v1_0 REAL, score_c_v1_0 REAL, score_s_v1_0 REAL, status TEXT, rubric_name TEXT);
CREATE TABLE reproscore_outcome (id INTEGER PRIMARY KEY, repository_run_id INTEGER, ros REAL, rcs REAL, rcs_v1_0 REAL, install_success REAL, status TEXT);
CREATE TABLE resource_licences (id INTEGER PRIMARY KEY, repository_run_id INTEGER, licence_api TEXT, licence_file TEXT, matched_files_json TEXT, status TEXT);
CREATE TABLE resource_acquisitions (id INTEGER PRIMARY KEY, repository_run_id INTEGER, method TEXT, version_id TEXT, version_label TEXT, files_total INTEGER);
"""


def build_study_db(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany('INSERT INTO repositories VALUES (?,?,?,?)', [(1, 'o/r', 'codeberg', 1), (2, '123', 'zenodo', 0)])
    db.execute("INSERT INTO repository_metadata VALUES (1, 1, 'R', 'd', 'A; B', 'MIT', NULL, 'x; y')")
    db.executemany('INSERT INTO repository_runs VALUES (?,?,?,?,?,?,?,?)', [
        (1, 1, 'INTERRUPTED', 'job ended', '2026-10-01 10:00:00', None, None, None),
        (2, 1, 'SUCCESS', None, '2026-10-02 10:00:00', '2026-10-02 10:05:00', 300, None),
        (3, 2, 'NO_NOTEBOOKS', None, '2026-10-02 11:00:00', '2026-10-02 11:01:00', 60, None)])
    db.execute("INSERT INTO notebooks VALUES (7, 1, 'nb/a.ipynb', 'python')")
    db.execute("INSERT INTO notebook_executions (id, repository_run_id, repository_id, notebook_id, notebook_name, execution_status, "
               "total_code_cells, executed_cells, error_type, error_category, error_message, error_cell_index, created_at) "
               "VALUES (5, 2, 1, 7, 'nb/a.ipynb', 'SUCCESS_WITH_ERRORS', 3, 3, 'KeyError', 'DATA_ERROR', 'x', 2, '2026-10-02 10:04:00')")
    db.execute('INSERT INTO notebook_reproducibility_metrics VALUES (1, 2, 5, 7, 0.5, 2, 0.25, 0.75, 0.75, 1.0, 4, 1, 0, 1, 1)')
    db.executemany('INSERT INTO notebook_cell_comparisons VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', [
        (1, 2, 5, 7, 0, 'identical', '[]', '[]', 1, 1, None, 0), (2, 2, 5, 7, 1, 'error', '[]', '["KeyError"]', 1, 1, 'KeyError', 0)])
    db.execute("INSERT INTO resource_readiness VALUES (1, 2, 60, 90, 10, 50, 95, 70, 50, 80, 10, 40, 90, 60, 'ok', 'default')")
    db.execute("INSERT INTO reproscore_outcome VALUES (1, 2, 40, 50, 45, 1, 'ok')")
    db.execute("INSERT INTO resource_licences VALUES (1, 2, NULL, 'MIT', ?, 'ok')",
               (json.dumps([{'filename': 'LICENSE', 'matcher': 'exact', 'confidence': 100.0, 'licence': 'MIT'}]),))
    db.execute("INSERT INTO resource_acquisitions VALUES (1, 2, 'git clone --depth 1', ?, 'main', 3)", ('abc1234' + '0' * 33,))
    db.commit()
    db.close()


class StudyDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / 'study.sqlite'
        build_study_db(cls.db)
        os.environ['NBF_WEB_RELEASES'] = f'unit-test={cls.db};Bad Name=/x'
        os.environ.pop('NBF_WEB_DEFAULT_RELEASE', None)
        import study_data
        import workspace_data
        cls.study_data, cls.workspace_data = study_data, workspace_data

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        os.environ.pop('NBF_WEB_RELEASES', None)

    def test_releases(self):
        names = {r['name']: r for r in self.study_data.list_releases()}
        self.assertTrue(names['thesis-2026']['default'])
        self.assertTrue(names['unit-test']['available'])
        self.assertNotIn('Bad Name', names)

    def test_ids(self):
        parse = self.study_data.parse_id
        self.assertEqual(parse('db-12'), ('thesis-2026', 12))
        self.assertEqual(parse('unit-test.7'), ('unit-test', 7))
        for bad in ('12', 'nope.3', 'unit-test.x', 'db-'):
            with self.assertRaises(ValueError):
                parse(bad)

    def test_workspace_summaries(self):
        workspace = self.workspace_data.read_pipeline_workspace('unit-test')
        self.assertEqual(workspace['release'], 'unit-test')
        repos = {r['id']: r for r in workspace['repositories']}
        self.assertEqual(set(repos), {'unit-test.1', 'unit-test.2'})
        repo = repos['unit-test.1']
        self.assertEqual((repo['rrs'], repo['rrsV10'], repo['ros'], repo['rcs']), (60.0, 50.0, 40.0, 50.0))
        self.assertEqual(repo['licenceDetected'], 'MIT')
        self.assertEqual(repo['version'], 'main @ abc1234')
        self.assertEqual(repo['lastRunStatus'], 'SUCCESS')
        notebook = repo['notebooks'][0]
        self.assertEqual(notebook['id'], 'unit-test.7')
        self.assertEqual((notebook['scoreNormalised'], notebook['scoreStrict'], notebook['errorCategory']), (0.75, 0.25, 'DATA_ERROR'))
        self.assertEqual(notebook['reproducibilityScore'], 0.5)

    def test_repository_detail(self):
        detail = self.study_data.repository_detail('unit-test.1')
        self.assertEqual([r['status'] for r in detail['runs']], ['SUCCESS', 'INTERRUPTED'])
        run = detail['runs'][0]
        self.assertEqual([v['version'] for v in run['readiness']['versions']], ['1.1', '1.0'])
        self.assertEqual(run['readiness']['versions'][0]['dimensions']['c'], 95.0)
        self.assertEqual(run['outcome']['ros'], 40.0)
        self.assertEqual(run['licence']['files'], ['LICENSE (MIT, exact, 100%)'])
        self.assertEqual(run['acquisition']['versionLabel'], 'main')
        self.assertEqual((run['environment'], run['packages'], run['snapshots'], run['stages']), (None, [], [], []))
        self.assertNotIn('run_environments', detail['sections'])
        self.assertIsNone(detail['runs'][1]['readiness'])
        with self.assertRaises(LookupError):
            self.study_data.repository_detail('unit-test.99')

    def test_notebook_detail(self):
        detail = self.study_data.notebook_detail('unit-test.7')
        execution = detail['executions'][0]
        self.assertEqual(execution['metrics']['normalised'], 0.75)
        self.assertEqual(execution['metrics']['counts']['assessable'], 4.0)
        self.assertEqual([c['outcome'] for c in execution['cells']], ['identical', 'error'])
        self.assertEqual(execution['cells'][1]['notes'], ['KeyError'])
        self.assertEqual(execution['error']['category'], 'DATA_ERROR')
        self.assertEqual((execution['usage'], execution['imports']), (None, []))

    @unittest.skipUnless(FROZEN.is_file(), 'frozen thesis database not present')
    def test_thesis_workspace_unchanged_and_frozen_db_untouched(self):
        workspace = self.workspace_data.read_pipeline_workspace()
        self.assertEqual(workspace['release'], 'thesis-2026')
        ids = [r['id'] for r in workspace['repositories']]
        self.assertTrue(ids and all(i.startswith('db-') or not i.startswith('unit-test') for i in ids))
        detail = self.study_data.repository_detail(next(i for i in ids if i.startswith('db-')))
        self.assertEqual(detail['sections'], [])
        self.assertEqual(hashlib.sha256(FROZEN.read_bytes()).hexdigest(), FROZEN_SHA)


if __name__ == '__main__':
    unittest.main()
