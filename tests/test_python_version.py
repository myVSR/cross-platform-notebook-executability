#!/usr/bin/env python3
"""Offline tests for src/python_version.py. Run: python3 tests/test_python_version.py"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import python_version as pv  # noqa: E402


def notebook(version=None, kernel='python3'):
    metadata = {'kernelspec': {'name': kernel}}
    if version:
        metadata['language_info'] = {'name': 'python', 'version': version}
    return json.dumps({'cells': [], 'metadata': metadata, 'nbformat': 4, 'nbformat_minor': 5})


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ('PYTHON_DEFAULT_VERSION', 'PYTHON_MIN_SUPPORTED', 'PYTHON_MAX_SUPPORTED'):
            os.environ.pop(name, None)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def decide(self, notebooks=()):
        return pv.decide(self.root, notebooks)

    def test_no_evidence_gives_default(self):
        d = self.decide()
        self.assertEqual((d['use'], d['source']), ('3.10', 'default'))

    def test_lower_bound_does_not_become_the_version(self):
        # GarminDB: python_requires='>=3.0' used to request Python 3.0
        self.write('setup.py', "setup(name='x', python_requires='>=3.0')")
        d = self.decide()
        self.assertEqual((d['use'], d['source']), ('3.10', 'default (allowed by constraints)'))

    def test_constraint_excluding_default_picks_nearest_allowed(self):
        self.write('pyproject.toml', '[project]\nrequires-python = ">=3.6,<3.9"\n')
        self.assertEqual(self.decide()['use'], '3.8')
        self.write('pyproject.toml', '[tool.poetry.dependencies]\npython = "^3.11"\n')
        self.assertEqual(self.decide()['use'], '3.11')

    def test_virtualenv_name_in_python_version_is_rejected(self):
        self.write('.python-version', 'mlexpert\n')
        d = self.decide()
        self.assertEqual(d['use'], '3.10')
        self.assertFalse(d['evidence'][0]['accepted'])

    def test_runtime_txt_exact_patch_is_kept(self):
        self.write('binder/runtime.txt', 'python-3.9.7\n')
        d = self.decide()
        self.assertEqual((d['use'], d['source']), ('3.9.7', 'binder/runtime.txt'))

    def test_binder_runtime_beats_root_and_python_version(self):
        self.write('runtime.txt', 'python-3.11\n')
        self.write('.python-version', '3.12.1\n')
        self.write('binder/runtime.txt', 'python-3.9\n')
        self.assertEqual(self.decide()['use'], '3.9')

    def test_environment_yml_and_pipfile(self):
        self.write('environment.yml', 'name: x\ndependencies:\n  - numpy\n  - python=3.9\n')
        self.assertEqual(self.decide()['source'], 'environment.yml')
        shutil.rmtree(self.root); self.root.mkdir()
        self.write('Pipfile', '[requires]\npython_version = "3.11"\n')
        self.assertEqual(self.decide()['use'], '3.11')

    def test_notebook_metadata_used_when_no_pin(self):
        for i, v in enumerate(['3.8.5', '3.8.10', '3.11.2']):
            self.write(f'n{i}.ipynb', notebook(v))
        d = self.decide(['n0.ipynb', 'n1.ipynb', 'n2.ipynb'])
        self.assertEqual((d['use'], d['source']), ('3.8', 'notebook metadata'))
        self.assertEqual(d['notebook_versions'], {'3.8': 2, '3.11': 1})

    def test_notebook_version_outside_constraints_is_not_used(self):
        self.write('a.ipynb', notebook('3.7.3'))
        self.write('setup.cfg', '[options]\npython_requires = >=3.9\n')
        self.assertEqual(self.decide(['a.ipynb'])['source'], 'default (allowed by constraints)')

    def test_old_and_python2_versions_are_mapped_and_flagged(self):
        self.write('a.ipynb', notebook('3.5.2'))
        d = self.decide(['a.ipynb'])
        self.assertEqual((d['requested'], d['use']), ('3.5', '3.8'))
        self.assertTrue(d['notes'])
        self.write('b.ipynb', notebook(None, kernel='python2'))
        self.write('c.ipynb', notebook('2.7.18'))
        d = self.decide(['b.ipynb', 'c.ipynb'])
        self.assertTrue(d['python2'])
        self.assertEqual(d['use'], '3.10')

    def test_non_python_notebooks_are_ignored(self):
        self.write('julia.ipynb', json.dumps({'metadata': {'kernelspec': {'name': 'julia-1.11', 'language': 'julia'},
                                                           'language_info': {'name': 'julia', 'version': '1.11.2'}}}))
        self.write('scala.ipynb', json.dumps({'metadata': {'language_info': {'name': 'scala', 'version': '2.12.10'}}}))
        d = self.decide(['julia.ipynb', 'scala.ipynb'])
        self.assertEqual((d['use'], d['notebook_versions'], d['python2']), ('3.10', {}, False))

    def test_pin_conflicting_with_constraint_is_skipped(self):
        self.write('.python-version', '3.7\n')
        self.write('setup.cfg', '[options]\npython_requires = >=3.9\n')
        d = self.decide()
        self.assertEqual(d['use'], '3.10')
        self.assertIn('conflicts', d['notes'][0])


class SpecifierTest(unittest.TestCase):
    def allowed(self, spec, version):
        return pv.allows_minor(pv.parse_specifier(spec), pv.parse_version(version))

    def test_operators(self):
        cases = [('>=3.7', '3.10', True), ('<3.9', '3.9', False), ('<=3.9', '3.9', True), ('>3.8', '3.8', True),
                 ('~=3.8', '3.12', True), ('~=3.8.1', '3.9', False), ('==3.8.*', '3.8', True),
                 ('!=3.9.*', '3.9', False), ('^3.8', '4.0', False), ('~3.8', '3.9', False),
                 ('>=3.6, <3.10', '3.10', False), ('>=3.6,!=3.7.*', '3.7', False)]
        for spec, version, expected in cases:
            with self.subTest(spec=spec, version=version):
                self.assertEqual(self.allowed(spec, version), expected)

    def test_garbage_is_not_a_specifier(self):
        self.assertIsNone(pv.parse_specifier('python3 please'))


if __name__ == '__main__':
    unittest.main(verbosity=1)
