# Sampling design for the cross-platform notebook study (draft for review)

Status: v2, decisions D1–D9 **approved 2026-10-05**. Revised after a review of v1
(`notes/chatgpt_sampling_design.md` and the chat-log entry of 2026-10-03).

Frame sizes were first probed on 2026-10-03. They were re-measured on 2026-10-05 with
`analysis/sampling/probe_frames.py` (results in `data/sampling/probes/2026-10-05/probes.csv`), and are
measured again on the day the frames are drawn.

Every step has a script and stored results (`analysis/sampling/README.md`).

Changes from v1:
- Draw ~500 per platform, not 460.
- The Zenodo strata cover all record types and are mutually exclusive; tar-only records form their own stratum.
- GitHub uses exact random IDs.
- The cutoff is the platform creation date.
- The primary outcome is resource-level and pre-specified, with multiplicity handled.
- Acquisition outcomes are recorded for every drawn resource, and replacements are logged.
- One final eligibility rule applies on all platforms.
- The SD is re-estimated after an interim check.
- The paired subsample is explicitly secondary.

## 1. Why a new design

The thesis cohort (318 resources) is a purposive quota sample: 15 resources × 7 purpose
categories × 3 platforms, found by keyword search with an AI assistant, partly by hand,
with no dated query log (`thesis/evaluation-sample-candidates.md`). This has four problems:
- Its population is undefined, and it cannot be redrawn.
- The purpose category is both the sampling variable and the classifier's reference.
- 40 Zenodo entries were never checked file by file.
- 8 GitHub repositories were removed after execution.

Any venue reviewing a cross-platform comparison will ask what population the results describe
and whether the sample can be redrawn.

The thesis cohort remains useful as the **pilot**: it validates the pipeline at scale, and it allows
the thesis pipeline and the new one to be compared on the same resources (methodology results).

## 2. Research questions and outcomes

- **RQ1:** How does computational reproducibility of notebooks differ between GitHub, Codeberg and Zenodo?
- **RQ2:** Which resource characteristics (age, declared dependencies, size, Python version, …) explain the outcomes?
- **RQ3:** What are the dominant causes of failure on each platform?
- **RQ4 (secondary, paired):** How do outcomes differ between a live repository and its archived Zenodo snapshot
  of the same project?

**Unit of sampling and analysis:** the **resource** (repository or Zenodo record). Notebooks are nested
observations.

**Primary outcome (pre-specified):** for each acquired resource, the share of its Python notebooks that
execute without error under the standard protocol,
Y_r = (notebooks executing cleanly) / (Python notebooks found). Every resource gets equal weight.
- **Primary comparison:** the three pairwise platform differences in the mean of Y_r, with Holm correction.
- **Secondary:**
  - notebook-level mixed-effects models (notebooks in resources);
  - output reproducibility (comparison version 2 scores);
  - failure causes;
  - acquisition success (section 6.4);
  - the paired analysis (RQ4).

  Secondary analyses are reported with effect sizes and confidence intervals, and are not used for the
  sample-size justification.

## 3. Population definition (D1)

**Recommendation — one definition for all platforms:** *public resources that contain at least one
Jupyter notebook file (`.ipynb`), with a platform creation date before the cutoff (D3).*

Eligibility is decided in two steps:
1. **Screening (frame construction, platform-specific):** a cheap indicator that the resource probably
   contains notebooks (table below).
2. **Final eligibility (identical on all platforms):** at least one `.ipynb` file found by the pipeline's
   notebook discovery after acquisition. A screened-in resource without notebooks is recorded as
   "ineligible after acquisition", not as a failure.

| Platform | Screening indicator | Source |
|---|---|---|
| GitHub | language statistics include *Jupyter Notebook* (> 0 bytes) | GitHub Linguist via the API (GraphQL `languages`) |
| Codeberg | same | Forgejo `GET /repos/{owner}/{repo}/languages` (verified: e.g. aiocoap `"Jupyter Notebook": 13780`) |
| Zenodo | file name `*.ipynb` in the record, or an archive that may contain one | search API (`files.entries.key`) + archive inspection (section 5) |

**Known screening gap:** Linguist leaves files in documentation and vendored paths (e.g. `docs/`) out of
the language statistics. Repositories whose only notebooks lie there are missed by the screen. The GitHub
and Codeberg pilots (section 5) estimate this false-negative rate. They check the file tree (`git/trees`,
recursive) of a random set of repositories that the screen classed as "no Jupyter". The rate is reported
as a coverage limitation.

**Rejected alternative:** *primary language Jupyter Notebook* (GitHub search `language:"Jupyter Notebook"`).
It excludes Python projects with notebooks, has no Zenodo equivalent, and makes platforms incomparable.

**Analysis subset:** notebooks with a Python kernel (as now). The share of non-Python notebooks is reported
per platform. Resources with notebooks but none in Python are eligible members of the population, are
counted, and are excluded from Y_r (Y_r is undefined).

## 4. Inclusion and exclusion criteria (D2)

Recommendation:
- **Exclude** forks and mirrors (GitHub `isFork`/`isMirror`; Forgejo `fork`/`mirror`), because they duplicate
  other resources. Report their share in the frame.
- **Include** archived (read-only) repositories, and record the flag as a covariate.
- **Exclude** empty or private/inaccessible resources at screening. These are counted as frame attrition,
  not silently dropped.
- **Zenodo versions:** keep one record per concept (all versions of a deposit share a `conceptrecid`): the
  latest version created before the cutoff. Record the number of versions as a covariate.
- **Zenodo resource types:** include all types (software, dataset, publication, "computational notebook" …);
  the type is a covariate.
- **No size-based exclusion after sampling.** The pipeline's size limits (e.g. Zenodo archive limits) apply
  equally to all resources and are reported as acquisition outcomes. The thesis removed the 8 largest
  GitHub repositories after execution; this must not recur.
- **Same project on several platforms:**
  - kept in each platform sample;
  - identified afterwards by URL/DOI links and counted;
  - a sensitivity analysis clusters them by project, though with random draws the overlap is expected to be very small;
  - the paired subsample (section 7) studies them on purpose.

## 5. Sampling frames (probed 2026-10-03)

### Codeberg — census of the frame
- `GET /api/v1/repos/search` enumerates all public repositories: **443,949** (X-Total-Count). The
  results include `fork`, `mirror`, `archived`, `created_at` and `empty`.
- For each non-fork, non-mirror, non-empty repository: `GET /repos/{owner}/{repo}/languages`.
- **Cost:** ~4,500 search pages plus up to ~444k language calls. Codeberg allows 2,000 requests per
  10 minutes, so crawling takes about **1.5–2 days** (resumable, with a polite delay, and a token if available).
- **Outcome:** the complete list of Codeberg repositories that pass the screen (expected: a few thousand).
  A simple random sample is drawn from it, or all of it is taken if it is small (D5).
- **Screening check:** tree listing of 500 random "no Jupyter" repositories.
- **Check order:** candidates are checked in a seeded random order (seed 20261005), so the checked part is
  always a simple random sample of the candidates. The census can stop early, either by hand or after N
  screened-in repositories. The export then reports the checked fraction and the estimated frame size.
  A full census remains the default.

### GitHub — exact random repository IDs (D7)
- **Search cannot enumerate the population.** `language:"Jupyter Notebook"` reports **11,228,473**
  repositories but returns at most 1,000 results per query. That is ≈ 9,000 per creation week in 2020, so
  even day slices exceed the cap. The search also matches only the *primary* language.
- **Procedure:**
  1. Draw IDs uniformly at random from [1, max ID], where the maximum ID is found by bisection: the largest
     ID of a repository created before the cutoff.
  2. Look up each drawn ID with `GET /repositories/{id}`. This is one small request per draw; it was tested
     on 2026-10-05 and is long-standing, though not in GitHub's REST reference. A 200 answer means a public
     repository with exactly this ID. A 404 means a deleted or private repository: record it as "no public
     repository" and continue drawing. (`GET /repositories?since=ID−1` with an exact-ID check is the
     documented alternative, but it returns 100 repositories per call.)
  3. Never take the next repository: it would over-weight repositories that follow large ID gaps.
  4. Apply D2 to the lookup result (fork, `mirror_url`, size 0 = empty, `created_at`). Then read the language
     statistics of the remaining repositories with GraphQL (`nodes(ids: …) { languages }`, up to 100 per call).

  Every public repository then has the same inclusion probability, and the drawn sequence (IDs,
  outcomes) is saved.
- **Pilot (1,000 drawn IDs):**
  - the share of IDs that resolve to a public repository;
  - the share of those that pass the screen;
  - the screening false-negative rate (tree check);
  - requests per eligible repository.

  These figures set the cost. A token-less test of 8 draws (2026-10-05) found 2 public repositories. If
  ~0.5–1 % of draws give a screened-in repository, 500 need 50,000–100,000 draws: 10–20 hours at 5,000
  requests per hour and token.
- **Fallback, only if the pilot shows the cost is prohibitive:** date-sliced search on the primary
  language, with the narrower population stated explicitly.

### Zenodo — three mutually exclusive strata (D4)
Probed counts (all record types; `files.entries.key` matches the file names of the record, not the
content of archives):

| Stratum | Definition | Latest versions | All versions before cutoff | Inspection | Plan |
|---|---|---:|---:|---|---|
| **Z1** direct | ≥ 1 file `*.ipynb` | 10,571 | 11,762 | file list | enumerate all; sample from it |
| **Z2** ZIP | no `*.ipynb`, ≥ 1 `*.zip` | 515,528 | 931,947 | container listing (`/files/{key}/container`), one call per ZIP; listings stop at ~700–950 entries (`truncated`) | random subsample (~20,000), list contents |
| **Z3** tar | no `*.ipynb`, no `*.zip`, ≥ 1 `*.tar`, `*.tar.gz`, `*.tgz` | 37,917 | 54,788 | container listing **not supported** (HTTP 500 on 6/6 tested) → streamed download, reading member names only (up to 200 MB per file) | random subsample (~1,000), streamed |

The counts come from probes on 2026-10-05.

**Versions:** only 398,309 of the 515,528 current Z2 versions were created before the cutoff. A deposit's
current version is often newer than the cutoff while an older version falls before it. The frame
therefore enumerates **all versions** created before the cutoff and keeps the latest one per concept
(D2).

**Remaining over-coverage:** a newer pre-cutoff version that is in no stratum (e.g. one without archives)
is invisible to the frame. It is checked for every *sampled* record (`/versions`), and such records are
recorded as ineligible.

**Enumeration:** the search API returns at most 10,000 hits per query (25 per page for guests, 100 with
a token). Each stratum is therefore listed in creation-time windows, halved until each window holds at
most 10,000 hits. For every window the stored row count is compared with the reported total.

- Records with other archive formats (`.7z`, `.rar`, …) or none are outside the frame. Their count is
  reported as a coverage limitation.
- Of the Z2 records, 254,261 are software; most of them are GitHub-integration releases (the record links to
  a GitHub repository). This shapes the interpretation of "Zenodo" results, and the share is reported.
- **Z2 inspection stopping rule (amendment 2026-10-08):**
  - The ZIP subsample is inspected in its seeded random order. It stops once the pool (concepts with a notebook or unknown content) reaches 460, and after at least 1,000 concepts; it never goes beyond 20,000.
  - Why 460: the draw gives Z2 at most 500 − 2 × 50 = 400 primary plus 60 reserve records, so a larger pool changes neither the allocation cap nor, materially, Ñ_Z2 (relative SE ≈ 1/√460 ≈ 5%).
  - The inspected prefix is a simple random sample, and m = its size.
  - Stopping at a fixed number of hits (inverse sampling) biases p̂ = pool/m upwards by about 1/pool (0.2%). This is negligible and is reported.
  - Files that are restricted or removed since listing (HTTP 403/404/410) are recorded as `no_access`. They count in m but not in the pool (not open, so not eligible).
- **Unknown content:** container listings can be truncated or fail, and tar downloads may hit size limits.
  Such records are kept as "content unknown" in the stratum's denominator, with their number reported.
- **Weights:** a record in stratum h is sampled with probability π_h = n_h / N_h (Z1: the final draw from
  the enumerated list; Z2, Z3: subsample fraction × final-draw fraction).
  - Zenodo estimates use inverse-probability weights 1/π_h (Horvitz–Thompson / weighted means), with
    stratum-aware standard errors.
  - Stratum-specific results are also reported unweighted.
- **Allocation:** proportional to the estimated number of eligible records per stratum (from the
  subsamples), with at least 50 per stratum so that every stratum can be estimated on its own.

## 6. Time window, stratification, sample size, attrition

### 6.1 Cutoff (D3)
**Platform creation date before 2026-07-01**, i.e. the date the resource was created on the platform, not
the creation date of the project or the notebook. The fields used are:
- GitHub: `createdAt`;
- Codeberg: `created_at`;
- Zenodo: the record's `created`, not `publication_date`, which the uploader sets freely and may backdate.

The frame date and the exact timestamps are recorded. There is no lower bound: age is a covariate.

### 6.2 Stratification
By platform creation year within each platform (proportional allocation), so that age — the main
confounder for environment drift — is balanced and estimable. For Zenodo, year strata are nested in
Z1–Z3.

### 6.3 Sample size (D5)
- **Pilot estimate:** the thesis data cover 282 resources with scored notebooks. There the
  resource-level primary outcome Y_r has SD ≈ 0.36 (planning SD 0.358 = mean of the platform SDs 0.30,
  0.35, 0.42). The ICC of clean execution is 0.30. Source: `analysis/sampling/pilot_power.py`, results in
  `data/sampling/pilot/pilot-power.json`.
- **Unadjusted:** with two-sided α = .05 and power .8, a two-platform comparison needs
  n = 2 (z₀.₉₇₅ + z₀.₈)² σ² / d² resources per platform:
  - **804** for a 5-point difference;
  - **314** for 8 points;
  - **201** for 10 points.
- **Three pairwise comparisons:** planning at Bonferroni level α = .05/3 (conservative for Holm), an
  8-point difference needs **419** resources per platform (exact normal quantiles). At **400** the
  detectable difference is **8.2 points** (7.1 at α = .05).
- **Recommendation: 400 acquired, eligible resources with at least one Python notebook per platform.**
  The paper states "powered for ≈ 8-point differences after multiplicity correction".
- **Interim check:** the SD comes from a purposive sample and may not hold for a probability sample. After
  the first ~100 acquired resources per platform, the SD is recomputed (blind to platform differences: only
  the pooled SD is looked at). The target is raised if the SD exceeds 0.40. This rule is fixed now and
  reported.
- Codeberg: if fewer resources pass the screen than needed, all of them are taken (a census), and the
  achieved precision is reported.

### 6.4 Draw size and attrition (D9)
- **Initial draw: ~500 per platform** (400 / 0.85 = 471. Zenodo lost 22 % of resources in the thesis, and
  at 500, 20 % attrition still leaves 400). Total ≈ 1,500 drawn, ≈ 1,200 analysed.
- Resources are processed **in random order** from the drawn list, and processing stops when 400 per
  platform are analysable. Any further resources needed are drawn from the same frame with the same seed
  sequence. Every replacement is logged.
- **Every drawn resource gets an acquisition outcome:**
  - acquired;
  - not available (deleted/private);
  - download failed;
  - size limit;
  - ineligible after acquisition (no `.ipynb`);
  - no Python notebook.
- Acquisition success is **reported per platform as an outcome in its own right** (e.g. "Zenodo archive
  not obtainable" is a platform property). Replacement keeps the analysed sample size, but the primary
  outcome then describes *acquirable* resources; the paper states this. A sensitivity analysis counts
  failed acquisitions as Y_r = 0.

### 6.5 Scale
About 1,500 resources (≈ 4.5× the thesis cohort; ~12,000 notebooks at the thesis mean of 8 per resource),
run on the cluster with 32–64 parallel tasks.

### 6.6 The draw (S5, `analysis/sampling/draw_sample.py`)
- **Codeberg:** proportional allocation over creation years (largest remainder), seeded shuffle within each
  year. If the frame holds no more than n, all of it is taken. If the census was stopped early, π = checked
  fraction × draw fraction.
- **GitHub:** the exact-ID sequence is already a uniform random sample. The first n repositories that pass
  D2 and the screen (in draw order) are the sample, and the next ones are the reserve. The sample is
  self-weighting; creation year is recorded, not stratified.
- **Zenodo (two-phase stratified):**
  - Pool_h = inspected concepts with a notebook, or with unknown content (resolved at acquisition, D1).
    Z1 = all open concepts.
  - Estimated eligible: Ñ_h = N_h · |pool_h| / m_h.
  - Allocation: n_h ∝ Ñ_h, at least 50, at most |pool_h|; within a stratum, by creation year.
  - Inclusion probability π = (m_h / N_h) · (n_h / |pool_h|), weight 1/π.
  - Version check for every drawn record, primary and reserve. A record that is not the newest version of
    its concept before the cutoff is ineligible. An ineligible primary record is replaced by the next reserve
    record of the same stratum; an ineligible reserve record is dropped. Both stay in `draw.csv`.
- **Processing order:** one seeded shuffle across platforms (D9). The cohort file feeds `run_array.sbatch`.
  The reserve is used in its own order, and every use is logged.
- **Guard:** the script refuses to draw while a frame is unfinished.

## 7. Paired subsample (D6, secondary)

- **GitHub ↔ Zenodo:** Zenodo software records created by the GitHub integration point to their repository
  (`related_identifiers`, "isSupplementTo github.com/…").
  - Draw ~150 pairs at random from Z2 records whose GitHub repository still exists.
  - Run the Zenodo snapshot and the live repository, and optionally the GitHub commit of the release tag.
  - This compares the archived snapshot with the live repository for the *same* project.
- **GitHub → Codeberg migrations** (repositories whose README or description says they moved): a small
  exploratory set, if enough are found in the Codeberg census.
- Pairs are analysed separately (within-pair differences, McNemar / paired tests). They are **not** mixed
  into the population estimates and **not** part of the power justification. The precision achieved (CI of
  the paired difference) is reported.

## 8. Purpose categories and covariates

- Purpose categories are **not** a sampling variable. Notebooks are classified after the run (classifier
  plus a human-validated subset with two annotators and κ), and the category is used as a covariate.
- Covariates recorded for every resource:
  - creation year and last update;
  - size and number of notebooks;
  - stars and forks (GitHub, Codeberg);
  - resource type, versions and stratum (Zenodo);
  - archived flag;
  - declared dependency files;
  - licence (file-based);
  - notebook Python version.

## 9. Making the sampling reproducible

- Scripts in `analysis/sampling/` (steps S0–S5, see its README) draw the frames. Every request is logged
  as a JSON line with a timestamp. Frames are saved as dated files
  (`data/sampling/<platform>/frame-…-<date>.csv.gz`) with SHA-256. The crawl state (SQLite) and the
  request logs go into the replication package.
- Random draws use a fixed, recorded seed. The following are saved:
  - the drawn sample (including the GitHub ID sequence);
  - the eligibility decision for every inspected resource, with the reason;
  - the acquisition outcomes;
  - the replacements.
- API terms and rate limits are respected (tokens, polite delays, retry on 429). There is no web scraping
  where `robots.txt` disallows it; the thesis ran into this for Zenodo search, and the API with a token is
  the documented route.
- Frames, samples and the analysis plan (sections 2 and 6, fixed before the run) are part of the
  replication package (Zenodo deposit).

## 10. Remaining threats (to state in the paper)

- Platform populations differ in size and community, so results describe each platform's population, not
  "notebooks in general". Zenodo's archive strata consist largely of GitHub release snapshots.
- **GitHub:** deleted repositories cannot be sampled (survivorship). Linguist screening misses notebooks in
  documentation or vendored paths (the rate is estimated in the pilot).
- **Zenodo:** archive contents are partly unknown before download. Formats other than zip and tar are not
  covered.
- **Acquirability:** the primary outcome is conditional on successful acquisition (section 6.4).
- **Time:** frames are snapshots at the frame date, while reproducibility is measured at run time. Time
  travel limits environment drift but does not remove it.

## 11. Decisions needed

| # | Decision | Recommendation |
|---|---|---|
| D1 | Population definition | resources containing ≥ 1 `.ipynb` on all three platforms; screening per platform, identical final eligibility after acquisition |
| D2 | Inclusion/exclusion | exclude forks, mirrors and empty resources; include archived ones; one Zenodo version per concept; all Zenodo types; no post-hoc size exclusion |
| D3 | Cutoff date | platform creation date before 2026-07-01 (GitHub `createdAt`, Codeberg `created_at`, Zenodo `created`); no lower bound |
| D4 | Zenodo strata | Z1 direct (enumerate), Z2 ZIP (subsample ~20,000, container listing), Z3 tar (subsample ~1,000, streamed); inverse-probability weights |
| D5 | Sample size | 400 analysable resources per platform (8.2 points after multiplicity correction); interim SD check after 100; Codeberg all if fewer |
| D6 | Paired subsample | ~150 GitHub↔Zenodo integration pairs, secondary and not powered; Codeberg migrations exploratory |
| D7 | GitHub sampling method | exact random repository IDs (no next-repository rule) + GraphQL screening; pilot of 1,000 IDs first |
| D8 | Thesis cohort | pilot and methodology comparison only (old vs new pipeline), not part of the population estimates |
| D9 | Draw size and attrition | ~500 drawn per platform, processed in random order; logged replacements; acquisition outcome for every drawn resource; sensitivity analysis with failures as 0 |

## 12. Next steps

1. Codeberg census, with the screening check (S2; running since 2026-10-05).
2. GitHub exact-ID pilot of 1,000 IDs: hit rates, screening false negatives, cost (S3; needs `GITHUB_TOKEN`).
3. Zenodo (S4; enumeration running since 2026-10-05):
   - enumerate Z1–Z3;
   - Z2 container-listing subsample;
   - Z3 streamed subsample;
   - estimate the stratum sizes.
4. Fix the analysis plan (sections 2 and 6) in the repository before the run.
5. Draw the samples (seeded), run on the cluster, record acquisition outcomes and replacements, do the interim SD
   check, and classify afterwards.
