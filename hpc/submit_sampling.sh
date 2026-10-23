#!/bin/bash
###############################################################################
# Submits the sampling steps S0-S4 as independent SLURM jobs (from the hpc/ folder):
#   bash submit_sampling.sh                 # probe, codeberg, zenodo, github
#   bash submit_sampling.sh codeberg zenodo # only these steps
#   bash submit_sampling.sh draw            # S5, once the other jobs have finished (it refuses otherwise)
# Checks the tokens in .env first (a few requests from the login node). The GitHub step is
# skipped if GITHUB_TOKEN is missing or rejected. Progress:
#   squeue -u $USER;  tail results/nbf-sampling-<step>-<job>.out
#   python3 analysis/sampling/codeberg_census.py status   (or zenodo_frame.py status)
###############################################################################
set -uo pipefail
BUNDLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BUNDLE" || exit 1
source "$HOME/.notebookfair/env.sh" || { echo "run pipeline/hpc_setup_cluster.sh first"; exit 1; }
mkdir -p results
steps=("$@")
[ ${#steps[@]} -gt 0 ] || steps=(probe codeberg zenodo github)

# token check: prints ok/missing/rejected per token, never the token itself
mapfile -t checks < <(python3 - <<'PY'
import sys; sys.path.insert(0, 'analysis/sampling')
import requests
from common import token
def check(name, url, header):
    value = token(name)
    if not value:
        return 'missing'
    try:
        r = requests.get(url, headers={'Authorization': header.format(value)}, timeout=30)
    except requests.RequestException as error:
        return f'unreachable ({type(error).__name__})'
    return 'ok' if r.status_code == 200 else f'rejected (HTTP {r.status_code})'
print('GITHUB_TOKEN', check('GITHUB_TOKEN', 'https://api.github.com/rate_limit', 'Bearer {}'))
print('ZENODO_TOKEN', check('ZENODO_TOKEN', 'https://zenodo.org/api/records?q=ipynb&size=100', 'Bearer {}'))
PY
)
printf '%s\n' "${checks[@]}"
github_ok=0
[[ " ${checks[*]} " == *"GITHUB_TOKEN ok"* ]] && github_ok=1

for step in "${steps[@]}"; do
    if [ "$step" = github ] && [ "$github_ok" -eq 0 ]; then
        echo "[SUBMIT] github skipped: GITHUB_TOKEN is not usable (fix .env, then: bash submit_sampling.sh github)"
        continue
    fi
    case "$step" in
        probe)    time=00:30:00 ;;
        draw)     time=04:00:00 ;;
        *)        time=3-00:00:00 ;;
    esac
    sbatch --job-name="nbf-sampling-$step" --time="$time" sampling.sbatch "$step"
done
