#!/usr/bin/env python3
"""Prepare a complete re-run of a drawn cohort as a new study round (same sample, fresh runs).

Copies the draw (draw.csv, cohort-primary.csv, cohort-reserve.csv, summary.json and their .sha256) into
<draw>-<suffix>/, and writes there:
  cohort-all.csv   every resource of the cohort so far: the primary sample plus every reserve resource used by a
                   top-up of the earlier round (category,platform,url; the first pass of the new round)
  topup-log.csv    only the replacements of the earlier round (action = replaced), so that the new round knows
                   which reserve resources belong to the cohort and never uses them twice; re-run counts and
                   'reserve exhausted' entries start afresh
  RERUN.md         what was copied, from where, why, and the SHA-256 of the inputs
The new round's top-ups (study.py with NBF_STUDY / NBF_DRAW_DIR) append to this topup-log.csv.

Usage: python3 pipeline/prepare_rerun.py --draw-dir data/sampling/draw/2026-10-09 --suffix r2 --reason "..."
Refuses to overwrite an existing target folder.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

COPY = ['draw.csv', 'cohort-primary.csv', 'cohort-reserve.csv', 'summary.json',
        'draw.csv.sha256', 'cohort-primary.csv.sha256', 'cohort-reserve.csv.sha256']


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_csv(path):
    with open(path, newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def prepare(draw_dir: Path, suffix: str, reason: str) -> Path:
    target = draw_dir.with_name(f'{draw_dir.name}-{suffix}')
    if target.exists():
        sys.exit(f'{target} exists already; nothing done')
    log_path = draw_dir / 'topup-log.csv'
    log = read_csv(log_path) if log_path.exists() else []
    replaced = [r for r in log if r['action'] == 'replaced']
    used = {r['replaced_by'] for r in replaced}
    draw = read_csv(draw_dir / 'draw.csv')
    cohort = [r for r in draw if r['role'] == 'primary' or (r['role'] == 'reserve' and r['url'] in used)]
    missing = used - {r['url'] for r in cohort}
    if missing:
        sys.exit(f'replacements not in draw.csv: {sorted(missing)[:5]}')

    target.mkdir(parents=True)
    for name in COPY:
        if (draw_dir / name).exists():
            shutil.copy2(draw_dir / name, target / name)
    with open(target / 'cohort-all.csv', 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['category', 'platform', 'url'])
        writer.writeheader()
        writer.writerows(dict(category='unclassified', platform=r['platform'], url=r['url'])
                         for r in sorted(cohort, key=lambda r: int(r['processing_order'] or 0)))
    if log:
        with open(target / 'topup-log.csv', 'w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(log[0]))
            writer.writeheader()
            writer.writerows(replaced)
    counts = {}
    for r in cohort:
        counts[r['platform']] = counts.get(r['platform'], 0) + 1
    inputs = '\n'.join(f'- `{p.name}` sha256 `{sha256(p)}`' for p in [draw_dir / n for n in COPY + ['topup-log.csv']]
                       if p.exists())
    (target / 'RERUN.md').write_text(f"""# Re-run of the cohort of `{draw_dir.name}`

Prepared {datetime.now(timezone.utc).isoformat(timespec='seconds')} by `pipeline/prepare_rerun.py`.

**Reason:** {reason}

- The sample is unchanged: `draw.csv`, `cohort-primary.csv`, `cohort-reserve.csv`, `summary.json` are copies.
- `cohort-all.csv`: {len(cohort)} resources ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}) = primary + the
  {len(used)} reserve resources used by the earlier round's top-ups. Every one is run again from scratch.
- `topup-log.csv`: the earlier round's {len(replaced)} replacements only. The re-runs and 'reserve exhausted' entries
  are not carried over, so pipeline errors get their two re-runs (plan P4) in this round.

Inputs (from `{draw_dir.name}/`):
{inputs}
""")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--draw-dir', type=Path, required=True)
    parser.add_argument('--suffix', default='r2')
    parser.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    target = prepare(args.draw_dir.resolve(), args.suffix, args.reason)
    print(f'prepared {target}')


if __name__ == '__main__':
    main()
