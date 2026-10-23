#!/usr/bin/env python3
"""Probe: Zenodo's resource type "Computational notebook" (software / computationalnotebook) vs the S4 frame.

Zenodo offers the subtype `software-computationalnotebook`
(https://zenodo.org/search?f=resource_type%3Asoftware%2Binner%3Asoftware-computationalnotebook).
Question: can it replace or complement the file-based strata Z1-Z3 (notes/sampling-design.md, D4)?

Counts only (size=1 searches, ~25 requests), all versions created before the cutoff, as in zenodo_frame.py:
  - typed records per stratum Z1/Z2/Z3 and outside them, per creation year
  - what the typed records outside Z1-Z3 contain (other notebook formats, other archives, restricted)
  - how much of Z1 (records with *.ipynb) is typed as a computational notebook, software or dataset
  - all records outside Z1-Z3 with archive formats the frame does not open (rar, 7z, xz, bz2, zst)
Output: data/sampling/zenodo/subtype-probe.json (+ request log). Usage:
  python3 analysis/sampling/zenodo_subtype_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CUTOFF, SAMPLING_DIR, Http, now, write_json  # noqa: E402
from zenodo_frame import API, STRATA  # noqa: E402

OUT = SAMPLING_DIR / 'zenodo'
TYPED = 'metadata.resource_type.id:"software-computationalnotebook"'
BEFORE = f'created:[* TO {CUTOFF[:10]}}}'
OUTSIDE = 'NOT files.entries.key:(*.ipynb OR *.zip OR *.tar OR *.tar.gz OR *.tgz)'
OTHER_ARCHIVES = 'files.entries.key:(*.rar OR *.7z OR *.tar.xz OR *.txz OR *.tar.bz2 OR *.tbz2 OR *.tar.zst)'

QUERIES = {
    'typed_latest_versions': (TYPED, False),
    'typed_before_cutoff': (f'{TYPED} AND {BEFORE}', True),
    **{f'typed_{s}': (f'{TYPED} AND {BEFORE} AND {q}', True) for s, q in STRATA.items()},
    'typed_outside_strata': (f'{TYPED} AND {BEFORE} AND {OUTSIDE}', True),
    'typed_outside_open_access': (f'{TYPED} AND {BEFORE} AND {OUTSIDE} AND access_right:open', True),
    'typed_outside_mathematica_nb': (f'{TYPED} AND {BEFORE} AND {OUTSIDE} AND files.entries.key:*.nb', True),
    'typed_outside_rmd_qmd': (f'{TYPED} AND {BEFORE} AND {OUTSIDE} AND files.entries.key:(*.Rmd OR *.rmd OR *.qmd)', True),
    'typed_outside_html': (f'{TYPED} AND {BEFORE} AND {OUTSIDE} AND files.entries.key:*.html', True),
    'typed_outside_other_archives': (f'{TYPED} AND {BEFORE} AND {OUTSIDE} AND {OTHER_ARCHIVES}', True),
    'Z1_all': (f'{BEFORE} AND {STRATA["Z1"]}', True),
    'Z1_typed_software': (f'{BEFORE} AND {STRATA["Z1"]} AND resource_type.type:software', True),
    'Z1_typed_dataset': (f'{BEFORE} AND {STRATA["Z1"]} AND resource_type.type:dataset', True),
    'all_outside_strata_other_archives': (f'{BEFORE} AND {OUTSIDE} AND {OTHER_ARCHIVES}', True),
    **{f'typed_created_{y}': (f'{TYPED} AND created:[{y}-01-01 TO {y + 1}-01-01}}', True) for y in range(2019, 2027)},
}


def main():
    http = Http(OUT / 'subtype-probe-requests.jsonl', min_interval=2.1)
    counts = {}
    for name, (query, all_versions) in QUERIES.items():
        hits = http.get(API, params=dict(q=query, all_versions=str(all_versions).lower(), size=1)).json()['hits']
        counts[name] = hits['total']
        print(f'{name:38s} {counts[name]:>8,}')
    typed = counts['typed_before_cutoff']
    summary = dict(
        typed_share_by_stratum={k: round(counts[f'typed_{k}'] / typed, 3) for k in STRATA} | {
            'outside': round(counts['typed_outside_strata'] / typed, 3)},
        Z1_share_typed_notebook=round(counts['typed_Z1'] / counts['Z1_all'], 3),
        Z1_share_typed_software=round(counts['Z1_typed_software'] / counts['Z1_all'], 3),
        Z1_share_typed_dataset=round(counts['Z1_typed_dataset'] / counts['Z1_all'], 3),
    )
    write_json(OUT / 'subtype-probe.json', dict(probed_at=now(), cutoff=CUTOFF, api=API, queries={k: v[0] for k, v in QUERIES.items()},
                                               all_versions={k: v[1] for k, v in QUERIES.items()}, counts=counts, summary=summary))
    print(summary)


if __name__ == '__main__':
    main()
