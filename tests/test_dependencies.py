#!/usr/bin/env python3
"""Offline tests for src/dependencies.py. Run: python3 tests/test_dependencies.py"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import dependencies as dep  # noqa: E402


def notebook(*cells):
    return json.dumps({'cells': [{'cell_type': 'code', 'source': c, 'metadata': {}, 'outputs': []} for c in cells],
                       'metadata': {}, 'nbformat': 4, 'nbformat_minor': 5})


class ImportsTest(unittest.TestCase):
    def test_forms_the_thesis_rule_got_wrong(self):
        names, py2 = dep.imports_in_cell('import os, sys\nimport numpy as np, pandas as pd\n'
                                         'if True:\n    import torch\nfrom sklearn.model_selection import x\n'
                                         'from . import local\n%matplotlib inline\n!ls\n')
        self.assertEqual(names, {'os', 'sys', 'numpy', 'pandas', 'torch', 'sklearn.model_selection'})
        self.assertFalse(py2)

    def test_python2_cell_falls_back_and_is_flagged(self):
        names, py2 = dep.imports_in_cell('import urllib2, json\nprint "hello"\n')
        self.assertEqual(names, {'urllib2', 'json'})
        self.assertTrue(py2)

    def test_pip_magics(self):
        self.assertEqual(dep.pip_magic_specs('!pip install -q pandas==1.0.1 "seaborn>=0.9" -r req.txt $x\n'
                                             '%pip install --upgrade rasterio[s3]\n'),
                         ['pandas==1.0.1', 'seaborn>=0.9', 'rasterio[s3]'])

    def test_mapping(self):
        self.assertEqual(dep.map_import('sklearn.cross_validation')[:2], ('scikit-learn', 'override'))
        self.assertEqual(dep.map_import('mpl_toolkits.basemap')[:2], ('basemap', 'override'))
        self.assertEqual(dep.map_import('mpl_toolkits.mplot3d')[:2], ('matplotlib', 'override'))
        self.assertEqual(dep.map_import('google.colab')[0], None)
        self.assertEqual(dep.map_import('bs4')[:2], ('beautifulsoup4', 'pipreqs'))
        self.assertEqual(dep.map_import('somepkg')[:2], ('somepkg', 'same name'))

    def test_conda_specs(self):
        self.assertEqual(dep.conda_spec('pytorch=1.8'), 'torch==1.8.*')
        self.assertEqual(dep.conda_spec('numpy=1.21.5=py39h_0'), 'numpy==1.21.5')
        self.assertEqual(dep.conda_spec('conda-forge::pandas>=1.3'), 'pandas>=1.3')
        self.assertEqual(dep.conda_spec('r-base=4.1'), '')
        self.assertEqual(dep.conda_spec('python=3.8'), '')

    def test_poetry_specs(self):
        self.assertEqual(dep.poetry_spec('pandas', '^1.3'), 'pandas>=1.3,<2.0')
        self.assertEqual(dep.poetry_spec('x', {'version': '~0.4.1'}), 'x>=0.4.1,<0.5')
        self.assertEqual(dep.poetry_spec('y', '*'), 'y')


class AnalyseTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def test_full_resource(self):
        self.write('notebooks/analysis.ipynb', notebook(
            'import numpy as np, statistics\nimport helpers\nfrom mylib.core import f\nimport cv2\n'
            'import google.colab\nimport urllib2\n', '!pip install requests==2.25.1'))
        self.write('notebooks/helpers.py', 'x = 1\n')
        self.write('src/mylib/core.py', 'def f(): pass\n')
        self.write('requirements.txt', 'numpy==1.21.0\n-r extra.txt\n-e .\n--index-url https://example.org/simple\n')
        self.write('extra.txt', 'scipy>=1.5  # comment\n')
        self.write('requirements-dev.txt', 'pytest\n')
        self.write('environment.yml', 'name: x\ndependencies:\n  - python=3.8\n  - pytorch=1.8\n  - r-base\n'
                                      '  - pip\n  - pip:\n    - kaggle==1.5.12\n')
        self.write('pyproject.toml', '[tool.poetry.dependencies]\npython = "^3.8"\nmylib = "^1.0"\npandas = "^1.3"\n')
        result = dep.analyse(self.root, ['notebooks/analysis.ipynb'])

        sources = {s['path']: s for s in result['sources']}
        self.assertTrue(sources['requirements.txt']['used'])
        self.assertFalse(sources['requirements-dev.txt']['used'])
        self.assertIn('-e .: the resource itself / local path, used from the resource folder',
                      sources['requirements.txt']['notes'])
        plan = {r['name']: r for r in result['requirements']}
        self.assertEqual(plan['numpy']['spec'], 'numpy==1.21.0')
        self.assertEqual(plan['scipy']['spec'], 'scipy>=1.5')                  # via -r include
        self.assertEqual(plan['torch']['spec'], 'torch==1.8.*')                # conda -> PyPI
        self.assertEqual(plan['kaggle']['spec'], 'kaggle==1.5.12')             # pip section
        self.assertEqual(plan['pandas']['spec'], 'pandas>=1.3,<2.0')           # poetry
        self.assertEqual(plan['requests']['origin_kind'], '%pip')
        self.assertEqual(plan['mylib']['decision'], 'skip')                    # own package (src/)
        self.assertNotIn('pytest', plan)
        self.assertEqual(result['pip_options'], ['--index-url https://example.org/simple'])

        imports = {i['module']: i for i in result['imports']}
        self.assertEqual(imports['statistics']['classification'], 'stdlib')
        self.assertEqual((imports['helpers']['classification'], imports['helpers']['local_path']), ('local', 'notebooks'))
        self.assertEqual((imports['mylib.core']['classification'], imports['mylib.core']['local_path']), ('local', 'src'))
        self.assertEqual(result['import_path'], ['src'])
        self.assertEqual((imports['cv2']['distribution'], imports['cv2']['mapping']), ('opencv-python', 'override'))
        self.assertEqual(imports['google.colab']['classification'], 'not-installable')
        self.assertEqual(imports['urllib2']['classification'], 'python2-stdlib')
        self.assertEqual(imports['numpy']['cells'], [0])

    def test_nothing_is_written_into_the_resource(self):
        self.write('a.ipynb', notebook('import pandas'))
        self.write('requirements.txt', 'pandas\n')
        before = sorted((p, p.stat().st_mtime_ns) for p in self.root.rglob('*'))
        dep.analyse(self.root, ['a.ipynb'])
        self.assertEqual(before, sorted((p, p.stat().st_mtime_ns) for p in self.root.rglob('*')))

    def test_conflicting_pins_are_recorded(self):
        self.write('requirements.txt', 'pandas==1.0\n')
        self.write('Pipfile', '[packages]\npandas = "==1.3"\n')
        self.write('a.ipynb', notebook('import pandas'))
        plan = {r['name']: r for r in dep.analyse(self.root, ['a.ipynb'])['requirements']}
        self.assertEqual(plan['pandas']['spec'], 'pandas==1.0')
        self.assertEqual(plan['pandas']['conflicts'], ['Pipfile: pandas==1.3'])


if __name__ == '__main__':
    unittest.main(verbosity=1)
