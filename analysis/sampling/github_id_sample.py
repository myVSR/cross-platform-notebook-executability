#!/usr/bin/env python3
"""Step S3 — GitHub exact random-ID sampling (notes/sampling-design.md, section 5, D7).

Every public GitHub repository has a numeric ID. IDs are drawn uniformly at random without
replacement from [1, max ID], where max ID is the largest ID of a repository created before the
cutoff. A drawn ID is looked up with GET /repositories/{id}: 200 = a public repository with exactly
this ID (accepted for screening), 404 = deleted or private (recorded, no replacement by a
neighbouring ID). Accepted repositories pass D2 (not fork, no mirror_url, size > 0, created before
the cutoff) and are screened by their language statistics (Jupyter Notebook bytes > 0).
The draw order is a pure function of the seed: draw k is the k-th distinct ID of
random.Random(seed).randint(1, max_id), so a later extension continues the same sequence.

Commands (resumable; state in data/sampling/github/<name>.sqlite, default name "ids"; the pilot is
the first 1,000 draws of this one seeded sequence, and the sample continues it):
  maxid       bisection over IDs for the largest repository created before the cutoff
  draw        look up draws 1..N (--n) of the seeded sequence (403/451 = blocked repository, recorded
              like a deleted one: no public repository; rate-limit answers are retried)
  screen      language statistics for accepted repositories that pass D2 (GraphQL in batches of
              100 with a token; REST /languages without one)
  treecheck   recursive file tree of a seeded random sample of screened-out repositories
              (--n): how many contain .ipynb files anyway (screening false negatives)
  extend      continue the sequence in batches (--batch, default 1000 draws, each followed by
              screening) until --until-screened repositories pass D2 and the screen (default 650:
              500 for the sample + reserve), or --max-draws is reached
  report      summary.json (hit rates, cost per eligible repository, extrapolation) and the
              draws as CSV.gz (+ SHA-256)
Needs GITHUB_TOKEN (environment or .env) for anything beyond a tiny test: without a token
GitHub allows 60 requests per hour.

Usage:
  python3 analysis/sampling/github_id_sample.py maxid
  python3 analysis/sampling/github_id_sample.py draw --n 1000 --seed 20261005
  python3 analysis/sampling/github_id_sample.py screen
  python3 analysis/sampling/github_id_sample.py treecheck --n 200 --seed 20261005
  python3 analysis/sampling/github_id_sample.py report
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CUTOFF, SAMPLING_DIR, Http, json_of, now, rng, settled, status_of, token,  # noqa: E402
                    write_csv, write_json)

API = 'https://api.github.com'
OUT = SAMPLING_DIR / 'github'
CUTOFF_UTC = datetime.fromisoformat(CUTOFF).replace(tzinfo=timezone.utc)
SCHEMA = """
CREATE TABLE IF NOT EXISTS draws (seq INTEGER PRIMARY KEY, id INTEGER UNIQUE, http_status INTEGER, full_name TEXT,
    node_id TEXT, fork INTEGER, mirror INTEGER, archived INTEGER, created_at TEXT, pushed_at TEXT, size INTEGER,
    default_branch TEXT, language TEXT, stars INTEGER, forks INTEGER, d2_pass INTEGER, d2_reason TEXT, drawn_at TEXT);
CREATE TABLE IF NOT EXISTS languages (id INTEGER PRIMARY KEY, method TEXT, ok INTEGER, jupyter_bytes INTEGER,
    languages_json TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS treecheck (id INTEGER PRIMARY KEY, seed INTEGER, http_status INTEGER, files INTEGER,
    ipynb_files INTEGER, ipynb_example TEXT, truncated INTEGER, checked_at TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
GRAPHQL = """query($ids: [ID!]!) { rateLimit { cost remaining }
  nodes(ids: $ids) { ... on Repository { databaseId
    languages(first: 100) { edges { size node { name } } } } } }"""


def connect(args):
    OUT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(OUT / f'{args.name}.sqlite', timeout=60)
    db.executescript(SCHEMA)
    return db


def client(args):
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    if token('GITHUB_TOKEN'):
        headers['Authorization'] = f'Bearer {token("GITHUB_TOKEN")}'
    else:
        print('warning: no GITHUB_TOKEN; GitHub allows 60 requests per hour', file=sys.stderr)
    return Http(OUT / f'{args.name}-requests.jsonl', min_interval=args.interval, headers=headers)


def meta(db, key, value=None):
    if value is None:
        row = db.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row[0] if row else None
    db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, str(value)))
    db.commit()


def created(repo):
    return datetime.fromisoformat(repo['created_at'].replace('Z', '+00:00'))


def maxid(args):
    """Largest ID with a repository created before the cutoff (IDs grow with creation time)."""
    db, http = connect(args), client(args)
    lo, hi = 1, args.upper                      # invariant: a repository at/after lo is before the cutoff
    while hi - lo > 1:
        mid = (lo + hi) // 2
        page = http.get(f'{API}/repositories', params=dict(since=mid)).json()
        if not page:
            hi = mid
            continue
        first = page[0]['id']
        repo = http.get(f'{API}/repositories/{first}', ok=(200, 404)).json()
        if 'created_at' not in repo:            # vanished between the two calls: try the next one
            repo = http.get(f'{API}/repositories/{page[1]["id"]}').json()
        if created(repo) < CUTOFF_UTC:
            lo = first
        else:
            hi = mid
        print(f'  [{lo}, {hi}]  id {repo["id"]} created {repo["created_at"]}', flush=True)
    meta(db, 'max_id', lo)
    meta(db, 'max_id_found_at', now())
    print(f'max ID before {CUTOFF}: {lo}')


def id_sequence(seed, max_id):
    generator, seen = rng(seed), set()
    while len(seen) < max_id:                       # stops when every ID has been drawn (small test ranges)
        value = generator.randint(1, max_id)
        if value not in seen:
            seen.add(value)
            yield value


def d2(repo):
    """D2 before screening: (passes, reason)."""
    if repo['fork']:
        return 0, 'fork'
    if repo.get('mirror_url'):
        return 0, 'mirror'
    if not repo.get('size'):
        return 0, 'empty'
    if created(repo) >= CUTOFF_UTC:
        return 0, 'created_on_or_after_cutoff'
    return 1, ''


def draw(args):
    db, http = connect(args), client(args)
    max_id = meta(db, 'max_id')
    if not max_id:
        sys.exit('run "maxid" first')
    stored_seed = meta(db, 'seed')
    if stored_seed and int(stored_seed) != args.seed:
        sys.exit(f'this sample uses seed {stored_seed}; use another --name for a different seed')
    meta(db, 'seed', args.seed)
    done = {row[0] for row in db.execute('SELECT seq, http_status FROM draws') if settled(row[1])}
    for seq, repo_id in enumerate(id_sequence(args.seed, int(max_id)), 1):
        if seq > args.n:
            break
        if seq in done:
            continue
        # 404 deleted or private, 451 legal block, 403 blocked by GitHub (e.g. terms of service); rate-limit
        # answers are retried by Http and never reach this point
        response = http.get(f'{API}/repositories/{repo_id}', ok=(200, 403, 404, 451), soft=True)
        status = status_of(response)
        repo = json_of(response)
        if repo:
            passes, reason = d2(repo)
        elif not settled(status):
            passes, reason = 0, f'not resolved (HTTP {status}); retried on the next run'
            print(f'  id {repo_id}: {reason}', flush=True)
        elif status in (403, 451):
            try:
                message = response.json().get('message', '')
            except ValueError:
                message = ''
            passes, reason = 0, f'no public repository (blocked, HTTP {status}: {message})'[:200]
        else:
            passes, reason = 0, 'no public repository' if status == 404 else f'no public repository (HTTP {status})'
        db.execute('INSERT OR REPLACE INTO draws VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            seq, repo_id, status, *(
                (repo['full_name'], repo['node_id'], int(repo['fork']), int(bool(repo.get('mirror_url'))),
                 int(repo['archived']), repo['created_at'], repo.get('pushed_at'), repo.get('size'),
                 repo.get('default_branch'), repo.get('language'), repo.get('stargazers_count'),
                 repo.get('forks_count')) if repo else (None,) * 12),
            passes, reason, now()))
        db.commit()
        if seq % 250 == 0:
            print(f'[{now()}] {seq}/{args.n} drawn', flush=True)
    meta(db, 'draws_requested', args.n)


def screen(args):
    db, http = connect(args), client(args)
    todo = db.execute('SELECT d.id, d.node_id, d.full_name FROM draws d LEFT JOIN languages l ON l.id = d.id '
                      'WHERE d.d2_pass = 1 AND l.id IS NULL ORDER BY d.seq').fetchall()
    print(f'{len(todo)} repositories to screen')
    if token('GITHUB_TOKEN'):
        for start in range(0, len(todo), 100):
            batch = todo[start:start + 100]
            data = json_of(http.post(f'{API}/graphql', json=dict(query=GRAPHQL, variables=dict(ids=[b[1] for b in batch])),
                                     soft=True))
            if data is None or 'data' not in data:
                print(f'  graphql batch {start // 100 + 1} not read; retried on the next run', flush=True)
                continue
            nodes = {n['databaseId']: n for n in (data.get('data') or {}).get('nodes') or [] if n}
            for repo_id, _, _ in batch:
                node = nodes.get(repo_id)
                langs = {e['node']['name']: e['size'] for e in node['languages']['edges']} if node else {}
                db.execute('INSERT OR REPLACE INTO languages VALUES (?,?,?,?,?,?)', (
                    repo_id, 'graphql', int(node is not None), langs.get('Jupyter Notebook', 0) if node else None,
                    json.dumps(langs, sort_keys=True) if node else None, now()))
            db.commit()
    else:
        for repo_id, _, name in todo:
            response = http.get(f'{API}/repos/{name}/languages', ok=(200, 403, 404, 451), soft=True)
            status, langs = status_of(response), json_of(response)
            if not settled(status) or (status == 200 and langs is None):
                print(f'  {name}: languages not read (HTTP {status}); retried on the next run', flush=True)
                continue                                       # no row: the next run screens it again
            langs = langs or {}
            db.execute('INSERT OR REPLACE INTO languages VALUES (?,?,?,?,?,?)', (
                repo_id, 'rest', int(status == 200), langs.get('Jupyter Notebook', 0)
                if status == 200 else None, json.dumps(langs, sort_keys=True), now()))
            db.commit()


def screened_in(db):
    return db.execute('SELECT COUNT(*) FROM languages WHERE jupyter_bytes > 0').fetchone()[0]


def extend(args):
    """Draw and screen in batches until enough screened-in repositories exist (S5 input)."""
    db = connect(args)
    while screened_in(db) < args.until_screened:
        drawn = db.execute('SELECT COUNT(*) FROM draws').fetchone()[0]
        if drawn >= args.max_draws:
            print(f'stopped at --max-draws {args.max_draws}: {screened_in(db)} screened-in')
            break
        args.n = min(drawn + args.batch, args.max_draws)
        draw(args)
        screen(args)
        print(f'[{now()}] {args.n} draws, {screened_in(db)} screened-in (target {args.until_screened})', flush=True)
    meta(db, 'extend_target', args.until_screened)


def treecheck(args):
    db, http = connect(args), client(args)
    negatives = db.execute('SELECT d.id, d.full_name, d.default_branch FROM draws d JOIN languages l ON l.id = d.id '
                           'WHERE l.ok = 1 AND l.jupyter_bytes = 0 ORDER BY d.seq').fetchall()
    done = {row[0] for row in db.execute('SELECT id, http_status FROM treecheck WHERE seed = ?', (args.seed,))
            if settled(row[1])}
    for repo_id, name, branch in rng(args.seed).sample(negatives, min(args.n, len(negatives))):
        if repo_id in done:
            continue
        response = http.get(f'{API}/repos/{name}/git/trees/{branch}', params=dict(recursive=1), soft=True)
        status, data = status_of(response), json_of(response, {})
        if status != 200:
            print(f'  {name}: tree not read (HTTP {status})', flush=True)
        blobs = [e['path'] for e in data.get('tree', []) if e.get('type') == 'blob']
        ipynb = [p for p in blobs if p.lower().endswith('.ipynb')]
        db.execute('INSERT OR REPLACE INTO treecheck VALUES (?,?,?,?,?,?,?,?)', (
            repo_id, args.seed, status, len(blobs), len(ipynb), ipynb[0] if ipynb else None,
            int(bool(data.get('truncated'))) if status == 200 else None, now()))
        db.commit()


def report(args):
    db = connect(args)
    one = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    draws = one('SELECT COUNT(*) FROM draws')
    public = one('SELECT COUNT(*) FROM draws WHERE http_status = 200')
    reasons = dict(db.execute("SELECT COALESCE(NULLIF(d2_reason, ''), 'pass'), COUNT(*) FROM draws "
                        'WHERE http_status = 200 GROUP BY 1').fetchall())
    d2_pass = one('SELECT COUNT(*) FROM draws WHERE d2_pass = 1')
    screened = one('SELECT COUNT(*) FROM languages WHERE ok = 1')
    jupyter = one('SELECT COUNT(*) FROM languages WHERE jupyter_bytes > 0')
    tree = db.execute('SELECT COUNT(*), SUM(http_status = 200), SUM(ipynb_files > 0) FROM treecheck').fetchone()
    log = OUT / f'{args.name}-requests.jsonl'
    requests_total = sum(1 for _ in open(log)) if log.exists() else 0
    rate = jupyter / draws if draws else 0
    summary = dict(
        reported_at=now(), cutoff=CUTOFF, max_id=meta(db, 'max_id'), seed=meta(db, 'seed'), draws=draws,
        public_repository=public, public_share=public / draws if draws else None,
        d2_outcomes=reasons, d2_pass=d2_pass, languages_read=screened, screened_in_jupyter=jupyter,
        jupyter_share_of_d2_pass=jupyter / screened if screened else None, screened_in_per_draw=rate,
        treecheck=dict(sampled=tree[0], read=tree[1], with_ipynb=tree[2],
                       false_negative_rate=(tree[2] / tree[1]) if tree[1] else None),
        requests_logged=requests_total,
        draws_needed_for_500_screened_in=round(500 / rate) if rate else None,
        hours_at_5000_requests_per_hour=round(500 / rate / 5000, 1) if rate else None)
    write_json(OUT / f'{args.name}-summary.json', summary)
    names = [d[0] for d in db.execute('SELECT * FROM draws LIMIT 0').description]
    rows = [dict(zip(names + ['jupyter_bytes'], r)) for r in db.execute(
        'SELECT d.*, l.jupyter_bytes FROM draws d LEFT JOIN languages l ON l.id = d.id ORDER BY d.seq')]
    write_csv(OUT / f'{args.name}-draws.csv.gz', rows, names + ['jupyter_bytes'])
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['maxid', 'draw', 'screen', 'extend', 'treecheck', 'report'])
    parser.add_argument('--name', default='ids', help='sample name (state file and outputs)')
    parser.add_argument('--n', type=int, default=1000)
    parser.add_argument('--seed', type=int, default=20261005)
    parser.add_argument('--upper', type=int, default=2_000_000_000, help='upper bound for the max-ID bisection')
    parser.add_argument('--interval', type=float, default=0.75, help='seconds between requests')
    parser.add_argument('--until-screened', type=int, default=650, help='extend: screened-in repositories wanted')
    parser.add_argument('--batch', type=int, default=1000, help='extend: draws per batch')
    parser.add_argument('--max-draws', type=int, default=400_000, help='extend: upper limit of draws')
    args = parser.parse_args()
    dict(maxid=maxid, draw=draw, screen=screen, extend=extend, treecheck=treecheck, report=report)[args.command](args)


if __name__ == '__main__':
    main()
