#!/usr/bin/env python3
"""Step S4 — Zenodo frame in three mutually exclusive strata (notes/sampling-design.md, section 5, D4).

  Z1  records with >= 1 file *.ipynb
  Z2  no *.ipynb, >= 1 *.zip                     -> notebooks only inside ZIP archives (if any)
  Z3  no *.ipynb, no *.zip, >= 1 *.tar/.tar.gz/.tgz
All versions created before the cutoff are enumerated (all_versions=true), because a deposit's
current version is often newer than the cutoff; `export` keeps per concept (conceptrecid) the
latest enumerated version and its stratum. A newer pre-cutoff version outside the strata (e.g.
without archives) cannot be seen in the frame: it is checked for sampled records at draw time
(over-coverage, recorded as ineligible).

The search API returns at most 10,000 hits per query, so each stratum is enumerated in windows of
the creation time that are split in halves until each holds <= 10,000 hits; every window's
stored row count is compared with its reported total.

Commands (resumable; state in data/sampling/zenodo/frame.sqlite):
  enumerate --stratum Z1|Z2|Z3   list the stratum's records (all versions, created before the cutoff)
  inspect-zip --n N --seed S     container listing of every ZIP file of up to N random Z2 concepts, stopping
                                 once --target-pool (460) concepts hold a notebook or unknown content
  inspect-tar --n N --seed S     streamed download of the tar files of N random Z3 concepts, reading
                                 the member names only (stops at --max-mb per record -> "unknown")
  export                         per-stratum frames (CSV.gz + SHA-256) and summary.json
  status
Pacing: 30 searches per minute with or without a token (X-RateLimit-Limit, checked 2026-10-05);
ZENODO_TOKEN raises the page size from 25 to 100 hits, i.e. 4x fewer requests. Usage:
  python3 analysis/sampling/zenodo_frame.py enumerate --stratum Z1
  python3 analysis/sampling/zenodo_frame.py inspect-zip --n 20000 --seed 20261005
  python3 analysis/sampling/zenodo_frame.py export
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CUTOFF, SAMPLING_DIR, Http, ServerError, now, rng, token, write_csv, write_json  # noqa: E402

API = 'https://zenodo.org/api/records'
OUT = SAMPLING_DIR / 'zenodo'
STRATA = {
    'Z1': 'files.entries.key:*.ipynb',
    'Z2': 'files.entries.key:*.zip AND NOT files.entries.key:*.ipynb',
    'Z3': ('files.entries.key:(*.tar OR *.tar.gz OR *.tgz) AND NOT files.entries.key:*.zip '
           'AND NOT files.entries.key:*.ipynb'),
}
TAR_SUFFIXES = ('.tar', '.tar.gz', '.tgz')
STREAM_ATTEMPTS = 3                     # a dropped download is retried, then recorded as 'unknown'
NETWORK_ERRORS = (requests.RequestException, urllib3.exceptions.HTTPError)
NO_ACCESS = (403, 404, 410)              # file restricted or removed since listing: not eligible, counted in m
START = datetime(2000, 1, 1, tzinfo=timezone.utc)
END = datetime.fromisoformat(CUTOFF).replace(tzinfo=timezone.utc)
MAX_HITS = 10_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, stratum TEXT, conceptrecid TEXT, created TEXT,
    publication_date TEXT, resource_type TEXT, access_right TEXT, version_index INTEGER, version_is_last INTEGER,
    title TEXT, files INTEGER, total_bytes INTEGER, ipynb_files INTEGER, zip_files INTEGER, tar_files INTEGER,
    archive_keys TEXT, github_url TEXT, window_start TEXT, listed_at TEXT);
CREATE INDEX IF NOT EXISTS records_concept ON records (conceptrecid);
CREATE TABLE IF NOT EXISTS listing_errors (stratum TEXT, window_start TEXT, list_offset INTEGER, error TEXT,
    recorded_at TEXT, PRIMARY KEY (stratum, window_start, list_offset));
CREATE TABLE IF NOT EXISTS windows (stratum TEXT, start TEXT, end TEXT, total INTEGER, stored INTEGER,
    finished_at TEXT, PRIMARY KEY (stratum, start, end));
CREATE TABLE IF NOT EXISTS inspections (id INTEGER, archive TEXT, method TEXT, seed INTEGER, http_status INTEGER,
    entries INTEGER, ipynb_entries INTEGER, ipynb_example TEXT, truncated INTEGER, bytes_read INTEGER,
    outcome TEXT, checked_at TEXT, PRIMARY KEY (id, archive));
CREATE TABLE IF NOT EXISTS inspection_samples (stratum TEXT, seed INTEGER, seq INTEGER, conceptrecid TEXT,
    id INTEGER, PRIMARY KEY (stratum, seed, seq));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect():
    OUT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(OUT / 'frame.sqlite', timeout=60)
    db.executescript(SCHEMA)
    return db


def client(args):
    headers = {'Accept': 'application/json'}
    if token('ZENODO_TOKEN'):
        headers['Authorization'] = f'Bearer {token("ZENODO_TOKEN")}'
    interval = args.interval if args.interval is not None else 2.1     # 30 searches per minute, token or not
    return Http(OUT / 'requests.jsonl', min_interval=interval, headers=headers)


def page_size():
    return 100 if token('ZENODO_TOKEN') else 25


def stamp(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%S')


def search(http, stratum, start, end, page=1, size=1):
    query = f'{STRATA[stratum]} AND created:[{stamp(start)} TO {stamp(end)}}}'
    return http.get(API, params=dict(q=query, all_versions='true', size=size, page=page, sort='oldest')).json()['hits']


def github_url(metadata):
    for item in metadata.get('related_identifiers') or []:
        identifier = item.get('identifier') or ''
        if 'github.com/' in identifier:
            return identifier
    return None


def row(hit, stratum, window_start):
    metadata, files = hit['metadata'], hit.get('files') or []
    keys = [f['key'] for f in files]
    lower = [k.lower() for k in keys]
    version = ((metadata.get('relations') or {}).get('version') or [{}])[0]
    archives = [k for k, low in zip(keys, lower) if low.endswith(('.zip',) + TAR_SUFFIXES)]
    return (hit['id'], stratum, str(hit['conceptrecid']), hit['created'], metadata.get('publication_date'),
            (metadata.get('resource_type') or {}).get('type'), metadata.get('access_right'), version.get('index'),
            int(bool(version.get('is_last'))), (metadata.get('title') or '')[:300], len(files),
            sum(f.get('size') or 0 for f in files), sum(low.endswith('.ipynb') for low in lower),
            sum(low.endswith('.zip') for low in lower), sum(low.endswith(TAR_SUFFIXES) for low in lower),
            json.dumps(archives[:50]), github_url(metadata), window_start, now())


def enumerate_stratum(args):
    db = connect()
    finished = db.execute('SELECT value FROM meta WHERE key = ?', (f'enumerate_{args.stratum}_finished',)).fetchone()
    if finished and not getattr(args, 'force', False):
        print(f'{args.stratum}: enumeration finished at {finished[0]}; frame kept (--force to enumerate again)')
        return
    http = client(args)
    size = page_size()
    done = {(s, e) for s, e in db.execute('SELECT start, end FROM windows WHERE stratum = ? AND finished_at IS NOT NULL',
                                          (args.stratum,))}
    stack = [(START, END)]
    while stack:
        start, end = stack.pop()
        if (stamp(start), stamp(end)) in done:
            continue
        total = search(http, args.stratum, start, end)['total']
        if total > MAX_HITS:
            middle = start + (end - start) / 2
            middle = middle.replace(microsecond=0)
            stack += [(middle, end), (start, middle)]      # earlier half first
            continue
        stored_before = db.execute('SELECT COUNT(*) FROM records WHERE stratum = ? AND window_start = ?',
                                   (args.stratum, stamp(start))).fetchone()[0]
        page = 1
        while (page - 1) * size < total:
            hits = search_robust(db, http, args.stratum, start, end, page, size)
            db.executemany('INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           [row(h, args.stratum, stamp(start)) for h in hits])
            db.commit()
            if not hits and not db.execute('SELECT 1 FROM listing_errors WHERE stratum = ? AND window_start = ? '
                                           'AND list_offset >= ?', (args.stratum, stamp(start), (page - 1) * size)).fetchone():
                break
            page += 1
        stored = db.execute('SELECT COUNT(*) FROM records WHERE stratum = ? AND window_start = ?',
                            (args.stratum, stamp(start))).fetchone()[0]
        unlistable = db.execute('SELECT COUNT(*) FROM listing_errors WHERE stratum = ? AND window_start = ?',
                                (args.stratum, stamp(start))).fetchone()[0]
        db.execute('INSERT OR REPLACE INTO windows VALUES (?,?,?,?,?,?)',
                   (args.stratum, stamp(start), stamp(end), total, stored, now()))
        db.commit()
        if total:
            flag = (f'  ({unlistable} unlistable, recorded)' if unlistable and stored + unlistable == total else
                    '' if stored == total else f'  MISMATCH (stored {stored}, unlistable {unlistable}, before {stored_before})')
            print(f'[{now()}] {args.stratum} {stamp(start)} .. {stamp(end)}: {total}{flag}', flush=True)
    db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (f'enumerate_{args.stratum}_finished', now()))
    db.commit()
    count = db.execute('SELECT COUNT(*) FROM records WHERE stratum = ?', (args.stratum,)).fetchone()[0]
    print(f'{args.stratum}: {count} record versions enumerated')


def search_robust(db, http, stratum, start, end, page, size):
    """Hits of one search page. If Zenodo keeps answering 5xx for it (seen 2026-10-06 for one page of a
    Z2 window: most likely one record that cannot be serialised), the page is fetched as pages of 25 and a
    failing one record by record; records that still fail are recorded in listing_errors (stratum, window,
    offset) and skipped. Zenodo allows page * size <= 10,000, which holds for every split."""
    try:
        return search(http, stratum, start, end, page=page, size=size)['hits']
    except ServerError as error:
        print(f'[{now()}] {stratum} {stamp(start)} page {page}: {error}; fetching it in parts', flush=True)
    quick = Http(OUT / 'requests.jsonl', min_interval=http.min_interval, headers=dict(http.session.headers),
                 max_retries=3)
    hits, small = [], 25
    for sub in range(max(1, size // small)):
        page_small = (page - 1) * (size // small) + sub + 1
        try:
            hits += search(quick, stratum, start, end, page=page_small, size=small)['hits']
            continue
        except ServerError:
            pass
        for k in range(small):
            offset = (page_small - 1) * small + k
            try:
                hits += search(quick, stratum, start, end, page=offset + 1, size=1)['hits']
            except ServerError as error:
                db.execute('INSERT OR REPLACE INTO listing_errors VALUES (?,?,?,?,?)',
                           (stratum, stamp(start), offset, str(error)[:300], now()))
                db.commit()
                print(f'[{now()}] {stratum} {stamp(start)}: unlistable record at offset {offset} (recorded)', flush=True)
    return hits


def concept_frame(db, stratum=None):
    """Latest enumerated version per concept (across strata), restricted to `stratum` if given."""
    sql = ('SELECT r.* FROM records r JOIN (SELECT conceptrecid, MAX(created) AS latest FROM records '
           'GROUP BY conceptrecid) m ON m.conceptrecid = r.conceptrecid AND m.latest = r.created')
    rows = db.execute(sql + (' WHERE r.stratum = ?' if stratum else '') + ' ORDER BY r.id',
                      (stratum,) if stratum else ()).fetchall()
    names = [d[0] for d in db.execute('SELECT * FROM records LIMIT 0').description]
    return [dict(zip(names, r)) for r in rows]


def inspection_sample(db, stratum, n, seed):
    """Seeded random sample of open-access concepts of the stratum (stored, so it can be extended)."""
    stored = db.execute('SELECT id FROM inspection_samples WHERE stratum = ? AND seed = ? ORDER BY seq',
                        (stratum, seed)).fetchall()
    if len(stored) >= n:
        return [r[0] for r in stored[:n]]
    frame = [r for r in concept_frame(db, stratum) if r['access_right'] == 'open']
    order = rng(seed).sample(frame, len(frame))                # full permutation: extending keeps the prefix
    db.executemany('INSERT OR IGNORE INTO inspection_samples VALUES (?,?,?,?,?)',
                   [(stratum, seed, i, r['conceptrecid'], r['id']) for i, r in enumerate(order[:n], 1)])
    db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (f'inspection_{stratum}_{seed}_frame_size', len(frame)))
    db.commit()
    return [r['id'] for r in order[:n]]


def inspect_zip(args):
    """Container listings of the Z2 subsample, in its seeded order.

    Stops early once the pool (concepts with a notebook or unknown content, the draw's Z2 pool) reaches
    --target-pool: the draw can allocate at most n - 2 * min-stratum primary records plus the stratum reserve
    to Z2 (500 - 100 + 60 = 460 by default), so more hits change neither the cap nor, materially, the estimate
    (relative SE about 1/sqrt(pool)). The inspected prefix of the random order is itself a random sample;
    the draw uses only inspected concepts (m = their number), the rest stays listed as not inspected.
    """
    db, http = connect(), client(args)
    ids = inspection_sample(db, 'Z2', args.n, args.seed)
    done = {r[0] for r in db.execute('SELECT DISTINCT id FROM inspections')}
    inspected = pool = 0
    for index, record_id in enumerate(ids, 1):
        if record_id not in done:
            keys = [k for k in json.loads(db.execute('SELECT archive_keys FROM records WHERE id = ?',
                                                     (record_id,)).fetchone()[0]) if k.lower().endswith('.zip')]
            for key in keys:
                status, names, data, error = None, [], {}, None
                try:
                    response = http.get(f'{API}/{record_id}/files/{quote(key)}/container',
                                        ok=(200, 400, 403, 404, 410, 500))
                    status = response.status_code
                    data = response.json() if status == 200 else {}
                    names = [e['key'] for e in data.get('entries', [])]
                except (NETWORK_ERRORS + (ValueError,)) as exc:     # persisting errors: record, go on
                    error = f'{type(exc).__name__}: {exc}'[:300]
                    print(f'  {record_id} {key}: {error}', flush=True)
                ipynb = [k for k in names if k.lower().endswith('.ipynb')]
                outcome = ('notebook' if ipynb else 'no_access' if status in NO_ACCESS
                           else 'unknown' if status != 200 or data.get('truncated') else 'no_notebook')
                db.execute('INSERT OR REPLACE INTO inspections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                    record_id, key, 'container', args.seed, status, len(names), len(ipynb),
                    ipynb[0] if ipynb else error, int(bool(data.get('truncated'))), None, outcome, now()))
                db.commit()
                if ipynb:
                    break                                          # one notebook decides the record
        inspected = index
        outcomes = {r[0] for r in db.execute('SELECT outcome FROM inspections WHERE id = ?', (record_id,))}
        pool += bool(outcomes & {'notebook', 'unknown'})              # the draw's pool rule (draw_sample.py)
        if index % 500 == 0:
            print(f'[{now()}] {index}/{len(ids)} Z2 records inspected, pool {pool}', flush=True)
        if args.target_pool and pool >= args.target_pool and index >= args.min_n:
            print(f'[{now()}] pool {pool} >= target {args.target_pool} after {index} records: inspection complete',
                  flush=True)
            break
    db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (f'inspection_Z2_{args.seed}_stop', json.dumps(dict(
        inspected=inspected, pool=pool, target_pool=args.target_pool, min_n=args.min_n, max_n=args.n))))
    db.commit()
    mark_inspected(db, 'Z2', args.seed, inspected)


def inspect_tar(args):
    db, http = connect(), client(args)
    ids = inspection_sample(db, 'Z3', args.n, args.seed)
    done = {r[0] for r in db.execute('SELECT DISTINCT id FROM inspections')}
    limit = args.max_mb * 1024 * 1024
    for record_id in ids:
        if record_id in done:
            continue
        keys = [k for k in json.loads(db.execute('SELECT archive_keys FROM records WHERE id = ?',
                                                 (record_id,)).fetchone()[0]) if k.lower().endswith(TAR_SUFFIXES)]
        for key in keys:
            for attempt in range(1, STREAM_ATTEMPTS + 1):
                names, ipynb, status, outcome, counter = [], [], None, 'unknown', CountingReader(limit)
                response = None
                try:
                    response = http.get(f'{API}/{record_id}/files/{quote(key)}/content', stream=True,
                                        ok=(200, 403, 404, 410))
                    status = response.status_code
                    if status in NO_ACCESS:
                        outcome = 'no_access'
                    if status == 200:
                        counter.raw = response.raw
                        counter.raw.decode_content = False
                        with tarfile.open(fileobj=counter, mode='r|*') as archive:
                            for member in archive:
                                names.append(member.name)
                                if member.name.lower().endswith('.ipynb'):
                                    ipynb.append(member.name)
                                    break
                        outcome = 'notebook' if ipynb else 'no_notebook'
                except LimitReached:
                    outcome = 'notebook' if ipynb else 'unknown'
                except (tarfile.TarError, OSError, EOFError) as error:
                    outcome = 'notebook' if ipynb else 'unknown'
                    print(f'  {record_id} {key}: {type(error).__name__}: {error}', flush=True)
                except NETWORK_ERRORS as error:                  # connection dropped mid-stream: retry the file
                    outcome = 'notebook' if ipynb else 'unknown'
                    print(f'  {record_id} {key}: attempt {attempt}/{STREAM_ATTEMPTS}: {type(error).__name__}: {error}',
                          flush=True)
                    if not ipynb and attempt < STREAM_ATTEMPTS:
                        time.sleep(10 * attempt)
                        continue
                finally:
                    if response is not None:
                        response.close()
                break
            db.execute('INSERT OR REPLACE INTO inspections VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                record_id, key, 'stream', args.seed, status, len(names), len(ipynb), ipynb[0] if ipynb else None,
                int(outcome == 'unknown'), counter.read_bytes, outcome, now()))
            db.commit()
            if ipynb:
                break
    mark_inspected(db, 'Z3', args.seed, len(ids))


def mark_inspected(db, stratum, seed, n):
    """Record that the inspection subsample of a stratum is complete (checked by draw_sample.py)."""
    db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (f'inspection_{stratum}_{seed}_done', f'{n} {now()}'))
    db.commit()


class LimitReached(Exception):
    pass


class CountingReader:
    """File-like wrapper around the raw HTTP stream that stops after `limit` bytes."""

    def __init__(self, limit):
        self.raw, self.limit, self.read_bytes = None, limit, 0

    def read(self, size=-1):
        if self.read_bytes >= self.limit:
            raise LimitReached()
        data = self.raw.read(min(size if size and size > 0 else 1 << 20, self.limit - self.read_bytes))
        self.read_bytes += len(data)
        return data


def record_outcome(db, stratum, seed):
    """Per sampled record: notebook if any archive has one, unknown if none has but one was unknown."""
    rows = db.execute('SELECT s.id, GROUP_CONCAT(i.outcome) FROM inspection_samples s LEFT JOIN inspections i '
                      'ON i.id = s.id WHERE s.stratum = ? AND s.seed = ? GROUP BY s.id', (stratum, seed)).fetchall()
    counts = dict(notebook=0, no_notebook=0, unknown=0, no_access=0, not_inspected=0)
    for _, outcomes in rows:
        values = (outcomes or '').split(',')
        key = ('not_inspected' if not outcomes else 'notebook' if 'notebook' in values
               else 'unknown' if 'unknown' in values else 'no_access' if 'no_access' in values else 'no_notebook')
        counts[key] += 1
    return counts


def export(args):
    db = connect()
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    names = [d[0] for d in db.execute('SELECT * FROM records LIMIT 0').description]
    summary = dict(exported_at=now(), cutoff=CUTOFF, strata={})
    for stratum in STRATA:
        versions = db.execute('SELECT COUNT(*) FROM records WHERE stratum = ?', (stratum,)).fetchone()[0]
        if not versions:
            continue
        frame = concept_frame(db, stratum)
        target = OUT / f'frame-zenodo-{stratum}-{day}.csv.gz'
        digest = write_csv(target, frame, names)
        windows = db.execute('SELECT COUNT(*), SUM(total), SUM(stored), SUM(total != stored) FROM windows '
                             'WHERE stratum = ?', (stratum,)).fetchone()
        by = lambda column: dict(sorted(  # noqa: E731
            ((k or 'none'), v) for k, v in __import__('collections').Counter(r[column] for r in frame).items()))
        unlistable = db.execute('SELECT COUNT(*) FROM listing_errors WHERE stratum = ?', (stratum,)).fetchone()[0]
        info = dict(versions_enumerated=versions, windows=windows[0], hits_reported=windows[1], rows_stored=windows[2],
                    unlistable_records=unlistable,
                    windows_with_mismatch=windows[3], concepts_latest_version_in_stratum=len(frame),
                    open_access=sum(r['access_right'] == 'open' for r in frame), by_access_right=by('access_right'),
                    by_resource_type=by('resource_type'), with_github_link=sum(bool(r['github_url']) for r in frame),
                    frame=target.name, frame_sha256=digest)
        seeds = [r[0] for r in db.execute('SELECT DISTINCT seed FROM inspection_samples WHERE stratum = ?', (stratum,))]
        info['inspections'] = {}
        for seed in seeds:
            counts = record_outcome(db, stratum, seed)
            known = counts['notebook'] + counts['no_notebook']
            frame_size = db.execute('SELECT value FROM meta WHERE key = ?',
                                    (f'inspection_{stratum}_{seed}_frame_size',)).fetchone()
            info['inspections'][seed] = dict(counts, sampled=sum(counts.values()),
                                             frame_size=int(frame_size[0]) if frame_size else None,
                                             notebook_share_of_known=counts['notebook'] / known if known else None)
        summary['strata'][stratum] = info
    summary['meta'] = dict(db.execute('SELECT key, value FROM meta').fetchall())
    write_json(OUT / 'summary.json', summary)
    print(json.dumps(summary, indent=2))


def status(args):
    db = connect()
    print(json.dumps(dict(
        versions=dict(db.execute('SELECT stratum, COUNT(*) FROM records GROUP BY stratum').fetchall()),
        windows=dict(db.execute('SELECT stratum, COUNT(*) FROM windows GROUP BY stratum').fetchall()),
        mismatches=db.execute('SELECT COUNT(*) FROM windows WHERE total != stored').fetchone()[0],
        inspections=dict(db.execute('SELECT outcome, COUNT(*) FROM inspections GROUP BY outcome').fetchall()),
        meta=dict(db.execute('SELECT key, value FROM meta').fetchall())), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['enumerate', 'inspect-zip', 'inspect-tar', 'export', 'status'])
    parser.add_argument('--stratum', choices=list(STRATA))
    parser.add_argument('--force', action='store_true', help='enumerate: list a finished stratum again')
    parser.add_argument('--target-pool', type=int, default=460,
                        help='inspect-zip: stop once this many concepts hold a notebook or unknown content (0 = never)')
    parser.add_argument('--min-n', type=int, default=1000, help='inspect-zip: inspect at least this many concepts')
    parser.add_argument('--n', type=int, default=1000)
    parser.add_argument('--seed', type=int, default=20261005)
    parser.add_argument('--max-mb', type=int, default=1024, help='tar inspection: stop reading a file after this')
    parser.add_argument('--interval', type=float, default=None, help='seconds between requests')
    parser.add_argument('--window-start', help='tests only: enumerate from this day (YYYY-MM-DD)')
    parser.add_argument('--window-end', help='tests only: enumerate up to this day (YYYY-MM-DD)')
    args = parser.parse_args()
    global START, END
    if args.window_start:
        START = datetime.fromisoformat(args.window_start).replace(tzinfo=timezone.utc)
    if args.window_end:
        END = min(END, datetime.fromisoformat(args.window_end).replace(tzinfo=timezone.utc))
    if args.command == 'enumerate' and not args.stratum:
        parser.error('enumerate needs --stratum')
    {'enumerate': enumerate_stratum, 'inspect-zip': inspect_zip, 'inspect-tar': inspect_tar,
     'export': export, 'status': status}[args.command](args)


if __name__ == '__main__':
    main()
