#!/usr/bin/env python3
"""Offline tests for analysis/nbprocess/error_context.py (EXTERNAL_SERVICE_ERROR).
Run: python3 tests/test_error_context.py"""
import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
os.environ.setdefault('RUN_ID', '1')                  # summary.py reads these at import
os.environ.setdefault('DB_FILE', ':memory:')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.nbprocess import error_context as ec  # noqa: E402


def notebook(*sources):
    return {'cells': [{'cell_type': 'code', 'source': s, 'outputs': []} for s in sources]}


class CategorizeTest(unittest.TestCase):
    def test_georoc_password_widget_before_the_error(self):
        nb = notebook('from functions.api import ApiKey', 'my_api = ApiKey()  # shows Password(description="API Key:")',
                      'import x', 'my_api.api.api_query("ping")')
        nb['cells'][1]['source'] = 'key = widgets.Password(description="API Key:")'
        category, by_type, evidence = ec.categorize('AttributeError', "'NoneType' object has no attribute 'api_query'",
                                                    '', nb, 3)
        self.assertEqual((category, by_type), ('EXTERNAL_SERVICE_ERROR', 'CODE_ERROR'))
        self.assertIn('Password widget in cell 1', evidence)

    def test_password_widget_only_in_the_output(self):
        nb = notebook('from functions.api import ApiKey', 'my_api = ApiKey()', 'my_api.api.api_query("ping")')
        nb['cells'][1]['outputs'] = [{'output_type': 'display_data',
                                      'data': {'text/plain': "Password(description='API Key:')"}}]
        category, _, evidence = ec.categorize('AttributeError', "'NoneType' object has no attribute 'api_query'", '', nb, 2)
        self.assertEqual(category, 'EXTERNAL_SERVICE_ERROR')
        self.assertIn('shown by cell 1', evidence)

    def test_prompt_after_the_error_does_not_count(self):
        nb = notebook('1/0', 'from getpass import getpass; getpass()')
        self.assertEqual(ec.categorize('ZeroDivisionError', 'division by zero', '', nb, 0)[0], 'OTHER_ERROR')

    def test_http_answers_and_credentials_in_text(self):
        cases = [
            ('HTTPError', '403 Client Error: Forbidden for url: https://api.x.org'),
            ('JSONDecodeError', 'Expecting value', 'requests ... response status 503'),
            ('ValueError', 'Invalid API key provided'),
            ('Exception', 'Rate limit exceeded, retry later'),
            ('RuntimeError', 'missing credentials for the bucket'),
            ('NoCredentialsError', 'Unable to locate credentials'),
        ]
        for error_type, message, *traceback in cases:
            category, _, evidence = ec.categorize(error_type, message, traceback[0] if traceback else '', None, None)
            self.assertEqual(category, 'EXTERNAL_SERVICE_ERROR', (error_type, message))
            self.assertTrue(evidence)

    def test_secret_environment_variable(self):
        self.assertEqual(ec.categorize('KeyError', "'OPENAI_API_KEY'")[0], 'EXTERNAL_SERVICE_ERROR')
        self.assertEqual(ec.categorize('KeyError', "'column_a'")[0], 'DATA_ERROR')

    def test_unchanged_categories(self):
        self.assertEqual(ec.categorize('ModuleNotFoundError', "No module named 'requests'", 'api key')[0], 'DEPENDENCY_ERROR')
        self.assertEqual(ec.categorize('ConnectionError', 'Name or service not known')[0], 'NETWORK_ERROR')
        self.assertEqual(ec.categorize('FileNotFoundError', "No such file: './data/x'"),
                         ('FILE_ERROR', 'FILE_ERROR', None))
        self.assertEqual(ec.categorize('TypeError', 'unsupported operand: 401 + str')[0], 'CODE_ERROR')

    def test_columns_added_once(self):
        connection = sqlite3.connect(':memory:')
        connection.execute('CREATE TABLE notebook_executions (id INTEGER PRIMARY KEY, error_category TEXT)')
        ec.ensure_columns(connection)
        ec.ensure_columns(connection)
        columns = [r[1] for r in connection.execute('PRAGMA table_info(notebook_executions)')]
        self.assertEqual(columns, ['id', 'error_category', 'error_category_by_type', 'error_evidence'])


if __name__ == '__main__':
    unittest.main(verbosity=1)
