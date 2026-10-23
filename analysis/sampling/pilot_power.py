#!/usr/bin/env python3
"""Step S1 — pilot statistics and sample size (notes/sampling-design.md, section 6.3).

Input: analysis/thesis/results/notebook-cohort.csv (one row per scored notebook of the thesis
cohort, exported from the frozen database by analysis/thesis/reproduce.py; its SHA-256 is recorded).

Primary outcome per resource: Y_r = share of its notebooks with status SUCCESS (clean execution).
Computes per platform the mean and SD of Y_r (and of the thesis score), notebooks per resource,
the ICC of clean execution (one-way ANOVA estimator), the resources per platform needed for a
two-platform difference d (two-sided, power .8) at alpha .05 and at the Bonferroni level .05/3,
the detectable difference at the planned n, and the draw size for several attrition rates.
SD used for planning: mean of the three platform SDs.

Output: data/sampling/pilot/pilot-power.json, pilot-power.csv (+ .sha256)
Usage:  python3 analysis/sampling/pilot_power.py [--planned 400]
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, SAMPLING_DIR, now, sha256_file, write_csv, write_json  # noqa: E402

COHORT = ROOT / 'analysis' / 'thesis' / 'results' / 'notebook-cohort.csv'
POWER = 0.8


def n_per_group(sd, d, alpha):
    return 2 * ((norm.ppf(1 - alpha / 2) + norm.ppf(POWER)) * sd / d) ** 2


def detectable(sd, n, alpha):
    return (norm.ppf(1 - alpha / 2) + norm.ppf(POWER)) * sd * math.sqrt(2 / n)


def icc_oneway(groups):
    n_total, k = sum(map(len, groups)), len(groups)
    grand = sum(map(sum, groups)) / n_total
    ssb = sum(len(g) * (statistics.mean(g) - grand) ** 2 for g in groups)
    ssw = sum(sum((x - statistics.mean(g)) ** 2 for x in g) for g in groups)
    msb, msw = ssb / (k - 1), ssw / (n_total - k)
    n0 = (n_total - sum(len(g) ** 2 for g in groups) / n_total) / (k - 1)
    return (msb - msw) / (msb + (n0 - 1) * msw)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--planned', type=int, default=400, help='planned analysable resources per platform')
    args = parser.parse_args()

    rows = list(csv.DictReader(open(COHORT, encoding='utf-8')))
    by_resource = defaultdict(list)
    for row in rows:
        by_resource[(row['platform'], row['url'])].append(row)
    clean, score, sizes = defaultdict(list), defaultdict(list), []
    for (platform, _), notebooks in by_resource.items():
        clean[platform].append(sum(n['status'] == 'SUCCESS' for n in notebooks) / len(notebooks))
        score[platform].append(statistics.mean(float(n['score']) for n in notebooks))
        sizes.append(len(notebooks))
    platforms = sorted(clean)
    per_platform = {p: dict(resources=len(clean[p]), clean_mean=statistics.mean(clean[p]),
                            clean_sd=statistics.stdev(clean[p]), score_mean=statistics.mean(score[p]),
                            score_sd=statistics.stdev(score[p])) for p in platforms}
    sd = statistics.mean(v['clean_sd'] for v in per_platform.values())
    icc = icc_oneway([[1.0 if n['status'] == 'SUCCESS' else 0.0 for n in nbs] for nbs in by_resource.values()])

    alphas = {'alpha .05': 0.05, 'alpha .05/3 (Bonferroni, 3 pairwise)': 0.05 / 3}
    sample_sizes = [dict(difference=d, alpha=label, resources_per_platform=math.ceil(n_per_group(sd, d, a)))
                    for label, a in alphas.items() for d in (0.05, 0.08, 0.10)]
    detectable_at_plan = {label: detectable(sd, args.planned, a) for label, a in alphas.items()}
    draws = {f'{a:.2f}': math.ceil(args.planned / (1 - a)) for a in (0.10, 0.15, 0.20, 0.22, 0.25)}

    result = dict(
        computed_at=now(), input=str(COHORT.relative_to(ROOT)), input_sha256=sha256_file(COHORT),
        outcome='Y_r = share of notebooks of a resource with status SUCCESS', power=POWER,
        notebooks=len(rows), resources=len(by_resource),
        notebooks_per_resource=dict(median=statistics.median(sizes), mean=statistics.mean(sizes)),
        icc_clean=icc, per_platform=per_platform, planning_sd=sd, sample_sizes=sample_sizes,
        planned_per_platform=args.planned, detectable_difference_at_planned=detectable_at_plan,
        draw_size_by_attrition=draws)
    out = SAMPLING_DIR / 'pilot'
    write_json(out / 'pilot-power.json', result)
    write_csv(out / 'pilot-power.csv', sample_sizes, ['difference', 'alpha', 'resources_per_platform'])

    print(f'input {COHORT.name} ({len(rows)} notebooks, {len(by_resource)} resources)')
    for p, v in per_platform.items():
        print(f"  {p:9} resources {v['resources']:3}  Y_r mean {v['clean_mean']:.3f} sd {v['clean_sd']:.3f}"
              f"  score mean {v['score_mean']:.3f} sd {v['score_sd']:.3f}")
    print(f"notebooks per resource: median {statistics.median(sizes)}, mean {statistics.mean(sizes):.1f}; "
          f"ICC(clean) {icc:.2f}; planning SD {sd:.3f}")
    for s in sample_sizes:
        print(f"  d = {s['difference']:.2f}, {s['alpha']:38}: {s['resources_per_platform']} per platform")
    for label, d in detectable_at_plan.items():
        print(f'  detectable at n = {args.planned}, {label}: {d:.3f}')
    print('  draw size by attrition: ' + ', '.join(f'{a} -> {n}' for a, n in draws.items()))


if __name__ == '__main__':
    main()
