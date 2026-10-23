#!/usr/bin/env python3
"""Build the two manual audits of the paper (Threats to Validity): output normalisation and failure classification.

Both audits are blind: annotators see the evidence, never the pipeline's label. Each audit is one self-contained
HTML page (open it in a browser; answers are kept in the browser and downloaded as a CSV), plus a key file with the
pipeline's labels that stays with the authors. analysis/audit/score_audits.py compares the answers with each other
(Cohen's kappa) and with the key.

Normalisation audit (one comparison cell per item, stratified, seeded):
  reproduced but not identical   identical normalised, image similar, identical tolerance: does the match hide a
                                 real difference? (a false match)
  different                      does the mismatch only come from incidental content? (a false mismatch)
  The items are shuffled together, so the annotator cannot tell which group an item comes from.
Failure audit (the first error of one failing notebook per item, stratified by the pipeline's category, seeded;
  the failures that context moved to EXTERNAL_SERVICE_ERROR are drawn on purpose): annotators assign the cause from
  a written guideline (analysis/audit/GUIDELINE.md).

Inputs: the merged database of the round and the run artefacts (original and executed notebooks). Artefact paths
stored on the HPC are found under --artifact-root.
Usage:
  python3 analysis/audit/make_audit_kits.py --db results/study-r2/merged.sqlite \
      --artifact-root hpc_20_10_2026_complete_run/hpc --out results/audit-r2 [--seed 20261021]
Output: <out>/normalisation.html, normalisation-key.csv, failures.html, failures-key.csv, MANIFEST.json
The kits contain third-party notebook content: share them only with the annotators, do not publish them.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html
import json
import random
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FROZEN = (ROOT / 'data' / 'output' / 'db' / 'db.sqlite').resolve()
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

NORMALISATION_PLAN = {'identical normalised': 35, 'image similar': 30, 'identical tolerance': 15, 'different': 40}
FAILURE_PLAN = {'DEPENDENCY_ERROR': 18, 'FILE_ERROR': 16, 'CODE_ERROR': 14, 'OTHER_ERROR': 12, 'DATA_ERROR': 10,
                'EXTERNAL_SERVICE_ERROR': 16, 'EXECUTION_FAILURE': 8, 'EXECUTION_ENVIRONMENT_ERROR': 4,
                'NETWORK_ERROR': 2}
# the guideline's labels (analysis/audit/GUIDELINE.md) and the pipeline categories they correspond to
FAILURE_LABELS = [('dependency', 'Dependency: a package is missing, cannot be installed, or has an incompatible version'),
                  ('file', 'Missing file: a file or folder the notebook needs is not there'),
                  ('data', 'Data: the data is there but its content or format does not fit'),
                  ('code', "Code: a fault in the notebook's own code (order of cells, typo, wrong use)"),
                  ('external', 'External service: needs credentials, an API key, or a remote service answered with an error'),
                  ('network', 'Network: a remote host could not be reached at all'),
                  ('environment', 'Environment: hardware, GPU, memory, system library, operating system, kernel'),
                  ('no-execution', 'No execution: the notebook could not be run at all (invalid file, kernel crash, timeout)'),
                  ('other', 'Other cause'), ('cannot-judge', 'Cannot judge from what is shown')]
CATEGORY_LABEL = {'DEPENDENCY_ERROR': 'dependency', 'FILE_ERROR': 'file', 'DATA_ERROR': 'data', 'CODE_ERROR': 'code',
                  'EXTERNAL_SERVICE_ERROR': 'external', 'NETWORK_ERROR': 'network', 'RESOURCE_ERROR': 'environment',
                  'EXECUTION_ENVIRONMENT_ERROR': 'environment', 'EXECUTION_FAILURE': 'no-execution',
                  'OTHER_ERROR': 'other'}
NORMALISATION_LABELS = [('same', 'Same result: the outputs differ only in incidental content (formatting, timing, '
                                 'addresses, paths, rendering, progress output)'),
                        ('different', 'Different result: a reader would get a different result from the two outputs'),
                        ('cannot-judge', 'Cannot judge from what is shown')]
TEXT_LIMIT, LINES_LIMIT = 4000, 60


def local(path: str, root: Path) -> Path:
    marker = '/hpc/'
    return root / path.split(marker, 1)[1] if marker in path else Path(path)


def load_notebook(path: Path):
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def code_cells(notebook):
    cells = notebook.get('cells')
    if cells is None:
        cells = [c for ws in notebook.get('worksheets', []) for c in ws.get('cells', [])]
    return [(i, c) for i, c in enumerate(cells) if c.get('cell_type') == 'code']


def text(value):
    return ''.join(value) if isinstance(value, list) else (value or '')


def clip(value, limit=TEXT_LIMIT):
    value = ANSI.sub('', value)
    lines = value.splitlines()
    if len(lines) > LINES_LIMIT:
        value = '\n'.join(lines[:LINES_LIMIT // 2] + [f'... [{len(lines) - LINES_LIMIT} lines cut] ...']
                          + lines[-LINES_LIMIT // 2:])
    return value if len(value) <= limit else value[:limit // 2] + f'\n... [{len(value) - limit} characters cut] ...\n' + value[-limit // 2:]


def collapse(value):
    """Consecutive repeated lines once, with a count (warnings printed per call bury the result)."""
    lines, out = value.splitlines(), []
    for line in lines:
        if out and out[-1][0] == line:
            out[-1][1] += 1
        else:
            out.append([line, 1])
    return '\n'.join(line + (f'   [repeated {n} times]' if n > 1 else '') for line, n in out)


def merged(outputs):
    """Consecutive stream outputs of the same stream as one (kernels split them arbitrarily)."""
    result = []
    for out in outputs or []:
        if out.get('output_type') == 'stream' and result and result[-1].get('output_type') == 'stream' \
                and result[-1].get('name') == out.get('name'):
            result[-1] = dict(result[-1], text=text(result[-1].get('text')) + text(out.get('text')))
        else:
            result.append(out)
    return result


def render_outputs(outputs) -> str:
    """Outputs as HTML: text escaped, images inline, HTML outputs in a sandboxed frame (no scripts)."""
    parts = []
    for out in merged(outputs):
        kind = out.get('output_type')
        if kind == 'stream':
            parts.append(f'<pre class="stream">{html.escape(clip(collapse(text(out.get("text")))))}</pre>')
        elif kind == 'error':
            body = '\n'.join(text(t) for t in out.get('traceback') or []) or f"{out.get('ename')}: {out.get('evalue')}"
            parts.append(f'<pre class="error">{html.escape(clip(body))}</pre>')
        else:
            data = out.get('data') or {}
            if 'image/png' in data or 'image/jpeg' in data:
                mime = 'image/png' if 'image/png' in data else 'image/jpeg'
                raw = text(data[mime]).replace('\n', '')
                parts.append(f'<img alt="image output" src="data:{mime};base64,{raw}">')
            elif 'image/svg+xml' in data:
                svg = base64.b64encode(text(data['image/svg+xml']).encode()).decode()
                parts.append(f'<img alt="svg output" src="data:image/svg+xml;base64,{svg}">')
            elif 'text/html' in data:
                parts.append('<iframe sandbox="" class="html" srcdoc="'
                             + html.escape(clip(text(data['text/html']), 20000), quote=True) + '"></iframe>')
            elif 'text/plain' in data:
                parts.append(f'<pre>{html.escape(clip(text(data["text/plain"])))}</pre>')
            else:
                parts.append(f'<pre class="muted">[{html.escape(", ".join(sorted(data)) or kind or "output")}]</pre>')
    return ''.join(parts) or '<p class="muted">(no output)</p>'


def stratified(rows, plan, key, rng):
    chosen = []
    for group, n in plan.items():
        pool = [r for r in rows if r[key] == group]
        rng.shuffle(pool)
        chosen += pool[:n]
    return chosen


def latest_runs_sql():
    return 'SELECT repository_id, MAX(id) AS run_id FROM repository_runs GROUP BY repository_id'


def artefact(con, run_id, kind, notebook_path):
    row = con.execute('SELECT path FROM run_artifacts WHERE repository_run_id = ? AND kind = ? AND notebook_path = ? '
                      'ORDER BY id DESC LIMIT 1', (run_id, kind, notebook_path)).fetchone()
    return row[0] if row else None


def normalisation_items(con, root, rng):
    rows = [dict(zip(('id', 'run_id', 'notebook', 'cell_index', 'outcome', 'rules', 'platform'), r)) for r in con.execute(
        'WITH latest AS (' + latest_runs_sql() + ') '
        'SELECT c.id, c.repository_run_id, n.name, c.cell_index, c.outcome, c.rules_json, LOWER(r.platform) '
        'FROM notebook_cell_comparisons c JOIN latest l ON l.run_id = c.repository_run_id '
        'JOIN notebooks n ON n.id = c.notebook_id JOIN repositories r ON r.id = n.repository_id '
        'WHERE c.outcome IN (%s)' % ','.join('?' * len(NORMALISATION_PLAN)), list(NORMALISATION_PLAN))]
    items, cache = [], {}
    for row in stratified(rows, {k: 3 * v for k, v in NORMALISATION_PLAN.items()}, 'outcome', rng):
        if sum(i['outcome'] == row['outcome'] for i in items) >= NORMALISATION_PLAN[row['outcome']]:
            continue
        pair = []
        for kind in ('original notebook', 'executed notebook'):
            path = artefact(con, row['run_id'], kind, row['notebook'])
            if path and path not in cache:
                cache[path] = load_notebook(local(path, root))
            pair.append(cache.get(path) if path else None)
        if not all(pair):
            continue                                        # artefact missing locally: take the next candidate
        left = dict(code_cells(pair[0]))
        right = code_cells(pair[1])
        positions = [i for i, _ in code_cells(pair[0])]
        if row['cell_index'] not in left:
            continue
        position = positions.index(row['cell_index'])
        if position >= len(right):
            continue
        cell, other = left[row['cell_index']], right[position][1]
        items.append(dict(row, source=text(cell.get('source', cell.get('input'))),
                          stored=render_outputs(cell.get('outputs')), executed=render_outputs(other.get('outputs'))))
    rng.shuffle(items)
    for number, item in enumerate(items, 1):
        item['item'] = f'N{number:03d}'
    return items


def failure_items(con, root, rng):
    rows = [dict(zip(('execution_id', 'run_id', 'notebook', 'category', 'by_type', 'error_type', 'message', 'platform',
                      'status'), r)) for r in con.execute(
        'WITH latest AS (' + latest_runs_sql() + ') '
        'SELECT e.id, e.repository_run_id, e.notebook_name, e.error_category, e.error_category_by_type, e.error_type, '
        '       e.error_message, LOWER(r.platform), e.execution_status '
        'FROM notebook_executions e JOIN latest l ON l.run_id = e.repository_run_id '
        "JOIN repositories r ON r.id = e.repository_id WHERE e.execution_status != 'SUCCESS'")]
    for r in rows:                       # no executed copy: no exception type, the category is the type-based one
        r['by_type'] = r['by_type'] or r['category']
    # the context-moved failures first among EXTERNAL_SERVICE_ERROR: half of that stratum
    moved = [r for r in rows if r['category'] == 'EXTERNAL_SERVICE_ERROR' and r['by_type'] != r['category']]
    rng.shuffle(moved)
    plan = dict(FAILURE_PLAN)
    chosen_moved = moved[:plan['EXTERNAL_SERVICE_ERROR'] // 2]
    plan['EXTERNAL_SERVICE_ERROR'] -= len(chosen_moved)
    rest = [r for r in rows if r not in chosen_moved]
    candidates = chosen_moved + stratified(rest, {k: 3 * v for k, v in plan.items()}, 'category', rng)
    items, counts = [], {}
    for row in candidates:
        if counts.get(row['category'], 0) >= FAILURE_PLAN[row['category']]:
            continue
        path = artefact(con, row['run_id'], 'executed notebook', row['notebook'])
        notebook = load_notebook(local(path, root)) if path else None
        cells = code_cells(notebook) if notebook else []
        failing = next((k for k, (_, c) in enumerate(cells) if any(o.get('output_type') == 'error'
                                                                   for o in c.get('outputs') or [])), None)
        if row['category'] != 'EXECUTION_FAILURE' and failing is None:
            continue                                        # no executed copy with the error: take the next candidate
        context = []
        if failing is not None:
            earlier = cells[max(0, failing - 3):failing]
            flagged = [(i, c) for i, c in cells[:max(0, failing - 3)]
                       if re.search(r'getpass|input\(|os\.environ|api[_ -]?key|token|password|login|drive\.mount',
                                    text(c.get('source')), re.I)]
            context = [('earlier cell (flagged: credentials or input)', text(c.get('source'))) for _, c in flagged[-3:]]
            context += [('earlier cell', text(c.get('source'))) for _, c in earlier]
        failed_packages = [n for (n,) in con.execute("SELECT name FROM run_packages WHERE repository_run_id = ? "
                                                     "AND status = 'failed' ORDER BY name", (row['run_id'],))]
        items.append(dict(row, context=context,
                          cell=text(cells[failing][1].get('source')) if failing is not None else '',
                          outputs=render_outputs(cells[failing][1].get('outputs')) if failing is not None else
                          f'<pre class="error">{html.escape(clip(row["message"] or ""))}</pre>',
                          failed_packages=failed_packages))
        counts[row['category']] = counts.get(row['category'], 0) + 1
    rng.shuffle(items)
    for number, item in enumerate(items, 1):
        item['item'] = f'F{number:03d}'
    return items


STYLE = """
:root { --bg:#fbfaf7; --fg:#1f1f1f; --muted:#6b6a65; --line:#dddbd3; --card:#ffffff; --accent:#1c5cab; --err:#b3261e; }
@media (prefers-color-scheme: dark) { :root { --bg:#161616; --fg:#ecebe6; --muted:#a3a29c; --line:#3a3936; --card:#202020;
  --accent:#86b6ef; --err:#ee8a83; } }
body { background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; margin:0 auto; max-width:1200px; padding:16px; }
h1 { font-size:1.4rem; } h2 { font-size:1.05rem; margin:0 0 8px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:14px; margin:18px 0; }
.cols { display:grid; grid-template-columns:1fr 1fr; gap:12px; } @media (max-width:800px) { .cols { grid-template-columns:1fr; } }
pre { white-space:pre-wrap; word-break:break-word; font:12.5px/1.35 ui-monospace, monospace; background:rgba(127,127,127,.08);
  padding:8px; border-radius:6px; margin:4px 0; max-height:420px; overflow:auto; }
pre.error { color:var(--err); } .muted { color:var(--muted); } img { max-width:100%; background:#fff; }
iframe.html { width:100%; height:260px; border:1px solid var(--line); background:#fff; }
fieldset { border:none; padding:0; margin:10px 0 0; } label { display:block; margin:3px 0; cursor:pointer; }
textarea { width:100%; min-height:40px; font:inherit; background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:6px; }
.bar { position:sticky; top:0; background:var(--bg); padding:8px 0; border-bottom:1px solid var(--line); display:flex; gap:12px;
  align-items:center; flex-wrap:wrap; z-index:2; }
button { font:inherit; padding:6px 14px; border-radius:6px; border:1px solid var(--accent); background:var(--accent); color:#fff; cursor:pointer; }
input[type=text] { font:inherit; padding:5px; }
.tag { font-size:12px; color:var(--muted); }
"""

SCRIPT = """
const KEY = 'audit:' + document.body.dataset.audit;
function load() { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; } }
function save(state) { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} }
const state = load();
document.querySelectorAll('[data-item]').forEach(card => {
  const id = card.dataset.item, s = state[id] || {};
  card.querySelectorAll('input[type=radio]').forEach(r => { r.checked = s.label === r.value;
    r.addEventListener('change', () => { (state[id] = state[id] || {}).label = r.value; save(state); progress(); }); });
  const t = card.querySelector('textarea'); t.value = s.comment || '';
  t.addEventListener('input', () => { (state[id] = state[id] || {}).comment = t.value; save(state); });
});
const who = document.getElementById('annotator'); who.value = state._annotator || '';
who.addEventListener('input', () => { state._annotator = who.value; save(state); });
function progress() {
  const n = document.querySelectorAll('[data-item]').length;
  const done = [...document.querySelectorAll('[data-item]')].filter(c => (state[c.dataset.item] || {}).label).length;
  document.getElementById('progress').textContent = done + ' of ' + n + ' answered';
}
progress();
document.getElementById('download').addEventListener('click', () => {
  if (!who.value.trim()) { alert('Please enter your initials first.'); return; }
  const rows = [['audit', 'item', 'annotator', 'label', 'comment']];
  document.querySelectorAll('[data-item]').forEach(c => { const s = state[c.dataset.item] || {};
    rows.push([document.body.dataset.audit, c.dataset.item, who.value.trim(), s.label || '', (s.comment || '').replace(/\\s+/g, ' ')]); });
  const csv = rows.map(r => r.map(v => '"' + String(v).replace(/"/g, '""') + '"').join(',')).join('\\n') + '\\n';
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
  a.download = document.body.dataset.audit + '-' + who.value.trim() + '.csv'; a.click();
});
"""


def page(audit, title, intro, cards):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{STYLE}</style></head><body data-audit="{audit}">
<h1>{html.escape(title)}</h1>{intro}
<div class="bar"><label style="display:inline">Your initials: <input type="text" id="annotator" size="6"></label>
<span id="progress"></span><button id="download">Download answers (CSV)</button>
<span class="tag">Answers are kept in this browser until you download them.</span></div>
{''.join(cards)}<script>{SCRIPT}</script></body></html>"""


def radios(item, labels):
    return '<fieldset>' + ''.join(
        f'<label><input type="radio" name="{item}" value="{v}"> {html.escape(t)}</label>' for v, t in labels) + \
        f'</fieldset><textarea placeholder="Comment (optional)"></textarea>'


def normalisation_page(items):
    intro = """<p>Each item shows one code cell of a published notebook: the output <b>stored</b> by its authors and the output
of our <b>re-execution</b>. Decide whether a reader would get the <b>same result</b> from both. Differences in formatting,
timing, memory addresses, temporary paths, widget identifiers, progress bars or image encoding are incidental. A changed number,
table, plot or message that carries a result is a different result. Small numeric noise counts as the same result only when
it would not change what a reader concludes. See GUIDELINE.md, part A.</p>"""
    cards = [f"""<div class="card" data-item="{i['item']}"><h2>{i['item']}</h2>
<pre>{html.escape(clip(i['source'], 2500))}</pre><div class="cols"><div><b>Stored</b>{i['stored']}</div>
<div><b>Re-executed</b>{i['executed']}</div></div>{radios(i['item'], NORMALISATION_LABELS)}</div>""" for i in items]
    return page('normalisation', 'Audit A: stored vs re-executed outputs', intro, cards)


def failure_page(items):
    intro = """<p>Each item shows the <b>first error</b> of a notebook that did not run cleanly in our re-execution: the failing
cell, its error output, up to three earlier cells (and earlier cells that ask for input or credentials), and the packages
that could not be installed for the resource. Choose the <b>cause</b> of the failure with GUIDELINE.md, part B. Choose the
cause, not the exception type: an <code>AttributeError</code> raised inside a library because a newer version removed a
function is a dependency problem.</p>"""
    cards = []
    for i in items:
        context = ''.join(f'<p class="tag">{html.escape(label)}</p><pre>{html.escape(clip(src, 1500))}</pre>'
                          for label, src in i['context'])
        packages = ', '.join(i['failed_packages']) or 'none'
        cards.append(f"""<div class="card" data-item="{i['item']}"><h2>{i['item']}</h2>
<p class="tag">Notebook: {html.escape(i['notebook'] or '')} &middot; packages that could not be installed: {html.escape(packages)}</p>
{context}<p class="tag">failing cell</p><pre>{html.escape(clip(i['cell'], 2500))}</pre>{i['outputs']}
{radios(i['item'], FAILURE_LABELS)}</div>""")
    return page('failures', 'Audit B: causes of failure', intro, cards)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--artifact-root', type=Path, required=True, help='local folder that holds the HPC results/')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=20261021)
    args = parser.parse_args(argv)
    if args.db.resolve() == FROZEN:
        sys.exit('refusing the frozen evaluation database')
    if args.out.exists() and any(args.out.iterdir()):
        sys.exit(f'{args.out} is not empty; the kits are drawn once (answers refer to their item numbers)')
    args.out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    norm = normalisation_items(con, args.artifact_root, random.Random(f'{args.seed}-normalisation'))
    fail = failure_items(con, args.artifact_root, random.Random(f'{args.seed}-failures'))
    (args.out / 'normalisation.html').write_text(normalisation_page(norm), encoding='utf-8')
    (args.out / 'failures.html').write_text(failure_page(fail), encoding='utf-8')
    import csv
    with open(args.out / 'normalisation-key.csv', 'w', newline='', encoding='utf-8') as stream:
        w = csv.writer(stream)
        w.writerow(['item', 'comparison_id', 'run_id', 'platform', 'notebook', 'cell_index', 'outcome', 'rules', 'expected'])
        for i in sorted(norm, key=lambda i: i['item']):
            w.writerow([i['item'], i['id'], i['run_id'], i['platform'], i['notebook'], i['cell_index'], i['outcome'],
                        i['rules'], 'different' if i['outcome'] == 'different' else 'same'])
    with open(args.out / 'failures-key.csv', 'w', newline='', encoding='utf-8') as stream:
        w = csv.writer(stream)
        w.writerow(['item', 'execution_id', 'run_id', 'platform', 'notebook', 'category', 'category_by_type',
                    'moved_by_context', 'expected', 'expected_by_type', 'error_type'])
        for i in sorted(fail, key=lambda i: i['item']):
            w.writerow([i['item'], i['execution_id'], i['run_id'], i['platform'], i['notebook'], i['category'], i['by_type'],
                        int(i['by_type'] != i['category']), CATEGORY_LABEL.get(i['category'], 'other'),
                        CATEGORY_LABEL.get(i['by_type'], 'other'), i['error_type']])
    manifest = dict(created_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), seed=args.seed,
                    db=str(args.db), db_sha256=hashlib.sha256(args.db.read_bytes()).hexdigest(),
                    normalisation_plan=NORMALISATION_PLAN, failure_plan=FAILURE_PLAN,
                    normalisation_items={k: sum(i['outcome'] == k for i in norm) for k in NORMALISATION_PLAN},
                    failure_items={k: sum(i['category'] == k for i in fail) for k in FAILURE_PLAN},
                    failure_items_moved_by_context=sum(i['by_type'] != i['category'] for i in fail),
                    files={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(args.out.iterdir())})
    (args.out / 'MANIFEST.json').write_text(json.dumps(manifest, indent=1) + '\n')
    print(json.dumps({k: manifest[k] for k in ('normalisation_items', 'failure_items', 'failure_items_moved_by_context')},
                     indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
