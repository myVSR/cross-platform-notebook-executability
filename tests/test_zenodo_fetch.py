#!/usr/bin/env python3
"""Offline tests for src/zenodo_fetch.py (HTTP is replaced by in-memory files).

Run: python3 tests/test_zenodo_fetch.py
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import zenodo_fetch as zf  # noqa: E402

MiB = zf.MiB
NOTEBOOK = json.dumps({'cells': [], 'metadata': {}, 'nbformat': 4, 'nbformat_minor': 5}).encode()


def zip_bytes(files, links=()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, 'target')
    return buffer.getvalue()


def tar_bytes(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeZenodo:
    """Serves one record: {key: bytes}; sizes can be overridden to test limits."""

    def __init__(self, files, sizes=None, bad_md5=(), containers=None):
        self.files, self.sizes = files, sizes or {}
        self.bad_md5, self.containers = set(bad_md5), containers or {}

    def record(self):
        entries = []
        for key, data in self.files.items():
            md5 = hashlib.md5(b'other' if key in self.bad_md5 else data).hexdigest()
            entries.append(dict(key=key, size=self.sizes.get(key, len(data)), checksum=f'md5:{md5}',
                                links=dict(self=f'https://zenodo.org/api/records/1/files/{key}/content')))
        return dict(id=1, revision_id=3, metadata=dict(version='v1.0'), files=entries)

    def get_json(self, url):
        if url.endswith('/container'):
            key = url.split('/files/')[1].rsplit('/container', 1)[0]
            key = zf.urllib.parse.unquote(key)
            if key in self.containers:
                return self.containers[key]
            raise zf.FetchError('HTTP 500')
        return self.record()

    def open_url(self, url, timeout=60, **_):
        if '/container/' in url:
            archive, member = url.split('/files/')[1].split('/container/')
            with zipfile.ZipFile(io.BytesIO(self.files[zf.urllib.parse.unquote(archive)])) as z:
                return io.BytesIO(z.read(zf.urllib.parse.unquote(member)))
        key = url.split('/files/')[1].rsplit('/content', 1)[0]
        return io.BytesIO(self.files[key])

    def listing(self, key, sizes=None):
        """A complete container listing of ZIP `key`, as Zenodo returns it."""
        with zipfile.ZipFile(io.BytesIO(self.files[key])) as z:
            return dict(truncated=False, entries=[
                dict(key=i.filename, size=(sizes or {}).get(i.filename, i.file_size), crc=i.CRC,
                     links=dict(content=f'https://zenodo.org/api/records/1/files/{key}/container/{i.filename}'))
                for i in z.infolist()])


class ZenodoFetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.env = mock.patch.dict(os.environ, {'ZENODO_MAX_FILE_BYTES': str(MiB),
                                                'ZENODO_MAX_ARCHIVE_BYTES': str(4 * MiB),
                                                'ZENODO_MAX_TOTAL_BYTES': str(6 * MiB)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_fetch(self, fake, plan_only=False):
        with mock.patch.object(zf, 'get_json', fake.get_json), mock.patch.object(zf, 'open_url', fake.open_url):
            manifest = {}
            zf.acquire('1', self.tmp / 'record', manifest, plan_only=plan_only)
            return manifest

    def tree(self):
        root = self.tmp / 'record'
        return sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p.name != zf.MANIFEST_NAME)

    def test_every_archive_is_used_not_only_the_first_zip(self):
        fake = FakeZenodo({'code.zip': zip_bytes({'code/a.ipynb': NOTEBOOK}),
                           'more.zip': zip_bytes({'more/b.ipynb': NOTEBOOK}),
                           'paper.tar.gz': tar_bytes({'paper/c.ipynb': NOTEBOOK})})
        manifest = self.run_fetch(fake)
        self.assertEqual(self.tree(), ['code.zip', 'code/a.ipynb', 'more.zip', 'more/b.ipynb', 'paper.tar.gz',
                                       'paper/c.ipynb'])          # the archives stay next to their contents
        self.assertTrue(all(e['action'] == 'extracted' and e['archive_kept'] for e in manifest['files']))

    def test_large_archive_is_not_kept_and_a_snapshot_is_only_unpacked(self):
        zf.KEEP_ARCHIVE_BYTES, keep = 10, zf.KEEP_ARCHIVE_BYTES
        self.addCleanup(setattr, zf, 'KEEP_ARCHIVE_BYTES', keep)
        manifest = self.run_fetch(FakeZenodo({'code.zip': zip_bytes({'code/a.ipynb': NOTEBOOK}), 'b.ipynb': NOTEBOOK}))
        self.assertEqual(self.tree(), ['b.ipynb', 'code/a.ipynb'])               # above the keep limit
        self.assertFalse(next(e for e in manifest['files'] if e['key'] == 'code.zip')['archive_kept'])

    def test_archives_next_to_direct_notebooks_are_extracted(self):
        fake = FakeZenodo({'main.ipynb': NOTEBOOK, 'extra.zip': zip_bytes({'extra/x.ipynb': NOTEBOOK})})
        self.run_fetch(fake)
        self.assertEqual(self.tree(), ['extra.zip', 'extra/x.ipynb', 'main.ipynb'])

    def test_single_archive_with_single_folder_is_flattened(self):
        fake = FakeZenodo({'v1.zip': zip_bytes({'owner-repo-abc123/nb/a.ipynb': NOTEBOOK,
                                                'owner-repo-abc123/README.md': b'x'})})
        manifest = self.run_fetch(fake)
        self.assertEqual(self.tree(), ['README.md', 'nb/a.ipynb'])
        self.assertEqual(manifest['flattened'], 'owner-repo-abc123')

    def test_clashing_archives_go_into_their_own_folder(self):
        fake = FakeZenodo({'a.zip': zip_bytes({'nb/x.ipynb': NOTEBOOK}),
                           'b.zip': zip_bytes({'nb/x.ipynb': NOTEBOOK + b' '})})
        manifest = self.run_fetch(fake)
        self.assertEqual(self.tree(), ['a.zip', 'b.zip', 'b/nb/x.ipynb', 'nb/x.ipynb'])
        self.assertEqual([e.get('extracted_into') for e in manifest['files']], [None, 'b'])

    def test_size_rules(self):
        data = b'0' * 10
        fake = FakeZenodo({'big.ipynb': NOTEBOOK, 'small.csv': data, 'huge.csv': data,
                           'data.zip': zip_bytes({'d.csv': data}), 'code.zip': zip_bytes({'c.ipynb': NOTEBOOK}),
                           'unknown.tar': tar_bytes({'u.ipynb': NOTEBOOK}), 'giant.zip': zip_bytes({'g.ipynb': NOTEBOOK})},
                          sizes={'big.ipynb': 3 * MiB, 'huge.csv': 2 * MiB, 'data.zip': 2 * MiB,
                                 'code.zip': 2 * MiB, 'unknown.tar': 3 * MiB, 'giant.zip': 5 * MiB},
                          containers={'data.zip': dict(entries=[dict(key='d.csv')], truncated=False),
                                      'code.zip': dict(entries=[dict(key='c.ipynb')], truncated=False)})
        actions = {e['key']: (e['action'], e['reason']) for e in self.run_fetch(fake, plan_only=True)['files']}
        self.assertEqual(actions['big.ipynb'][0], 'download')          # notebooks always
        self.assertEqual(actions['small.csv'][0], 'download')
        self.assertEqual(actions['huge.csv'], ('skip', 'data file larger than 1 MiB'))
        self.assertEqual(actions['data.zip'], ('skip', 'archive without notebooks larger than 1 MiB'))
        self.assertEqual(actions['code.zip'], ('download', 'archive with notebooks'))
        self.assertEqual(actions['giant.zip'][0], 'skip')             # over the archive limit
        # 3 + 2 MiB already planned; the 3 MiB tar would pass the archive limit but not the 6 MiB total
        self.assertEqual(actions['unknown.tar'], ('skip', 'total download limit of 6 MiB reached'))

    def test_path_traversal_and_escaping_links_are_rejected(self):
        outside = io.BytesIO()
        with tarfile.open(fileobj=outside, mode='w:gz') as archive:
            info = tarfile.TarInfo('pkg/escape')
            info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
            archive.addfile(info)
        for name, archive in (('evil.zip', zip_bytes({'../evil.ipynb': NOTEBOOK})),
                              ('abs.tar.gz', tar_bytes({'/etc/evil.ipynb': NOTEBOOK}))):
            with self.subTest(name), self.assertRaises(zf.FetchError):
                shutil.rmtree(self.tmp / 'record', ignore_errors=True)
                self.run_fetch(FakeZenodo({name: archive}))
        self.assertFalse((self.tmp / 'evil.ipynb').exists())
        # a link leaving the archive is skipped and recorded; the rest of the archive is used
        shutil.rmtree(self.tmp / 'record', ignore_errors=True)
        manifest = self.run_fetch(FakeZenodo({'escape.tar.gz': outside.getvalue(), 'a.ipynb': NOTEBOOK}))
        self.assertFalse(os.path.lexists(self.tmp / 'record' / 'pkg' / 'escape'))
        member = next(e for e in manifest['files'] if e['key'] == 'escape.tar.gz')['members'][0]
        self.assertEqual((member['path'], member['action']), ('pkg/escape', 'skip'))

    def test_tar_links_inside_the_archive_work_with_and_without_the_data_filter(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for member, data in (('pkg/a.ipynb', NOTEBOOK),):
                info = tarfile.TarInfo(member)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo('pkg/b.ipynb')
            link.type, link.linkname = tarfile.SYMTYPE, 'a.ipynb'
            archive.addfile(link)
        for without_filter in (False, True):
            with self.subTest(without_filter=without_filter):
                shutil.rmtree(self.tmp / 'record', ignore_errors=True)
                if without_filter and hasattr(zf.tarfile, 'data_filter'):     # like Python < 3.11.4 on the HPC
                    saved = zf.tarfile.data_filter
                    del zf.tarfile.data_filter
                    self.addCleanup(setattr, zf.tarfile, 'data_filter', saved)
                self.run_fetch(FakeZenodo({'code.tar.gz': buffer.getvalue(), 'n.ipynb': NOTEBOOK}))
                self.assertTrue((self.tmp / 'record' / 'pkg' / 'b.ipynb').is_symlink())
                self.assertEqual((self.tmp / 'record' / 'pkg' / 'b.ipynb').read_bytes(), NOTEBOOK)

    def test_zip_link_is_stored_as_a_plain_file_and_recorded(self):
        manifest = self.run_fetch(FakeZenodo({'link.zip': zip_bytes({'pkg/a.ipynb': NOTEBOOK}, links=['pkg/b']),
                                              'notes.txt': b'n'}))
        link = self.tmp / 'record' / 'pkg' / 'b'
        self.assertTrue(link.is_file() and not link.is_symlink())
        members = {m['path']: m['action'] for m in manifest['files'][0]['members']}
        self.assertEqual(members, {'pkg/a.ipynb': 'extracted', 'pkg/b': 'link stored as file'})

    def test_manifest_logs_every_transfer(self):
        fake = FakeZenodo({'a.ipynb': NOTEBOOK, 'code.zip': zip_bytes({'c/x.ipynb': NOTEBOOK, 'c/d.csv': b'1'})})
        manifest = self.run_fetch(fake)
        for entry in manifest['files']:
            self.assertEqual(entry['bytes_transferred'], len(fake.files[entry['key']]))
            self.assertGreaterEqual(entry['seconds'], 0)
            self.assertTrue(entry['md5_verified'])
        archive = next(e for e in manifest['files'] if e['key'] == 'code.zip')
        self.assertEqual(sorted((m['path'], m['size']) for m in archive['members']), [('c/d.csv', 1), ('c/x.ipynb', len(NOTEBOOK))])
        self.assertTrue(manifest['started_at'])

    def test_md5_mismatch_fails(self):
        with mock.patch.object(zf.time, 'sleep'), self.assertRaises(zf.FetchError):
            self.run_fetch(FakeZenodo({'a.ipynb': NOTEBOOK}, bad_md5=['a.ipynb']))

    def test_destination_must_be_empty(self):
        (self.tmp / 'record').mkdir()
        (self.tmp / 'record' / 'old.ipynb').write_bytes(NOTEBOOK)
        with self.assertRaises(zf.FetchError):
            self.run_fetch(FakeZenodo({'a.ipynb': NOTEBOOK}))

    def test_noise_files(self):
        self.assertTrue(zf.is_noise('__MACOSX/figures/._Fig7.ipynb'))
        self.assertTrue(zf.is_noise('figures/._Fig7.ipynb'))
        self.assertTrue(zf.is_noise('a/.ipynb_checkpoints/x-checkpoint.ipynb'))
        self.assertFalse(zf.is_noise('figures/Fig7.ipynb'))

    @unittest.skipUnless(shutil.which('bsdtar'), 'bsdtar not installed')
    def test_7z_via_bsdtar(self):
        source = self.tmp / 'src'
        (source / 'pkg').mkdir(parents=True)
        (source / 'pkg' / 'n.ipynb').write_bytes(NOTEBOOK)
        archive = self.tmp / 'x.7z'
        subprocess.run(['bsdtar', '--format', '7zip', '-cf', str(archive), '-C', str(source), 'pkg'], check=True)
        self.run_fetch(FakeZenodo({'x.7z': archive.read_bytes(), 'notes.txt': b'n'}))
        self.assertEqual(self.tree(), ['notes.txt', 'pkg/n.ipynb', 'x.7z'])


    def test_oversized_zip_is_taken_member_by_member(self):
        fake = FakeZenodo({'big.zip': zip_bytes({'repo/a.ipynb': NOTEBOOK, 'repo/small.csv': b'1,2',
                                                 'repo/huge.h5': b'x', 'repo/.ipynb_checkpoints/a-checkpoint.ipynb': NOTEBOOK})},
                          sizes={'big.zip': 5 * MiB})
        fake.containers['big.zip'] = fake.listing('big.zip', sizes={'repo/huge.h5': 2 * MiB})
        manifest = self.run_fetch(fake)
        self.assertEqual(self.tree(), ['a.ipynb', 'small.csv'])           # single archive -> flattened
        entry = manifest['files'][0]
        self.assertEqual(entry['action'], 'extracted_partially')
        self.assertIn('2 members taken, 1 left out', entry['reason'])
        self.assertNotIn('_members', entry)
        actions = {m['path']: m['action'] for m in entry['members']}
        self.assertEqual(actions, {'repo/a.ipynb': 'downloaded member', 'repo/small.csv': 'downloaded member',
                                   'repo/huge.h5': 'skip'})
        self.assertEqual(entry['bytes_transferred'], len(NOTEBOOK) + 3)

    def test_identical_notebooks_are_listed_once(self):
        other = json.dumps({'cells': [{'cell_type': 'code', 'source': 'x'}], 'metadata': {},
                            'nbformat': 4, 'nbformat_minor': 5}).encode()
        fake = FakeZenodo({'a.ipynb': NOTEBOOK, 'b.ipynb': other,
                           'v1.zip': zip_bytes({'pkg/a.ipynb': NOTEBOOK, 'pkg/new.ipynb': NOTEBOOK + b'\n'}),
                           'v2.zip': zip_bytes({'pkg2/a.ipynb': NOTEBOOK, 'pkg2/b.ipynb': other})})
        manifest = self.run_fetch(fake)
        self.assertEqual(sorted((d['path'], d['same_as']) for d in manifest['duplicate_notebooks']),
                         [('pkg/a.ipynb', 'a.ipynb'), ('pkg2/a.ipynb', 'a.ipynb'), ('pkg2/b.ipynb', 'b.ipynb')])

    def test_copies_inside_one_archive_are_kept(self):
        # e.g. a repository snapshot with docs/ copies of its example notebooks
        fake = FakeZenodo({'repo-v1.zip': zip_bytes({'repo/examples/a.ipynb': NOTEBOOK,
                                                     'repo/docs/a.ipynb': NOTEBOOK})})
        self.assertEqual(self.run_fetch(fake)['duplicate_notebooks'], [])


class RetryTest(unittest.TestCase):
    def test_429_honours_retry_after_then_succeeds(self):
        error = urllib.error.HTTPError('u', 429, 'Too Many Requests', {'Retry-After': '7'}, None)
        sleeps = []
        with mock.patch.object(zf.urllib.request, 'urlopen', side_effect=[error, error, io.BytesIO(b'{}')]):
            response = zf.open_url('https://zenodo.org/api/records/1', sleep=sleeps.append)
        self.assertEqual(response.read(), b'{}')
        self.assertEqual(sleeps, [7, 7])

    def test_404_is_not_retried(self):
        error = urllib.error.HTTPError('u', 404, 'Not Found', {}, None)
        with mock.patch.object(zf.urllib.request, 'urlopen', side_effect=[error]) as urlopen:
            with self.assertRaises(zf.FetchError):
                zf.open_url('https://zenodo.org/api/records/1', sleep=lambda s: None)
        self.assertEqual(urlopen.call_count, 1)

    def test_token_is_sent_when_set(self):
        with mock.patch.dict(os.environ, {'ZENODO_TOKEN': 'secret'}):
            self.assertEqual(zf.headers()['Authorization'], 'Bearer secret')


if __name__ == '__main__':
    unittest.main(verbosity=1)
