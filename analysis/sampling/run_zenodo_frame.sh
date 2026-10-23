#!/bin/bash
# Runs the Zenodo frame (step S4) unattended; resumable: run it again after a stop.
# With ZENODO_TOKEN (environment or .env) pages hold 100 instead of 25 hits (about 4x faster);
# a restart after adding the token continues where it stopped.
# Log: data/sampling/zenodo/run.log   Stop: kill "$(cat data/sampling/zenodo/run.pid)"
set -euo pipefail
cd "$(dirname "$0")/../.."
S=analysis/sampling/zenodo_frame.py
mkdir -p data/sampling/zenodo
echo $$ > data/sampling/zenodo/run.pid
python3 $S enumerate --stratum Z1
python3 $S enumerate --stratum Z3
python3 $S enumerate --stratum Z2
python3 $S export                                       # frames before inspection
python3 $S inspect-tar --n 1000 --seed 20261005 --max-mb 200
python3 $S inspect-zip --n 20000 --seed 20261005 --target-pool 460   # stops early once the Z2 pool suffices
python3 $S export                                       # frames + inspection results
rm -f data/sampling/zenodo/run.pid
