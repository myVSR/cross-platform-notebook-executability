"""Cell-by-cell output comparison with explicit normalisation (comparison version 2).

The original (stored) and executed notebooks have the same cells in the same order, so
code cells are compared by position. Every code cell gets one outcome:

  empty                 no output stored and none produced: not assessable
  not stored            no output stored, output produced: not assessable (nothing to compare with)
  identical             outputs equal (execution counts and metadata ignored)
  identical normalised  equal after the normalisation rules listed in `rules`
  identical tolerance   equal after normalisation and numeric tolerance
  image similar         text and data equal after normalisation, images differ in bytes but not
                        visibly (re-rendering noise: fonts, antialiasing, PNG encoding)
  image only            text and data equal after normalisation, images differ visibly
  error                 the executed cell raised an error the stored output did not have
  missing               output stored, none produced
  different             anything else

Normalisation (each applied rule is recorded per cell):
  stderr streams are ignored; consecutive stream chunks are merged and carriage-return
  overwrites resolved; progress-bar and warning lines are dropped; memory addresses,
  timestamps, clock times, timing reports, home/temporary paths, widget model ids and
  generated element ids are masked; whitespace at line ends is ignored. Images that differ
  in bytes are compared visually (needs Pillow + numpy): both are put on a white background,
  converted to greyscale and scaled to the same size (longest side IMAGE_SIDE = 64 pixels);
  they are "similar" when at most VISUAL_TOLERANCE (0.3%) of the pixels differ by more than
  PIXEL_THRESHOLD (10% of the grey range). Images whose aspect ratio differs by more than 2%
  are different. The measured values are kept per cell. Limitation: changes smaller than
  about 1/64 of a plot's extent can pass as similar.

Scores (None when the notebook has no assessable cell):
  strict        identical / assessable
  normalised    (identical + identical normalised + identical tolerance + image similar) / assessable
  text_data     as normalised, also counting "image only" (visibly different images) as reproduced
  before_error  normalised, over the assessable cells before the first error cell
assessable = code cells with stored output.
"""
from __future__ import annotations

import json
import math
import re

VERSION = 2
REL_TOL, ABS_TOL = 1e-6, 1e-12
# Calibrated on matplotlib line plots (tests/test_compare_cells.py, chat log 2026-10-03): at 64 px, re-rendering
# noise (other dpi/font) changed <= 0.07% of pixels, one moved data point >= 0.75%, another curve 6.4%.
# At 256 px the noise (1.3-2.4%) exceeded a small data change (0.4%). To be validated on cohort images.
IMAGE_SIDE, PIXEL_THRESHOLD, VISUAL_TOLERANCE = 64, 0.1, 0.003

MASKS = [
    ('memory address', re.compile(r'0x[0-9a-fA-F]{6,}'), '0xADDR'),
    ('timestamp', re.compile(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?'), '<TIMESTAMP>'),
    ('clock time', re.compile(r'\b\d{1,2}:\d{2}:\d{2}(\.\d+)?\b'), '<CLOCK>'),
    ('timing report', re.compile(r'^(CPU times|Wall time):.*$', re.M), '<TIMING>'),     # %time / %%time
    ('timing report', re.compile(r'[\d.]+\s*(ns|µs|us|ms|s) ± [\d.]+\s*(ns|µs|us|ms|s) per loop.*'), '<TIMING>'),
    ('home path', re.compile(r'(/home/[^/\s\'"]+|/Users/[^/\s\'"]+|[A-Za-z]:\\\\?Users\\\\?[^\\\s\'"]+)'), '<HOME>'),
    ('temporary path', re.compile(r'/tmp/[^\s\'"]+|/var/folders/[^\s\'"]+'), '<TMP>'),
    ('generated id', re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'), '<UUID>'),
    ('generated id', re.compile(r'(id|for|data-id)="[^"]*\d[^"]*"'), r'\1="<ID>"'),
]
PROGRESS = re.compile(r'(\d+%\|)|(\|\s*\d+/\d+\s*\[)|(it/s\])|(s/it\])|(\d+/\d+ \[=*>?\.*\])')
WARNING_LINE = re.compile(r'^\S.*:\d+: \w*(Warning|Deprecated\w*):')
NUMBER = re.compile(r'[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?')
WIDGET = 'application/vnd.jupyter.widget-view+json'


def _text(value):
    return ''.join(value) if isinstance(value, list) else ('' if value is None else str(value))


def resolve_carriage_returns(text):
    return '\n'.join(line.rsplit('\r', 1)[-1] for line in text.split('\n'))


def normalise_text(text, rules):
    text = resolve_carriage_returns(text.replace('\r\n', '\n'))
    kept = []
    skip_indented = False
    for line in text.split('\n'):
        if PROGRESS.search(line):
            rules.add('progress bar')
            continue
        if WARNING_LINE.match(line):
            rules.add('warning line')
            skip_indented = True
            continue
        if skip_indented and line.startswith(('  ', '\t')):
            continue
        skip_indented = False
        kept.append(line.rstrip())
    text = '\n'.join(kept).strip('\n')
    for name, pattern, replacement in MASKS:
        text, count = pattern.subn(replacement, text)
        if count:
            rules.add(name)
    return text


def output_items(outputs):
    """Comparable items of a cell's outputs: (kind, key, value), images and errors separated."""
    items, stdout, notes = [], [], set()

    def flush():
        if stdout:
            items.append(('text', 'stdout', ''.join(stdout)))
            stdout.clear()

    for output in outputs or []:
        kind = output.get('output_type')
        if kind == 'stream':
            if output.get('name') == 'stderr':
                notes.add('stderr ignored')
                continue
            stdout.append(_text(output.get('text')))
            continue
        flush()
        if kind == 'error':
            items.append(('error', output.get('ename', ''), _text(output.get('evalue'))))
        elif kind in ('execute_result', 'display_data', 'pyout', 'display'):
            data = output.get('data') or {k: v for k, v in output.items() if '/' in k or k in ('png', 'text', 'html')}
            for mime, value in sorted(data.items()):
                mime = {'png': 'image/png', 'text': 'text/plain', 'html': 'text/html'}.get(mime, mime)
                if mime.startswith('image/') and 'svg' not in mime:
                    items.append(('image', mime, _text(value)))
                elif mime == WIDGET:
                    items.append(('text', mime, '<widget>'))
                    notes.add('widget model id ignored')
                elif mime in ('application/javascript', 'text/javascript'):
                    notes.add('javascript ignored')
                elif isinstance(value, dict) or (isinstance(value, list) and mime.endswith('json')):
                    items.append(('text', mime, json.dumps(value, sort_keys=True)))      # JSON-valued outputs
                else:
                    items.append(('text', mime, _text(value)))
    flush()
    return items, notes


def numbers_close(a, b):
    """Equal apart from numbers that agree within tolerance."""
    if NUMBER.sub('#', a) != NUMBER.sub('#', b):
        return False
    for x, y in zip(NUMBER.findall(a), NUMBER.findall(b)):
        fx, fy = float(x), float(y)
        if not math.isclose(fx, fy, rel_tol=REL_TOL, abs_tol=ABS_TOL):
            return False
    return True


def image_difference(a, b):
    """Visual difference of two base64 images: dict(mean_diff, changed_fraction, similar) or a reason."""
    try:
        import base64
        import io
        import numpy as np
        from PIL import Image
    except ImportError:
        return dict(similar=None, reason='Pillow/numpy not available')
    try:
        images = []
        for data in (a, b):
            image = Image.open(io.BytesIO(base64.b64decode(''.join(data.split())))).convert('RGBA')
            background = Image.new('RGBA', image.size, (255, 255, 255, 255))
            images.append(Image.alpha_composite(background, image).convert('L'))
    except Exception as error:  # noqa: BLE001 - undecodable image: cannot judge
        return dict(similar=None, reason=f'not decodable: {type(error).__name__}')
    (wa, ha), (wb, hb) = images[0].size, images[1].size
    if abs(wa / ha - wb / hb) > 0.02 * (wa / ha):
        return dict(similar=False, reason=f'aspect ratio differs ({wa}x{ha} vs {wb}x{hb})')
    scale = IMAGE_SIDE / max(wa, ha)
    size = (max(1, round(wa * min(scale, 1))), max(1, round(ha * min(scale, 1))))
    x, y = (np.asarray(i.resize(size, Image.LANCZOS), dtype=float) / 255 for i in images)
    difference = np.abs(x - y)
    mean, changed = float(difference.mean()), float((difference > PIXEL_THRESHOLD).mean())
    return dict(similar=changed <= VISUAL_TOLERANCE, mean_diff=round(mean, 5),
                changed_fraction=round(changed, 5), sizes=[[wa, ha], [wb, hb]])


def compare_cell(stored, executed):
    """(outcome, sorted rules, notes) for one code cell's stored and executed outputs.
    Visual image measurements, if any, are added to `notes` as 'image k: ...' entries."""
    a, notes_a = output_items(stored)
    b, notes_b = output_items(executed)
    notes = sorted(notes_a | notes_b)
    has_a = bool(stored)
    has_b = bool(executed)
    if not has_a:
        return ('empty' if not has_b else 'not stored'), [], notes
    if any(kind == 'error' for kind, _, _ in b) and not any(kind == 'error' for kind, _, _ in a):
        return 'error', [], notes
    if not has_b:
        return 'missing', [], notes
    if a == b and json.dumps(strip(stored), sort_keys=True) == json.dumps(strip(executed), sort_keys=True):
        return 'identical', [], notes
    rules = set()
    text_a = [(k, key, normalise_text(v, rules)) for k, key, v in a if k != 'image']
    text_b = [(k, key, normalise_text(v, rules)) for k, key, v in b if k != 'image']
    images_a = [(key, v) for k, key, v in a if k == 'image']
    images_b = [(key, v) for k, key, v in b if k == 'image']
    common = {key for _, key, _ in text_a} & {key for _, key, _ in text_b}
    if {key for _, key, _ in text_a} != {key for _, key, _ in text_b} and common:
        rules.add('compared common output formats only')
        text_a = [t for t in text_a if t[1] in common]
        text_b = [t for t in text_b if t[1] in common]
    text_equal = text_a == text_b
    within = not text_equal and len(text_a) == len(text_b) and all(
        x[:2] == y[:2] and numbers_close(x[2], y[2]) for x, y in zip(text_a, text_b))
    if text_equal or within:
        if images_a == images_b:
            if within:
                rules.add('numeric tolerance')
                return 'identical tolerance', sorted(rules), notes
            return 'identical normalised', sorted(rules), notes
        if len(images_a) == len(images_b):
            results = [image_difference(x[1], y[1]) if x != y else dict(similar=True)
                       for x, y in zip(images_a, images_b)]
            for k, r in enumerate(results):
                if 'mean_diff' in r or 'reason' in r:
                    notes.append(f"image {k}: " + (f"mean diff {r['mean_diff']}, changed pixels {r['changed_fraction']}"
                                                   if 'mean_diff' in r else r['reason']))
            if all(r.get('similar') for r in results):
                rules.add('image compared visually')
                return 'image similar', sorted(rules), notes
        return 'image only', sorted(rules), notes
    return 'different', sorted(rules), notes


def strip(outputs):
    """Outputs without fields that never carry results (execution counts, metadata)."""
    return [{k: v for k, v in o.items() if k not in ('execution_count', 'metadata', 'prompt_number')} for o in outputs or []]


def code_cells(notebook):
    cells = notebook.get('cells')
    if cells is None:
        cells = [c for ws in notebook.get('worksheets', []) for c in ws.get('cells', [])]
    return [(i, c) for i, c in enumerate(cells) if c.get('cell_type') == 'code']


def compare_notebooks(original, executed):
    """Per-cell outcomes and score variants for two notebooks (dicts as loaded from JSON)."""
    left, right = code_cells(original), code_cells(executed)
    cells = []
    aligned = len(left) == len(right)
    for position, (index, cell) in enumerate(left):
        other = right[position][1] if position < len(right) else {}
        outcome, rules, notes = compare_cell(cell.get('outputs'), other.get('outputs'))
        source_changed = _text(cell.get('source', cell.get('input'))) != _text(other.get('source', other.get('input')))
        errors = [o.get('ename') for o in other.get('outputs') or [] if o.get('output_type') == 'error']
        cells.append(dict(cell_index=index, outcome=outcome, rules=rules, notes=notes,
                          stored_outputs=len(cell.get('outputs') or []), executed_outputs=len(other.get('outputs') or []),
                          error=errors[0] if errors else None, source_changed=source_changed))
    counts = {}
    for c in cells:
        counts[c['outcome']] = counts.get(c['outcome'], 0) + 1
    assessable = [c for c in cells if c['outcome'] not in ('empty', 'not stored')]
    reproduced = ('identical', 'identical normalised', 'identical tolerance', 'image similar')
    first_error = next((i for i, c in enumerate(cells) if c['error']), None)
    before = [c for c in (cells[:first_error] if first_error is not None else cells)
              if c['outcome'] not in ('empty', 'not stored')]

    def share(pool, accepted):
        return round(sum(c['outcome'] in accepted for c in pool) / len(pool), 4) if pool else None

    return dict(version=VERSION, aligned=aligned, cells=cells, code_cells=len(cells), assessable=len(assessable),
                counts=counts, first_error_cell=cells[first_error]['cell_index'] if first_error is not None else None,
                scores=dict(strict=share(assessable, ('identical',)), normalised=share(assessable, reproduced),
                            text_data=share(assessable, reproduced + ('image only',)),
                            before_error=share(before, reproduced)))
