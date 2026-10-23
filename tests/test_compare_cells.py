#!/usr/bin/env python3
"""Synthetic notebooks with known outcomes for analysis/nbprocess/compare_cells.py.
Run: python3 tests/test_compare_cells.py"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.nbprocess import compare_cells as cc  # noqa: E402


def stdout(text):
    return {'output_type': 'stream', 'name': 'stdout', 'text': text}


def stderr(text):
    return {'output_type': 'stream', 'name': 'stderr', 'text': text}


def result(plain, **extra):
    return {'output_type': 'execute_result', 'execution_count': 1, 'metadata': {}, 'data': {'text/plain': plain, **extra}}


def error(name='ValueError', value='bad'):
    return {'output_type': 'error', 'ename': name, 'evalue': value, 'traceback': ['...']}


def image(data):
    return {'output_type': 'display_data', 'metadata': {}, 'data': {'image/png': data, 'text/plain': '<Figure size 640x480 with 1 Axes>'}}


def notebook(*outputs):
    return {'cells': [{'cell_type': 'code', 'source': f'cell {i}', 'outputs': list(o), 'metadata': {}}
                      for i, o in enumerate(outputs)] + [{'cell_type': 'markdown', 'source': '# text'}],
            'metadata': {}, 'nbformat': 4, 'nbformat_minor': 5}


class CellTest(unittest.TestCase):
    def outcome(self, stored, executed):
        return cc.compare_cell(stored, executed)[:2]

    def test_assessability(self):
        self.assertEqual(self.outcome([], []), ('empty', []))
        self.assertEqual(self.outcome([], [stdout('x')]), ('not stored', []))
        self.assertEqual(self.outcome([stdout('x')], []), ('missing', []))

    def test_identical_ignores_execution_count_and_metadata(self):
        a = result('42')
        b = dict(result('42'), execution_count=7, metadata={'tags': []})
        self.assertEqual(self.outcome([a], [b]), ('identical', []))

    def test_normalisation_rules(self):
        cases = [
            ('<object at 0x7f3a2b1c9d40>', '<object at 0x55d1e2f3a4b0>', 'memory address'),
            ('run at 2019-03-01 10:22:33', 'run at 2026-10-03 22:01:59', 'timestamp'),
            ('CPU times: user 1.2 s, sys: 10 ms, total: 1.21 s\nok', 'CPU times: user 3.4 s, sys: 12 ms, total: 3.41 s\nok',
             'timing report'),
            ('saved /home/alice/data/x.csv', 'saved /home/runner/data/x.csv', 'home path'),
            ('100%|██████████| 10/10 [00:01<00:00]\ndone', '100%|██████████| 10/10 [00:03<00:00]\ndone', 'progress bar'),
        ]
        for stored, executed, rule in cases:
            with self.subTest(rule):
                outcome, rules = self.outcome([stdout(stored)], [stdout(executed)])
                self.assertEqual(outcome, 'identical normalised')
                self.assertIn(rule, rules)

    def test_stderr_warnings_and_stream_chunks(self):
        outcome, _, notes = cc.compare_cell([stdout('a\nb\n')], [stdout('a\n'), stderr('DeprecationWarning: x'), stdout('b\n')])
        self.assertEqual(outcome, 'identical normalised')
        self.assertIn('stderr ignored', notes)
        outcome, rules = self.outcome([stdout('value 1\n')],
                                      [stdout('/x/lib.py:12: FutureWarning: old\n  warnings.warn(msg)\nvalue 1\n')])
        self.assertEqual((outcome, rules), ('identical normalised', ['warning line']))

    def test_numeric_tolerance(self):
        self.assertEqual(self.outcome([result('0.30000000000000004')], [result('0.3')])[0], 'identical tolerance')
        self.assertEqual(self.outcome([result('0.31')], [result('0.3')])[0], 'different')

    def test_images_reported_separately(self):
        self.assertEqual(self.outcome([image('AAAA')], [image('BBBB')])[0], 'image only')
        self.assertEqual(self.outcome([image('AAAA')], [image('AAAA')])[0], 'identical')

    def test_errors(self):
        self.assertEqual(self.outcome([result('1')], [error()])[0], 'error')
        self.assertEqual(self.outcome([error('KeyError', 'x')], [error('KeyError', 'x')])[0], 'identical')

    def test_widget_ids_ignored(self):
        w = lambda mid: {'output_type': 'display_data', 'metadata': {},
                         'data': {cc.WIDGET: {'model_id': mid, 'version_major': 2}, 'text/plain': 'IntSlider(value=1)'}}
        self.assertEqual(self.outcome([w('abc')], [w('def')])[0], 'identical normalised')


def plot_png(y, dpi=100, fontsize=10, compress=6):
    """A matplotlib line plot as base64 PNG (re-encoded with a given compression level)."""
    import base64, io
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from PIL import Image
    fig, ax = plt.subplots(figsize=(4, 3), dpi=dpi)
    ax.plot(range(len(y)), y)
    ax.set_title('result', fontsize=fontsize)
    raw = io.BytesIO()
    fig.savefig(raw, format='png')
    plt.close(fig)
    out = io.BytesIO()
    Image.open(io.BytesIO(raw.getvalue())).save(out, format='PNG', compress_level=compress)
    return base64.b64encode(out.getvalue()).decode()


try:
    import matplotlib, PIL, numpy  # noqa: F401,E401
    HAVE_IMAGING = True
except ImportError:
    HAVE_IMAGING = False


@unittest.skipUnless(HAVE_IMAGING, 'matplotlib/Pillow/numpy not available')
class ImageTest(unittest.TestCase):
    y = [1, 3, 2, 5, 4]

    def outcome(self, a, b):
        return cc.compare_cell([image(a)], [image(b)])

    def test_same_plot_other_bytes_is_similar(self):
        base = plot_png(self.y)
        for name, other in (('other compression', plot_png(self.y, compress=1)),
                            ('other resolution', plot_png(self.y, dpi=72)),
                            ('higher resolution', plot_png(self.y, dpi=150)),
                            ('slightly other font', plot_png(self.y, fontsize=10.3))):
            with self.subTest(name):
                self.assertNotEqual(base, other)
                outcome, rules, notes = self.outcome(base, other)
                self.assertEqual(outcome, 'image similar', notes)
                self.assertIn('image compared visually', rules)

    def test_other_data_is_a_visible_difference(self):
        for name, y in (('other curve', [5, 1, 4, 1, 3]), ('one point moved', [1, 3, 2, 5, 4.3])):
            with self.subTest(name):
                outcome, _, notes = self.outcome(plot_png(self.y), plot_png(y))
                self.assertEqual(outcome, 'image only', notes)
                self.assertTrue(any(n.startswith('image 0: mean diff') for n in notes))

    def test_similar_image_counts_in_normalised_score(self):
        report = cc.compare_notebooks(notebook([image(plot_png(self.y))]), notebook([image(plot_png(self.y, dpi=72))]))
        self.assertEqual(report['scores'], dict(strict=0.0, normalised=1.0, text_data=1.0, before_error=1.0))


class NotebookTest(unittest.TestCase):
    def test_scores(self):
        original = notebook([], [stdout('a')], [stdout('<x at 0x7f0000000001>')], [result('1')], [image('A')],
                            [stdout('z')], [])
        executed = notebook([], [stdout('a')], [stdout('<x at 0x7f0000000999>')], [error()], [image('B')],
                            [stdout('y')], [stdout('new')])
        report = cc.compare_notebooks(original, executed)
        self.assertEqual([c['outcome'] for c in report['cells']],
                         ['empty', 'identical', 'identical normalised', 'error', 'image only', 'different', 'not stored'])
        self.assertEqual(report['assessable'], 5)
        self.assertEqual(report['scores'], dict(strict=0.2, normalised=0.4, text_data=0.6, before_error=1.0))
        self.assertEqual(report['first_error_cell'], 3)

    def test_no_stored_outputs_is_not_assessable(self):
        report = cc.compare_notebooks(notebook([], []), notebook([stdout('x')], []))
        self.assertEqual(report['assessable'], 0)
        self.assertEqual(set(report['scores'].values()), {None})

    def test_no_code_cells(self):
        report = cc.compare_notebooks({'cells': []}, {'cells': []})
        self.assertEqual((report['code_cells'], report['scores']['normalised']), (0, None))


if __name__ == '__main__':
    unittest.main(verbosity=1)
