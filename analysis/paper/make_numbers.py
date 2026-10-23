#!/usr/bin/env python3
"""Every number in the MSR paper, as LaTeX macros (paper/msr2027/numbers.tex).

Nothing in the paper is typed by hand: the text uses \\num<Name>{} macros written here from
  data/sampling/{codeberg/summary.json, github/ids-summary.json, zenodo/summary.json, pilot/pilot-power.json}
  data/sampling/draw/<date>/summary.json                                   (--draw-dir)
  results/study/merged.sqlite + draw.csv                                     (--study-db, once the study is done)
Macros whose input is not available yet are defined as a visible \\TBD{name}, so the paper always compiles
and shows what is missing. The header records the SHA-256 of every input.

Usage: python3 analysis/paper/make_numbers.py [--draw-dir DIR] [--study-db results/study/merged.sqlite]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAMPLING = ROOT / 'data' / 'sampling'
OUT = ROOT / 'paper' / 'msr2027' / 'numbers.tex'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def load(path, inputs):
    path = Path(path)
    if not path.is_file():
        return None
    inputs[str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)] = sha(path)
    return json.loads(path.read_text())


def fmt(value, digits=0, percent=False):
    if value is None:
        return None
    if percent:
        value = 100 * value
    if digits == 0:
        return f'{round(value):,}'.replace(',', '{,}')
    return f'{value:,.{digits}f}'.replace(',', '{,}')


WORD = {'Z1': 'ZOne', 'Z2': 'ZTwo', 'Z3': 'ZThree'}     # LaTeX command names cannot contain digits


def latest_draw():
    draws = sorted(p for p in (SAMPLING / 'draw').glob('20*') if (p / 'summary.json').is_file())
    return draws[-1] if draws else None


def frame_numbers(macros, inputs):
    cb = load(SAMPLING / 'codeberg' / 'summary.json', inputs)
    if cb:
        steps = cb['filter_steps']
        macros.update(CodebergListed=fmt(steps['listed']), CodebergForks=fmt(steps['fork']),
                      CodebergMirrors=fmt(steps['mirror']), CodebergEmpty=fmt(steps['empty']),
                      CodebergCandidates=fmt(steps['screen_candidates']),
                      CodebergScreenedIn=fmt(steps['screened_in_jupyter']),
                      CodebergTreeSampled=fmt(cb['treecheck'][0]['read']) if cb.get('treecheck') else None,
                      CodebergTreeMissed=fmt(cb['treecheck'][0]['with_ipynb']) if cb.get('treecheck') else None,
                      CodebergRequests=fmt(cb.get('requests_logged')))
        tree = (cb.get('treecheck') or [{}])[0]
        if tree.get('read'):
            # zero misses in n checked: 95% upper bound of the miss rate by the rule of three (3/n)
            rejected = steps['languages_http_status'].get('200', 0) - steps['screened_in_jupyter']
            upper = 3 / tree['read']
            macros.update(CodebergRejected=fmt(rejected), CodebergMissUpper=fmt(upper, 1, True),
                          CodebergMaxMissed=fmt(rejected * upper),
                          CodebergMinCoverage=fmt(steps['screened_in_jupyter'] / (steps['screened_in_jupyter'] + rejected * upper), 0, True))
    gh = load(SAMPLING / 'github' / 'ids-summary.json', inputs)
    if gh:
        macros.update(GithubMaxId=fmt(int(gh['max_id'])), GithubDraws=fmt(gh['draws']),
                      GithubPublic=fmt(gh['public_repository']), GithubPublicShare=fmt(gh['public_share'], 1, True),
                      GithubForks=fmt(gh['d2_outcomes'].get('fork')), GithubEmpty=fmt(gh['d2_outcomes'].get('empty')),
                      GithubPassDTwo=fmt(gh['d2_pass']), GithubScreenedIn=fmt(gh['screened_in_jupyter']),
                      GithubJupyterShare=fmt(gh['jupyter_share_of_d2_pass'], 1, True),
                      GithubScreenedPerDraw=fmt(gh['screened_in_per_draw'], 2, True),
                      GithubTreeSampled=fmt(gh['treecheck']['read']), GithubTreeMissed=fmt(gh['treecheck']['with_ipynb']),
                      GithubRequests=fmt(gh.get('requests_logged')))
        if gh['treecheck'].get('read'):
            rejected = gh['d2_pass'] - gh['screened_in_jupyter']
            upper = 3 / gh['treecheck']['read']
            macros.update(GithubRejected=fmt(rejected), GithubMissUpper=fmt(upper, 1, True),
                          GithubMaxMissed=fmt(rejected * upper),
                          GithubMinCoverage=fmt(gh['screened_in_jupyter'] / (gh['screened_in_jupyter'] + rejected * upper), 0, True))
    zn = load(SAMPLING / 'zenodo' / 'summary.json', inputs)
    if zn:
        for stratum, v in zn['strata'].items():
            stratum = WORD[stratum]
            macros[f'Zenodo{stratum}Versions'] = fmt(v['versions_enumerated'])
            macros[f'Zenodo{stratum}Concepts'] = fmt(v['concepts_latest_version_in_stratum'])
            macros[f'Zenodo{stratum}GithubLinked'] = fmt(v.get('with_github_link'))
            ins = next(iter((v.get('inspections') or {}).values()), None)
            if ins:
                inspected = ins['notebook'] + ins['no_notebook'] + ins['unknown'] + ins.get('no_access', 0)
                macros[f'Zenodo{stratum}Inspected'] = fmt(inspected)
                macros[f'Zenodo{stratum}InspNotebook'] = fmt(ins['notebook'])
                macros[f'Zenodo{stratum}InspUnknown'] = fmt(ins['unknown'])
                macros[f'Zenodo{stratum}InspNone'] = fmt(ins['no_notebook'])
                macros[f'Zenodo{stratum}InspNotebookShareKnown'] = fmt(ins['notebook_share_of_known'], 1, True)
        macros['ZenodoVersionsTotal'] = fmt(sum(v['versions_enumerated'] for v in zn['strata'].values()))
        macros['ZenodoCutoff'] = zn.get('cutoff')
    pilot = load(SAMPLING / 'pilot' / 'pilot-power.json', inputs)
    if pilot:
        macros.update(PilotResources=fmt(pilot['resources']), PilotNotebooks=fmt(pilot['notebooks']),
                      PilotPlanningSD=fmt(pilot['planning_sd'], 2), PilotICC=fmt(pilot['icc_clean'], 2))
        for row in pilot.get('sample_sizes', []):
            if abs(row['difference'] - 0.08) < 1e-9:
                key = 'Bonf' if 'Bonferroni' in row['alpha'] else 'Plain'
                macros[f'PilotNEightPoints{key}'] = fmt(row['resources_per_platform'])
        macros['PilotPlannedPerPlatform'] = fmt(pilot.get('planned_per_platform'))
        for label, value in (pilot.get('detectable_difference_at_planned') or {}).items():
            macros['PilotDetectableBonf' if 'Bonferroni' in label else 'PilotDetectablePlain'] = fmt(value, 1, True)
        macros['PilotNotebooksPerResourceMedian'] = fmt(pilot['notebooks_per_resource']['median'])


def draw_numbers(draw_dir, macros, inputs):
    if draw_dir is None:
        return
    s = load(Path(draw_dir) / 'summary.json', inputs)
    if not s:
        return
    macros.update(DrawDate=s['drawn_at'][:10], DrawSeed=str(s['seed']), DrawPerPlatform=fmt(s['n']),
                  DrawReservePerPlatform=fmt(s['reserve']), Cutoff=s.get('cutoff'))
    total_primary = 0
    for platform in ('codeberg', 'github'):
        v = s.get(platform, {})
        total_primary += v.get('primary', 0)
    z = s.get('zenodo', {}).get('strata', {})
    for stratum, v in z.items():
        stratum = WORD.get(stratum, stratum)
        macros[f'Draw{stratum}'] = fmt(v.get('primary'))
        macros[f'Draw{stratum}Reserve'] = fmt(v.get('reserve'))
        macros[f'Draw{stratum}Weight'] = fmt(v.get('weight'), 1)
        macros[f'Draw{stratum}VersionIneligible'] = fmt(v.get('ineligible_by_version_check'))
        total_primary += v.get('primary') or 0
    macros['DrawPrimaryTotal'] = fmt(total_primary)


def study_numbers(db, macros, inputs):
    """Filled in once results/study/merged.sqlite exists (analysis scripts write their own JSON; see README)."""
    if db is None or not Path(db).is_file():
        return
    inputs[str(db)] = sha(db)
    # The result numbers come from the analysis scripts (analysis/paper/results_*.py) once written;
    # they write paper/msr2027/results/*.json, merged here.
    results = ROOT / 'paper' / 'msr2027' / 'results'
    for path in sorted(results.glob('*.json')):
        data = load(path, inputs)
        if data and 'quality_gate' in data and not data.get('macro_prefix'):
            macros['ResultsStatus'] = ('' if data['quality_gate'] else
                                       r'\textcolor{red}{\textbf{PRELIMINARY RESULTS (install quality gate failed; '
                                       r'see tables/install\_quality.csv)}}')
        if (data or {}).get('quality_gate') is False and not data.get('macro_prefix'):
            print(f'WARNING: {path.name}: the install quality gate failed; the result macros are PRELIMINARY '
                  '(see tables/install_quality.csv in the analysis output)')
        for key, value in (data or {}).get('macros', {}).items():
            macros[key] = value


EXPECTED = [  # result macros used in the text; shown as \TBD until the analyses have run
    'AnalysableCodeberg', 'AnalysableGithub', 'AnalysableZenodo', 'NotebooksAnalysed',
    'CleanCodeberg', 'CleanGithub', 'CleanZenodo', 'DiffCodebergGithub', 'DiffCodebergZenodo', 'DiffGithubZenodo',
    'ReproNormCodeberg', 'ReproNormGithub', 'ReproNormZenodo', 'ObtainableZenodo', 'TopFailureCause',
]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--draw-dir', type=Path, default=None)
    parser.add_argument('--study-db', type=Path, default=ROOT / 'results' / 'study' / 'merged.sqlite')
    args = parser.parse_args()
    macros, inputs = {}, {}
    frame_numbers(macros, inputs)
    draw_numbers(args.draw_dir or latest_draw(), macros, inputs)
    study_numbers(args.study_db, macros, inputs)
    for name in EXPECTED:
        macros.setdefault(name, None)
    lines = ['% Generated by analysis/paper/make_numbers.py - do not edit.',
             f'% generated {datetime.now(timezone.utc).isoformat(timespec="seconds")}',
             *[f'% input {k} sha256:{v}' for k, v in sorted(inputs.items())],
             r'\providecommand{\TBD}[1]{\textcolor{red}{[#1]}}']
    for name, value in sorted(macros.items()):
        assert re.fullmatch(r'[A-Za-z]+', name), f'invalid LaTeX macro name: {name}'
        body = value if value is not None else rf'\TBD{{{name}}}'
        lines.append(rf'\newcommand{{\num{name}}}{{{body}}}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(lines) + '\n')
    print(f'{OUT}: {len(macros)} macros, {sum(v is None for v in macros.values())} still to be computed')


if __name__ == '__main__':
    main()
