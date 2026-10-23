#!/usr/bin/env python3
"""List the kernel language of every original notebook stored by a round's passes.

The pipeline registers every .ipynb as a Python notebook (src/repo.sh) and runs it with the resource's Python
kernel, so R, Julia, ... notebooks enter the analysis and fail. This script reads only the notebooks' metadata
from the artefact store, so the analysis can count and exclude them.

Usage (on the cluster, in the study folder; light, reads metadata only):
    python3 analysis/paper/notebook_languages.py results/study-r3-* > notebook-languages-r3.csv

Artefacts are per array task: <pass>/task-N/artifacts/run-<id>/original/<notebook>.gz, where <id> is the run id in
that task's database (merged.sqlite: merge_sources.source_path + merge_run_map.source_run_id).

Output columns: pass, task, run_id (in the task database), notebook, language, kernel (empty if the notebook has no
such metadata; 'invalid' if it cannot be parsed).
"""
import csv
import gzip
import json
import sys
from pathlib import Path


def language_of(path):
    try:
        with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
            metadata = json.load(f).get('metadata') or {}
    except (OSError, ValueError, AttributeError):
        return 'invalid', ''
    info = metadata.get('language_info') or {}
    kernelspec = metadata.get('kernelspec') or {}
    language = str(info.get('name') or kernelspec.get('language') or '').lower()
    return language, str(kernelspec.get('name') or '')


def main(passes):
    out = csv.writer(sys.stdout)
    out.writerow(['pass', 'task', 'run_id', 'notebook', 'language', 'kernel'])
    for folder in map(Path, passes):
        for run in sorted(folder.glob('task-*/artifacts/run-*')):
            task = run.parent.parent.name
            original = run / 'original'
            for path in sorted(original.rglob('*.ipynb.gz')):
                language, kernel = language_of(path)
                out.writerow([folder.name, task, run.name[len('run-'):], str(path.relative_to(original))[:-len('.gz')],
                              language, kernel])


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
