#!/usr/bin/env python3
"""Offline tests for the package-index handling of src/install_env.py (pip isolated from the machine's
configuration, network failures retried, then INSTALL_NETWORK_FAIL). Run: python3 tests/test_install_network.py"""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import install_env as ie  # noqa: E402

# what pip printed in the 2026-10 study run when the cluster proxy failed
PROXY_FAILURE = ("WARNING: Retrying (Retry(total=0, connect=None, read=None, redirect=None, status=None)) after connection "
                 "broken by 'OSError('Tunnel connection failed: 500 Internal Server Error')': /pandas/\n"
                 "ERROR: Could not find a version that satisfies the requirement pandas (from versions: none)\n"
                 "ERROR: No matching distribution found for pandas\n")
MIRROR_FAILURE = 'ERROR: Index https://mirrors.example.org/pypi/web/simple/cycler/ does not provide upload-time metadata.\n'
REAL_FAILURE = ('ERROR: Could not find a version that satisfies the requirement nosuchpkg (from versions: none)\n'
                'ERROR: No matching distribution found for nosuchpkg\n')
RECOVERED_THEN_BUILD_ERROR = ("WARNING: Retrying (Retry(total=4)) after connection broken by 'ReadTimeoutError': /x/\n"
                              'error: subprocess-exited-with-error\nERROR: Failed building wheel for x\n')


class ClassificationTest(unittest.TestCase):
    def test_network_and_index_failures(self):
        self.assertTrue(ie.network_failure(1, PROXY_FAILURE))
        self.assertTrue(ie.network_failure(1, MIRROR_FAILURE))

    def test_failures_of_the_package_are_not_network(self):
        self.assertFalse(ie.network_failure(1, REAL_FAILURE))
        self.assertFalse(ie.network_failure(1, RECOVERED_THEN_BUILD_ERROR))
        self.assertFalse(ie.network_failure(0, PROXY_FAILURE))

    def test_environment_ignores_machine_pip_config(self):
        env = ie.pip_environment({'PIP_INDEX_URL': 'https://mirror', 'PIP_EXTRA_INDEX_URL': 'https://nvidia',
                                  'PIP_CONFIG_FILE': '/etc/pip.conf', 'https_proxy': 'http://u:p@proxy:3128', 'HOME': '/h'})
        self.assertEqual(env['PIP_CONFIG_FILE'], os.devnull)
        self.assertEqual(env['PIP_INDEX_URL'], ie.INDEX_URL)
        self.assertNotIn('PIP_EXTRA_INDEX_URL', env)
        self.assertEqual(env['https_proxy'], 'http://u:p@proxy:3128')                 # the proxy is kept
        self.assertEqual(ie.proxy_hosts(env)['https_proxy'], 'http://***@proxy:3128')  # credentials not recorded

    def test_resource_indexes_not_used_with_a_cutoff(self):
        options = ['--extra-index-url https://download.pytorch.org/whl/cu124', '-i https://mirror', '--pre',
                   '--find-links=./wheels']
        used, dropped = ie.split_resource_options(options, '2024-01-01T23:59:59Z')
        self.assertEqual(used, ['--pre'])
        self.assertEqual(len(dropped), 3)
        self.assertEqual(ie.split_resource_options(options, None), (options, []))


class LocalVersionTest(unittest.TestCase):
    def test_local_labels_are_removed_and_recorded(self):
        spec, note = ie.without_local_version("torch==2.5.1+cu124; sys_platform != 'darwin'")
        self.assertEqual(spec, "torch==2.5.1; sys_platform != 'darwin'")
        self.assertIn('+cu124', note)
        self.assertEqual(ie.without_local_version('lz4==3.1.3+dfsg')[0], 'lz4==3.1.3')
        self.assertEqual(ie.without_local_version('stack_data==0.6.4.dev0+g53501eb8b.d20251216')[0], 'stack_data==0.6.4.dev0')
        self.assertEqual(ie.without_local_version('numpy>=1.2'), ('numpy>=1.2', None))
        self.assertEqual(ie.without_local_version('pkg @ https://x/y+z.whl'), ('pkg @ https://x/y+z.whl', None))


class InstallerRetryTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def fake_python(self, outputs):
        """A 'python' that answers every call with the next of the given (exit code, output)."""
        state = self.dir / 'calls.json'
        state.write_text(json.dumps(outputs))
        script = self.dir / 'python'
        script.write_text(f'''#!{sys.executable}
import json, sys
path = {str(state)!r}
outputs = json.load(open(path))
code, text = outputs.pop(0) if len(outputs) > 1 else outputs[0]
json.dump(outputs, open(path, 'w'))
sys.stdout.write(text)
sys.exit(code)
''')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    def test_network_failure_is_retried_and_then_recovers(self):
        python = self.fake_python([[1, PROXY_FAILURE], [0, 'Successfully installed pandas']])
        installer = ie.Installer(python, self.dir, 60, [], network_retries=2, pause=0)
        code, _, _ = installer.install('pandas')
        self.assertEqual(code, 0)
        self.assertFalse(installer.network_down)
        self.assertEqual(len(installer.network_events), 1)

    def test_persistent_failure_marks_the_index_unreachable(self):
        python = self.fake_python([[1, PROXY_FAILURE]])
        installer = ie.Installer(python, self.dir, 60, [], network_retries=2, pause=0)
        code, _, _ = installer.install('pandas')
        self.assertNotEqual(code, 0)
        self.assertTrue(installer.network_down)
        self.assertEqual(len(installer.network_events), 3)               # first try + 2 retries
        code, output, _ = installer.install('matplotlib')                # skipped without calling pip
        self.assertEqual(code, ie.NETWORK_EXIT)
        self.assertIn('could not be reached', output)

    def test_package_failure_is_not_retried(self):
        python = self.fake_python([[1, REAL_FAILURE]])
        installer = ie.Installer(python, self.dir, 60, [], network_retries=2, pause=0)
        code, _, _ = installer.install('nosuchpkg')
        self.assertEqual(code, 1)
        self.assertFalse(installer.network_down)
        self.assertEqual(installer.network_events, [])

    def test_run_reports_network_failure_with_exit_75(self):
        python = self.fake_python([[0, 'pip 26.2.1 from x (python 3.10)'], [0, 'Usage: --uploaded-prior-to'],
                                   [0, 'env_var:\n'], [1, PROXY_FAILURE]])
        plan = {'requirements': [], 'imports': [], 'pip_options': ['-i https://mirror']}
        os.environ['NBF_PIP_NETWORK_RETRIES'] = '0'
        self.addCleanup(os.environ.pop, 'NBF_PIP_NETWORK_RETRIES')
        report, status = ie.run(python, plan, self.dir / 'out', 60, resource_date='2024-05-01')
        self.assertEqual(status, ie.NETWORK_EXIT)
        self.assertEqual(report['status'], 'network failure')
        self.assertTrue(report['network']['unreachable'])
        self.assertEqual(report['index']['index_url'], ie.INDEX_URL)
        self.assertEqual(report['index']['resource_options_not_used'], ['-i https://mirror'])
        self.assertTrue(all(p.get('failure_kind') == 'network' for p in report['packages'] if p['status'] == 'failed'))


if __name__ == '__main__':
    unittest.main()
