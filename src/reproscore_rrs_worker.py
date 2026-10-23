#!/usr/bin/env python3
"""Compute ReproScore's RRS with one ReproScore copy, isolated in its own process.

Both ReproScore versions (third_party/reproscore = 1.1, third_party/reproscore-1.0 = paper version) are
packages called `src`, so they cannot be imported into the same interpreter; src/reproscore_stage.py
calls this worker once per version. Only the given ReproScore folder is put on the path.

Usage: reproscore_rrs_worker.py TOOL_DIR REPO_DIR RUBRIC_YAML
Prints one JSON object: rrs, categories, penalties, submetrics, evidence, rubric_name.
"""
import json
import sys

tool_dir, repo_dir, rubric_path = sys.argv[1:4]
sys.path[:] = [tool_dir] + [p for p in sys.path[1:] if p]
from src.scoring.rrs import RRSScorer  # noqa: E402  (ReproScore's package)
from src.scoring.rubric import load_rubric  # noqa: E402

result = RRSScorer(rubric=load_rubric(rubric_path)).score(repo_dir)
print(json.dumps(dict(
    rrs=result.rrs,
    categories={k: v.raw_score for k, v in result.category_scores.items()},
    penalties=dict(environment=result.penalty_environment, data=result.penalty_data, seed=result.penalty_seed),
    submetrics={e.metric_id: e.raw_score for e in result.evidence},
    evidence=result.to_dict().get('evidence'),
    rubric_name=result.rubric_name), default=str))
