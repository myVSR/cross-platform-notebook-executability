#!/bin/bash
# Tests for analysis/sampling/extend_zenodo_reserve.py on a copy of the round-2 draw (no requests: --no-check-versions).
#  1. the extension continues the seeded cells: the records drawn so far are their prefix (else it refuses);
#  2. it appends reserve rows after every earlier reserve, never reuses a drawn record, and records itself in
#     summary.json;
#  3. a draw.csv with a record that is not next in its seeded cell is refused;
#  4. a second extension continues after the first.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/data/sampling/draw/2026-10-09-r2"
[ -f "$REPO/data/sampling/zenodo/frame.sqlite" ] || { echo "SKIP: no Zenodo frame"; exit 0; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
for f in draw.csv cohort-primary.csv cohort-reserve.csv summary.json topup-log.csv; do cp "$SRC/$f" "$TMP/"; done

python3 "$REPO/analysis/sampling/extend_zenodo_reserve.py" --draw-dir "$TMP" --extra Z1=10 Z2=all Z3=10 \
    --no-check-versions > "$TMP/out.json"
python3 - "$TMP" "$SRC" <<'PY'
import csv, json, sys
tmp, src = sys.argv[1:]
old = list(csv.DictReader(open(f'{src}/draw.csv')))
new = list(csv.DictReader(open(f'{tmp}/draw.csv')))
added = new[len(old):]
assert new[:len(old)] == old, 'earlier rows changed'
assert len(added) == 10 + 75 + 10, len(added)
assert not {r['resource_id'] for r in added} & {r['resource_id'] for r in old}, 'a drawn record reused'
first = min(int(r['processing_order']) for r in added)
assert first > max(int(r['processing_order']) for r in old if r['role'] == 'reserve' and r['processing_order'])
assert all(r['role'] == 'reserve' and r['platform'] == 'zenodo' for r in added)
reserve = list(csv.DictReader(open(f'{tmp}/cohort-reserve.csv')))
assert [r['url'] for r in reserve[-len(added):]] == [r['url'] for r in sorted(added, key=lambda r: int(r['processing_order']))] or \
       {r['url'] for r in reserve[-len(added):]} == {r['url'] for r in added}
summary = json.load(open(f'{tmp}/summary.json'))
assert summary['zenodo_extensions'][0]['strata']['Z2']['available'] == 75
print('ok 1-2: extension appended (95 rows), prefix check passed')
PY

# 4. a second extension continues after the first
python3 "$REPO/analysis/sampling/extend_zenodo_reserve.py" --draw-dir "$TMP" --extra Z1=5 --no-check-versions > /dev/null
python3 -c "
import json; s = json.load(open('$TMP/summary.json')); e = s['zenodo_extensions']
assert len(e) == 2 and e[1]['strata']['Z1']['drawn_before'] == e[0]['strata']['Z1']['drawn_before'] + 10, e[1]
print('ok 4: second extension continues after the first')"

# 3. tampered draw: replace a drawn Z1 record by one that is not next in its cell
for f in draw.csv cohort-primary.csv cohort-reserve.csv summary.json topup-log.csv; do cp "$SRC/$f" "$TMP/"; done
python3 - "$TMP" "$REPO" <<'PY'
import csv, sys
tmp, repo = sys.argv[1:]
sys.path.insert(0, f'{repo}/analysis/sampling')
import draw_sample as ds
from extend_zenodo_reserve import year_cells
rows = list(csv.DictReader(open(f'{tmp}/draw.csv')))
used = {r['resource_id'] for r in rows if r['platform'] == 'zenodo' and r['stratum'] == 'Z1'}
pool = ds.zenodo_pools(ds.open_ro(f'{repo}/data/sampling/zenodo/frame.sqlite'))['Z1']['rows']
cells = year_cells([dict(r, resource_id=r['id'], year=ds.year(r['created'])) for r in pool], '20261005-zenodo-Z1')
cell = cells[2026]
last = [r for r in cell if str(r['resource_id']) not in used][-1]           # the last of its cell, not the next
target = next(r for r in rows if r['platform'] == 'zenodo' and r['stratum'] == 'Z1' and r['year'] == '2026')
target['resource_id'] = str(last['resource_id'])
with open(f'{tmp}/draw.csv', 'w', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
PY
if python3 "$REPO/analysis/sampling/extend_zenodo_reserve.py" --draw-dir "$TMP" --extra Z1=5 --no-check-versions \
        > "$TMP/out3.txt" 2>&1; then
    echo "FAIL 3: a tampered draw was extended"; exit 1
fi
grep -q "not the first of the seeded cell" "$TMP/out3.txt" && echo "ok 3: tampered draw refused"
echo "ALL PASSED"
