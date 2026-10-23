#!/bin/bash
# Runs the GitHub exact-ID pilot (step S3: first 1,000 draws, report) and then extends the same seeded
# sequence until NBF_GITHUB_SCREENED (default 650) repositories pass D2 and the screen; unattended and
# resumable. NBF_GITHUB_EXTEND=0 runs the pilot only (check its report before the long part).
# Needs a valid GITHUB_TOKEN (environment or .env); stops at once if GitHub rejects it.
# Log: data/sampling/github/run.log
set -euo pipefail
cd "$(dirname "$0")/../.."
S=analysis/sampling/github_id_sample.py
mkdir -p data/sampling/github
python3 - <<'PY'
import sys; sys.path.insert(0, 'analysis/sampling')
import requests
from common import token
value = token('GITHUB_TOKEN')
if not value:
    sys.exit('[S3] no GITHUB_TOKEN in the environment or .env')
r = requests.get('https://api.github.com/rate_limit', headers={'Authorization': f'Bearer {value}'}, timeout=30)
if r.status_code != 200:
    sys.exit(f'[S3] GitHub rejects GITHUB_TOKEN (HTTP {r.status_code}); create a new token')
core = r.json()['resources']['core']
print(f"[S3] token ok: {core['remaining']}/{core['limit']} requests left this hour")
PY
python3 $S maxid
python3 $S draw --n "${NBF_GITHUB_DRAWS:-1000}" --seed 20261005
python3 $S screen
python3 $S treecheck --n 200 --seed 20261005
python3 $S report
# the sample: continue the same seeded sequence until enough screened-in repositories exist (S5 input)
if [ "${NBF_GITHUB_EXTEND:-1}" = 1 ]; then
    python3 $S extend --until-screened "${NBF_GITHUB_SCREENED:-650}"
    python3 $S report
fi
