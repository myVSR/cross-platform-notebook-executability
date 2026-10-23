# Re-run of the cohort of `2026-10-09`

Prepared 2026-10-21T10:12:56+00:00 by `pipeline/prepare_rerun.py`.

**Reason:** round 2 (results/study-r2) ran notebooks without the resource venv active (!pip to the pipeline Python), lost 8 resources to full node disks and path bugs, and stopped Zenodo at 355 of 400 analysable; the whole cohort runs again on the fixed pipeline, and the Zenodo reserve is extended along the same seed sequence (notes/analysis-plan.md section 11)

- The sample is unchanged: `draw.csv`, `cohort-primary.csv`, `cohort-reserve.csv`, `summary.json` are copies.
- `cohort-all.csv`: 1670 resources (codeberg 500, github 500, zenodo 670) = primary + the
  170 reserve resources used by the earlier round's top-ups. Every one is run again from scratch.
- `topup-log.csv`: the earlier round's 170 replacements only. The re-runs and 'reserve exhausted' entries
  are not carried over, so pipeline errors get their two re-runs (plan P4) in this round.

Inputs (from `2026-10-09/`):
- `draw.csv` sha256 `03f4fb6350cab70593dd0fb626ef37228ee52ca54fd984a264e577c7d37d0f95`
- `cohort-primary.csv` sha256 `37d69f645f6c92c03b1426b7fc50e7cc8712b10a6ffc8bccfc237ddd242379d5`
- `cohort-reserve.csv` sha256 `7e18de5397bdb35bf6394a71aa4ff468a14e5ad98b620ab4668819828e4db24b`
- `summary.json` sha256 `f242800598c8b4ecca47bfed457f4d91e3fd051bcbdf5a66cc01c9fb501f0774`
- `draw.csv.sha256` sha256 `b7beb07cc40783ae69409b1e40c4ecaf915950453a9be356e4553b4c87503e00`
- `cohort-primary.csv.sha256` sha256 `b5c02adf5f066b40aa084040be16f720f244f7bad13f0ec1ecae9b2c2b083c17`
- `cohort-reserve.csv.sha256` sha256 `cff940caa8cb1d40fb5b6cbfa6f1f0c811a8c84cec6d33eb2e66bba78dbe6499`
- `topup-log.csv` sha256 `18e17ab4efae775de3ba5f848ef6c91ec7c78d011f8f46289eb0bf093b72bb30`
