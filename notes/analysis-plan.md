# Analysis plan for the cross-platform study (draft for approval)

Status: approved 2026-10-11. To be approved and committed **before the main run on the drawn sample**, so
that the analyses are fixed before the results are known (deviations are reported as such). It
operationalises `notes/sampling-design.md` (sections 2 and 6) with the measures the pipeline records.
Decisions marked **P1–P6** need the authors’ confirmation.

## 1. Data

- **Main sample:** the draw of S5 (`data/sampling/draw/<date>/`, about 500 per platform) plus the logged
  reserve top-ups (`topup-log.csv`), run on the cluster and merged (`merged.sqlite`). One run per resource is
  analysed: the latest after pipeline-error re-runs.
- **Pilot:** the thesis cohort (318), run with the same pipeline. It is used only for the method comparison
  (section 6) and to check the pipeline at scale, not for the population estimates.
- **Pipeline version:** the commit in `MANIFEST.txt` and `pipeline_sessions`. ReproScore 1.1 + 1.0, licensee
  10.1.0, comparison version 2, and the error-context categories are part of the recorded provenance.

## 2. Units, populations, weights

- **Unit:** the resource; notebooks are nested in resources.
- **Analysis set (primary):** analysable resources, i.e. acquired with at least one Python notebook
  (`topup_from_reserve.py` definition).
- **Platform populations** as defined in the sampling design (D1–D3).
- **Weights:**
  - Codeberg and GitHub are self-weighting (the stored π is used if the Codeberg census was stopped early).
  - Zenodo uses inverse-probability weights 1/π per stratum (`draw.csv`); Zenodo estimates are weighted means with stratum-aware standard errors; stratum results are also reported unweighted.

## 3. Primary outcome and comparison (RQ1)

- **Primary outcome:** Y_r = share of the resource's Python notebooks with `execution_status = SUCCESS`
  (no error in any cell, within the time limit).
- **Primary comparison:** the three pairwise platform differences in mean Y_r (Codeberg–GitHub,
  Codeberg–Zenodo, GitHub–Zenodo).
  - Each is estimated with a 95% confidence interval (Welch; Zenodo with its weights: design-based variance).
  - Each is tested two-sided with Holm correction over the three comparisons (family-wise α = .05).
- **Power statement:** 400 per platform detects ≈ 8.2 points at Bonferroni level (S1, `pilot-power.json`).
- **Interim check (fixed rule):** after ~100 analysable resources per platform, the *pooled* SD of Y_r is
  computed without looking at platform differences. If it exceeds 0.40, the target per platform is raised
  to n = 2(z₀.₉₉₁₇ + z₀.₈)²·SD²/0.08², rounded up, within the reserve.

## 4. Secondary analyses (reported with effect sizes and CIs, no confirmatory claims)

| # | Question | Measure | Method |
|---|---|---|---|
| S1 | Notebook-level execution (RQ1) | clean execution per notebook | mixed-effects logistic regression, random intercept per resource, platform fixed effect; ICC reported |
| S2 | Output reproducibility (RQ1) | comparison-v2 `score_normalised` (also `strict`, `text_data`, `before_error`) over assessable cells; share of notebooks without stored outputs | resource means; same comparisons as section 3 |
| S3 | Explaining outcomes (RQ2) | Y_r ~ platform + creation year + declared dependency files (used or not) + pinned share + notebook Python version (and whether it was raised into 3.8–3.14) + number of notebooks + size + archived + licence present + RRS 1.1 categories | fractional logit (quasi-binomial) at resource level; Zenodo with weights |
| S4 | Failure causes (RQ3) | first-error categories (with EXTERNAL_SERVICE_ERROR; `error_category_by_type` for the thesis mapping), `dependency_cause`, environment/acquisition outcomes | shares per platform with CIs; χ² per platform pair (Holm) |
| S5 | Readiness vs outcome | RRS 1.1 (and 1.0), ROS, RCS (notebookfair-v1 and default rubric) | correlation of RRS with Y_r and with ROS per platform; readiness–outcome gap |
| S6 | Acquirability | acquisition outcome of every drawn resource (`topup-log.csv` + runs) | share obtainable per platform; Zenodo per stratum |
| S7 | Licences | file-based licence (licensee) vs platform API | shares per platform; agreement |
| S8 | Archived snapshot vs live repository (RQ4) | paired GitHub↔Zenodo subsample (D6) | within-pair differences (McNemar for clean execution; paired differences of Y_r) with CIs |

## 5. Sensitivity analyses (pre-specified)

1. **Acquisition failures as Y_r = 0** (intention-to-treat style) instead of excluding them.
2. **Deduplication:** identical notebooks within a resource counted once; resources found on several platforms clustered by project.
3. **Python version:** only resources whose notebooks declare Python 3.10, which needs no version change (P3).
4. **Time limit:** notebooks that timed out treated as missing instead of as failures.
5. **External services:** excluding EXTERNAL_SERVICE_ERROR notebooks from Y_r (they cannot run unattended).
6. **ReproScore version:** S3/S5 with RRS 1.0 instead of 1.1, and the per-sub-metric differences between the versions.
7. **Zenodo:** unknown archive content (`content = unknown` in `draw.csv`) excluded vs included.

## 6. Method comparison (pilot)

On the thesis cohort: the thesis pipeline (frozen DB) vs the new pipeline, comparing
- execution status per notebook,
- the thesis score vs comparison v2,
- error categories (`error_category_by_type`) and
- the Python version used.

Reported as a transition table and as the change in the thesis's main numbers. This is the "what changed and why" part, separate from the population estimates.

## 7. Missing data and exclusions

- No resource is excluded after the draw except by the eligibility rules (D1, D2). Every exclusion is counted with its reason.
- Notebooks without stored outputs have no comparison score; this is reported (share per platform), not imputed.
- Resources whose runs end in pipeline errors after two re-runs are reported separately (P4).

## 8. Reporting and reproducibility

- All analyses are scripts in `analysis/` that read the merged database and the draw. Their outputs (CSV/JSON) carry the input SHA-256, as in `analysis/thesis/reproduce.py`.
- The replication package contains the frames, draw, top-up log, merged database, artefacts, request logs and this plan with its commit.

## 9. Decisions needed

| # | Decision | Proposal |
|---|---|---|
| P1 | Primary outcome | Y_r = share of Python notebooks executing without error (as in the sampling design) |
| P2 | Main reproducibility measure for S2 | comparison-v2 `score_normalised` over assessable cells |
| P3 | Python-version sensitivity subset | notebooks declaring 3.10 (no version change) |
| P4 | Re-runs of pipeline errors | up to two, then reported as pipeline errors |
| P5 | Time limit per notebook | `NOTEBOOK_TIMEOUT_SECONDS` = 1800 s (pipeline default, `src/pyenv.sh`), the same for all |
| P6 | Multiplicity | Holm over the three primary comparisons only; secondary analyses are descriptive |

## 10. Clarifications (2026-10-09, before any study results; from the paper review)

These make the estimation precise; they do not change outcomes, comparisons or the sample. They are recorded
before the main results are seen.

**Three outcomes, reported separately:**
- availability: share of drawn resources obtained;
- clean execution: Y_r, the primary outcome;
- stored-output reproduction.

Unobtainable resources are not counted as irreproducible. Their Y_r = 0 version stays a sensitivity analysis.

**Estimand:**
- The primary estimand is the mean of Y_r over eligible, obtainable resources (resource level, equal weight per resource).
- Notebook-level shares come from the mixed model (S1) only.

**Variance:**
- Codeberg: SRS with a finite-population correction (n/N ≈ 20% of the screened-in census).
- GitHub: SRS.
- Zenodo: a bootstrap resampling both phases (inspection subsample and draw) within strata, B = 2000, with the Kish effective sample size reported.
- Differences: the samples are independent, so the variances add. Holm over the three primary comparisons.

**Weights:**
- π_h is recomputed with n_h = resources drawn from stratum h (primary + used reserve).
- A cross-stratum replacement carries its own stratum's weight.
- Unknown-content records found ineligible leave the domain (domain estimation).

**Screening coverage:** the 95% upper bound of the miss rate (rule of three: 3/n checked) is reported with the implied minimum coverage. The estimates refer to the screened-in populations.

**Wording:**
- RQ2 and RQ4 report associations ("associated with"), not causal effects or predictions.
- RQ4 = whether RRS adds explanatory power beyond the S3 characteristics.
- RQ3 reports the type→context transition table. Accuracy claims only if the validation sample (two annotators) is done.

## 11. Round 3 (decision 2026-10-21, before the run; no round-3 result exists yet)

**Why.** The review of round 2 found pipeline faults that are not properties of the resources:
- the kernel ran without the resource's environment active, so `!pip install` in a notebook used the pipeline's
  Python (311 notebooks, 142 runs; `notes/round2-shell-pip.txt`);
- node-local disks filled up, an output comparison crashed, and two path-handling bugs occurred (8 resources,
  `analysis/paper/infrastructure-failures-r2.csv`).

All of these are fixed; the fixes and their tests are in the repository.

In addition, the Zenodo reserve ran out at 355 analysable records. Section 6.4 of the sampling design says that
processing continues until 400 are analysable, with further resources drawn from the same frame with the same
seed sequence. Stopping at 355 was therefore a deviation from the design.

**What.**
- **Cohort.** The whole cohort of round 2 runs again on the fixed pipeline: the same 1,670 resources (primary plus
  used reserve), with no new draw for Codeberg or GitHub.
- **Zenodo reserve extension.** `analysis/sampling/extend_zenodo_reserve.py` extends the Zenodo reserve in
  `data/sampling/draw/2026-10-09-r3/`. It continues each stratum's seeded sequence and checks that the records
  already drawn are the first of every seeded cell. Z2 takes the rest of its pool (75); Z1 and Z3 take 60 more
  each. All records get the same version check as the original draw. Phase 1 is unchanged.
- **Top-ups.** Top-ups use the extension after the original reserve, with the unchanged rule (same stratum and
  year, then same stratum, then any stratum), until 400 Zenodo records are analysable or the extension is used
  up.
- **Unchanged.** Outcomes, estimators, comparisons, Holm correction and the sensitivity analyses do not change.

**Which round is reported (fixed now, whatever the results).**
1. Round 3 is the primary analysis if it completes and passes the quality gate: infrastructure-related
   installation failures in at most 1% of the analysable runs.
2. Resources damaged by the run are listed and excluded by the same rule as in round 2, and included as recorded
   in sensitivity variant 8.
3. Round 2 (`results/study-r2/`, with its 8 exclusions) is reported as a sensitivity analysis: "round 2
   (previous pipeline)", the three primary differences.
4. If round 3 does not complete or fails the gate in time for the submission, round 2 stays primary and round 3
   is not used for this submission. The reason is reported.
5. The run is not repeated, and no resources are dropped or added, in response to round-3 outcomes.

**New exploratory sensitivity variants** (added now, labelled exploratory, not pre-specified):
- resources whose Python version did not have to be moved into 3.8–3.14;
- resources not run with Python 3.8 (installed without the package-date cutoff).
