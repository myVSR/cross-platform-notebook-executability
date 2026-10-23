#!/usr/bin/env python3
"""Detect licences after a run from the stored resource files (needs Docker; run on the workstation).

On the cluster there is no Docker, so the pipeline records licence status "unavailable". The run stores
README, licence and dependency files under artifacts/run-<id>/resource/ (src/resource_files.py); this
script unpacks them per run into a temporary folder, runs the same detector (src/licence.py,
licensee in Docker) and replaces the run's resource_licences row (detector marked "from stored
resource files"). Package manifests other than Python ones (package.json, *.gemspec, ...) are not
stored, so licence_any can miss licences that are named only there; licence_file is unaffected.

Usage:
  python3 pipeline/licences_from_artifacts.py --db merged.sqlite \\
      --path-map /home/user/hpc/results=hpc_run/hpc/results [--all]
--path-map OLD=NEW rewrites the stored artefact paths (recorded on the cluster) to where the results are
now; repeatable. Without --all only runs whose licence status is not "ok" are processed.
Never use the frozen evaluation database (data/output/db/db.sqlite): the script refuses it.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import licence  # noqa: E402
import provenance  # noqa: E402

FROZEN = (ROOT / 'data' / 'output' / 'db' / 'db.sqlite').resolve()


def mapped(path, maps):
    for old, new in maps:
        if path.startswith(old):
            return new + path[len(old):]
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--path-map', action='append', default=[], help='OLD=NEW prefix of stored artefact paths')
    parser.add_argument('--all', action='store_true', help='also runs whose licence status is already "ok"')
    args = parser.parse_args(argv)
    if args.db.resolve() == FROZEN:
        sys.exit('refusing to write to the frozen evaluation database; use a copy')
    maps = [tuple(item.split('=', 1)) for item in args.path_map]
    connection = sqlite3.connect(args.db)
    runs = [r[0] for r in connection.execute(
        'SELECT DISTINCT a.repository_run_id FROM run_artifacts a LEFT JOIN resource_licences l '
        "ON l.repository_run_id = a.repository_run_id WHERE a.kind = 'resource file' "
        + ('' if args.all else "AND COALESCE(l.status, '') != 'ok' ") + 'ORDER BY 1')]
    counts = {}
    for run_id in runs:
        files = [mapped(r[0], maps) for r in connection.execute(
            "SELECT path FROM run_artifacts WHERE repository_run_id = ? AND kind = 'resource file'", (run_id,))]
        with tempfile.TemporaryDirectory() as folder:
            missing = 0
            for stored in files:
                relative = stored.split('/resource/', 1)[1].removesuffix('.gz')
                target = Path(folder) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.write_bytes(gzip.decompress(Path(stored).read_bytes()))
                except FileNotFoundError:
                    missing += 1
            if missing:
                print(f'run {run_id}: {missing} stored files not found (check --path-map)', file=sys.stderr)
            result = licence.detect(Path(folder))
        result['detector'] = f"{result.get('detector') or 'licensee'} (from stored resource files)"
        provenance.record_licence(connection, run_id, json.dumps(result))
        counts[result.get('status')] = counts.get(result.get('status'), 0) + 1
        print(f"run {run_id}: {result.get('status')} {result.get('licence_file') or ''}")
    print(json.dumps(dict(runs=len(runs), status=counts)))


if __name__ == '__main__':
    main()
