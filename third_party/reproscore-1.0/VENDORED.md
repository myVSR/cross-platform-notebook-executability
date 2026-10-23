# ReproScore 1.0 (frozen baseline)

The version behind the TPDL 2026 paper: upstream https://anonymous.example/reproscore commit
`1d7a5594fe6c97a82a73e0c80d066ef11d5e2998`, with notebookfair patches 1–3 (see
`../reproscore/VENDORED.md`). Patch 1 changes nothing for repositories scored at an absolute path
without hidden or excluded folders (the pipeline scores through such a path), patch 2 only replaces a
crash, and patch 3 only adds the optional ROS component R. RRS values are therefore those of 1.0.

Do not edit. The pipeline computes RRS with this copy next to ReproScore 1.1
(`../reproscore`) so that the effect of the 1.1 fixes can be measured (`resource_readiness.rrs_v1_0`).
