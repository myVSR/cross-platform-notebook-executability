#!/usr/bin/env python3
"""Extend the Zenodo reserve of a draw (notes/sampling-design.md, section 6.4: "processing stops when 400 per
platform are analysable. Any further resources needed are drawn from the same frame with the same seed sequence").

Round 2 exhausted the Zenodo reserve at 355 analysable records. This script continues each stratum's seeded draw
where draw_sample.py stopped, so the extension is the next part of the same random sequence:
  - the pool rows and the per-year cells are rebuilt exactly as in draw_sample.py (same frame, same seed, same
    sub-seeds per stratum and year), and the script checks that the records already in draw.csv are the first
    ones of every cell (it refuses otherwise);
  - the extension takes the next records of the cells, allocated over the years in proportion to what is left
    (largest remainder), in a seeded random order of their own;
  - every record gets the same version check as the original draw (newest version of its concept before the
    cutoff); a record that fails it is kept as 'ineligible', as in draw_sample.py.
The new records are appended to draw.csv (role reserve, processing order after every earlier reserve) and to
cohort-reserve.csv, so pipeline/topup_from_reserve.py uses them after the original reserve, with its usual rule
(same stratum and year, then same stratum, then any stratum). Phase 1 (m_h, |P_h|) is unchanged; the weights
follow from the number of records drawn per stratum, as before. summary.json gets a 'zenodo_extensions' entry.

Usage:
  python3 analysis/sampling/extend_zenodo_reserve.py --draw-dir data/sampling/draw/2026-10-09-r3 \
          --extra Z1=60 Z2=all Z3=60 [--no-check-versions] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import draw_sample as ds  # noqa: E402
from common import SAMPLING_DIR, Http, now, rng, sha256_file, token, write_csv, write_json  # noqa: E402


def read_csv(path):
    with open(path, newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def parse_extra(values):
    extra = {}
    for value in values:
        stratum, _, size = value.partition('=')
        extra[stratum] = None if size == 'all' else int(size)
    return extra


def year_cells(rows, seed_key):
    """The per-year cells of draw_sample.stratified_by_year, in their seeded order."""
    by_year = defaultdict(list)
    for row in sorted(rows, key=lambda r: str(r['resource_id'])):
        by_year[row['year']].append(row)
    for year, cell in by_year.items():
        rng(f'{seed_key}-{year}').shuffle(cell)
    return by_year


def extend(draw_dir: Path, extra: dict, check_versions: bool, dry_run: bool, frame=None):
    summary = json.loads((draw_dir / 'summary.json').read_text())
    seed = int(summary['seed'])
    frame = frame or SAMPLING_DIR / 'zenodo' / 'frame.sqlite'
    expected = summary['inputs']['zenodo']['sha256']
    if sha256_file(frame) != expected:
        sys.exit(f'{frame} is not the frame of this draw (sha256 {expected}); nothing done')
    draw = read_csv(draw_dir / 'draw.csv')
    fields = list(draw[0])
    number = 1 + len(summary.get('zenodo_extensions', []))
    pools = ds.zenodo_pools(ds.open_ro(frame))
    http = None
    if check_versions and not dry_run:
        headers = {'Authorization': f'Bearer {token("ZENODO_TOKEN")}'} if token('ZENODO_TOKEN') else {}
        http = Http(draw_dir / f'zenodo-versions-extension-{number}-requests.jsonl', min_interval=2.1, headers=headers)

    new_rows, report = [], dict(extension=number, created_at=now(), seed=seed, requested=extra, strata={},
                                version_check=bool(http))
    for stratum, size in extra.items():
        pool = pools[stratum]
        rows = [dict(platform='zenodo', stratum=stratum, year=ds.year(r['created']),
                     url=f'https://zenodo.org/records/{r["id"]}', resource_id=r['id'], name=r['title'],
                     conceptrecid=r['conceptrecid'], created=r['created'], content=r['content']) for r in pool['rows']]
        if len(rows) != summary['zenodo']['strata'][stratum]['pool']:
            sys.exit(f'{stratum}: pool has {len(rows)} rows, the draw had {summary["zenodo"]["strata"][stratum]["pool"]}')
        used = {str(r['resource_id']) for r in draw if r['platform'] == 'zenodo' and r['stratum'] == stratum}
        cells = year_cells(rows, f'{seed}-zenodo-{stratum}')
        left = {}
        for year, cell in cells.items():
            k = sum(str(r['resource_id']) in used for r in cell)
            if {str(r['resource_id']) for r in cell[:k]} != {str(r['resource_id']) for r in cell if str(r['resource_id']) in used}:
                sys.exit(f'{stratum} {year}: the drawn records are not the first of the seeded cell; nothing done')
            left[year] = cell[k:]
        available = sum(len(c) for c in left.values())
        if len(used) != sum(len(c) for c in cells.values()) - available:
            sys.exit(f'{stratum}: draw.csv holds records outside the pool; nothing done')
        take = available if size is None else min(size, available)
        alloc = ds.allocate({y: len(c) for y, c in left.items()}, take)
        chosen = [r for y, c in left.items() for r in c[:alloc[y]]]
        rng(f'{seed}-zenodo-{stratum}-extension-{number}-order').shuffle(chosen)
        accepted, ineligible = [], []
        for row in chosen:
            if http:
                newest = ds.newest_before_cutoff(http, row['conceptrecid'])
                row['version_check'] = 'ok' if newest == row['resource_id'] else f'newer pre-cutoff version {newest}'
                (accepted if newest == row['resource_id'] else ineligible).append(row)
            else:
                row['version_check'] = 'not checked'
                accepted.append(row)
        for order, row in enumerate(accepted, 1):
            row.update(role='reserve', order=f'x{number}-{order}', pi='', weight='')
        for row in ineligible:
            row.update(role='ineligible', order='', pi='', weight='')
        new_rows += accepted + ineligible
        report['strata'][stratum] = dict(pool=len(rows), drawn_before=len(used), available=available, taken=take,
                                         reserve=len(accepted), ineligible_by_version_check=len(ineligible),
                                         content=dict(Counter(r['content'] for r in accepted)),
                                         by_year=dict(sorted(Counter(r['year'] for r in accepted).items())))

    reserve = [r for r in new_rows if r['role'] == 'reserve']
    rng(f'{seed}-zenodo-extension-{number}-processing').shuffle(reserve)
    start = max(int(r['processing_order']) for r in draw if r['role'] == 'reserve' and r['processing_order'])
    for position, row in enumerate(reserve, start + 1):
        row['processing_order'] = position
    report['processing_order'] = [start + 1, start + len(reserve)]
    print(json.dumps(report, indent=2, default=str))
    if dry_run:
        return report

    report['draw_sha256_before'] = sha256_file(draw_dir / 'draw.csv')
    report['draw_sha256'] = write_csv(draw_dir / 'draw.csv', draw + new_rows, fields)
    old_reserve = read_csv(draw_dir / 'cohort-reserve.csv')
    report['cohort_reserve_sha256'] = write_csv(
        draw_dir / 'cohort-reserve.csv',
        old_reserve + [dict(category='unclassified', platform='zenodo', url=r['url']) for r in reserve],
        ['category', 'platform', 'url'])
    summary.setdefault('zenodo_extensions', []).append(report)
    write_json(draw_dir / 'summary.json', summary)
    print(f'[extension {number}] {len(reserve)} reserve records appended to {draw_dir / "draw.csv"}')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--draw-dir', type=Path, required=True)
    parser.add_argument('--extra', nargs='+', default=['Z1=60', 'Z2=all', 'Z3=60'],
                        help='records per stratum (a number or "all" for the rest of the pool)')
    parser.add_argument('--no-check-versions', dest='check_versions', action='store_false')
    parser.add_argument('--dry-run', action='store_true', help='show the extension, write nothing, no requests')
    parser.add_argument('--frame', type=Path, default=None, help='Zenodo frame.sqlite (default: data/sampling/zenodo)')
    args = parser.parse_args(argv)
    extend(args.draw_dir, parse_extra(args.extra), args.check_versions, args.dry_run, args.frame)
    return 0


if __name__ == '__main__':
    sys.exit(main())
