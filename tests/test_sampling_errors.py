#!/usr/bin/env python3
"""Error handling of the sampling crawlers against a local fake API (no network):
a listing entry that makes every page containing it fail with HTTP 500 (Codeberg, Zenodo), a GitHub
repository blocked with 403, and a GitHub rate-limit 403 that must be retried.
Run: python3 tests/test_sampling_errors.py"""
import io
import json
import os
import tarfile
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TMP = tempfile.mkdtemp()
os.environ['NBF_SAMPLING_DIR'] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'analysis' / 'sampling'))
import codeberg_census as cb  # noqa: E402
import common  # noqa: E402
import github_id_sample as gh  # noqa: E402
import zenodo_frame as zf  # noqa: E402

common.Http.backoff = staticmethod(lambda response, attempt: 0)        # no waiting in tests
CODEBERG_TOTAL, CODEBERG_BROKEN = 120, 57                               # entry at offset 57 cannot be listed
ZENODO_TOTAL, ZENODO_BROKEN = 230, 141
STATE = {'rate_limited_once': False, 'tar_requests': {}}


def tar_bytes():
    """A tar whose notebook comes after 300 kB of data, so a dropped connection hits before it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, size in (('data.bin', 300_000), ('analysis.ipynb', 10)):
            info = tarfile.TarInfo(name)
            info.size = size
            archive.addfile(info, io.BytesIO(b'x' * size))
    return buffer.getvalue()


TAR = tar_bytes()
DROP_ONCE, DROP_ALWAYS = 9001, 9002                                     # Z3 records: drop the 1st / every download
TREE_BAD_REQUEST = 'u/r3'                                               # Codeberg answers 400 for this tree
GITHUB_FLAKY = 9                                                        # GitHub id answering 503 until STATE flips
ZIP_FORBIDDEN = 8000                                                    # Z2 record turned restricted after listing
ZIP_WITH_NOTEBOOK = set(range(8001, 8040, 2))                           # every second other Z2 record holds a notebook


class FakeApi(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path.startswith('/api/v1/repos/') and '/git/trees/' in url.path:
            name = '/'.join(url.path.split('/')[4:6])
            STATE.setdefault('tree_requests', []).append(name)
            if name == TREE_BAD_REQUEST:
                return self.reply(400, {'message': 'Bad Request'})
            return self.reply(200, {'tree': [{'type': 'blob', 'path': 'README.md'}], 'truncated': False})
        if url.path == '/api/v1/repos/search':
            limit, page = int(q['limit']), int(q['page'])
            offsets = range((page - 1) * limit, min(page * limit, CODEBERG_TOTAL))
            if CODEBERG_BROKEN in offsets:
                return self.reply(500, {'message': 'internal error'})
            data = [dict(id=o + 1, full_name=f'u/r{o + 1}', fork=False, mirror=False, empty=False, private=False,
                         created_at='2020-01-01T00:00:00+00:00') for o in offsets]
            return self.reply(200, {'data': data}, {'X-Total-Count': str(CODEBERG_TOTAL)})
        if url.path == '/api/records':
            size, page = int(q['size']), int(q.get('page', 1))
            offsets = range((page - 1) * size, min(page * size, ZENODO_TOTAL))
            if size > 1 and len(offsets) and ZENODO_BROKEN in offsets or size == 1 and page - 1 == ZENODO_BROKEN:
                return self.reply(500, {'message': 'INTERNAL SERVER ERROR'})
            hits = [dict(id=1000 + o, conceptrecid=5000 + o, created='2020-01-01T00:00:00+00:00', files=[{'key': 'a.zip'}],
                         metadata=dict(resource_type={'type': 'software'}, access_right='open', title='t'))
                    for o in offsets]
            return self.reply(200, {'hits': {'total': ZENODO_TOTAL, 'hits': hits}})
        if url.path.startswith('/api/records/') and url.path.endswith('/container'):
            record = int(url.path.split('/')[3])
            STATE.setdefault('container_requests', []).append(record)
            if record == ZIP_FORBIDDEN:                                 # what Zenodo sends: quota left, Retry-After set
                return self.reply(403, {'status': 403, 'message': 'Permission denied.'},
                                  {'X-RateLimit-Limit': '133', 'X-RateLimit-Remaining': '132', 'Retry-After': '60'})
            entries = [{'key': 'src/analysis.ipynb'}] if record in ZIP_WITH_NOTEBOOK else [{'key': 'README.md'}]
            return self.reply(200, {'entries': entries, 'truncated': False})
        if url.path.startswith('/api/records/') and url.path.endswith('/content'):
            record = int(url.path.split('/')[3])
            STATE['tar_requests'][record] = STATE['tar_requests'].get(record, 0) + 1
            drop = record == DROP_ALWAYS or record == DROP_ONCE and STATE['tar_requests'][record] == 1
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(TAR)))
            self.end_headers()
            self.wfile.write(TAR[:100_000] if drop else TAR)               # short body -> IncompleteRead
            self.wfile.flush()
            self.close_connection = True
            return
        if url.path.startswith('/repositories/'):
            repo_id = int(url.path.rsplit('/', 1)[1])
            if repo_id == 7:
                return self.reply(403, {'message': 'Repository access blocked', 'block': {'reason': 'tos'}},
                                  {'X-RateLimit-Remaining': '4000'})
            if repo_id == GITHUB_FLAKY and STATE.get('github_flaky'):
                return self.reply(503, {'message': 'Service Unavailable'})
            if repo_id == 8 and not STATE['rate_limited_once']:
                STATE['rate_limited_once'] = True
                return self.reply(403, {'message': 'You have exceeded a secondary rate limit'}, {'Retry-After': '0'})
            return self.reply(200, dict(id=repo_id, full_name=f'o/r{repo_id}', node_id='n', fork=False,
                                        archived=False, created_at='2020-01-01T00:00:00Z', size=10))
        self.reply(404, {'message': 'Not Found'})


class ErrorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeApi)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)

    def test_codeberg_unlistable_entry_is_recorded_and_skipped(self):
        cb.API = f'{self.base}/api/v1'
        cb.enumerate_repos(Namespace(pass_=1, interval=0, max_pages=0))
        db = sqlite3.connect(cb.OUT / 'census.sqlite')
        ids = {r[0] for r in db.execute('SELECT id FROM repos')}
        self.assertEqual(len(ids), CODEBERG_TOTAL - 1)
        self.assertNotIn(CODEBERG_BROKEN + 1, ids)
        self.assertEqual(db.execute('SELECT list_offset FROM listing_errors').fetchall(), [(CODEBERG_BROKEN,)])
        self.assertIn('enumerate_pass_1_finished', dict(db.execute('SELECT key, value FROM meta').fetchall()))

    def test_zenodo_unlistable_record_is_recorded_and_window_completes(self):
        zf.API = f'{self.base}/api/records'
        zf.START = datetime(2019, 1, 1, tzinfo=timezone.utc)
        zf.END = datetime(2021, 1, 1, tzinfo=timezone.utc)
        os.environ.pop('ZENODO_TOKEN', None)
        zf.token = lambda name: 'x' if name == 'ZENODO_TOKEN' else None          # page size 100
        zf.enumerate_stratum(Namespace(stratum='Z2', interval=0))
        db = sqlite3.connect(zf.OUT / 'frame.sqlite')
        self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE stratum = 'Z2'").fetchone()[0], ZENODO_TOTAL - 1)
        self.assertEqual(db.execute('SELECT list_offset FROM listing_errors').fetchall(), [(ZENODO_BROKEN,)])
        self.assertEqual(db.execute('SELECT total, stored FROM windows').fetchone(), (ZENODO_TOTAL, ZENODO_TOTAL - 1))

    def test_zenodo_tar_stream_dropped_is_retried_then_unknown(self):
        zf.API = f'{self.base}/api/records'
        zf.time.sleep = lambda seconds: None                            # no waiting between attempts
        db = zf.connect()
        for record in (DROP_ONCE, DROP_ALWAYS):
            db.execute('INSERT OR REPLACE INTO records (id, stratum, conceptrecid, created, access_right, archive_keys) '
                       'VALUES (?, ?, ?, ?, ?, ?)', (record, 'Z3', f'c{record}', '2020-01-01T00:00:00+00:00', 'open',
                                                     json.dumps(['data.tar'])))
        db.commit()
        zf.inspect_tar(Namespace(n=2, seed=7, max_mb=10, interval=0))
        outcomes = dict(db.execute('SELECT id, outcome FROM inspections WHERE seed = 7').fetchall())
        self.assertEqual(outcomes, {DROP_ONCE: 'notebook', DROP_ALWAYS: 'unknown'})
        self.assertEqual(STATE['tar_requests'], {DROP_ONCE: 2, DROP_ALWAYS: zf.STREAM_ATTEMPTS})
        self.assertIn('inspection_Z3_7_done', dict(db.execute('SELECT key, value FROM meta').fetchall()))

    def test_rate_limit_detection(self):
        def response(status, headers, message=''):
            fake = common.requests.Response()
            fake.status_code, fake._content = status, json.dumps({'message': message}).encode()
            fake.headers.update(headers)
            return fake
        self.assertFalse(common.rate_limited(response(403, {'X-RateLimit-Remaining': '132', 'Retry-After': '60'},
                                                      'Permission denied.')))           # Zenodo
        self.assertTrue(common.rate_limited(response(403, {'X-RateLimit-Remaining': '0'})))          # quota used up
        self.assertTrue(common.rate_limited(response(403, {'X-RateLimit-Remaining': '4000', 'Retry-After': '30'},
                                                     'You have exceeded a secondary rate limit')))  # GitHub
        self.assertTrue(common.rate_limited(response(403, {'Retry-After': '30'})))
        self.assertTrue(common.rate_limited(response(429, {})))
        self.assertFalse(common.rate_limited(response(403, {'X-RateLimit-Remaining': '10'}, 'Repository access blocked')))

    def test_zenodo_zip_inspection_forbidden_and_adaptive_stop(self):
        zf.API = f'{self.base}/api/records'
        db = zf.connect()
        for record in range(ZIP_FORBIDDEN, 8040):
            db.execute('INSERT OR REPLACE INTO records (id, stratum, conceptrecid, created, access_right, archive_keys) '
                       'VALUES (?, ?, ?, ?, ?, ?)', (record, 'Z2', f'z{record}', '2020-01-01T00:00:00+00:00', 'open',
                                                     json.dumps(['code.zip'])))
        db.commit()
        STATE['container_requests'] = []
        frame = len([r for r in zf.concept_frame(db, 'Z2') if r['access_right'] == 'open'])   # + the enumeration test's
        zf.inspect_zip(Namespace(n=frame, seed=11, interval=0, target_pool=5, min_n=3))
        sample = [r[0] for r in db.execute("SELECT id FROM inspection_samples WHERE stratum = 'Z2' AND seed = 11 ORDER BY seq")]
        inspected = STATE['container_requests']
        self.assertEqual(inspected, sample[:len(inspected)])            # the seeded order, a prefix of it
        outcomes = dict(db.execute('SELECT id, outcome FROM inspections WHERE seed = 11').fetchall())
        self.assertEqual(sum(o == 'notebook' for o in outcomes.values()), 5)          # stopped at the target
        self.assertLess(len(inspected), frame)
        if ZIP_FORBIDDEN in outcomes:
            self.assertEqual(outcomes[ZIP_FORBIDDEN], 'no_access')
        stop = json.loads(dict(db.execute('SELECT key, value FROM meta').fetchall())['inspection_Z2_11_stop'])
        self.assertEqual((stop['inspected'], stop['pool']), (len(inspected), 5))
        # the forbidden record is recorded, not retried as a rate limit, and does not end the run
        zf.inspect_zip(Namespace(n=frame, seed=11, interval=0, target_pool=0, min_n=0))
        self.assertEqual(dict(db.execute('SELECT id, outcome FROM inspections WHERE id = ?', (ZIP_FORBIDDEN,)).fetchall()),
                         {ZIP_FORBIDDEN: 'no_access'})
        self.assertEqual(STATE['container_requests'].count(ZIP_FORBIDDEN), 1)
        self.assertEqual(zf.record_outcome(db, 'Z2', 11),
                         dict(notebook=len(ZIP_WITH_NOTEBOOK), no_notebook=frame - len(ZIP_WITH_NOTEBOOK) - 1,
                              unknown=0, no_access=1, not_inspected=0))

    def test_codeberg_tree_bad_request_is_recorded_and_check_goes_on(self):
        cb.API = f'{self.base}/api/v1'
        db = cb.connect()
        for repo_id in (3, 4, 5, 6):
            db.execute('INSERT OR REPLACE INTO repos (id, full_name, default_branch) VALUES (?, ?, ?)',
                       (repo_id, f'u/r{repo_id}', 'main'))
            db.execute('INSERT OR REPLACE INTO languages VALUES (?, 200, 0, ?, ?)', (repo_id, '{}', 'now'))
        db.commit()
        STATE['tree_requests'] = []
        cb.treecheck(Namespace(n=500, seed=3, interval=0))
        rows = dict(db.execute('SELECT id, http_status FROM treecheck WHERE seed = 3').fetchall())
        self.assertEqual(rows[3], 400)                                   # recorded, not raised
        self.assertTrue(all(rows[i] == 200 for i in (4, 5, 6)))         # and the others were still checked
        cb.treecheck(Namespace(n=500, seed=3, interval=0))              # a 400 is final: not requested again
        self.assertEqual(STATE['tree_requests'].count(TREE_BAD_REQUEST), 1)

    def test_github_server_error_is_recorded_then_retried_by_a_rerun(self):
        gh.API = self.base
        args = Namespace(name='flaky', interval=0, seed=1, n=12)
        db = gh.connect(args)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('max_id', '12')")
        db.commit()
        STATE['github_flaky'] = True
        gh.draw(args)                                                    # does not stop at the failing id
        rows = {r[0]: r[1:] for r in db.execute('SELECT id, http_status, d2_reason FROM draws')}
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[GITHUB_FLAKY][0], 503)
        self.assertIn('not resolved', rows[GITHUB_FLAKY][1])
        STATE['github_flaky'] = False
        gh.draw(args)                                                    # rerun: only the unresolved id again
        self.assertEqual(db.execute('SELECT http_status, d2_reason FROM draws WHERE id = ?', (GITHUB_FLAKY,)).fetchone(),
                         (200, ''))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM draws').fetchone()[0], 12)

    def test_soft_requests_never_raise(self):
        http = common.Http(Path(TMP) / 'soft.jsonl', max_retries=1)
        self.assertEqual(common.status_of(http.get(f'{self.base}/nowhere', soft=True)), 404)
        self.assertIsNone(http.get('http://127.0.0.1:9/', soft=True))          # nothing listens on port 9
        self.assertFalse(common.settled(None))
        self.assertFalse(common.settled(503))
        self.assertFalse(common.settled('invalid json'))
        self.assertTrue(common.settled(400) and common.settled(200) and common.settled(404))

    def test_github_blocked_and_rate_limited(self):
        gh.API = self.base
        args = Namespace(name='errors', interval=0, seed=1, n=0)
        db = gh.connect(args)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('max_id', '12')")
        db.commit()
        args.n = 12
        gh.draw(args)
        rows = dict(db.execute('SELECT id, d2_reason FROM draws').fetchall())
        self.assertEqual(len(rows), 12)
        self.assertIn('blocked, HTTP 403: Repository access blocked', rows[7])
        self.assertEqual(rows[8], '')                                   # retried after the rate limit, then fine
        self.assertTrue(STATE['rate_limited_once'])


if __name__ == '__main__':
    unittest.main(verbosity=1)
