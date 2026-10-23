#!/usr/bin/env python3
"""Offline tests for analysis/sampling/ (draw order, D2 rules, Zenodo strata and concept dedup,
tar read limit). Run: python3 tests/test_sampling.py"""
import io
import itertools
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TMP = tempfile.mkdtemp()
os.environ['NBF_SAMPLING_DIR'] = TMP          # never touch data/sampling
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'analysis' / 'sampling'))
import codeberg_census as cb  # noqa: E402
import draw_sample as ds  # noqa: E402
import github_id_sample as gh  # noqa: E402
import pilot_power  # noqa: E402
import zenodo_frame as zf  # noqa: E402


class GithubTest(unittest.TestCase):
    def test_draw_order_depends_only_on_the_seed_and_has_no_repeats(self):
        first = list(itertools.islice(gh.id_sequence(7, 50), 50))
        self.assertEqual(first, list(itertools.islice(gh.id_sequence(7, 50), 50)))
        self.assertEqual(sorted(first), list(range(1, 51)))
        self.assertNotEqual(first, list(itertools.islice(gh.id_sequence(8, 50), 50)))

    def test_extending_a_sample_keeps_its_prefix(self):
        self.assertEqual(list(itertools.islice(gh.id_sequence(3, 10**9), 10)),
                         list(itertools.islice(gh.id_sequence(3, 10**9), 20))[:10])

    def test_d2_rules(self):
        base = dict(fork=False, mirror_url=None, size=10, created_at='2020-01-01T00:00:00Z')
        self.assertEqual(gh.d2(base), (1, ''))
        self.assertEqual(gh.d2(dict(base, fork=True)), (0, 'fork'))
        self.assertEqual(gh.d2(dict(base, mirror_url='https://x')), (0, 'mirror'))
        self.assertEqual(gh.d2(dict(base, size=0)), (0, 'empty'))
        self.assertEqual(gh.d2(dict(base, created_at='2026-07-01T00:00:00Z')), (0, 'created_on_or_after_cutoff'))
        self.assertEqual(gh.d2(dict(base, created_at='2026-06-30T23:59:59Z')), (1, ''))


def hit(record_id, concept, created, keys, related=None):
    return dict(id=record_id, conceptrecid=concept, created=created, files=[dict(key=k, size=1) for k in keys],
                metadata=dict(resource_type=dict(type='software'), access_right='open', title='t',
                              relations=dict(version=[dict(index=0, is_last=True)]),
                              related_identifiers=related or []))


class ZenodoTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript(zf.SCHEMA)

    def insert(self, stratum, *args, **kwargs):
        self.db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        zf.row(hit(*args, **kwargs), stratum, 'w'))

    def test_row_counts_files_and_finds_github_link(self):
        values = zf.row(hit(1, 9, '2020-01-01', ['a.ipynb', 'B.ZIP', 'c.tar.gz', 'd.txt'],
                            [dict(identifier='https://github.com/o/r/tree/v1')]), 'Z1', 'w')
        names = [d[0] for d in sqlite3.connect(':memory:').executescript(zf.SCHEMA).execute(
            'SELECT * FROM records LIMIT 0').description]
        record = dict(zip(names, values))
        self.assertEqual((record['ipynb_files'], record['zip_files'], record['tar_files']), (1, 1, 1))
        self.assertEqual(record['github_url'], 'https://github.com/o/r/tree/v1')
        self.assertEqual(record['conceptrecid'], '9')

    def test_latest_version_per_concept_decides_the_stratum(self):
        self.insert('Z2', 1, 100, '2020-01-01T00:00:00', ['old.zip'])         # concept 100, version 1: ZIP
        self.insert('Z1', 2, 100, '2021-01-01T00:00:00', ['new.ipynb'])       # concept 100, version 2: notebook
        self.insert('Z2', 3, 200, '2022-01-01T00:00:00', ['only.zip'])
        self.assertEqual([r['id'] for r in zf.concept_frame(self.db, 'Z1')], [2])
        self.assertEqual([r['id'] for r in zf.concept_frame(self.db, 'Z2')], [3])
        self.assertEqual(sorted(r['id'] for r in zf.concept_frame(self.db)), [2, 3])

    def test_inspection_sample_is_seeded_and_extendable(self):
        for i in range(30):
            self.insert('Z2', i + 1, 1000 + i, f'2020-01-{i % 28 + 1:02d}T00:00:00', [f'{i}.zip'])
        first = zf.inspection_sample(self.db, 'Z2', 5, seed=11)
        self.assertEqual(first, zf.inspection_sample(self.db, 'Z2', 5, seed=11))
        self.assertEqual(zf.inspection_sample(self.db, 'Z2', 12, seed=11)[:5], first)

    def test_record_outcome_notebook_beats_unknown(self):
        self.db.executemany('INSERT INTO inspection_samples VALUES (?,?,?,?,?)',
                            [('Z2', 1, 1, 'a', 1), ('Z2', 1, 2, 'b', 2), ('Z2', 1, 3, 'c', 3), ('Z2', 1, 4, 'd', 4)])
        self.db.executemany('INSERT INTO inspections (id, archive, outcome) VALUES (?,?,?)',
                            [(1, 'x.zip', 'unknown'), (1, 'y.zip', 'notebook'), (2, 'z.zip', 'unknown'),
                             (3, 'w.zip', 'no_notebook')])
        self.assertEqual(zf.record_outcome(self.db, 'Z2', 1),
                         dict(notebook=1, no_notebook=1, unknown=1, no_access=0, not_inspected=1))

    def test_counting_reader_stops_at_the_limit(self):
        reader = zf.CountingReader(10)
        reader.raw = io.BytesIO(b'x' * 100)
        self.assertEqual(len(reader.read(8)), 8)
        self.assertEqual(len(reader.read(8)), 2)
        with self.assertRaises(zf.LimitReached):
            reader.read(8)


class CodebergOrderTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript(cb.SCHEMA)
        self.add(range(1, 41))

    def add(self, ids, **flags):
        for i in ids:
            row = {**dict(fork=0, mirror=0, empty=0, private=0, created_at='2020-01-01T00:00:00+02:00'), **flags}
            self.db.execute('INSERT INTO repos (id, full_name, fork, mirror, empty, private, created_at, default_branch) '
                            'VALUES (?,?,?,?,?,?,?,?)', (i, f'o/r{i}', row['fork'], row['mirror'], row['empty'],
                                                         row['private'], row['created_at'], 'main'))

    def test_order_is_random_but_seeded(self):
        first = [r[0] for r in cb.order_candidates(self.db, 5)]
        self.assertEqual(sorted(first), list(range(1, 41)))
        self.assertNotEqual(first, sorted(first))
        other = sqlite3.connect(':memory:')
        other.executescript(cb.SCHEMA)
        self.db.backup(other)
        other.execute('DELETE FROM check_order')
        other.execute('DELETE FROM meta')
        self.assertEqual([r[0] for r in cb.order_candidates(other, 5)], first)

    def test_new_candidates_are_appended_and_excluded_ones_skipped(self):
        first = [r[0] for r in cb.order_candidates(self.db, 5)]
        self.add(range(41, 51))
        self.add([51], fork=1)
        self.add([52], created_at='2026-08-01T00:00:00Z')
        again = [r[0] for r in cb.order_candidates(self.db, 5)]
        self.assertEqual(again[:40], first)
        self.assertEqual(sorted(again[40:]), list(range(41, 51)))

    def test_other_seed_is_refused(self):
        cb.order_candidates(self.db, 5)
        with self.assertRaises(SystemExit):
            cb.order_candidates(self.db, 6)


class AllocateTest(unittest.TestCase):
    def test_proportional_sum_and_caps(self):
        alloc = ds.allocate({2019: 30, 2020: 60, 2021: 10}, 50)
        self.assertEqual(sum(alloc.values()), 50)
        self.assertEqual(alloc, {2019: 15, 2020: 30, 2021: 5})
        self.assertEqual(ds.allocate({1: 3, 2: 4}, 50), {1: 3, 2: 4})             # all of a small frame
        alloc = ds.allocate({'Z1': 9000, 'Z2': 40000, 'Z3': 500}, 500, caps={'Z1': 9000, 'Z2': 900, 'Z3': 30}, minimum=50)
        self.assertEqual(alloc['Z3'], 30)                                            # capped by its pool
        self.assertEqual(sum(alloc.values()), 500)
        self.assertGreaterEqual(alloc['Z1'], 50)


def build_frames(root):
    """Synthetic S2-S4 state with the real schemas."""
    (root / 'codeberg').mkdir(parents=True)
    db = sqlite3.connect(root / 'codeberg' / 'census.sqlite')
    db.executescript(cb.SCHEMA)
    for i in range(1, 301):
        db.execute('INSERT INTO repos (id, full_name, fork, mirror, empty, private, created_at) VALUES (?,?,0,0,0,0,?)',
                   (i, f'o/r{i}', f'{2016 + i % 8}-03-01T00:00:00+00:00'))
        db.execute('INSERT INTO check_order VALUES (?,?,?,?,?)', (i, i, 1, 1, 'x'))
        db.execute('INSERT INTO languages VALUES (?,?,?,?,?)', (i, 200, 10 if i % 3 == 0 else 0, '{}', 'x'))
    db.commit()
    (root / 'github').mkdir()
    db = sqlite3.connect(root / 'github' / 'ids.sqlite')
    db.executescript(gh.SCHEMA)
    db.execute("INSERT INTO meta VALUES ('max_id', '1000')")
    for seq in range(1, 801):
        db.execute('INSERT INTO draws (seq, id, http_status, full_name, created_at, d2_pass) VALUES (?,?,?,?,?,?)',
                   (seq, seq * 7, 200, f'u/g{seq}', '2021-05-01T00:00:00Z', 1))
        db.execute('INSERT INTO languages VALUES (?,?,?,?,?,?)', (seq * 7, 'graphql', 1, 5 if seq % 2 else 0, '{}', 'x'))
    db.commit()
    (root / 'zenodo').mkdir()
    db = sqlite3.connect(root / 'zenodo' / 'frame.sqlite')
    db.executescript(zf.SCHEMA)
    for i in range(1, 2001):
        stratum = 'Z1' if i <= 200 else 'Z2' if i <= 1800 else 'Z3'
        db.execute('INSERT INTO records (id, stratum, conceptrecid, created, access_right, title) VALUES (?,?,?,?,?,?)',
                   (i, stratum, str(10000 + i), f'{2018 + i % 6}-01-01T00:00:00', 'open', f't{i}'))
    for stratum, ids in (('Z2', range(201, 601)), ('Z3', range(1801, 1901))):
        db.execute('INSERT INTO meta VALUES (?, ?)', (f'inspection_{stratum}_1_frame_size', str(1600 if stratum == 'Z2' else 200)))
        for seq, i in enumerate(ids, 1):
            db.execute('INSERT INTO inspection_samples VALUES (?,?,?,?,?)', (stratum, 1, seq, str(10000 + i), i))
            outcome = 'notebook' if i % 10 == 0 else 'unknown' if i % 10 == 1 else 'no_notebook'
            db.execute('INSERT INTO inspections (id, archive, outcome) VALUES (?,?,?)', (i, 'a.zip', outcome))
    db.commit()


class DrawTest(unittest.TestCase):
    def test_draw_end_to_end(self):
        import csv, json, subprocess
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        build_frames(root)
        script = Path(__file__).resolve().parents[1] / 'analysis' / 'sampling' / 'draw_sample.py'
        run = lambda out: subprocess.run(  # noqa: E731
            [sys.executable, str(script), '--n', '60', '--reserve', '10', '--reserve-per-stratum', '5',
             '--min-stratum', '10', '--no-check-versions', '--allow-incomplete', '--out', str(out)],
            env=dict(os.environ, NBF_SAMPLING_DIR=str(root)), capture_output=True, text=True, check=True)
        run(root / 'out1')
        run(root / 'out2')
        first = (root / 'out1' / 'cohort-primary.csv').read_text()
        self.assertEqual(first, (root / 'out2' / 'cohort-primary.csv').read_text())     # same seed, same draw
        cohort = list(csv.DictReader(io.StringIO(first)))
        self.assertEqual(list(cohort[0]), ['category', 'platform', 'url'])
        count = {p: sum(r['platform'] == p for r in cohort) for p in ('codeberg', 'github', 'zenodo')}
        self.assertEqual(count, {'codeberg': 60, 'github': 60, 'zenodo': 60})
        summary = json.loads((root / 'out1' / 'summary.json').read_text())
        self.assertEqual(summary['codeberg']['frame_screened_in'], 100)
        self.assertEqual(sum(summary['codeberg']['primary_by_year'].values()), 60)
        self.assertEqual(summary['github']['screened_in'], 400)
        strata = summary['zenodo']['strata']
        self.assertEqual(strata['Z2']['pool'], 80)                   # 40 notebook + 40 unknown of 400 inspected
        self.assertEqual(strata['Z2']['estimated_eligible'], 320)    # 1600 * 80 / 400
        self.assertAlmostEqual(strata['Z2']['pi'], (400 / 1600) * (strata['Z2']['allocated'] / 80))
        self.assertGreaterEqual(min(s['allocated'] for s in strata.values()), 10)
        drawn = list(csv.DictReader(io.StringIO((root / 'out1' / 'draw.csv').read_text())))
        github = sorted((int(r['order']), r['name']) for r in drawn if r['platform'] == 'github' and r['role'] == 'primary')
        self.assertEqual(github[0][1], 'u/g1')                       # first screened-in draws, in draw order
        self.assertEqual(len({r['url'] for r in drawn}), len(drawn))  # no resource twice


class PowerTest(unittest.TestCase):
    def test_sample_size_formula(self):
        self.assertEqual(round(pilot_power.n_per_group(0.358, 0.08, 0.05)), 314)
        self.assertAlmostEqual(pilot_power.detectable(0.358, 400, 0.05 / 3), 0.082, places=3)

    def test_icc_of_identical_groups_is_one(self):
        self.assertAlmostEqual(pilot_power.icc_oneway([[1.0, 1.0], [0.0, 0.0], [1.0, 1.0]]), 1.0)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
