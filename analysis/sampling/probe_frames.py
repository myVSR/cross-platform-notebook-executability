#!/usr/bin/env python3
"""Step S0 — frame-size probes for the sampling design (notes/sampling-design.md, section 5).

Reproduces every count quoted in the design: Codeberg repository total and the languages
endpoint, GitHub search totals for "Jupyter Notebook", Zenodo record counts per stratum
(Z1 direct .ipynb, Z2 ZIP without .ipynb, Z3 tar only) with and without all versions and
before the cutoff, and whether Zenodo lists the contents of tar archives.

Output: data/sampling/probes/<date>/probes.csv (+ .sha256), requests.jsonl
Usage:  python3 analysis/sampling/probe_frames.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CUTOFF, SAMPLING_DIR, Http, now, token, write_csv  # noqa: E402

ZENODO = 'https://zenodo.org/api/records'
Z1 = 'files.entries.key:*.ipynb'
Z2 = 'files.entries.key:*.zip AND NOT files.entries.key:*.ipynb'
Z3 = ('files.entries.key:(*.tar OR *.tar.gz OR *.tgz) AND NOT files.entries.key:*.zip '
      'AND NOT files.entries.key:*.ipynb')
BEFORE = f' AND created:[* TO {CUTOFF}}}'


def main():
    out = SAMPLING_DIR / 'probes' / date.today().isoformat()
    http = Http(out / 'requests.jsonl', min_interval=2.1)      # Zenodo guests: 30 searches per minute
    rows = []

    def add(platform, name, query, value, note=''):
        rows.append(dict(platform=platform, probe=name, query=query, value=value, note=note, probed_at=now()))
        print(f'{platform:9} {name:42} {value}')

    # Codeberg ---------------------------------------------------------------------------
    response = http.get('https://codeberg.org/api/v1/repos/search', params=dict(limit=1, sort='id', order='asc'))
    add('codeberg', 'public repositories (X-Total-Count)', '/repos/search', response.headers.get('X-Total-Count'))
    settings = http.get('https://codeberg.org/api/v1/settings/api').json()
    add('codeberg', 'max items per page', '/settings/api', settings.get('max_response_items'))
    languages = http.get('https://codeberg.org/api/v1/repos/aiocoap/aiocoap/languages').json()
    add('codeberg', 'languages example aiocoap/aiocoap: Jupyter bytes', '/repos/aiocoap/aiocoap/languages',
        languages.get('Jupyter Notebook'))

    # GitHub (search totals; unauthenticated search allows 10 requests per minute) ------
    github_headers = {'Accept': 'application/vnd.github+json'}
    if token('GITHUB_TOKEN'):
        github_headers['Authorization'] = f'Bearer {token("GITHUB_TOKEN")}'
    for name, query in [('primary language Jupyter Notebook (search total)', 'language:"Jupyter Notebook"'),
                        ('same, created 2020-01-01..2020-01-07', 'language:"Jupyter Notebook" created:2020-01-01..2020-01-07'),
                        (f'same, created before {CUTOFF}', f'language:"Jupyter Notebook" created:<{CUTOFF}')]:
        response = http.get('https://api.github.com/search/repositories', params=dict(q=query, per_page=1),
                            headers=github_headers, ok=(200, 401))
        if response.status_code == 401:                     # rejected token: the search works without one
            print('warning: GITHUB_TOKEN rejected (401); searching without it', file=sys.stderr)
            github_headers.pop('Authorization', None)
            response = http.get('https://api.github.com/search/repositories', params=dict(q=query, per_page=1),
                                headers=github_headers)
        data = response.json()
        add('github', name, query, data.get('total_count'),
            'search returns at most 1,000 results per query; matches the primary language only')

    # Zenodo --------------------------------------------------------------------------
    def zenodo_total(query, all_versions=False):
        params = dict(q=query, size=1)
        if all_versions:
            params['all_versions'] = 'true'
        return http.get(ZENODO, params=params).json()['hits']['total']

    add('zenodo', 'all records (latest versions)', '', zenodo_total(''))
    for stratum, query in (('Z1', Z1), ('Z2', Z2), ('Z3', Z3)):
        add('zenodo', f'{stratum} latest versions', query, zenodo_total(query))
        add('zenodo', f'{stratum} latest versions, created before cutoff', query + BEFORE, zenodo_total(query + BEFORE))
        add('zenodo', f'{stratum} all versions', query, zenodo_total(query, True))
        add('zenodo', f'{stratum} all versions, created before cutoff', query + BEFORE, zenodo_total(query + BEFORE, True))
    add('zenodo', 'Z1 software', Z1 + ' AND resource_type.type:software',
        zenodo_total(Z1 + ' AND resource_type.type:software'))
    add('zenodo', 'software (all)', 'resource_type.type:software', zenodo_total('resource_type.type:software'))
    add('zenodo', 'software with ZIP', 'resource_type.type:software AND files.entries.key:*.zip',
        zenodo_total('resource_type.type:software AND files.entries.key:*.zip'))
    github_linked = 'resource_type.type:software AND metadata.related_identifiers.identifier:*github.com*'
    add('zenodo', 'software linked to github.com', github_linked, zenodo_total(github_linked))
    add('zenodo', 'Z2 software', Z2 + ' AND resource_type.type:software',
        zenodo_total(Z2 + ' AND resource_type.type:software'))
    add('zenodo', 'Z2 software linked to github.com', Z2 + ' AND ' + github_linked, zenodo_total(Z2 + ' AND ' + github_linked))

    # Does Zenodo list the contents of archives? (ZIP: yes; tar: test 3 records) -----------
    for label, query, suffixes in (('zip', Z2, ('.zip',)), ('tar', Z3, ('.tar.gz', '.tgz', '.tar'))):
        hits = http.get(ZENODO, params=dict(q=query, size=3, sort='oldest')).json()['hits']['hits']
        for hit in hits:
            key = next(f['key'] for f in hit['files'] if f['key'].lower().endswith(suffixes))
            response = http.get(f'{ZENODO}/{hit["id"]}/files/{key}/container', ok=(200, 400, 404, 500))
            entries = len(response.json().get('entries', [])) if response.status_code == 200 else ''
            add('zenodo', f'container listing {label}: record {hit["id"]}', key,
                f'HTTP {response.status_code}', f'{entries} entries' if entries != '' else '')

    target = out / 'probes.csv'
    digest = write_csv(target, rows, ['platform', 'probe', 'query', 'value', 'note', 'probed_at'])
    print(f'\n{len(rows)} probes, {http.count} requests -> {target} (sha256 {digest[:12]})')


if __name__ == '__main__':
    main()
