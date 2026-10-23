#!/usr/bin/env python3
"""Tests for src/licence.py. The detector tests run licensee in Docker and are
skipped when the image notebookfair/licensee:10.1.0 is not available.
Run: python3 tests/test_licence.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import licence  # noqa: E402

MIT = """MIT License

Copyright (c) 2024 Someone

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


class SummariseTest(unittest.TestCase):
    def test_single_licence_file(self):
        result = licence.summarise({'matched_files': [
            {'filename': 'LICENSE', 'matcher': {'name': 'exact', 'confidence': 100}, 'matched_license': 'MIT'},
            {'filename': 'README.md', 'matcher': {'name': 'reference', 'confidence': 90}, 'matched_license': 'MIT'},
            {'filename': 'pyproject.toml', 'matcher': {'name': 'pyproject', 'confidence': 90}, 'matched_license': 'Apache-2.0'}]})
        self.assertEqual(result['licence_file'], 'MIT')
        self.assertEqual(result['licence_any'], ['Apache-2.0', 'MIT'])
        self.assertEqual([f['kind'] for f in result['matched_files']], ['licence file', 'readme', 'package'])

    def test_different_licence_files_give_noassertion(self):
        result = licence.summarise({'matched_files': [
            {'filename': 'BSD-3-Clause.txt', 'matcher': {'name': 'exact'}, 'matched_license': 'BSD-3-Clause'},
            {'filename': 'MIT.txt', 'matcher': {'name': 'exact'}, 'matched_license': 'MIT'}]})
        self.assertEqual((result['licence_file'], result['licence_files']), ('NOASSERTION', ['BSD-3-Clause', 'MIT']))

    def test_readme_or_package_only_is_not_a_licence_file(self):
        result = licence.summarise({'matched_files': [
            {'filename': 'README.md', 'matcher': {'name': 'reference'}, 'matched_license': 'GPL-3.0'}]})
        self.assertEqual((result['licence_file'], result['licence_any']), ('', ['GPL-3.0']))


def image_available():
    return shutil.which('docker') and subprocess.run(['docker', 'image', 'inspect', licence.IMAGE],
                                                     capture_output=True).returncode == 0


@unittest.skipUnless(image_available(), 'licensee image not built (docker build -t notebookfair/licensee:10.1.0 docker/licensee)')
class RuntimeTest(unittest.TestCase):
    """Runtime choice: Docker if present, else Singularity/Apptainer with a SIF (the cluster)."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        self.bin = self.folder / 'bin'
        self.bin.mkdir()
        self.sif = self.folder / 'licensee.sif'
        self.env = mock.patch.dict(os.environ, {'PATH': str(self.bin), 'LICENSEE_RUNTIME': 'auto'})
        self.env.start()
        self.addCleanup(self.env.stop)
        sif = mock.patch.object(licence, 'SIF', self.sif)
        sif.start()
        self.addCleanup(sif.stop)

    def tool(self, name):
        path = self.bin / name
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)

    def test_docker_first(self):
        self.tool('docker')
        self.tool('singularity')
        self.sif.write_text('x')
        self.assertEqual(licence.runtime()[0], 'docker')

    def test_singularity_needs_the_sif(self):
        self.tool('singularity')
        self.assertIsNone(licence.runtime()[0])
        self.sif.write_text('x')
        self.assertEqual(licence.runtime(), ('singularity', str(self.bin / 'singularity')))

    def test_apptainer_preferred_and_forced_runtime(self):
        self.tool('apptainer')
        self.tool('singularity')
        self.tool('docker')
        self.sif.write_text('x')
        os.environ['LICENSEE_RUNTIME'] = 'singularity'
        self.assertEqual(licence.runtime(), ('singularity', str(self.bin / 'apptainer')))

    def test_nothing_available_is_unavailable(self):
        result = licence.detect(self.folder)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('no singularity image', result['error'])


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_mit_licence_file(self):
        (self.root / 'LICENSE').write_text(MIT)
        result = licence.detect(self.root)
        self.assertEqual((result['status'], result['licence_file']), ('ok', 'MIT'))

    def test_unknown_licence_text_is_noassertion(self):
        (self.root / 'LICENSE').write_text('You may do what you like, but please cite our paper.\n')
        self.assertEqual(licence.detect(self.root)['licence_file'], 'NOASSERTION')

    def test_no_licence_file(self):
        (self.root / 'README.md').write_text('# A notebook collection\n')
        result = licence.detect(self.root)
        self.assertEqual((result['status'], result['licence_file'], result['licence_any']), ('ok', '', []))


if __name__ == '__main__':
    unittest.main(verbosity=1)
