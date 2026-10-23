#!/usr/bin/env python3
"""Integration test for src/install_env.py with a real venv and pip (needs network; skipped without).
Run: python3 tests/test_install_env.py"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
import dependencies as dep  # noqa: E402


def online():
    try:
        urllib.request.urlopen('https://pypi.org/simple/six/', timeout=10)
        return True
    except OSError:
        return False


class CutoffTest(unittest.TestCase):
    """Time travel: which upload date pip may use (offline)."""

    def test_end_of_resource_day(self):
        import install_env as ie
        self.assertEqual(ie.compute_cutoff('2023-01-10T08:00:00+02:00', '3.11.15', '3.11')[0], '2023-01-10T23:59:59Z')
        self.assertEqual(ie.compute_cutoff('2024-06-01', '3.10.20', '3.10')[0], '2024-06-01T23:59:59Z')

    def test_raised_interpreter_moves_cutoff_after_its_release(self):
        import install_env as ie
        cutoff, reason = ie.compute_cutoff('2018-05-03', '3.8.20', '3.6')
        self.assertEqual(cutoff, '2020-10-13T23:59:59Z')
        self.assertIn('instead of the requested 3.6', reason)
        cutoff, reason = ie.compute_cutoff('2020-02-01', '3.11.15', '3.11')       # metadata newer than the commit
        self.assertEqual(cutoff, '2023-10-24T23:59:59Z')
        self.assertEqual(ie.compute_cutoff('2023-12-01', '3.11.15', '3.11')[0], '2023-12-01T23:59:59Z')  # no move

    def test_no_date_means_no_cutoff(self):
        import install_env as ie
        self.assertIsNone(ie.compute_cutoff(None, '3.10', '3.10')[0])
        self.assertIsNone(ie.compute_cutoff('not a date', '3.10', '3.10')[0])


@unittest.skipUnless(online(), 'no network access to PyPI')
class InstallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        root = cls.tmp / 'resource'
        root.mkdir()
        (root / 'requirements.txt').write_text('six==1.16.0\nnbf-this-package-does-not-exist==1.0\n')
        (root / 'a.ipynb').write_text(json.dumps({'cells': [{'cell_type': 'code', 'metadata': {}, 'outputs': [],
            'source': 'import six\nimport yaml\nimport tomli\nimport json\n'}], 'metadata': {}, 'nbformat': 4,
            'nbformat_minor': 5}))
        subprocess.run([sys.executable, '-m', 'venv', str(cls.tmp / 'venv')], check=True)
        cls.python = str(cls.tmp / 'venv' / 'bin' / 'python')
        plan = dep.analyse(root, ['a.ipynb'], cls.python)
        (cls.tmp / 'plan.json').write_text(json.dumps(plan))
        cls.before = sorted(p.name for p in root.iterdir())
        cls.root = root
        subprocess.run([sys.executable, str(SRC / 'install_env.py'), '--python', cls.python, '--plan',
                        str(cls.tmp / 'plan.json'), '--out-dir', str(cls.tmp / 'out'), '--timeout', '600'], check=True)
        cls.report = json.loads((cls.tmp / 'out' / 'install-report.json').read_text())
        cls.packages = {p['name']: p for p in cls.report['packages']}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_unavailable_requirement_is_set_aside_and_rest_installed_jointly(self):
        six = self.packages['six']
        self.assertEqual((six['status'], six['installed_version'], six['origin']), ('installed', '1.16.0', 'declared'))
        self.assertTrue(six['method'].startswith('joint (attempt 2'), six['method'])
        missing = self.packages['nbf-this-package-does-not-exist']
        self.assertEqual((missing['status'], missing['method']), ('failed', 'set aside from the joint install'))
        self.assertTrue(missing['error'])

    def test_extras_are_mapped_and_recorded(self):
        pyyaml = self.packages['pyyaml']
        self.assertEqual((pyyaml['origin'], pyyaml['status'], pyyaml['imports']), ('inferred', 'installed', ['yaml']))
        self.assertEqual(self.packages['tomli']['origin'], 'inferred')
        self.assertNotIn('json', self.packages)                       # stdlib is never installed
        self.assertEqual(self.report['summary']['modules_missing'], [])

    def test_kernel_freeze_and_files(self):
        self.assertTrue(self.report['kernel']['version'])
        self.assertIn('six==1.16.0', self.report['freeze'])
        for name in ('declared-requirements.attempt1.txt', 'extra-requirements.txt', 'pip-freeze.txt', 'pip-check.txt', 'install.log'):
            self.assertTrue((self.tmp / 'out' / name).is_file(), name)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), self.before)   # resource untouched


if __name__ == '__main__':
    unittest.main(verbosity=1)
