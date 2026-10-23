#!/bin/bash
# Runs the whole Codeberg census (step S2) unattended; resumable: run it again after a stop.
# NBF_CODEBERG_STOP_AFTER=N stops the (random-order) language checks once N repositories with
# notebooks are found; default 0 = full census.
# Log: data/sampling/codeberg/run.log   Stop: kill "$(cat data/sampling/codeberg/run.pid)"
set -euo pipefail
cd "$(dirname "$0")/../.."
S=analysis/sampling/codeberg_census.py
mkdir -p data/sampling/codeberg
echo $$ > data/sampling/codeberg/run.pid
python3 $S enumerate --pass 1 --interval 0.3     # Codeberg: 2,000 requests per 10 minutes
python3 $S languages --interval 0.3 --seed 20261005 --stop-after "${NBF_CODEBERG_STOP_AFTER:-0}"
python3 $S enumerate --pass 2 --interval 0.3     # closes gaps from repositories deleted during pass 1
python3 $S languages --interval 0.3 --seed 20261005 --stop-after "${NBF_CODEBERG_STOP_AFTER:-0}"               # checks repositories found only in pass 2
python3 $S treecheck --n 500 --seed 20261005
python3 $S export
rm -f data/sampling/codeberg/run.pid
