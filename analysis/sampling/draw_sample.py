#!/usr/bin/env python3
"""Step S5 — draw the samples from the frames (notes/sampling-design.md, sections 5-6, D4, D5, D9).

Inputs (state of S2-S4, read only):
  data/sampling/codeberg/census.sqlite   screened-in repositories (jupyter_bytes > 0). If the census
                                         was stopped early, the checked part is a simple random sample of
                                         the candidates (seeded check order), so drawing from it is valid.
  data/sampling/github/ids.sqlite        exact random-ID draws: repositories that pass D2 and the screen,
                                         in draw order. The ID sequence is already a uniform random sample,
                                         so the first n screened-in repositories are the sample.
  data/sampling/zenodo/frame.sqlite      Z1 (all open concepts) and the inspected Z2/Z3 subsamples.

Design per platform (n = --n primary resources, default 500; --reserve more in a second seeded draw):
  Codeberg  stratified by creation year (proportional allocation, largest remainder); all of the frame
            if it holds no more than n.
  GitHub    first n screened-in repositories of the ID sequence (self-weighting; creation year recorded);
            the next --reserve are the reserve.
  Zenodo    two-phase stratified design. Phase 1 (S4): m_h of N_h concepts inspected per stratum
            (Z1: m = N). Pool_h = inspected concepts with a notebook, or with unknown content (truncated
            listing, tar over the size limit; resolved at acquisition, D1). Estimated eligible
            N~_h = N_h * |pool_h| / m_h. Allocation n_h proportional to N~_h, at least --min-stratum,
            capped at |pool_h|. Within a stratum: by creation year. Inclusion probability
            pi = (m_h / N_h) * (n_h / |pool_h|); weight = 1 / pi.
            Version check (on by default, one search per drawn record, primary and reserve): the record
            must be the newest version of its concept created before the cutoff; otherwise it is
            ineligible (over-coverage). An ineligible primary record is replaced by the next reserve record
            of the same stratum; an ineligible reserve record is dropped. Both are kept in draw.csv.
All random steps use --seed (default 20261005) with a fixed sub-seed per platform/stratum/year.

Output: data/sampling/draw/<date>/
  draw.csv            every drawn resource: platform, stratum, year, url, ids, role (primary | reserve |
                      ineligible), processing order, pi, weight, content, version check
  cohort-primary.csv  category,platform,url in random processing order (platforms mixed) for
                      pipeline/run_full_sample.sh / hpc/run_array.sbatch (NBF_COHORT=...)
  cohort-reserve.csv  the reserve in processing order (D9: used in this order, every use logged)
  summary.json        frame sizes, pools, allocations, input SHA-256, seed, replacements
Usage:
  python3 analysis/sampling/draw_sample.py [--n 500] [--reserve 150] [--seed 20261005]
         [--platforms codeberg github zenodo] [--no-check-versions] [--allow-incomplete]
Refuses to draw while a frame is unfinished (S2 language checks, S4 enumeration, S3 screened-in < n + reserve).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CUTOFF, SAMPLING_DIR, Http, now, rng, sha256_file, token, write_csv, write_json  # noqa: E402

FIELDS = ['platform', 'stratum', 'year', 'url', 'resource_id', 'name', 'conceptrecid', 'role', 'order',
          'pi', 'weight', 'content', 'version_check', 'created']


def open_ro(path):
    if not Path(path).exists():
        return None
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def allocate(weights, n, caps=None, minimum=0):
    """Integer allocation of n proportional to `weights` (largest remainder), each cell at least `minimum`
    and at most its cap (default: its weight, i.e. its size). The total is min(n, sum of caps).
    Deterministic: ties are broken by key."""
    caps = caps if caps is not None else weights
    total = min(n, sum(caps.values()))
    alloc = {k: min(minimum, caps[k]) for k in sorted(weights)}
    while sum(alloc.values()) < total:
        open_keys = [k for k in sorted(weights) if alloc[k] < caps[k]]
        w = {k: weights[k] for k in open_keys}
        if not sum(w.values()):
            w = {k: 1 for k in open_keys}
        remaining = total - sum(alloc.values())
        quota = {k: remaining * w[k] / sum(w.values()) for k in open_keys}
        moved = 0
        for k in open_keys:
            step = min(int(quota[k]), caps[k] - alloc[k])
            alloc[k] += step
            moved += step
        if moved == 0:                                         # remainder: largest fractions first
            for k in sorted(open_keys, key=lambda k: (-quota[k], k))[:remaining]:
                alloc[k] += 1
    return alloc


def stratified_by_year(rows, n, reserve, seed_key):
    """Proportional allocation over creation years; returns (primary, reserve) lists in a seeded
    random order. Each year cell is shuffled once; the primary takes its first k, the reserve is a
    second proportional allocation over what is left."""
    by_year = defaultdict(list)
    for row in sorted(rows, key=lambda r: str(r['resource_id'])):
        by_year[row['year']].append(row)
    for year, cell in by_year.items():
        rng(f'{seed_key}-{year}').shuffle(cell)
    first = allocate({y: len(c) for y, c in by_year.items()}, n)
    primary = [r for y, c in by_year.items() for r in c[:first[y]]]
    left = {y: c[first[y]:] for y, c in by_year.items()}
    second = allocate({y: len(c) for y, c in left.items()}, reserve)
    spare = [r for y, c in left.items() for r in c[:second[y]]]
    rng(f'{seed_key}-order').shuffle(primary)
    rng(f'{seed_key}-reserve-order').shuffle(spare)
    return primary, spare, dict(frame_by_year=dict(sorted(Counter(r['year'] for r in rows).items())),
                                primary_by_year=dict(sorted(first.items())), reserve_by_year=dict(sorted(second.items())))


def year(timestamp):
    return int(str(timestamp)[:4]) if timestamp else None


def draw_codeberg(args, info):
    db = open_ro(SAMPLING_DIR / 'codeberg' / 'census.sqlite')
    if db is None:
        return []
    rows = [dict(platform='codeberg', stratum='', year=year(created), url=f'https://codeberg.org/{name}',
                 resource_id=repo_id, name=name, created=created, content='screened-in')
            for repo_id, name, created in db.execute(
                'SELECT r.id, r.full_name, r.created_at FROM repos r JOIN languages l ON l.id = r.id '
                'WHERE l.jupyter_bytes > 0 ORDER BY r.id')]
    ordered = db.execute('SELECT COUNT(*) FROM check_order').fetchone()[0]
    checked = db.execute('SELECT COUNT(*) FROM languages').fetchone()[0]
    primary, spare, years = stratified_by_year(rows, args.n, args.reserve, f'{args.seed}-codeberg')
    census_complete = bool(ordered) and checked >= ordered
    fraction = checked / ordered if ordered else None
    for row in primary + spare:
        # inclusion probability: checked fraction (phase 1, random check order) x draw fraction
        draw_fraction = min(1.0, (args.n / len(rows))) if rows else None
        row['pi'] = round((fraction or 1) * draw_fraction, 8) if draw_fraction else None
    info['codeberg'] = dict(frame_screened_in=len(rows), candidates=ordered, checked=checked,
                            census_complete=census_complete, primary=len(primary), reserve=len(spare),
                            all_of_frame=len(rows) <= args.n, **years)
    return tag(primary, spare)


def draw_github(args, info):
    db = open_ro(SAMPLING_DIR / 'github' / f'{args.github_name}.sqlite')
    if db is None:
        return []
    rows = [dict(platform='github', stratum='', year=year(created), url=f'https://github.com/{name}',
                 resource_id=repo_id, name=name, created=created, content='screened-in', seq=seq)
            for seq, repo_id, name, created in db.execute(
                'SELECT d.seq, d.id, d.full_name, d.created_at FROM draws d JOIN languages l ON l.id = d.id '
                'WHERE d.d2_pass = 1 AND l.jupyter_bytes > 0 ORDER BY d.seq')]
    draws = db.execute('SELECT COUNT(*) FROM draws').fetchone()[0]
    max_id = db.execute("SELECT value FROM meta WHERE key = 'max_id'").fetchone()
    primary, spare = rows[:args.n], rows[args.n:args.n + args.reserve]
    for row in primary + spare:
        row['pi'] = None            # self-weighting: equal for every public repository (draws / max ID)
    info['github'] = dict(draws=draws, max_id=int(max_id[0]) if max_id else None, screened_in=len(rows),
                          primary=len(primary), reserve=len(spare), short_by=max(0, args.n - len(primary)),
                          primary_by_year=dict(sorted(Counter(r['year'] for r in primary).items())))
    return tag(primary, spare)


def tag(primary, spare):
    for order, row in enumerate(primary, 1):
        row.update(role='primary', order=order)
    for order, row in enumerate(spare, 1):
        row.update(role='reserve', order=order)
    for row in primary + spare:
        row['weight'] = round(1 / row['pi'], 4) if row.get('pi') else None
    return primary + spare


def zenodo_pools(db):
    """Per stratum: frame size N, inspected m, pool rows (notebook or unknown content)."""
    import zenodo_frame as zf      # same definitions as S4
    pools = {}
    latest = {r['id']: r for r in zf.concept_frame(db) if r['access_right'] == 'open'}
    z1 = [r for r in latest.values() if r['stratum'] == 'Z1']
    pools['Z1'] = dict(N=len(z1), m=len(z1), rows=[dict(r, content='notebook') for r in z1])
    for stratum in ('Z2', 'Z3'):
        seeds = [r[0] for r in db.execute('SELECT DISTINCT seed FROM inspection_samples WHERE stratum = ?', (stratum,))]
        if not seeds:
            continue
        seed = seeds[0]
        size = db.execute('SELECT value FROM meta WHERE key = ?', (f'inspection_{stratum}_{seed}_frame_size',)).fetchone()
        outcomes = dict(db.execute(
            "SELECT s.id, GROUP_CONCAT(i.outcome) FROM inspection_samples s JOIN inspections i ON i.id = s.id "
            'WHERE s.stratum = ? AND s.seed = ? GROUP BY s.id', (stratum, seed)).fetchall())
        rows = []
        for record_id, values in outcomes.items():
            values = values.split(',')
            content = 'notebook' if 'notebook' in values else 'unknown' if 'unknown' in values else None
            if content and record_id in latest:
                rows.append(dict(latest[record_id], content=content))
        pools[stratum] = dict(N=int(size[0]) if size else None, m=len(outcomes), rows=rows, inspection_seed=seed)
    return pools


def newest_before_cutoff(http, conceptrecid):
    hits = http.get('https://zenodo.org/api/records', params=dict(
        q=f'conceptrecid:{conceptrecid} AND created:[* TO {CUTOFF}}}', all_versions='true', size=1,
        sort='newest')).json()['hits']['hits']
    return hits[0]['id'] if hits else None


def draw_zenodo(args, info):
    db = open_ro(SAMPLING_DIR / 'zenodo' / 'frame.sqlite')
    if db is None:
        return []
    pools = zenodo_pools(db)
    estimated = {h: round(p['N'] * len(p['rows']) / p['m']) if p['m'] else 0 for h, p in pools.items()}
    alloc = allocate(estimated, args.n, caps={h: len(pools[h]['rows']) for h in pools}, minimum=args.min_stratum)
    http = None
    if args.check_versions:
        headers = {'Authorization': f'Bearer {token("ZENODO_TOKEN")}'} if token('ZENODO_TOKEN') else {}
        http = Http(SAMPLING_DIR / 'draw' / 'zenodo-versions-requests.jsonl', min_interval=2.1, headers=headers)
    out, info['zenodo'] = [], dict(strata={})
    for stratum, pool in pools.items():
        rows = [dict(platform='zenodo', stratum=stratum, year=year(r['created']), url=f'https://zenodo.org/records/{r["id"]}',
                     resource_id=r['id'], name=r['title'], conceptrecid=r['conceptrecid'], created=r['created'],
                     content=r['content']) for r in pool['rows']]
        n_h = alloc[stratum]
        primary, spare, years = stratified_by_year(rows, n_h, args.reserve_per_stratum, f'{args.seed}-zenodo-{stratum}')
        pi = (pool['m'] / pool['N']) * (n_h / len(rows)) if rows and pool['N'] else None
        replaced = []
        if http:
            accepted = []
            queue = list(spare)
            for row in primary:
                while row is not None:
                    newest = newest_before_cutoff(http, row['conceptrecid'])
                    row['version_check'] = 'ok' if newest == row['resource_id'] else f'newer pre-cutoff version {newest}'
                    if newest == row['resource_id']:
                        accepted.append(row)
                        break
                    row['role'] = 'ineligible'
                    replaced.append(row)
                    row = queue.pop(0) if queue else None
            checked_spare = []
            for row in queue:                      # the reserve is checked too, so it can be used as is (D9)
                newest = newest_before_cutoff(http, row['conceptrecid'])
                row['version_check'] = 'ok' if newest == row['resource_id'] else f'newer pre-cutoff version {newest}'
                (checked_spare if newest == row['resource_id'] else replaced).append(row)
            primary, spare = accepted, checked_spare
        for row in primary + spare:
            row['pi'] = round(pi, 8) if pi else None
            row.setdefault('version_check', 'not checked')
        tagged = tag(primary, spare)
        for row in replaced:
            row.update(role='ineligible', order=None, pi=None, weight=None)
        out += tagged + replaced
        info['zenodo']['strata'][stratum] = dict(
            frame_N=pool['N'], inspected_m=pool['m'], pool=len(rows),
            pool_unknown_content=sum(r['content'] == 'unknown' for r in rows),
            estimated_eligible=estimated[stratum], allocated=n_h, primary=len(primary), reserve=len(spare),
            ineligible_by_version_check=len(replaced), pi=pi, weight=(1 / pi) if pi else None,
            inspection_seed=pool.get('inspection_seed'), **years)
    info['zenodo']['version_check'] = bool(http)
    return out


def incomplete_inputs(args):
    """Reasons why a frame is not finished yet (the draw refuses unless --allow-incomplete)."""
    reasons = []
    def meta(path):
        db = open_ro(path)
        return dict(db.execute('SELECT key, value FROM meta').fetchall()) if db else None
    if 'codeberg' in args.platforms:
        m = meta(SAMPLING_DIR / 'codeberg' / 'census.sqlite')
        if m is None or not ({'languages_finished', 'languages_stopped_early'} & set(m)):
            reasons.append('codeberg: language checks not finished (S2)')
    if 'zenodo' in args.platforms:
        m = meta(SAMPLING_DIR / 'zenodo' / 'frame.sqlite')
        missing = [s for s in ('Z1', 'Z2', 'Z3') if m is None or f'enumerate_{s}_finished' not in m]
        if missing:
            reasons.append(f'zenodo: enumeration not finished for {", ".join(missing)} (S4)')
        for stratum in ('Z2', 'Z3'):
            if m is not None and not any(k.startswith(f'inspection_{stratum}_') and k.endswith('_done') for k in m):
                reasons.append(f'zenodo: {stratum} archive inspection not finished (S4)')
    if 'github' in args.platforms:
        db = open_ro(SAMPLING_DIR / 'github' / f'{args.github_name}.sqlite')
        hits = db.execute('SELECT COUNT(*) FROM languages WHERE jupyter_bytes > 0').fetchone()[0] if db else 0
        if hits < args.n + args.reserve:
            reasons.append(f'github: {hits} screened-in repositories, {args.n + args.reserve} needed (S3 extend)')
    return reasons


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--n', type=int, default=500, help='primary resources per platform')
    parser.add_argument('--reserve', type=int, default=150, help='reserve resources per platform (Codeberg, GitHub)')
    parser.add_argument('--reserve-per-stratum', type=int, default=60, help='Zenodo reserve per stratum')
    parser.add_argument('--min-stratum', type=int, default=50, help='Zenodo minimum per stratum')
    parser.add_argument('--seed', type=int, default=20261005)
    parser.add_argument('--platforms', nargs='+', default=['codeberg', 'github', 'zenodo'])
    parser.add_argument('--github-name', default='ids', help='state name of github_id_sample.py')
    parser.add_argument('--no-check-versions', dest='check_versions', action='store_false')
    parser.add_argument('--out', type=Path, default=None)
    parser.add_argument('--allow-incomplete', action='store_true', help='draw although a frame is not finished')
    parser.add_argument('--force', action='store_true', help='replace an existing draw in the output folder')
    args = parser.parse_args()
    reasons = incomplete_inputs(args)
    if reasons and not args.allow_incomplete:
        sys.exit('[S5] frames not finished:\n  ' + '\n  '.join(reasons) + '\n(--allow-incomplete to draw anyway)')

    out = args.out or SAMPLING_DIR / 'draw' / date.today().isoformat()
    if (out / 'cohort-primary.csv').exists() and not args.force:
        sys.exit(f'[S5] {out} already holds a draw (runs may use it); --force to replace it, or --out elsewhere')
    inputs = {name: SAMPLING_DIR / path for name, path in (
        ('codeberg', 'codeberg/census.sqlite'), ('github', f'github/{args.github_name}.sqlite'),
        ('zenodo', 'zenodo/frame.sqlite'))}
    info = dict(drawn_at=now(), seed=args.seed, cutoff=CUTOFF, n=args.n, reserve=args.reserve,
                incomplete_inputs=reasons,
                inputs={k: dict(path=str(p.relative_to(SAMPLING_DIR)), sha256=sha256_file(p) if p.exists() else None)
                        for k, p in inputs.items() if k in args.platforms})
    rows = []
    for platform in args.platforms:
        drawn = dict(codeberg=draw_codeberg, github=draw_github, zenodo=draw_zenodo)[platform](args, info)
        if not drawn:
            print(f'[S5] {platform}: no frame found under {SAMPLING_DIR} - skipped')
        rows += drawn

    primary = [r for r in rows if r['role'] == 'primary']
    reserve = [r for r in rows if r['role'] == 'reserve']
    # processing order across platforms: one seeded shuffle (D9), so array tasks mix platforms
    rng(f'{args.seed}-processing').shuffle(primary)
    rng(f'{args.seed}-reserve-processing').shuffle(reserve)
    for position, row in enumerate(primary, 1):
        row['processing_order'] = position
    for position, row in enumerate(reserve, 1):
        row['processing_order'] = position
    fields = FIELDS + ['processing_order']
    info['draw_sha256'] = write_csv(out / 'draw.csv', primary + reserve + [r for r in rows if r['role'] == 'ineligible'], fields)
    cohort = ['category', 'platform', 'url']
    for row in rows:
        row['category'] = 'unclassified'           # purpose is classified after the run (section 8)
    info['cohort_primary_sha256'] = write_csv(out / 'cohort-primary.csv', primary, cohort)
    info['cohort_reserve_sha256'] = write_csv(out / 'cohort-reserve.csv', reserve, cohort)
    info['totals'] = dict(primary=dict(Counter(r['platform'] for r in primary)),
                          reserve=dict(Counter(r['platform'] for r in reserve)),
                          ineligible=dict(Counter(r['platform'] for r in rows if r['role'] == 'ineligible')))
    write_json(out / 'summary.json', info)
    print(json.dumps(info, indent=2, default=str))
    print(f'\n[S5] cohort: {out / "cohort-primary.csv"} ({len(primary)} resources); reserve: {len(reserve)}')


if __name__ == '__main__':
    main()
