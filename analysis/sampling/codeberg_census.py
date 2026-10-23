#!/usr/bin/env python3
"""Step S2 — Codeberg census of the frame (notes/sampling-design.md, section 5).

Commands (all resumable; state in data/sampling/codeberg/census.sqlite):
  enumerate   list every public repository via /repos/search (sort=id, 50 per page). Paging is by
              offset, so a repository deleted during the crawl shifts later pages by one; run the
              command a second time (--pass 2) and the union closes such gaps (both passes are logged).
  languages   GET /repos/{owner}/{repo}/languages for every repository that passes D2 before
              screening (not fork, not mirror, not empty, not private, created before the cutoff).
              Candidates are checked in a seeded random order (table check_order): the checked
              part is always a simple random sample of the candidates, so the census can be stopped
              at any time (or with --stop-after N screened-in repositories) and still be used; the
              export then reports the checked fraction and the estimated frame size. Candidates found
              only by a later listing pass are appended in their own seeded random order.
  treecheck   screening check: recursive file tree of a seeded random sample of repositories whose
              languages show no "Jupyter Notebook"; counts those that contain .ipynb files anyway.
  export      frame (screened-in repositories) as CSV.gz + SHA-256, and summary.json with the
              count at every filter step, the request totals and the tree-check result.
  status      progress.

Usage:
  python3 analysis/sampling/codeberg_census.py enumerate [--pass 1]
  python3 analysis/sampling/codeberg_census.py languages [--seed 20261005] [--stop-after 0]
  python3 analysis/sampling/codeberg_census.py treecheck --n 500 --seed 20261005
  python3 analysis/sampling/codeberg_census.py export
Pacing: --interval seconds between requests (default 0.35, below Codeberg's 2,000 per 10 minutes);
429 answers are retried after Retry-After. CODEBERG_TOKEN (optional) is sent if set.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CUTOFF, SAMPLING_DIR, Http, ServerError, json_of, now, rng, settled, status_of, token,  # noqa: E402
                    write_csv, write_json)

API = 'https://codeberg.org/api/v1'
OUT = SAMPLING_DIR / 'codeberg'
PAGE_SIZE = 50
CUTOFF_UTC = datetime.fromisoformat(CUTOFF).replace(tzinfo=timezone.utc)
SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY, full_name TEXT, fork INTEGER, mirror INTEGER, empty INTEGER, archived INTEGER,
    private INTEGER, template INTEGER, created_at TEXT, updated_at TEXT, size INTEGER, default_branch TEXT,
    language TEXT, stars INTEGER, forks INTEGER, first_pass INTEGER, first_page INTEGER, listed_at TEXT);
CREATE TABLE IF NOT EXISTS listing_errors (pass INTEGER, list_offset INTEGER, http_status INTEGER, error TEXT,
    recorded_at TEXT, PRIMARY KEY (pass, list_offset));
CREATE TABLE IF NOT EXISTS pages (pass INTEGER, page INTEGER, items INTEGER, first_id INTEGER, last_id INTEGER,
    total_count INTEGER, fetched_at TEXT, PRIMARY KEY (pass, page));
CREATE TABLE IF NOT EXISTS languages (id INTEGER PRIMARY KEY, http_status INTEGER, jupyter_bytes INTEGER,
    languages_json TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS treecheck (id INTEGER PRIMARY KEY, seed INTEGER, http_status INTEGER, files INTEGER,
    ipynb_files INTEGER, ipynb_example TEXT, truncated INTEGER, checked_at TEXT);
CREATE TABLE IF NOT EXISTS check_order (seq INTEGER PRIMARY KEY, id INTEGER UNIQUE, seed INTEGER, batch INTEGER,
    added_at TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect():
    OUT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(OUT / 'census.sqlite', timeout=60)
    db.executescript(SCHEMA)
    return db


def http(args):
    headers = {'Accept': 'application/json'}
    if token('CODEBERG_TOKEN'):
        headers['Authorization'] = f'token {token("CODEBERG_TOKEN")}'
    return Http(OUT / 'requests.jsonl', min_interval=args.interval, headers=headers)


def before_cutoff(created_at):
    return created_at and datetime.fromisoformat(created_at).astimezone(timezone.utc) < CUTOFF_UTC


def fetch_listing(client, page, limit):
    """(items, X-Total-Count) of one /repos/search page (offset = (page - 1) * limit)."""
    response = client.get(f'{API}/repos/search', params=dict(limit=limit, page=page, sort='id', order='asc'))
    return response.json().get('data', []), response.headers.get('X-Total-Count')


def fetch_listing_robust(db, client, quick, pass_, page):
    """One 50-item page. If Codeberg keeps answering 5xx (seen 2026-10-06 at page 2734: most likely one
    entry that cannot be serialised), the page is fetched as 5 pages of 10, a failing one as 10 single
    entries, and every entry that still fails is recorded in listing_errors (pass, list offset) and
    skipped. Returns (items, total, positions covered)."""
    try:
        items, total = fetch_listing(client, page, PAGE_SIZE)
        return items, total, len(items)
    except ServerError as error:
        print(f'[{now()}] page {page}: {error}; fetching it in parts', flush=True)
    items, total, covered = [], None, 0
    for sub in range(PAGE_SIZE // 10):
        page10 = (page - 1) * (PAGE_SIZE // 10) + sub + 1
        try:
            part, total = fetch_listing(quick, page10, 10)
            items += part
            covered += len(part)
            if len(part) < 10:
                break
            continue
        except ServerError:
            pass
        for k in range(10):
            page1 = (page10 - 1) * 10 + k + 1
            try:
                part, total = fetch_listing(quick, page1, 1)
            except ServerError as error:
                db.execute('INSERT OR REPLACE INTO listing_errors VALUES (?,?,?,?,?)',
                           (pass_, page1 - 1, getattr(error.response, 'status_code', None), str(error)[:300], now()))
                db.commit()
                print(f'[{now()}] unlistable entry at offset {page1 - 1} (recorded)', flush=True)
                covered += 1
                continue
            items += part
            covered += len(part)
            if not part:
                return items, total, covered
    return items, total, covered


def enumerate_repos(args):
    db, client = connect(), http(args)
    quick = http(args)
    quick.max_retries = 3                           # parts of a failing page: fail fast
    done = {row[0] for row in db.execute('SELECT page FROM pages WHERE pass = ?', (args.pass_,))}
    page = 1
    while True:
        if page in done:
            page += 1
            continue
        items, total, covered = fetch_listing_robust(db, client, quick, args.pass_, page)
        stamp = now()
        for r in items:
            db.execute('INSERT OR IGNORE INTO repos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                r['id'], r['full_name'], r.get('fork'), r.get('mirror'), r.get('empty'), r.get('archived'),
                r.get('private'), r.get('template'), r.get('created_at'), r.get('updated_at'), r.get('size'),
                r.get('default_branch'), r.get('language'), r.get('stars_count'), r.get('forks_count'),
                args.pass_, page, stamp))
        db.execute('INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?)', (
            args.pass_, page, len(items), items[0]['id'] if items else None, items[-1]['id'] if items else None,
            total, stamp))
        db.commit()
        if page % 200 == 0:
            print(f'[{now()}] pass {args.pass_} page {page}: {db.execute("SELECT COUNT(*) FROM repos").fetchone()[0]} repositories', flush=True)
        if covered < PAGE_SIZE or (args.max_pages and page >= args.max_pages):
            break
        page += 1
    db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (f'enumerate_pass_{args.pass_}_finished', now()))
    db.commit()
    print(f'pass {args.pass_} finished: {db.execute("SELECT COUNT(*) FROM repos").fetchone()[0]} repositories')


def screen_candidates(db):
    """Repositories that pass D2 before screening, as (id, full_name, default_branch)."""
    rows = db.execute('SELECT id, full_name, default_branch, created_at FROM repos '
                      'WHERE NOT fork AND NOT mirror AND NOT empty AND NOT private ORDER BY id').fetchall()
    return [(i, name, branch) for i, name, branch, created in rows if before_cutoff(created)]


def order_candidates(db, seed):
    """Append candidates not yet in check_order, as a new batch in seeded random order; return the order."""
    stored = db.execute("SELECT value FROM meta WHERE key = 'check_order_seed'").fetchone()
    if stored and int(stored[0]) != seed:
        sys.exit(f'the check order was drawn with seed {stored[0]}; use --seed {stored[0]}')
    ordered = {row[0] for row in db.execute('SELECT id FROM check_order')}
    new = sorted(c[0] for c in screen_candidates(db) if c[0] not in ordered)
    if new:
        batch = db.execute('SELECT COALESCE(MAX(batch), 0) + 1 FROM check_order').fetchone()[0]
        start = db.execute('SELECT COALESCE(MAX(seq), 0) FROM check_order').fetchone()[0]
        rng(f'{seed}-{batch}').shuffle(new)
        stamp = now()
        db.executemany('INSERT INTO check_order VALUES (?,?,?,?,?)',
                       [(start + i, repo_id, seed, batch, stamp) for i, repo_id in enumerate(new, 1)])
        db.execute("INSERT OR REPLACE INTO meta VALUES ('check_order_seed', ?)", (seed,))
        db.commit()
        print(f'check order: batch {batch} with {len(new)} candidates (seed {seed})', flush=True)
    return db.execute('SELECT o.id, r.full_name FROM check_order o JOIN repos r ON r.id = o.id '
                      'ORDER BY o.seq').fetchall()


def jupyter_hits(db):
    return db.execute('SELECT COUNT(*) FROM languages WHERE jupyter_bytes > 0').fetchone()[0]


def languages(args):
    db, client = connect(), http(args)
    checked = {row[0] for row in db.execute('SELECT id, http_status FROM languages') if settled(row[1])}
    todo = [c for c in order_candidates(db, args.seed) if c[0] not in checked]
    print(f'{len(todo)} repositories to check ({len(checked)} already checked)', flush=True)
    if args.max_checks:
        todo = todo[:args.max_checks]
    stopped = args.stop_after and jupyter_hits(db) >= args.stop_after
    for index, (repo_id, name) in enumerate([] if stopped else todo, 1):
        response = client.get(f'{API}/repos/{quote(name)}/languages', ok=(200, 403, 404, 451), soft=True)
        status, data = status_of(response), json_of(response)
        if status == 200 and data is None:
            status = 'invalid json'                            # not settled: retried on the next run
        if not settled(status):
            print(f'  {name}: languages not read (HTTP {status}); retried on the next run', flush=True)
        data = data or {}
        db.execute('INSERT OR REPLACE INTO languages VALUES (?,?,?,?,?)', (
            repo_id, status, data.get('Jupyter Notebook', 0) if status == 200 else None,
            json.dumps(data, sort_keys=True) if status == 200 else None, now()))
        if index % 100 == 0:
            db.commit()
        if index % 5000 == 0:
            print(f'[{now()}] {index}/{len(todo)} checked; {jupyter_hits(db)} with Jupyter so far', flush=True)
        if args.stop_after and data.get('Jupyter Notebook', 0) > 0 and jupyter_hits(db) >= args.stop_after:
            stopped = True
            break
    db.commit()
    if stopped:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('languages_stopped_early', ?)",
                   (f'{now()}: {jupyter_hits(db)} screened-in repositories reached --stop-after {args.stop_after}',))
        print(f'stopped: {jupyter_hits(db)} screened-in repositories (--stop-after {args.stop_after})')
    elif not args.max_checks:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('languages_finished', ?)", (now(),))
    db.commit()


def treecheck(args):
    db, client = connect(), http(args)
    negatives = db.execute('SELECT r.id, r.full_name, r.default_branch FROM repos r JOIN languages l ON l.id = r.id '
                           'WHERE l.http_status = 200 AND l.jupyter_bytes = 0 ORDER BY r.id').fetchall()
    sample = rng(args.seed).sample(negatives, min(args.n, len(negatives)))
    done = {row[0] for row in db.execute('SELECT id, http_status FROM treecheck WHERE seed = ?', (args.seed,))
            if settled(row[1])}
    for repo_id, name, branch in sample:
        if repo_id in done:
            continue
        files, ipynb, truncated, status, page = 0, [], 0, None, 1
        while True:
            # any answer is recorded (e.g. 400 for a branch the API cannot resolve); only 5xx/unreachable are retried later
            response = client.get(f'{API}/repos/{quote(name)}/git/trees/{quote(branch or "HEAD", safe="")}',
                                  params=dict(recursive='true', per_page=1000, page=page), soft=True)
            status, data = status_of(response), json_of(response)
            if status != 200 or data is None:
                if status != 200:
                    print(f'  {name}: tree not read (HTTP {status})', flush=True)
                break
            entries = [e for e in data.get('tree') or [] if e.get('type') == 'blob']
            files += len(entries)
            ipynb += [e['path'] for e in entries if e['path'].lower().endswith('.ipynb')]
            truncated = int(bool(data.get('truncated')))
            if not truncated or page >= 20:
                break
            page += 1
        db.execute('INSERT OR REPLACE INTO treecheck VALUES (?,?,?,?,?,?,?,?)', (
            repo_id, args.seed, status, files, len(ipynb), ipynb[0] if ipynb else None, truncated, now()))
        db.commit()
    rows = db.execute('SELECT COUNT(*), SUM(http_status = 200), SUM(ipynb_files > 0) FROM treecheck WHERE seed = ?',
                      (args.seed,)).fetchone()
    print(f'tree check (seed {args.seed}): {rows[0]} sampled, {rows[1]} trees read, {rows[2]} contain .ipynb')


def export(args):
    db = connect()
    count = lambda sql, *p: db.execute(sql, p).fetchone()[0]  # noqa: E731
    total = count('SELECT COUNT(*) FROM repos')
    rows = db.execute('SELECT * FROM repos ORDER BY id').fetchall()
    names = [d[0] for d in db.execute('SELECT * FROM repos LIMIT 0').description]
    repos = [dict(zip(names, r)) for r in rows]
    steps = dict(listed=total,
                 fork=sum(bool(r['fork']) for r in repos),
                 mirror=sum(bool(r['mirror']) and not r['fork'] for r in repos),
                 empty=sum(bool(r['empty']) and not r['fork'] and not r['mirror'] for r in repos),
                 private=sum(bool(r['private']) and not (r['fork'] or r['mirror'] or r['empty']) for r in repos))
    candidates = screen_candidates(db)
    steps['created_on_or_after_cutoff'] = total - sum(steps[k] for k in ('fork', 'mirror', 'empty', 'private')) - len(candidates)
    steps['screen_candidates'] = len(candidates)
    lang = dict(db.execute('SELECT http_status, COUNT(*) FROM languages GROUP BY http_status').fetchall())
    steps['languages_checked'] = sum(lang.values())
    steps['languages_http_status'] = lang
    steps['screened_in_jupyter'] = count('SELECT COUNT(*) FROM languages WHERE jupyter_bytes > 0')
    steps['unlistable_entries'] = dict(db.execute('SELECT pass, COUNT(*) FROM listing_errors GROUP BY pass').fetchall())
    # checked part of the seeded random check order: a simple random sample of the candidates
    ordered = count('SELECT COUNT(*) FROM check_order')
    checked_ok = count('SELECT COUNT(*) FROM languages WHERE http_status = 200')
    fraction = steps['languages_checked'] / ordered if ordered else None
    steps['check_order'] = dict(
        candidates_ordered=ordered, checked=steps['languages_checked'], checked_fraction=fraction,
        complete=bool(ordered) and steps['languages_checked'] >= ordered,
        estimated_screened_in_total=(round(steps['screened_in_jupyter'] / (checked_ok / ordered))
                                     if ordered and checked_ok else None),
        note='estimate = screened-in / (checked with HTTP 200 / candidates); exact when complete')
    frame = db.execute(
        'SELECT r.*, l.jupyter_bytes, l.languages_json, o.seq FROM repos r JOIN languages l ON l.id = r.id '
        'LEFT JOIN check_order o ON o.id = r.id WHERE l.jupyter_bytes > 0 ORDER BY r.id').fetchall()
    fields = names + ['jupyter_bytes', 'languages_json', 'check_seq']
    frame_rows = [dict(zip(fields, r)) for r in frame]
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    target = OUT / f'frame-codeberg-{stamp}.csv.gz'
    digest = write_csv(target, frame_rows, fields)
    tree = db.execute('SELECT seed, COUNT(*), SUM(http_status = 200), SUM(ipynb_files > 0) FROM treecheck GROUP BY seed').fetchall()
    requests_logged = sum(1 for _ in open(OUT / 'requests.jsonl')) if (OUT / 'requests.jsonl').exists() else 0
    summary = dict(exported_at=now(), cutoff=CUTOFF, frame=target.name, frame_sha256=digest, filter_steps=steps,
                   passes={p: n for p, n in db.execute('SELECT pass, COUNT(*) FROM pages GROUP BY pass')},
                   x_total_count_last=count('SELECT total_count FROM pages ORDER BY fetched_at DESC LIMIT 1'),
                   treecheck=[dict(seed=s, sampled=n, read=r, with_ipynb=w,
                                   false_negative_rate=(w / r if r else None)) for s, n, r, w in tree],
                   requests_logged=requests_logged, meta=dict(db.execute('SELECT key, value FROM meta').fetchall()))
    write_json(OUT / 'summary.json', summary)
    print(json.dumps(summary, indent=2))


def status(args):
    db = connect()
    print(json.dumps(dict(
        repositories=db.execute('SELECT COUNT(*) FROM repos').fetchone()[0],
        pages_by_pass=dict(db.execute('SELECT pass, COUNT(*) FROM pages GROUP BY pass').fetchall()),
        screen_candidates=len(screen_candidates(db)),
        languages_checked=db.execute('SELECT COUNT(*) FROM languages').fetchone()[0],
        with_jupyter=db.execute('SELECT COUNT(*) FROM languages WHERE jupyter_bytes > 0').fetchone()[0],
        check_order=db.execute('SELECT COUNT(*) FROM check_order').fetchone()[0],
        treecheck=db.execute('SELECT COUNT(*) FROM treecheck').fetchone()[0],
        meta=dict(db.execute('SELECT key, value FROM meta').fetchall())), indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['enumerate', 'languages', 'treecheck', 'export', 'status'])
    parser.add_argument('--pass', dest='pass_', type=int, default=1)
    parser.add_argument('--n', type=int, default=500)
    parser.add_argument('--seed', type=int, default=20261005)
    parser.add_argument('--interval', type=float, default=0.35)
    parser.add_argument('--max-pages', type=int, default=0, help='stop after this page (tests only)')
    parser.add_argument('--max-checks', type=int, default=0, help='stop after this many language checks (tests only)')
    parser.add_argument('--stop-after', type=int, default=0,
                        help='languages: stop once this many screened-in repositories are found (0 = full census)')
    args = parser.parse_args()
    dict(enumerate=enumerate_repos, languages=languages, treecheck=treecheck, export=export, status=status)[args.command](args)


if __name__ == '__main__':
    main()
