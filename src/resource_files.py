#!/usr/bin/env python3
"""Store the small descriptive files of an acquired resource in the run's artefact folder.

The clone is deleted after a run (always on the cluster), but later steps need some of its files:
licence detection (licensee needs Docker, which the cluster lacks), classification (README as context,
Step 6), and dependency analyses. Stored (gzip, original paths) under <artifact dir>/resource/:
  - in the root and in every folder that holds an analysed notebook: README*, LICENSE*, LICENCE*,
    COPYING*, NOTICE*, AUTHORS*, CITATION.cff, codemeta.json, .zenodo.json
  - anywhere up to --max-depth folders deep: the dependency files the pipeline reads
    (requirements*.txt, environment.yml/.yaml, Pipfile(.lock), pyproject.toml, setup.py,
    setup.cfg, runtime.txt, .python-version, poetry.lock, apt.txt, install.R, DESCRIPTION) and
    container/environment specifications (Dockerfile*, Containerfile*, docker-compose*.yml,
    Singularity*, *.def, binder postBuild), which ReproScore and later analyses read
Skipped: .git, node_modules, virtual environments, files over --max-file-kb, and anything beyond
--max-total-mb. Prints a JSON summary (stored, skipped with reasons, bytes).

Usage: resource_files.py REPO_DIR ARTIFACT_DIR [--notebooks "a.ipynb;b/c.ipynb"]
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import json
import os
from pathlib import Path, PurePosixPath

DESCRIPTIVE = ['readme*', 'license*', 'licence*', 'copying*', 'notice*', 'authors*', 'citation.cff',
               'codemeta.json', '.zenodo.json']
DEPENDENCY = ['requirements*.txt', 'environment.yml', 'environment.yaml', 'pipfile', 'pipfile.lock',
              'pyproject.toml', 'setup.py', 'setup.cfg', 'runtime.txt', '.python-version', 'poetry.lock',
              'apt.txt', 'install.r', 'description',
              # container / environment specifications
              'dockerfile', 'dockerfile.*', '*.dockerfile', 'containerfile', 'containerfile.*',
              'docker-compose*.yml', 'docker-compose*.yaml', 'singularity', 'singularity.*', '*.def', 'postbuild']
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'env', '.tox', 'site-packages',
             '.ipynb_checkpoints', '__MACOSX'}


def matches(name, patterns):
    lower = name.lower()
    return any(fnmatch.fnmatch(lower, p) for p in patterns)


def candidates(repo, notebooks, max_depth):
    """(relative path, reason) of every file to keep, in a stable order."""
    folders = {PurePosixPath('.')} | {PurePosixPath(n).parent for n in notebooks}
    found = {}
    for current, dirs, files in os.walk(repo):
        relative_dir = PurePosixPath(Path(current).relative_to(repo).as_posix())
        depth = 0 if str(relative_dir) == '.' else len(relative_dir.parts)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and depth < max_depth)
        for name in sorted(files):
            path = relative_dir / name if str(relative_dir) != '.' else PurePosixPath(name)
            if relative_dir in folders and matches(name, DESCRIPTIVE):
                found[str(path)] = 'descriptive'
            elif matches(name, DEPENDENCY):
                found[str(path)] = 'dependency'
    return sorted(found.items())


def store(repo, out, notebooks=(), max_depth=3, max_file_kb=1024, max_total_mb=20):
    repo, out = Path(repo), Path(out) / 'resource'
    stored, skipped, total = [], [], 0
    for relative, reason in candidates(repo, notebooks, max_depth):
        source = repo / relative
        if source.is_symlink() or not source.is_file():
            skipped.append(dict(path=relative, reason='not a regular file'))
            continue
        size = source.stat().st_size
        if size > max_file_kb * 1024:
            skipped.append(dict(path=relative, reason=f'larger than {max_file_kb} KB', bytes=size))
            continue
        if total + size > max_total_mb * 1024 * 1024:
            skipped.append(dict(path=relative, reason=f'total limit {max_total_mb} MB reached', bytes=size))
            continue
        target = out / f'{relative}.gz'
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(source, 'rb') as stream, gzip.open(target, 'wb') as packed:
            packed.write(stream.read())
        stored.append(dict(path=relative, kind=reason, bytes=size))
        total += size
    return dict(stored=stored, skipped=skipped, bytes=total)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('repo_dir')
    parser.add_argument('artifact_dir')
    parser.add_argument('--notebooks', default='', help='analysed notebooks, separated by ";"')
    parser.add_argument('--max-depth', type=int, default=3)
    parser.add_argument('--max-file-kb', type=int, default=1024)
    parser.add_argument('--max-total-mb', type=int, default=20)
    args = parser.parse_args()
    notebooks = [n.strip() for n in args.notebooks.split(';') if n.strip()]
    print(json.dumps(store(args.repo_dir, args.artifact_dir, notebooks, args.max_depth, args.max_file_kb,
                           args.max_total_mb)))


if __name__ == '__main__':
    main()
