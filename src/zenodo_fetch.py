#!/usr/bin/env python3
"""Acquire the files of one Zenodo record into a local folder.

Usage: zenodo_fetch.py RECORD_ID DEST [--plan-only] [--manifest PATH]

Rules (replacing "first ZIP only, 256 MB total"):
- every .ipynb file is downloaded;
- every archive (.zip, .tar, .tar.gz/.tgz, .tar.bz2, .tar.xz, .rar, .7z) is
  downloaded and extracted. Archives that contain notebooks, or whose content
  cannot be listed, may be up to ZENODO_MAX_ARCHIVE_BYTES (default 2 GiB);
  archives without notebooks count as data files. A ZIP above the archive
  limit whose content Zenodo can list is acquired member by member
  ("partial"): its notebooks, plus its other members within the file limit;
- other (data) files are downloaded up to ZENODO_MAX_FILE_BYTES each
  (default 256 MiB);
- downloads stop at ZENODO_MAX_TOTAL_BYTES (default 5 GiB);
- an extracted archive itself stays next to its contents up to ZENODO_KEEP_ARCHIVE_BYTES (default: the data-file
  limit), since notebooks may open the archive (study run 2026-10: a notebook reading risis_dataset.zip failed).
A notebook that is byte-identical to one from a *different* record file (the
same notebook shipped loose and inside an archive, or in two archive versions)
is a packaging duplicate: it is listed in the manifest's "duplicate_notebooks"
so discovery registers it once. Identical copies within one archive (e.g. a
repository snapshot with docs/ copies of its examples) are kept, as they would
be in a Git clone of the same repository.
Skipped files are listed with their reason in the manifest
(DEST/.zenodo-acquisition.json by default), so that later missing-file errors
can be traced to them. Every download is checked against Zenodo's MD5.

The API is retried on HTTP 429 (honouring Retry-After) and on 5xx/network
errors; ZENODO_TOKEN is sent when set. Archive members with absolute paths or
'..' components are rejected. Links: Python's zipfile stores a ZIP symlink as a
small file holding the target (recorded as such); tar links are extracted only
through the 'data' filter, which refuses links leaving the folder; bsdtar
refuses unsafe links by default.

The manifest records, per record file and per archive member, size, checksum
(MD5 / CRC32) and whether it was verified, the action and reason, and for every
transfer its seconds and bytes (including retries); plus the record version,
revision and concept id, the number of API requests, start and end time. Archives are extracted into DEST as a
user would unpack them; a record that is exactly one archive with a single
top-level folder is flattened (the GitHub-release layout).

Exit status: 0 acquired, 1 failed (reason in the manifest's "error" field),
3 nothing downloadable (no files, or every file skipped).
Python 3.10+ standard library; .rar/.7z need bsdtar (libarchive-tools).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tarfile
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

API = 'https://zenodo.org/api/records/'
MiB = 1024 * 1024
TAR_SUFFIXES = ('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')
LIBARCHIVE_SUFFIXES = ('.rar', '.7z')
ARCHIVE_SUFFIXES = ('.zip',) + TAR_SUFFIXES + LIBARCHIVE_SUFFIXES
MANIFEST_NAME = '.zenodo-acquisition.json'
COUNTERS = dict(api_requests=0)


class FetchError(Exception):
    pass


def limits():
    def env(name, default):
        return int(os.environ.get(name) or default)
    return dict(file=env('ZENODO_MAX_FILE_BYTES', 256 * MiB),
                archive=env('ZENODO_MAX_ARCHIVE_BYTES', 2048 * MiB),
                total=env('ZENODO_MAX_TOTAL_BYTES', 5120 * MiB))


def headers():
    result = {'User-Agent': 'NotebookFair (+https://anonymous.example/repository)'}
    token = os.environ.get('ZENODO_TOKEN', '').strip()
    if token:
        result['Authorization'] = f'Bearer {token}'
    return result


def open_url(url, timeout=60, attempts=5, max_wait=300, sleep=time.sleep):
    """urlopen with retries on 429 (Retry-After), 5xx and network errors."""
    waited = 0
    for attempt in range(1, attempts + 1):
        COUNTERS['api_requests'] += 1
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers()), timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500 or attempt == attempts:
                raise FetchError(f'HTTP {error.code} for {url}') from error
            retry_after = error.headers.get('Retry-After') if error.headers else None
            delay = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == attempts:
                raise FetchError(f'network error for {url}: {error}') from error
            delay = 2 ** attempt
        delay = min(delay, max_wait - waited)
        if delay <= 0:
            raise FetchError(f'retry budget of {max_wait}s exhausted for {url}')
        waited += delay
        sleep(delay)


def get_json(url):
    with open_url(url, timeout=60) as response:
        return json.load(response)


def kind(key):
    lower = key.lower()
    if lower.endswith('.ipynb'):
        return 'notebook'
    if lower.endswith(ARCHIVE_SUFFIXES):
        return 'archive'
    return 'data'


def safe_member(name):
    """Return the member path if it stays inside the destination, else None."""
    path = PurePosixPath(name.replace('\\', '/'))
    if not name or path.is_absolute() or '..' in path.parts or (path.parts and ':' in path.parts[0]):
        return None
    return path


def is_noise(path):
    """macOS resource forks and checkpoint copies are not notebooks of the record."""
    parts = PurePosixPath(path).parts
    return '__MACOSX' in parts or '.ipynb_checkpoints' in parts or (parts and parts[-1].startswith('._'))


def list_zip(record_id, key):
    """(notebook count or None if unknown, complete member list or None) via Zenodo's container listing."""
    url = f'{API}{record_id}/files/{urllib.parse.quote(key, safe="")}/container'
    try:
        listing = get_json(url)
    except FetchError:
        return None, None
    members = listing.get('entries', [])
    found = sum(1 for e in members if e['key'].lower().endswith('.ipynb') and not is_noise(e['key']))
    if listing.get('truncated'):
        return found or None, None  # a truncated listing without notebooks proves nothing
    return found, members


def plan(record, record_id, cap, list_containers=True):
    """Decide for every file of the record whether to download it, and why."""
    entries, total = [], 0
    files = sorted(record.get('files') or [], key=lambda f: (kind(f.get('key', '')) != 'notebook', f.get('key', '')))
    for item in files:
        key = str(item.get('key', ''))
        size = int(item.get('size') or 0)
        entry = dict(key=key, size=size, kind=kind(key), checksum=item.get('checksum'),
                     url=(item.get('links') or {}).get('content') or (item.get('links') or {}).get('self'))
        members = None
        if entry['kind'] == 'archive':
            entry['notebooks_inside'], members = (list_zip(record_id, key)
                                                  if list_containers and key.lower().endswith('.zip') else (None, None))
        if safe_member(key) is None:
            entry.update(action='skip', reason='unsafe file name')
        elif entry['kind'] == 'notebook':
            entry.update(action='download', reason='notebook')
        elif entry['kind'] == 'archive' and entry['notebooks_inside'] and size > cap['archive'] and members:
            # Too large to download whole: take the notebooks and the members within the file limit.
            chosen, left_out = [], []
            for member in members:
                path = safe_member(member['key'])
                if path is None or is_noise(member['key']) or member['key'].endswith('/'):
                    continue
                is_notebook = member['key'].lower().endswith('.ipynb')
                if (is_notebook or member['size'] <= cap['file']) and total + size_of(chosen) + member['size'] <= cap['total']:
                    chosen.append(member)
                else:
                    left_out.append(dict(path=member['key'], size=member['size'], crc=member.get('crc'), action='skip',
                                         reason=f'member larger than {cap["file"] // MiB} MiB'
                                         if member['size'] > cap['file'] else 'total download limit reached'))
            entry.update(action='partial', reason=f'archive with notebooks larger than {cap["archive"] // MiB} MiB; '
                                                  f'{len(chosen)} members taken, {len(left_out)} left out',
                         _members=chosen, members=left_out)
            size = size_of(chosen)
        elif entry['kind'] == 'archive' and entry['notebooks_inside'] != 0:
            limit = cap['archive']
            reason = 'archive with notebooks' if entry['notebooks_inside'] else 'archive, content not listed'
            entry.update(action='download' if size <= limit else 'skip',
                         reason=reason if size <= limit else f'{reason} larger than {limit // MiB} MiB')
        else:
            label = 'archive without notebooks' if entry['kind'] == 'archive' else 'data file'
            entry.update(action='download' if size <= cap['file'] else 'skip',
                         reason=label if size <= cap['file'] else f'{label} larger than {cap["file"] // MiB} MiB')
        if entry['action'] == 'partial':
            total += size
        elif entry['action'] == 'download':
            if total + size > cap['total']:
                entry.update(action='skip', reason=f'total download limit of {cap["total"] // MiB} MiB reached')
            else:
                total += size
        entries.append(entry)
    return entries


def size_of(members):
    return sum(m['size'] for m in members)


def download_member(member, target):
    """Download one ZIP member through the container API and check its CRC32."""
    url = (member.get('links') or {}).get('content')
    if not isinstance(url, str) or not url.startswith('https://zenodo.org/'):
        raise FetchError(f'missing download URL for archive member {member["key"]}')
    target.parent.mkdir(parents=True, exist_ok=True)
    started, transferred = time.monotonic(), 0
    for attempt in range(1, 4):
        crc = 0
        try:
            with open_url(url, timeout=120) as response, target.open('wb') as output:
                while chunk := response.read(MiB):
                    crc = zlib.crc32(chunk, crc)
                    transferred += len(chunk)
                    output.write(chunk)
        except (OSError, FetchError) as error:
            target.unlink(missing_ok=True)
            if attempt == 3:
                raise FetchError(f'download failed for archive member {member["key"]}: {error}') from error
            time.sleep(2 ** attempt)
            continue
        if member.get('crc') is not None and crc != member['crc']:
            target.unlink(missing_ok=True)
            if attempt == 3:
                raise FetchError(f'CRC32 mismatch for archive member {member["key"]}')
            continue
        return dict(path=member['key'], size=member['size'], crc=member.get('crc'), action='downloaded member',
                    verified=None if member.get('crc') is None else 1,
                    seconds=round(time.monotonic() - started, 3), bytes_transferred=transferred)


def find_duplicate_notebooks(destination, origins):
    """Notebooks byte-identical to an earlier one from a different record file.

    `origins` maps a notebook's path (relative to destination) to the record
    file it came from; notebooks of unknown origin are never treated as copies.
    Earlier means shallower path first, then alphabetical.
    """
    seen, duplicates = {}, []
    notebooks = [p for p in destination.rglob('*.ipynb')
                 if p.is_file() and not is_noise(str(p.relative_to(destination)))]
    for path in sorted(notebooks, key=lambda p: (len(p.relative_to(destination).parts), str(p))):
        relative = str(path.relative_to(destination))
        origin = origins.get(relative)
        if origin is None:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        first = seen.setdefault(digest, (relative, origin))
        if first[1] != origin:
            duplicates.append(dict(path=relative, same_as=first[0], from_file=origin, same_as_from_file=first[1]))
    return duplicates


# archives up to this size stay in the resource next to their extracted contents (default: the data-file limit)
KEEP_ARCHIVE_BYTES = int(os.environ.get('ZENODO_KEEP_ARCHIVE_BYTES', os.environ.get('ZENODO_MAX_FILE_BYTES', 256 * 1024 * 1024)))


def archive_target(destination, key, roots, occupied):
    """Unpack in place like a user would; on a clash with files already present
    use a folder named after the archive instead of overwriting."""
    if not roots & occupied:
        return destination, None
    stem = PurePosixPath(key).name
    for suffix in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if stem.lower().endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    return destination / stem, stem


def download(entry, target):
    url = entry['url']
    if not isinstance(url, str) or not url.startswith('https://zenodo.org/'):
        raise FetchError(f'missing or unexpected download URL for {entry["key"]}')
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.part')
    started, transferred = time.monotonic(), 0
    for attempt in range(1, 4):
        digest = hashlib.md5()
        try:
            with open_url(url, timeout=120) as response, partial.open('wb') as output:
                while chunk := response.read(MiB):
                    digest.update(chunk)
                    transferred += len(chunk)
                    output.write(chunk)
        except (OSError, FetchError) as error:
            partial.unlink(missing_ok=True)
            if attempt == 3:
                raise FetchError(f'download failed for {entry["key"]}: {error}') from error
            time.sleep(2 ** attempt)
            continue
        expected = str(entry.get('checksum') or '')
        if expected.startswith('md5:') and expected[4:] != digest.hexdigest():
            partial.unlink(missing_ok=True)
            if attempt == 3:
                raise FetchError(f'MD5 mismatch for {entry["key"]}')
            continue
        entry['md5_verified'] = expected.startswith('md5:')
        entry['seconds'] = round(time.monotonic() - started, 3)
        entry['bytes_transferred'] = transferred
        partial.replace(target)
        return


def archive_members(path):
    """Every member as dict(name, is_dir, is_link, size, crc); raises FetchError if unreadable."""
    name = path.name.lower()
    try:
        if name.endswith('.zip'):
            with zipfile.ZipFile(path) as archive:
                return [dict(name=m.filename, is_dir=m.is_dir(), size=m.file_size, crc=m.CRC,
                             is_link=(m.external_attr >> 16) & 0o170000 == 0o120000) for m in archive.infolist()]
        if name.endswith(TAR_SUFFIXES):
            with tarfile.open(path) as archive:
                return [dict(name=m.name, is_dir=m.isdir(), size=m.size, crc=None,
                             is_link=m.issym() or m.islnk() or not (m.isfile() or m.isdir()))
                        for m in archive.getmembers()]
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as error:
        raise FetchError(f'cannot read archive {path.name}: {error}') from error
    tool = shutil.which('bsdtar')
    if not tool:
        raise FetchError(f'cannot read {path.name}: bsdtar (libarchive-tools) is not installed')
    listing = subprocess.run([tool, '-tvf', str(path)], capture_output=True, text=True)
    if listing.returncode:
        raise FetchError(f'cannot read archive {path.name}: {listing.stderr.strip()[:200]}')
    names = subprocess.run([tool, '-tf', str(path)], capture_output=True, text=True).stdout.splitlines()
    modes = [line[:1] for line in listing.stdout.splitlines()]
    return [dict(name=n, is_dir=m == 'd', is_link=m in ('l', 'h'), size=None, crc=None) for n, m in zip(names, modes)]


def extract(path, destination):
    """Extract safely; return the file members (dicts) with the action taken for each."""
    members = archive_members(path)
    name = path.name.lower()
    for member in members:
        stripped = member['name'].rstrip('/')
        if safe_member(stripped) is None and stripped not in ('', '.'):
            raise FetchError(f'unsafe path in {path.name}: {member["name"]}')
    destination.mkdir(parents=True, exist_ok=True)
    skipped_links = set()
    try:
        if name.endswith('.zip'):
            with zipfile.ZipFile(path) as archive:
                archive.extractall(destination)
        elif name.endswith(TAR_SUFFIXES):
            with tarfile.open(path) as archive:
                # The same rule with and without tarfile's data filter (Python >= 3.11.4; the HPC pipeline ran 3.11.0
                # and refused 9 records with links in 2026-10): links whose target leaves the archive and device
                # files are skipped and recorded, everything else is extracted.
                safe = []
                for info in archive.getmembers():
                    if info.isdev():
                        skipped_links.add(info.name.rstrip('/'))
                        continue
                    if info.issym() or info.islnk():
                        base = PurePosixPath(info.name).parent if info.issym() else PurePosixPath()
                        target = posixpath.normpath(str(base / info.linkname))
                        if info.linkname.startswith('/') or target == '..' or target.startswith('../'):
                            skipped_links.add(info.name.rstrip('/'))
                            continue
                    if not hasattr(tarfile, 'data_filter') and (info.isfile() or info.isdir()):
                        info.mode &= 0o755                      # no setuid/setgid/sticky, like the data filter
                    safe.append(info)
                if hasattr(tarfile, 'data_filter'):
                    archive.extractall(destination, members=safe, filter='data')
                else:
                    archive.extractall(destination, members=safe)
        else:
            result = subprocess.run([shutil.which('bsdtar'), '-xf', str(path), '-C', str(destination)],
                                    capture_output=True, text=True)
            if result.returncode:
                raise FetchError(f'bsdtar failed for {path.name}: {result.stderr.strip()[:200]}')
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as error:
        raise FetchError(f'extraction failed for {path.name}: {error}') from error
    files = []
    for member in members:
        if member['is_dir']:
            continue
        action = 'extracted'
        if member['name'].rstrip('/') in skipped_links:
            action = 'skip'
            member = dict(member, reason='link or device file pointing outside the archive (not extracted)')
        elif member['is_link']:
            action = 'link stored as file' if name.endswith('.zip') else 'link extracted'
        files.append(dict(path=member['name'], size=member['size'], crc=member['crc'], action=action,
                          **({'reason': member['reason']} if member.get('reason') else {})))
    return files


def top_level(names):
    return {PurePosixPath(n).parts[0] for n in names if PurePosixPath(n).parts}


def flatten_single_folder(destination):
    entries = [p for p in destination.iterdir() if p.name != MANIFEST_NAME]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        temporary = destination / (inner.name + '.flatten-tmp')
        inner.rename(temporary)
        for child in temporary.iterdir():
            child.rename(destination / child.name)
        temporary.rmdir()
        return inner.name
    return None


def acquire(record_id, destination, manifest, plan_only=False, list_containers=True):
    """Fill `manifest` (a dict) while acquiring, so a failure still leaves a record of it."""
    manifest.update(record_id=str(record_id), limits=limits(), files=[], error=None,
                    started_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    record = get_json(f'{API}{record_id}')
    manifest.update(record_version=(record.get('metadata') or {}).get('version'),
                    record_revision=record.get('revision', record.get('revision_id')), record_updated=record.get('updated'),
                    record_created=record.get('created'),
                    concept_record_id=record.get('conceptrecid'))
    entries = plan(record, record_id, manifest['limits'], list_containers=list_containers)
    manifest['files'] = entries
    wanted = [e for e in entries if e['action'] in ('download', 'partial')]
    if plan_only or not wanted:
        return manifest

    destination.mkdir(parents=True, exist_ok=True)
    if any(p.name != MANIFEST_NAME for p in destination.iterdir()):
        raise FetchError(f'destination is not empty: {destination}')
    staging = destination / '.zenodo-archives'
    archives = [e for e in wanted if e['kind'] == 'archive' and e['action'] == 'download']
    partial = [e for e in wanted if e['action'] == 'partial']
    for entry in wanted:
        if entry['action'] == 'partial':
            continue
        target = (staging if entry['kind'] == 'archive' else destination) / Path(*safe_member(entry['key']).parts)
        download(entry, target)
        entry['action'] = 'downloaded'
    occupied = {p.name for p in destination.iterdir() if p.name not in (MANIFEST_NAME, staging.name)}
    for entry in archives:
        path = staging / Path(*safe_member(entry['key']).parts)
        roots = top_level(m['name'] for m in archive_members(path) if not m['is_dir'])
        target, folder = archive_target(destination, entry['key'], roots, occupied)
        if folder:
            entry['extracted_into'] = folder
        entry['members'] = extract(path, target)
        entry['action'] = 'extracted'
        entry['notebooks_extracted'] = sum(m['path'].lower().endswith('.ipynb') and not is_noise(m['path'])
                                           for m in entry['members'])
        occupied |= roots if target == destination else {target.name}
        # keep the archive itself next to its contents, as a user who downloaded and unpacked the record would have
        # it (notebooks may open the archive themselves); not above the data-file limit, and never over a file
        kept = destination / Path(*safe_member(entry['key']).parts)
        if path.stat().st_size <= KEEP_ARCHIVE_BYTES and not kept.exists():
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), kept)
            entry['archive_kept'] = True
            occupied.add(PurePosixPath(entry['key']).parts[0])
        else:
            path.unlink()
            entry['archive_kept'] = False
    shutil.rmtree(staging, ignore_errors=True)
    for entry in partial:
        members = entry.pop('_members')
        roots = top_level(m['key'] for m in members)
        target, folder = archive_target(destination, entry['key'], roots, occupied)
        if folder:
            entry['extracted_into'] = folder
        entry['members'] = [download_member(member, target / Path(*safe_member(member['key']).parts))
                            for member in members] + entry.get('members', [])
        entry['seconds'] = round(sum(m['seconds'] for m in entry['members'] if m.get('seconds')), 3)
        entry['bytes_transferred'] = sum(m.get('bytes_transferred') or 0 for m in entry['members'])
        entry['action'] = 'extracted_partially'
        entry['notebooks_extracted'] = sum(m['key'].lower().endswith('.ipynb') for m in members)
        occupied |= roots if target == destination else {target.name}
    if len(entries) == 1 and (archives or partial):
        for entry in archives:          # the archive is the whole record (a snapshot): unpacked only, like a clone
            if entry.get('archive_kept'):
                (destination / Path(*safe_member(entry['key']).parts)).unlink(missing_ok=True)
                entry['archive_kept'] = False
        manifest['flattened'] = flatten_single_folder(destination)
    # Which record file each notebook came from (packaging duplicates need two different ones).
    origins = {e['key']: e['key'] for e in entries if e['kind'] == 'notebook' and e['action'] == 'downloaded'}
    for entry in archives + partial:
        prefix = PurePosixPath(entry['extracted_into']) if entry.get('extracted_into') else PurePosixPath()
        for member in entry.get('members', []):
            if member['action'] != 'skip':
                origins[str(prefix / member['path'])] = entry['key']
    manifest['duplicate_notebooks'] = find_duplicate_notebooks(destination, origins)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('record_id')
    parser.add_argument('destination', type=Path)
    parser.add_argument('--plan-only', action='store_true', help='decide and report, download nothing')
    parser.add_argument('--manifest', type=Path, help=f'default: DESTINATION/{MANIFEST_NAME}')
    args = parser.parse_args(argv)
    manifest_path = args.manifest or args.destination / MANIFEST_NAME
    status = 0
    manifest = {}
    try:
        acquire(args.record_id, args.destination.resolve(), manifest, plan_only=args.plan_only)
        if not args.plan_only and not any(e['action'] in ('downloaded', 'extracted', 'extracted_partially')
                                          for e in manifest['files']):
            manifest['error'] = ('record has no files' if not manifest['files']
                                 else 'every file was skipped: ' + '; '.join(
                                     f"{e['key']} ({e['reason']})" for e in manifest['files']))
            status = 3
    except FetchError as error:
        manifest.setdefault('files', [])
        manifest['error'] = str(error)
        status = 1
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    manifest['api_requests'] = COUNTERS['api_requests']
    for entry in manifest.get('files', []):
        entry.pop('url', None)
        entry.pop('_members', None)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1), encoding='utf-8')
    skipped = [e for e in manifest.get('files', []) if e.get('action') == 'skip']
    print(f"[ZENODO] record {args.record_id}: "
          + (f"FAILED: {manifest['error']}" if manifest.get('error') else
             f"{sum(e.get('action') != 'skip' for e in manifest['files'])} files "
             f"{'planned' if args.plan_only else 'acquired'}, {len(skipped)} skipped"), file=sys.stderr)
    for entry in skipped:
        print(f"[ZENODO]   skipped {entry['key']} ({entry['size'] // MiB} MiB): {entry['reason']}", file=sys.stderr)
    return status


if __name__ == '__main__':
    sys.exit(main())
