#!/usr/bin/env python3
"""Write the re-run cohort as a CSV for pipeline/run_full_sample.sh (columns category, platform, url).

Usage: make_cohort.py {318,326} OUTPUT.csv
  318  the thesis cohort: base + extension list minus the 8 exclusions
  326  corpus option A: the thesis cohort plus the 8 excluded GitHub repositories
URLs are deduplicated after normalisation (case, trailing slash and .git, Zenodo /record/).
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize(url):
    return url.strip().rstrip('/').removesuffix('.git').lower().replace('/record/', '/records/')


def read(name):
    with (ROOT / 'data' / name).open(encoding='utf-8-sig') as stream:
        return list(csv.DictReader(stream))


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('318', '326'):
        sys.exit(__doc__)
    excluded = {normalize(r['url']) for r in read('evaluation-sample-exclusions.csv')}
    rows, seen = [], set()
    for row in read('evaluation-sample-dataset.csv') + read('evaluation-sample-extension.csv') + \
            (read('evaluation-sample-exclusions.csv') if sys.argv[1] == '326' else []):
        key = normalize(row['url'])
        if key in seen or (sys.argv[1] == '318' and key in excluded):
            continue
        seen.add(key)
        rows.append(dict(category=row['category'].strip(), platform=row['platform'].strip().lower(),
                         url=row['url'].strip().rstrip('/')))
    out = Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['category', 'platform', 'url'])
        writer.writeheader()
        writer.writerows(rows)
    by_platform = {}
    for row in rows:
        by_platform[row['platform']] = by_platform.get(row['platform'], 0) + 1
    print(f'{out}: {len(rows)} resources {by_platform}')


if __name__ == '__main__':
    main()
