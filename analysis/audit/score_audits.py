#!/usr/bin/env python3
"""Score the two manual audits (analysis/audit/make_audit_kits.py, GUIDELINE.md) and write the paper macros.

Inputs (in the kit folder): normalisation-key.csv, failures-key.csv, and the annotators' downloads
(normalisation-<initials>.csv, failures-<initials>.csv; exactly two annotators per audit), optionally
adjudicated.csv with annotator ADJ (labels settled together where the two disagreed).

Reported per audit:
  agreement        raw agreement and Cohen's kappa of the two annotators (items both labelled, "cannot-judge" kept as a
                   label of its own)
  reference label  the adjudicated label if present, else the label both annotators gave, else (an item only one
                   annotator labelled) that annotator's label; items on which the two disagree and that are not yet
                   adjudicated are counted as unresolved and left out of the rates below. The items are in random
                   order, so a second annotator who labelled only the first k items gives a random double-coded
                   subset; kappa is computed on the items both labelled.
Audit A (normalisation):
  false matches    share of the reproduced-but-not-identical cells (identical normalised, image similar, identical
                   tolerance) whose reference label is "different", overall and per outcome, with Wilson 95% intervals
  false mismatches share of the "different" cells whose reference label is "same"
Audit B (failures):
  accuracy         share of items where the pipeline's category (with context) matches the reference label, and the
                   same for the category by exception type alone
  precision        per pipeline category: share of its items whose reference label is that category
  context          for the items moved to EXTERNAL_SERVICE_ERROR by context: share where the reference is "external",
                   and share where the type-based category would have been right

Weighting (--db): the items are drawn per stratum (cell outcome, failure category), so overall rates are weighted by
the strata sizes of the round the paper reports, which may differ from the round the items were drawn from. The rules
being audited (compare_cells.py, error_context.py) are identical in both rounds; only the mix of cases changes.

Usage: python3 analysis/audit/score_audits.py results/audit-r2 [--db results/study-r3/merged.sqlite]
       [--paper-json paper/msr2027/results/audit.json]
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPRODUCED = ('identical normalised', 'image similar', 'identical tolerance')


def read(path):
    with open(path, newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def wilson(k, n, z=1.959964):
    if not n:
        return None, None, None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return p, max(0.0, centre - half), min(1.0, centre + half)


def kappa(pairs):
    """Cohen's kappa for a list of (label_a, label_b)."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def answers(folder: Path, audit: str):
    """{annotator: {item: label}} from the downloads of one audit, plus the adjudicated labels."""
    by_annotator = {}
    for path in sorted(folder.glob(f'{audit}-*.csv')):
        if path.name.endswith('-key.csv'):
            continue
        for row in read(path):
            if row['label']:
                by_annotator.setdefault(row['annotator'], {})[row['item']] = row['label']
    adjudicated = {}
    if (folder / 'adjudicated.csv').exists():
        adjudicated = {r['item']: r['label'] for r in read(folder / 'adjudicated.csv')
                       if r['audit'] == audit and r['label']}
    return by_annotator, adjudicated


def comment_tags(folder: Path, audit: str, tags=('random', 'float')):
    """{tag: {label: items}} for the comment tags of the guideline (any annotator)."""
    found = {t: Counter() for t in tags}
    for path in sorted(folder.glob(f'{audit}-*.csv')):
        if path.name.endswith('-key.csv'):
            continue
        for row in read(path):
            words = row.get('comment', '').lower()
            for t in tags:
                if t in words:
                    found[t][row['label']] += 1
    return {t: dict(c) for t, c in found.items()}


def reference_labels(key, by_annotator, adjudicated):
    names = sorted(by_annotator)
    if len(names) not in (1, 2):
        sys.exit(f'expected answers of one or two annotators, found {names}')
    if len(names) == 1:                                # provisional: no agreement until the second annotator
        names.append(None)
    a, b = by_annotator[names[0]], by_annotator.get(names[1], {})
    pairs = [(a[i['item']], b[i['item']]) for i in key if i['item'] in a and i['item'] in b]
    reference, single = {}, 0
    for item in key:
        i = item['item']
        if i in adjudicated:
            reference[i] = adjudicated[i]
        elif i in a and i in b:
            if a[i] == b[i]:
                reference[i] = a[i]
        elif i in a or i in b:
            reference[i] = a.get(i) or b.get(i)
            single += 1
    agreement = dict(annotators=[n for n in names if n], items_both=len(pairs), items_single=single,
                     items_labelled={n: len(by_annotator[n]) for n in names if n},
                     raw=round(sum(x == y for x, y in pairs) / len(pairs), 4) if pairs else None,
                     kappa=round(kappa(pairs), 4) if pairs else None,
                     unresolved=sum(item['item'] not in reference for item in key))
    return reference, agreement


def rate(items, reference, wrong):
    judged = [i for i in items if reference.get(i['item']) in ('same', 'different')]
    k = sum(reference[i['item']] == wrong for i in judged)
    p, low, high = wilson(k, len(judged))
    return dict(n=len(judged), wrong=k, share=p, low=low, high=high,
                cannot_judge=sum(reference.get(i['item']) == 'cannot-judge' for i in items))


def score_normalisation(folder):
    key = read(folder / 'normalisation-key.csv')
    by_annotator, adjudicated = answers(folder, 'normalisation')
    reference, agreement = reference_labels(key, by_annotator, adjudicated)
    reproduced = [i for i in key if i['outcome'] in REPRODUCED]
    result = dict(agreement=agreement, false_match=rate(reproduced, reference, 'different'),
                  false_match_by_outcome={o: rate([i for i in key if i['outcome'] == o], reference, 'different')
                                          for o in REPRODUCED},
                  false_mismatch=rate([i for i in key if i['outcome'] == 'different'], reference, 'same'),
                  comment_tags=comment_tags(folder, 'normalisation'))
    return result


def score_failures(folder):
    key = read(folder / 'failures-key.csv')
    by_annotator, adjudicated = answers(folder, 'failures')
    reference, agreement = reference_labels(key, by_annotator, adjudicated)
    judged = [i for i in key if reference.get(i['item']) not in (None, 'cannot-judge')]

    def share(items, column):
        k = sum(i[column] == reference[i['item']] for i in items)
        p, low, high = wilson(k, len(items))
        return dict(n=len(items), correct=k, share=p, low=low, high=high)

    precision = {}
    for category in sorted({i['category'] for i in judged}):
        precision[category] = share([i for i in judged if i['category'] == category], 'expected')
    moved = [i for i in judged if i['moved_by_context'] == '1']
    return dict(agreement=agreement, judged=len(judged),
                cannot_judge=sum(reference.get(i['item']) == 'cannot-judge' for i in key),
                accuracy=share(judged, 'expected'), accuracy_by_type=share(judged, 'expected_by_type'),
                precision=precision, moved_context_right=share(moved, 'expected'),
                moved_type_right=share(moved, 'expected_by_type'),
                reference_labels=dict(Counter(reference[i['item']] for i in judged)))


def strata_sizes(db):
    """Cells per comparison outcome and failing notebooks per category in the latest run of every resource."""
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    latest = 'SELECT MAX(id) FROM repository_runs GROUP BY repository_id'
    cells = dict(con.execute(f'SELECT outcome, COUNT(*) FROM notebook_cell_comparisons WHERE repository_run_id IN ({latest}) '
                             'GROUP BY outcome').fetchall())
    failures = dict(con.execute(f"SELECT error_category, COUNT(*) FROM notebook_executions WHERE repository_run_id IN "
                                f"({latest}) AND execution_status != 'SUCCESS' GROUP BY error_category").fetchall())
    con.close()
    return cells, failures


def weighted(rates, sizes):
    """Stratum rates {stratum: (share)} weighted by stratum sizes; strata without a rate are left out."""
    pairs = [(sizes.get(k, 0), v) for k, v in rates.items() if v is not None and sizes.get(k)]
    total = sum(n for n, _ in pairs)
    return sum(n * v for n, v in pairs) / total if total else None


def pct(value):
    return None if value is None else f'{100 * value:.1f}'


def fmt2(value):
    return None if value is None else f'{value:.2f}'


def macros(norm, fail):
    m = {}
    if norm:
        m.update(AuditNormKappa=fmt2(norm['agreement']['kappa']), AuditNormItems=str(norm['agreement']['items_both']),
                 AuditFalseMatchN=str(norm['false_match']['n']), AuditFalseMatch=str(norm['false_match']['wrong']),
                 AuditFalseMatchShare=pct(norm['false_match']['share']),
                 AuditFalseMatchHigh=pct(norm['false_match']['high']),
                 AuditFalseMismatchN=str(norm['false_mismatch']['n']),
                 AuditFalseMismatch=str(norm['false_mismatch']['wrong']),
                 AuditFalseMismatchShare=pct(norm['false_mismatch']['share']))
    if fail:
        m.update(AuditFailKappa=fmt2(fail['agreement']['kappa']), AuditFailItems=str(fail['judged']),
                 AuditFailAccuracy=pct(fail['accuracy']['share']), AuditFailAccuracyLow=pct(fail['accuracy']['low']),
                 AuditFailAccuracyHigh=pct(fail['accuracy']['high']),
                 AuditFailAccuracyByType=pct(fail['accuracy_by_type']['share']),
                 AuditMovedN=str(fail['moved_context_right']['n']),
                 AuditMovedRight=pct(fail['moved_context_right']['share']),
                 AuditMovedTypeRight=pct(fail['moved_type_right']['share']))
        for category, value in fail['precision'].items():
            word = ''.join(part.capitalize() for part in category.lower().replace('_error', '').split('_'))
            m[f'AuditPrecision{word}'] = pct(value['share'])
    return {k: v for k, v in m.items() if v is not None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--paper-json', type=Path, default=ROOT / 'paper' / 'msr2027' / 'results' / 'audit.json')
    parser.add_argument('--no-paper', action='store_true')
    parser.add_argument('--db', type=Path, help='merged database of the reported round: weights the stratum rates')
    args = parser.parse_args(argv)
    have = lambda audit: any(p for p in args.folder.glob(f'{audit}-*.csv') if not p.name.endswith('-key.csv'))  # noqa: E731
    norm = score_normalisation(args.folder) if have('normalisation') else None
    fail = score_failures(args.folder) if have('failures') else None
    if args.db:
        cells, failures = strata_sizes(args.db)
        if norm:
            norm['weighted_false_match'] = weighted({o: r['share'] for o, r in norm['false_match_by_outcome'].items()}, cells)
        if fail:
            fail['weighted_accuracy'] = weighted({c: r['share'] for c, r in fail['precision'].items()}, failures)
        weights_from = str(args.db)
    else:
        weights_from = None
    result = dict(scored_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), weights_from=weights_from,
                  normalisation=norm, failures=fail)
    (args.folder / 'scores.json').write_text(json.dumps(result, indent=1) + '\n')
    m = macros(norm, fail)
    if norm and norm.get('weighted_false_match') is not None:
        m['AuditFalseMatchWeighted'] = pct(norm['weighted_false_match'])
    if fail and fail.get('weighted_accuracy') is not None:
        m['AuditFailAccuracyWeighted'] = pct(fail['weighted_accuracy'])
    print(json.dumps(m, indent=1))
    if not args.no_paper:
        manifest = json.loads((args.folder / 'MANIFEST.json').read_text())
        args.paper_json.write_text(json.dumps(dict(generated_by='analysis/audit/score_audits.py',
                                                   generated_at=result['scored_at'], kits=str(args.folder),
                                                   kit_db_sha256=manifest['db_sha256'], macro_prefix='Audit',
                                                   macros=m), indent=1) + '\n')
        print(f'paper macros: {args.paper_json} ({len(m)})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
