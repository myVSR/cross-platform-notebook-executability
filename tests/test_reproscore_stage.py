#!/usr/bin/env python3
"""Tests for src/reproscore_stage.py with the ReproScore copy in third_party/reproscore.
Run: python3 tests/test_reproscore_stage.py"""
import json
import os
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
import reproscore_stage as rs  # noqa: E402

INSTALLED = (rs.tool_dir() / 'src' / 'scoring' / 'rrs.py').is_file()


def database(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE repositories (id INTEGER PRIMARY KEY, platform TEXT, repository TEXT);
        CREATE TABLE repository_runs (id INTEGER PRIMARY KEY, repository_id INTEGER, run_status TEXT);
        CREATE TABLE run_stage_usage (id INTEGER PRIMARY KEY, repository_run_id INTEGER, stage TEXT, exit_status INTEGER);
        CREATE TABLE run_packages (id INTEGER PRIMARY KEY, repository_run_id INTEGER, name TEXT, decision TEXT, status TEXT);
        CREATE TABLE notebook_executions (id INTEGER PRIMARY KEY, repository_run_id INTEGER, execution_status TEXT,
                                          error_category TEXT);
        CREATE TABLE notebook_reproducibility_metrics (id INTEGER PRIMARY KEY, repository_run_id INTEGER,
            comparison_version INTEGER, cells_assessable INTEGER, score_normalised REAL);
        INSERT INTO notebook_reproducibility_metrics (repository_run_id, comparison_version, cells_assessable,
            score_normalised) VALUES (1, 2, 4, 0.5), (1, 2, 2, 1.0), (1, 2, 0, NULL), (1, 1, 5, 0.0), (2, 2, 0, NULL);
        INSERT INTO repositories VALUES (1, 'codeberg', 'o/r'), (2, 'zenodo', '1');
        INSERT INTO repository_runs VALUES (1, 1, 'SUCCESS'), (2, 2, 'SUCCESS'), (3, 2, 'NO_NOTEBOOKS');
        INSERT INTO run_stage_usage (repository_run_id, stage, exit_status) VALUES (1, 'environment', 0), (2, 'environment', 0);
        INSERT INTO run_packages (repository_run_id, name, decision, status) VALUES
            (1, 'a', 'install', 'installed'), (1, 'b', 'install', 'failed'), (1, 'c', 'skip', 'skipped'),
            (2, 'a', 'install', 'installed');
        INSERT INTO notebook_executions (repository_run_id, execution_status, error_category) VALUES
            (1, 'SUCCESS', NULL), (1, 'SUCCESS_WITH_ERRORS', 'DEPENDENCY_ERROR'), (1, 'SUCCESS_WITH_ERRORS', 'FILE_ERROR'),
            (1, 'SUCCESS', NULL), (2, 'SUCCESS', NULL);
    """)
    rs.connect(path).close()
    return connection


class MappingTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        self.connection = database(self.folder / 'db.sqlite')

    def test_components_from_the_run_records(self):
        fields, executed, compared = rs.evidence_from_db(self.connection, 1)
        self.assertEqual((executed, compared), (4, 2))
        self.assertEqual(fields['output_reproduction'], 0.75)          # v2 notebooks with assessable cells only
        self.assertEqual(fields['install_success'], False)            # one declared package failed
        self.assertEqual(fields['notebook_exec_rate'], 0.5)
        self.assertEqual(fields['import_success_rate'], 0.75)
        self.assertIsNone(fields['execution_success'])
        self.assertIsNone(fields['output_determinism'])
        fields, _, compared = rs.evidence_from_db(self.connection, 2)
        self.assertEqual((fields['install_success'], fields['notebook_exec_rate']), (True, 1.0))
        self.assertEqual((fields['output_reproduction'], compared), (None, 0))   # no stored outputs: R not measured

    def test_nothing_measured_without_environment_and_notebooks(self):
        fields, executed, _ = rs.evidence_from_db(self.connection, 3)
        self.assertEqual(executed, 0)
        self.assertTrue(all(v is None for v in fields.values()))

    def test_symlink_path_is_clean_and_removed(self):
        repo = self.folder / '.hidden' / 'build' / 'repo'
        repo.mkdir(parents=True)
        link, temporary = rs.scoring_path(repo)
        self.assertTrue(rs.clean_prefix(link.parent))
        self.assertEqual(link.resolve(), repo.resolve())
        shutil.rmtree(temporary)


@unittest.skipUnless(INSTALLED, 'ReproScore not found (third_party/reproscore or REPROSCORE_DIR)')
class ScoringTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        self.db = self.folder / 'db.sqlite'
        database(self.db).close()
        self.repo = self.folder / 'repo'
        (self.repo / 'figures').mkdir(parents=True)
        (self.repo / 'figures' / 'plot.png').write_bytes(b'png')
        (self.repo / 'requirements.txt').write_text('numpy==1.26.4\npandas==2.2.2\n')
        (self.repo / 'README.md').write_text('# Demo\n\n## Installation\n\npip install -r requirements.txt\n')
        (self.repo / 'analysis.ipynb').write_text(json.dumps({'cells': [
            {'cell_type': 'markdown', 'metadata': {}, 'source': '# Load the data'},
            {'cell_type': 'code', 'metadata': {}, 'outputs': [], 'execution_count': None,
             'source': 'import numpy as np\nnp.random.seed(1)\nx = np.random.rand(3)'}],
            'metadata': {}, 'nbformat': 4, 'nbformat_minor': 5}))

    def run_stage(self, *args, cwd=None):
        env = dict(os.environ, PYTHONPATH=str(ROOT))                   # ROOT/src must not shadow reproscore's src
        out = subprocess.run([sys.executable, str(ROOT / 'src' / 'reproscore_stage.py'), '--db', str(self.db), *args],
                             capture_output=True, text=True, check=True, env=env, cwd=cwd)
        return json.loads(out.stdout)

    def test_rrs_outcome_and_composite(self):
        readiness = self.run_stage('rrs', '--run-id', '1', '--repo-dir', str(self.repo))
        self.assertEqual(readiness['status'], 'ok')
        self.assertTrue(0 <= readiness['rrs'] <= 100)
        outcome = self.run_stage('outcome', '--run-id', '1')
        self.assertEqual(outcome['status'], 'ok')
        self.assertEqual((outcome['install_success'], outcome['output_reproduction']), (0, 0.75))
        self.assertIsNotNone(outcome['rcs'])
        db = sqlite3.connect(self.db)
        row = db.execute('SELECT tool_commit, rubric_name, rubric_sha256, submetrics_json, tool_commit_v1_0, rrs_v1_0, '
                         'status_v1_0, submetrics_v1_0_json FROM resource_readiness').fetchone()
        self.assertTrue(row[0].startswith('1.1.0'))
        self.assertEqual(row[1], 'default')
        self.assertEqual(len(json.loads(row[3])), 26)
        self.assertTrue(row[4].endswith('+notebookfair.3'))                    # paper version next to it
        self.assertEqual(row[6], 'ok')
        self.assertIsNotNone(row[5])
        self.assertEqual(len(json.loads(row[7])), 26)
        row = db.execute('SELECT rubric_name, ros_components_json, mapping_version FROM reproscore_outcome').fetchone()
        self.assertEqual((row[0], row[2]), ('notebookfair-v1', 2))
        self.assertEqual(json.loads(row[1]), {'I': 0.0, 'N': 50.0, 'E': 75.0, 'R': 75.0})
        # default rubric: same evidence, R ignored; I 0.30, N 0.10, E 0.10 -> (0 + 5 + 7.5) / 0.5 = 25
        row = db.execute('SELECT ros_default, ros_default_components_json, rcs_default, rcs FROM reproscore_outcome').fetchone()
        self.assertEqual(row[0], 25.0)
        self.assertEqual(json.loads(row[1]), {'I': 0.0, 'N': 50.0, 'E': 75.0})
        self.assertIsNotNone(row[2])
        self.assertNotEqual(row[2], row[3])
        self.assertIsNotNone(db.execute('SELECT rcs_v1_0 FROM reproscore_outcome').fetchone()[0])

    def test_score_does_not_depend_on_how_the_path_is_given(self):
        absolute = self.run_stage('rrs', '--run-id', '1', '--repo-dir', str(self.repo))['rrs']
        relative = self.run_stage('rrs', '--run-id', '1', '--repo-dir', '../repo', cwd=str(self.repo))['rrs']
        self.assertIsNotNone(absolute)
        self.assertEqual(absolute, relative)

    def test_notebooks_without_code_cells_are_scored(self):
        # upstream 1d7a559 raised TypeError here (inline_explanation_density); notebookfair patch 2
        empty = self.folder / 'empty'
        empty.mkdir()
        (empty / 'notes.ipynb').write_text(json.dumps({'cells': [], 'metadata': {}, 'nbformat': 4, 'nbformat_minor': 5}))
        result = self.run_stage('rrs', '--run-id', '1', '--repo-dir', str(empty))
        self.assertEqual(result['status'], 'ok')

    def test_baseline_can_be_switched_off(self):
        env = dict(os.environ, REPROSCORE_BASELINE_DIR='')
        out = subprocess.run([sys.executable, str(ROOT / 'src' / 'reproscore_stage.py'), '--db', str(self.db), 'rrs',
                              '--run-id', '1', '--repo-dir', str(self.repo)], capture_output=True, text=True, check=True, env=env)
        result = json.loads(out.stdout)
        self.assertEqual(result['status'], 'ok')
        self.assertIsNone(result['rrs_v1_0'])

    def test_versions_differ_where_1_1_fixes_apply(self):
        (self.repo / 'README.md').rename(self.repo / 'readme.md')            # A1: 1.0 does not see it
        result = self.run_stage('rrs', '--run-id', '1', '--repo-dir', str(self.repo))
        self.assertGreater(result['rrs'], result['rrs_v1_0'])

    def test_folders_above_the_repository_do_not_count(self):
        # notebookfair patch 1, tested without the symlink workaround: score the folder directly
        sys.path.insert(0, str(rs.tool_dir()))
        from src.scoring.rrs import RRSScorer
        nested = self.folder / '.cache' / 'build' / 'repo'
        shutil.copytree(self.repo, nested)
        self.assertEqual(RRSScorer().score(str(nested)).rrs, RRSScorer().score(str(self.repo)).rrs)
        sys.path.remove(str(rs.tool_dir()))

    def test_merge_copies_the_scores(self):
        self.run_stage('rrs', '--run-id', '1', '--repo-dir', str(self.repo))
        self.run_stage('outcome', '--run-id', '1')
        target = self.folder / 'merged.sqlite'
        subprocess.run([sys.executable, str(ROOT / 'pipeline' / 'merge_task_dbs.py'), '--target', str(target),
                        str(self.db)], capture_output=True, text=True, check=True)
        merged = sqlite3.connect(target)
        self.assertEqual(merged.execute('SELECT COUNT(*) FROM resource_readiness').fetchone()[0], 1)
        self.assertEqual(merged.execute('SELECT COUNT(*) FROM reproscore_outcome').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main(verbosity=1)
