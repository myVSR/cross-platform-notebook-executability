# Sampling scripts

Implements `notes/sampling-design.md` (decisions D1–D9, approved 2026-10-05). Every step has a
script, writes its results to `data/sampling/<step>/`, and logs every HTTP request as one JSON line
(time, URL, status, bytes, rate-limit headers), so each count and each frame can be traced back to the
requests that produced it. Frames and summaries carry a SHA-256 file. Random draws use a recorded seed
(default `20261005`). All crawl steps are resumable (SQLite state) and can be stopped and restarted.

Shared settings (`common.py`): cutoff `2026-07-01` (platform creation date, D3), optional tokens
`GITHUB_TOKEN`, `CODEBERG_TOKEN`, `ZENODO_TOKEN` from the environment or `.env` (git-ignored),
`NBF_SAMPLING_DIR` to redirect all output (used for tests).

| Step | Script | What it does | Results |
|---|---|---|---|
| S0 | `probe_frames.py` | frame-size probes quoted in the design (Codeberg total, GitHub search totals, Zenodo strata counts with/without all versions and cutoff, ZIP/tar container listing test) | `data/sampling/probes/<date>/probes.csv`, `requests.jsonl` |
| S1 | `pilot_power.py` | pilot statistics from the thesis cohort (`analysis/thesis/results/notebook-cohort.csv`, exported from the frozen DB; SHA-256 recorded), ICC, sample sizes at α .05 and .05/3, detectable difference at n = 400, draw size by attrition | `data/sampling/pilot/pilot-power.json`, `pilot-power.csv` |
| S2 | `codeberg_census.py` (`run_codeberg_census.sh`) | census: all public repositories (two listing passes), D2 filters, `/languages` for every candidate in seeded random order (stoppable: the checked part is a random sample; `NBF_CODEBERG_STOP_AFTER`), tree check of 500 screened-out repositories, frame export | `data/sampling/codeberg/frame-codeberg-<date>.csv.gz`, `summary.json`, `run.log` |
| S3 | `github_id_sample.py` | max ID before the cutoff (bisection), exact random-ID draws (`GET /repositories/{id}`, 404 = no public repository, never the neighbour), D2, language screening (GraphQL batches), tree check, report with cost extrapolation | `data/sampling/github/<name>-summary.json`, `<name>-draws.csv.gz` |
| S4 | `zenodo_frame.py` (`run_zenodo_frame.sh`) | strata Z1/Z2/Z3 enumerated with all versions before the cutoff in creation-time windows (≤ 10,000 hits each, stored rows checked against totals), latest version per concept, ZIP container listing of 20,000 random Z2 concepts, streamed tar reading of 1,000 random Z3 concepts | `data/sampling/zenodo/frame-zenodo-Z?-<date>.csv.gz`, `summary.json`, `run.log` |
| S5 | `draw_sample.py` | draws 500 per platform + reserve: Codeberg stratified by creation year; GitHub = first screened-in repositories of the seeded ID sequence (self-weighting); Zenodo two-phase stratified (Z1/Z2/Z3, allocation ∝ estimated eligible, ≥ 50 per stratum, weights 1/π) with a version check per drawn record; random processing order across platforms; refuses while a frame is unfinished | `data/sampling/draw/<date>/cohort-primary.csv`, `cohort-reserve.csv`, `draw.csv`, `summary.json` |

Not in git (large; deposited with the replication package): the SQLite state files and the request
logs of S2–S4 (`data/sampling/{codeberg,github,zenodo}/*.sqlite`, `*requests.jsonl`).

## Commands

**Run S2–S4 on the cluster, not on the laptop** (long crawls; see `hpc/README.md`, section Sampling):
`cd hpc && bash submit_sampling.sh` (after `bash hpc/sync_from_repo.sh` in the repository). Single steps, e.g. for tests:

```bash
python3 analysis/sampling/probe_frames.py                     # S0, ~2 minutes
python3 analysis/sampling/pilot_power.py                      # S1, seconds
bash analysis/sampling/run_codeberg_census.sh                 # S2, ~2 days (the cluster)
bash analysis/sampling/run_zenodo_frame.sh                    # S4, ~1 day with ZENODO_TOKEN (the cluster)
bash analysis/sampling/run_github_pilot.sh                    # S3 pilot + extension, needs a valid GITHUB_TOKEN
python3 analysis/sampling/draw_sample.py                      # S5, after S2-S4
python3 analysis/sampling/codeberg_census.py status           # progress
python3 analysis/sampling/zenodo_frame.py status
```
Tokens: Codeberg needs none (2,000 requests per 10 minutes either way). The Zenodo token raises the page size
from 25 to 100 but not the rate (30 searches per minute). GitHub needs a token for S3.

`data/sampling/_laptop-partial-2026-10-05/`: partial S2/S4 state from the stopped laptop runs (not used).

## Results so far

See the dated entries in `notes/chat-log.md` and the results files above.
