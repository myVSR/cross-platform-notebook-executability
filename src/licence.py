#!/usr/bin/env python3
"""File-based licence detection with licensee, the detector behind GitHub's licence field.

Usage: licence.py DIR [--json]
Runs `licensee detect --json` (Docker image LICENSEE_IMAGE, default
notebookfair/licensee:10.1.0, built from docker/licensee) on DIR mounted read-only,
so GitHub, Codeberg and Zenodo resources are measured with the same detector
and version. Runtime (LICENSEE_RUNTIME = auto | docker | singularity): Docker if
available, otherwise Singularity/Apptainer with the same image converted to a SIF
(LICENSEE_SIF, default ~/.notebookfair/licensee-10.1.0.sif; pipeline/build_licensee_sif.sh),
e.g. on the cluster, where there is no Docker. The image id is recorded in the detector field. Prints the licence-file result (SPDX id, NOASSERTION, or empty) or,
with --json, the full result:

  licence_file   the project licence from licence files only, as GitHub reports it:
                 one SPDX id if every licence file names the same licence,
                 NOASSERTION if they differ or the text is not a known licence,
                 empty if there is no licence file
  licence_files  every licence named by a licence file
  licence_any    every licence named anywhere (also README and package manifests)
  matched_files  filename, kind (licence file / readme / package), matcher, confidence, licence
  status         ok | unavailable (no Docker or no image) | error

The value from the platform API stays in repository_metadata.license; this is a
separate, labelled measurement (no value is invented in the metadata).
Python 3.10+ standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

IMAGE = os.environ.get('LICENSEE_IMAGE', 'notebookfair/licensee:10.1.0')
SIF = Path(os.environ.get('LICENSEE_SIF') or Path.home() / '.notebookfair' / 'licensee-10.1.0.sif')
LICENCE_FILE_MATCHERS = {'exact', 'dice', 'copyright', 'reference', 'cc_false_positive'}


def kind(matched):
    name = str(matched.get('filename', '')).lower()
    matcher = str((matched.get('matcher') or {}).get('name', ''))
    if name.startswith('readme'):
        return 'readme'
    # no matcher: a licence file whose text is not a known licence (-> NOASSERTION)
    if matcher and matcher not in LICENCE_FILE_MATCHERS:
        return 'package'
    return 'licence file'


def summarise(output):
    """Turn licensee's JSON into the result described in the module docstring."""
    files = [dict(filename=m.get('filename'), kind=kind(m), matcher=(m.get('matcher') or {}).get('name'),
                  confidence=(m.get('matcher') or {}).get('confidence'), licence=m.get('matched_license'))
             for m in output.get('matched_files') or []]
    licence_files = sorted({f['licence'] or 'NOASSERTION' for f in files if f['kind'] == 'licence file'})
    if not licence_files:
        project = ''
    elif len(licence_files) == 1:
        project = licence_files[0]
    else:
        project = 'NOASSERTION'
    return dict(licence_file=project, licence_files=licence_files,
                licence_any=sorted({f['licence'] or 'NOASSERTION' for f in files}), matched_files=files)


def sif_image_id():
    """Docker image id the SIF was built from (written next to it by build_licensee_sif.sh)."""
    info = Path(f'{SIF}.json')
    try:
        return json.loads(info.read_text()).get('docker_image_id')
    except (OSError, ValueError):
        return None


def runtime():
    """(name, command prefix to run licensee with /repo mounted) or (None, reason)."""
    wanted = os.environ.get('LICENSEE_RUNTIME', 'auto').lower()
    if wanted in ('auto', 'docker') and shutil.which('docker'):
        return 'docker', None
    if wanted in ('auto', 'singularity'):
        binary = shutil.which('apptainer') or shutil.which('singularity')
        if binary and SIF.is_file():
            return 'singularity', binary
        if wanted == 'singularity':
            return None, 'singularity/apptainer not found' if not binary else f'no SIF image at {SIF}'
    if wanted == 'docker':
        return None, 'docker not installed'
    return None, f'docker not installed and no singularity image at {SIF}'


def detect(directory, timeout=300):
    base = dict(detector=f'licensee ({IMAGE})', licence_file='', licence_files=[], licence_any=[], matched_files=[])
    name, binary = runtime()
    if name is None:
        return dict(base, status='unavailable', error=binary)
    directory = Path(directory).resolve()
    if not directory.is_dir():
        return dict(base, status='error', error=f'not a directory: {directory}')
    if name == 'docker':
        command = ['docker', 'run', '--rm', '--network', 'none', '-v', f'{directory}:/repo:ro', IMAGE,
                   'detect', '--json', '/repo']
    else:
        image_id = sif_image_id()
        base['detector'] = f"licensee ({IMAGE} via {Path(binary).name} {SIF.name}" + \
            (f", image {image_id[:19]}" if image_id else "") + ")"
        command = [binary, 'exec', '--containall', '--bind', f'{directory}:/repo:ro', str(SIF),
                   'licensee', 'detect', '--json', '/repo']
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return dict(base, status='error', error=f'licensee timed out after {timeout}s')
    if result.returncode != 0 and not result.stdout.strip():
        reason = result.stderr.strip()[:300]
        status = 'unavailable' if ('Unable to find image' in reason or 'Cannot connect' in reason
                                   or 'permission denied' in reason.lower()) else 'error'
        return dict(base, status=status, error=reason)
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        return dict(base, status='error', error=f'unexpected licensee output: {result.stdout[:200]}')
    return dict(base, status='ok', **summarise(output))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    result = detect(args.directory)
    print(json.dumps(result) if args.json else result['licence_file'])
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
