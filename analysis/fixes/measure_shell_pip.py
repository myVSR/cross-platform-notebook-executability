#!/usr/bin/env python3
"""Round-2 check (2026-10-20): where did shell installs inside notebooks (`!pip install ...`) go?

Until 2026-10-20 the kernel did not run with the resource's venv active: PATH still started with the pyenv shims, so
`!pip` in a notebook ran the pipeline's own Python (~/.pyenv/versions/3.11.0), shared by every task on the cluster.
The packages named on such lines were installed into the venv beforehand (src/dependencies.py reads %pip and !pip
lines as declared dependencies), so the misdirected call mostly repeated an install the kernel already had. This
script measures how often it happened and how often it may have changed an outcome.

It reads every executed notebook (artifacts/run-*/executed/**/*.ipynb.gz) under the given study run folders and counts:
  notebooks / runs   whose outputs show pip working on the pipeline interpreter (its site-packages path, or
                     "Uninstalling attrs")
  installed          of these, notebooks in which pip installed or upgraded packages there ("Successfully installed")
  affected           notebooks whose first error is a missing module that one of their own `!pip install` lines names
                     (the outcome could have differed with the venv active)

Usage: python3 -I analysis/fixes/measure_shell_pip.py <results/study-r2-*> ... [--pipeline-python 3.11.0] [--csv out.csv]
Round 2: python3 -I analysis/fixes/measure_shell_pip.py hpc/results/study-r2-*
"""
import argparse
import csv
import gzip
import json
import re
import sys
from pathlib import Path

PIP_LINE = re.compile(r'\s*!\s*(?:python3? -m )?pip3? install (.*)')


def notebook_report(path: Path, marker: str):
    try:
        nb = json.load(gzip.open(path))
    except (OSError, ValueError):
        return None
    text = json.dumps(nb)
    if marker not in text and 'Uninstalling attrs' not in text:
        return None
    named, first, outputs = set(), None, []
    for cell in nb.get('cells', []):
        if cell.get('cell_type') != 'code':
            continue
        source = ''.join(cell.get('source', ''))
        for line in source.splitlines():
            m = PIP_LINE.match(line)
            if m:
                named |= {re.split(r'[=<>\[~!;]', w)[0].lower().replace('-', '_') for w in m.group(1).split()
                          if not w.startswith('-')}
        for out in cell.get('outputs', []):
            t = out.get('text', '')
            outputs.append(''.join(t) if isinstance(t, list) else str(t))
            if out.get('output_type') == 'error' and first is None:
                first = (out.get('ename') or '', out.get('evalue') or '')
    missing = ''
    if first and first[0] in ('ModuleNotFoundError', 'ImportError'):
        m = re.search(r"No module named '([^'.]+)", first[1])
        missing = m.group(1).lower() if m else ''
    return dict(notebook=str(path), run=str(path).split('/executed/')[0],
                installed=any('Successfully installed' in o for o in outputs),
                first_error=first[0] if first else '', missing_module=missing,
                affected=bool(missing and missing in named))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--pipeline-python', default='3.11.0')
    parser.add_argument('--csv', type=Path)
    args = parser.parse_args(argv)
    marker = f'versions/{args.pipeline_python}/lib/python{".".join(args.pipeline_python.split(".")[:2])}/site-packages'
    files = [p for run in args.runs for p in sorted(run.glob('task-*/artifacts/run-*/executed/**/*.ipynb.gz'))]
    rows = [r for r in (notebook_report(p, marker) for p in files) if r]
    print(f'executed notebooks read: {len(files)}')
    print(f'pip on the pipeline interpreter: {len(rows)} notebooks in {len({r["run"] for r in rows})} runs')
    print(f'  installed or upgraded packages there: {sum(r["installed"] for r in rows)} notebooks')
    print(f'  first error a missing module named on their own !pip line: {sum(r["affected"] for r in rows)} notebooks '
          f'({", ".join(sorted(r["missing_module"] for r in rows if r["affected"]))})')
    if args.csv:
        with open(args.csv, 'w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ['notebook'])
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == '__main__':
    sys.exit(main())
