#!/usr/bin/env python3
"""The whole study as one restartable sequence of SLURM jobs (run from the hpc/ folder).

  bash study.sh status    where every stage stands (nothing is submitted)
  bash study.sh next      submit every stage that is due; run it again whenever you like (e.g. once a
                          day, or from scrontab): it checks what is finished or still running first,
                          so running it twice never submits anything twice
  bash study.sh autopilot submit study_driver.sbatch: runs `next` every 30 min until the study is complete,
                          logs every round (results/study/driver.log) and never stops on an error
                          (failed rounds: results/study/driver-errors.log); resubmits itself before its time limit
  bash study.sh pilot     the thesis cohort (318) with the same pipeline (method comparison), separately
  bash study.sh classify  classification of the study database (GPU partition; after the study)

Stages of `next`, each started only when the one before is complete:
  1 prepare    Pythons 3.8-3.14, Jupyter tool environment, licensee SIF   (marker ~/.notebookfair/prepare.ok)
  2 sampling   probe, codeberg, zenodo, github frames (sampling.sbatch; resumable, self-resubmitting)
  3 draw       S5 draw -> data/sampling/draw/<date>/cohort-primary.csv, cohort-reserve.csv
  4 passes     pass 1 = the primary cohort (run_array.sbatch, job name nbf-study, run study-1-main).
               When a pass has finished: merge all passes into results/study/merged.sqlite (rebuilt, oldest
               first), then topup_from_reserve.py: replacements (not obtainable / no Python notebook)
               and re-runs (pipeline errors, interrupted, never run; at most 2 per resource) form the next
               pass. The study is complete when a top-up needs nothing more.
State: results/study/state.json (passes, cohorts, job ids); every decision is also in topup-log.csv.
Settings: NBF_TARGET (analysable resources per platform, default 400).
Study rounds (study.env in this folder, KEY=VALUE; the environment wins):
  NBF_STUDY=study-r2                       results/study-r2/ (state, passes study-r2-<n>-<kind>, merged.sqlite)
  NBF_DRAW_DIR=data/sampling/draw/<date>-r2 the draw of this round
  NBF_FIRST_COHORT=cohort-all.csv          pass 1 (default cohort-primary.csv)
  NBF_RERUN_OF=data/sampling/draw/<date>   prepare NBF_DRAW_DIR from this draw if it does not exist
                                           (pipeline/prepare_rerun.py: the whole cohort again, fresh top-up log)
  NBF_DRAW_FROM=data/draw-<name>           or copy NBF_DRAW_DIR once from this folder of the bundle (a draw
                                           folder prepared on the workstation, e.g. round 3 with the Zenodo
                                           reserve extension; sync_from_repo.sh puts it there)
A named NBF_DRAW_DIR that is missing and cannot be prepared stops study.py; it never starts a new draw.
"""
from __future__ import annotations

import csv
import getpass
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent


def load_study_env():
    """study.env (KEY=VALUE) names the study round; variables already set in the environment win."""
    path = BUNDLE / 'study.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))


load_study_env()
RESULTS = BUNDLE / 'results'
STUDY = RESULTS / os.environ.get('NBF_STUDY', 'study')      # a study round: its passes, state and merged database
DRAW_DIR = os.environ.get('NBF_DRAW_DIR')                   # default: the latest draw
FIRST_COHORT = os.environ.get('NBF_FIRST_COHORT', 'cohort-primary.csv')   # in the draw folder: pass 1
DRAW_FROM = os.environ.get('NBF_DRAW_FROM')                 # a bundle folder: NBF_DRAW_DIR is copied from it
RERUN_OF = os.environ.get('NBF_RERUN_OF')                   # a draw folder: NBF_DRAW_DIR is prepared from it
STATE = STUDY / 'state.json'
SAMPLING = BUNDLE / 'data' / 'sampling'
NBF_HOME = Path.home() / '.notebookfair'
TARGET = int(os.environ.get('NBF_TARGET', '400'))
SCREENED = int(os.environ.get('NBF_GITHUB_SCREENED', '650'))


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else dict(created=now(), passes=[])


def save_state(state):
    STUDY.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2) + '\n')
    tmp.replace(STATE)


def running_jobs():
    """Names of this user's queued or running jobs."""
    try:
        out = subprocess.run(['squeue', '-u', getpass.getuser(), '-h', '-o', '%j'], capture_output=True,
                             text=True, timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError) as error:
        sys.exit(f'squeue failed ({error}); run this on a the cluster login node')
    return set(out.split())


def sbatch(args, dry):
    command = ['sbatch', *args]
    print('  $', ' '.join(command))
    if dry:
        return None
    out = subprocess.run(command, cwd=BUNDLE, capture_output=True, text=True)
    print('   ', (out.stdout or out.stderr).strip())
    if out.returncode != 0:
        sys.exit('sbatch failed')
    return out.stdout.strip().split()[-1]


def meta(db):
    import sqlite3
    if not Path(db).exists():
        return {}
    connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        return dict(connection.execute('SELECT key, value FROM meta').fetchall())
    except sqlite3.Error:
        return {}


def env_tokens():
    values = {}
    path = BUNDLE / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('"\'')
    return values


# --- stage checks --------------------------------------------------------------------------------
def sampling_status():
    status = {}
    status['probe'] = (SAMPLING / 'pilot' / 'pilot-power.json').exists() and any(SAMPLING.glob('probes/*/probes.csv'))
    m = meta(SAMPLING / 'codeberg' / 'census.sqlite')
    status['codeberg'] = bool({'languages_finished', 'languages_stopped_early'} & set(m)) and \
        (SAMPLING / 'codeberg' / 'summary.json').exists()
    m = meta(SAMPLING / 'zenodo' / 'frame.sqlite')
    status['zenodo'] = all(f'enumerate_{s}_finished' in m for s in ('Z1', 'Z2', 'Z3')) and \
        all(any(k.startswith(f'inspection_{s}_') and k.endswith('_done') for k in m) for s in ('Z2', 'Z3')) and \
        (SAMPLING / 'zenodo' / 'summary.json').exists()
    github = SAMPLING / 'github' / 'ids.sqlite'
    screened = 0
    if github.exists():
        import sqlite3
        try:
            screened = sqlite3.connect(f'file:{github}?mode=ro', uri=True).execute(
                'SELECT COUNT(*) FROM languages WHERE jupyter_bytes > 0').fetchone()[0]
        except sqlite3.Error:
            screened = 0
    status['github'] = screened >= SCREENED and (SAMPLING / 'github' / 'ids-summary.json').exists()
    return status, screened


def latest_draw():
    if DRAW_DIR:
        draw = BUNDLE / DRAW_DIR
        if not draw.exists() and DRAW_FROM and (BUNDLE / DRAW_FROM / 'cohort-primary.csv').exists():
            # a draw folder prepared on the workstation (shipped in the bundle): copied once, never overwritten,
            # because the top-ups append to the copy (topup-log.csv)
            shutil.copytree(BUNDLE / DRAW_FROM, draw)
            print(f'  draw folder {DRAW_DIR} copied from {DRAW_FROM}')
        elif not draw.exists() and RERUN_OF and (BUNDLE / RERUN_OF / 'cohort-primary.csv').exists():
            # a re-run round: same sample, fresh runs (pipeline/prepare_rerun.py)
            subprocess.run([sys.executable, str(BUNDLE / 'pipeline' / 'prepare_rerun.py'), '--draw-dir',
                            str(BUNDLE / RERUN_OF), '--suffix', draw.name.rsplit('-', 1)[-1],
                            '--reason', os.environ.get('NBF_RERUN_REASON', 're-run of the whole cohort')], check=True)
        if not (draw / 'cohort-primary.csv').exists():
            # a named draw that is missing must never lead to a new draw
            sys.exit(f'NBF_DRAW_DIR={DRAW_DIR} has no cohort-primary.csv (and no NBF_DRAW_FROM/NBF_RERUN_OF to '
                     'prepare it from); nothing submitted')
        return draw
    draws = sorted(p.parent for p in SAMPLING.glob('draw/*/cohort-primary.csv'))
    return draws[-1] if draws else None


def count_rows(path):
    with open(path, newline='', encoding='utf-8') as stream:
        return sum(1 for _ in csv.DictReader(stream))


# --- commands ---------------------------------------------------------------------------------------
def status_report():
    state, jobs = load_state(), running_jobs()
    print(f'Study folder: {BUNDLE}   round {STUDY.name} (results/{STUDY.name}/)   target {TARGET} analysable '
          f'resources per platform')
    env = (NBF_HOME / 'env.sh').exists()
    tokens = env_tokens()
    print(f'  setup      env.sh {"ok" if env else "MISSING (bash pipeline/hpc_setup_cluster.sh)"}; '
          f'.env GITHUB_TOKEN {"set" if tokens.get("GITHUB_TOKEN") else "MISSING"}, '
          f'ZENODO_TOKEN {"set" if tokens.get("ZENODO_TOKEN") else "missing (optional)"}')
    sif = (NBF_HOME / 'licensee-10.1.0.sif').exists()
    print(f'  prepare    {"done" if (NBF_HOME / "prepare.ok").exists() else "running" if "nbf-prepare" in jobs else "to do"}'
          f'; licensee SIF {"ok" if sif else "missing (licences recorded as unavailable)"}')
    sampling, screened = sampling_status()
    for step, done in sampling.items():
        extra = f' ({screened}/{SCREENED} screened-in)' if step == 'github' else ''
        print(f'  sampling   {step:9} {"done" if done else "running" if f"nbf-sampling-{step}" in jobs else "to do"}{extra}')
    draw = latest_draw()
    print(f'  draw       {draw.relative_to(BUNDLE) if draw else ("running" if "nbf-sampling-draw" in jobs else "to do")}')
    for p in state['passes']:
        print(f"  pass {p['index']:<5} {p['run']:24} {p['resources']:>5} resources  "
              f"{'running' if 'nbf-study' in jobs and p is state['passes'][-1] and not p.get('evaluated') else 'evaluated' if p.get('evaluated') else 'finished, to evaluate'}"
              + (f"  -> {p['next']}" if p.get('next') else ''))
    if state.get('complete'):
        print(f"  COMPLETE   {state['complete']}: results/{STUDY.name}/merged.sqlite")
    errors = STUDY / 'driver-errors.log'
    failed = sum(line.startswith('=== [') for line in errors.read_text().splitlines()) if errors.exists() else 0
    print(f"  driver     {'running' if 'nbf-driver' in jobs else 'not running'}"
          f"{f'; {failed} failed round(s) in results/{STUDY.name}/driver-errors.log' if failed else ''}")
    return state, jobs


def next_stage(dry=False):
    state, jobs = status_report()
    print('\nNext:')
    if not (NBF_HOME / 'env.sh').exists():
        sys.exit('  run bash pipeline/hpc_setup_cluster.sh on the login node first')
    if not (NBF_HOME / 'prepare.ok').exists():
        if 'nbf-prepare' not in jobs:
            RESULTS.mkdir(exist_ok=True)
            sbatch(['prepare.sbatch'], dry)
        else:
            print('  prepare is running')
        return
    sampling, _ = sampling_status()
    pending = [s for s, done in sampling.items() if not done]
    if pending:
        tokens = env_tokens()
        for step in pending:
            if f'nbf-sampling-{step}' in jobs:
                print(f'  sampling {step} is running')
                continue
            if step == 'github' and not tokens.get('GITHUB_TOKEN'):
                print('  sampling github waits for GITHUB_TOKEN in .env')
                continue
            time = {'probe': '00:30:00'}.get(step, '3-00:00:00')
            sbatch([f'--job-name=nbf-sampling-{step}', f'--time={time}', 'sampling.sbatch', step], dry)
        return
    draw = latest_draw()
    if draw is None:
        if 'nbf-sampling-draw' not in jobs:
            sbatch(['--job-name=nbf-sampling-draw', '--time=04:00:00', 'sampling.sbatch', 'draw'], dry)
        else:
            print('  the draw is running')
        return
    if state.get('complete'):
        print('  nothing: the study is complete (classification: bash study.sh classify)')
        return
    if 'nbf-study' in jobs:
        print('  a pass is running; run this again when it has finished')
        return
    passes = state['passes']
    if not passes:
        submit_pass(state, 1, 'main', draw / FIRST_COHORT, dry)
        return
    last = passes[-1]
    if last.get('evaluated'):
        if last.get('next_cohort'):     # the top-up was done but submitting its pass failed: submit it now
            print(f"  submitting {last['next']} (its submission did not go through before)")
            submit_pass(state, last['index'] + 1, last['next_kind'], BUNDLE / last['next_cohort'], dry)
        else:
            print('  nothing to do')
        return
    # the last pass has finished: merge all passes, then top up
    runs = [str(RESULTS / p['run']) for p in passes]
    if not any((RESULTS / p['run']).glob('task-*/db.sqlite') for p in passes):
        sys.exit(f"  no task database in results/{last['run']}: the pass produced nothing. Check "
                 f"results/nbf-study-*.out and results/{last['run']}/task-*/; then run this again")
    print(f'  merging {len(runs)} pass(es) into results/study/merged.sqlite, then the top-up')
    if dry:
        return
    subprocess.run(['bash', str(BUNDLE / 'merge.sh'), '--study', str(STUDY / 'merged.sqlite'), *runs], check=True)
    if (BUNDLE / 'MANIFEST.txt').exists():           # the pipeline commit, next to the database (read by the analysis)
        (STUDY / 'MANIFEST.txt').write_text((BUNDLE / 'MANIFEST.txt').read_text())
    subprocess.run([sys.executable, str(BUNDLE / 'pipeline' / 'topup_from_reserve.py'), '--draw-dir', str(draw),
                    '--db', str(STUDY / 'merged.sqlite'), '--target', str(TARGET)], check=True)
    summaries = sorted(draw.glob('topup-*-summary.json'), key=lambda p: int(p.stem.split('-')[1]))
    summary = json.loads(summaries[-1].read_text()) if summaries else {}
    files = [draw / f for f in (summary.get('topup_cohort'), summary.get('rerun_cohort')) if f]
    last['evaluated'], last['topup'] = now(), summary.get('topup')
    if not files:
        state['complete'] = now()
        last['next'] = 'complete'
        save_state(state)
        print('  study complete: ' + json.dumps({p: v.get('outcomes') for p, v in summary.get('platforms', {}).items()}))
        return
    combined = STUDY / f"cohort-pass-{len(passes) + 1}.csv"
    rows = []
    for path in files:
        with open(path, newline='', encoding='utf-8') as stream:
            rows += list(csv.DictReader(stream))
    with open(combined, 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['category', 'platform', 'url'])
        writer.writeheader()
        writer.writerows(rows)
    last['next'] = f'pass {len(passes) + 1}'
    last['next_kind'] = f"topup{summary.get('topup')}"
    last['next_cohort'] = str(combined.relative_to(BUNDLE))
    save_state(state)
    submit_pass(state, len(passes) + 1, last['next_kind'], combined, dry)


def submit_pass(state, index, kind, cohort, dry):
    n = count_rows(cohort)
    tasks = max(1, min(32, n))
    run = f'{STUDY.name}-{index}-{kind}'
    job = sbatch(['--job-name=nbf-study', f'--array=0-{tasks - 1}%16',
                  f'--export=ALL,NBF_COHORT={cohort.relative_to(BUNDLE)},NBF_RUN_NAME={run}', 'run_array.sbatch'], dry)
    if not dry:
        if state['passes']:
            state['passes'][-1].pop('next_cohort', None)       # submitted: nothing pending any more
        state['passes'].append(dict(index=index, kind=kind, run=run, cohort=str(cohort.relative_to(BUNDLE)),
                                    resources=n, tasks=tasks, job=job, submitted=now()))
        save_state(state)


def pilot(dry=False):
    jobs = running_jobs()
    if 'nbf-pilot' in jobs:
        print('pilot is running')
    elif (RESULTS / 'pilot-318').exists():
        print('pilot done: bash merge.sh results/pilot-318')
    else:
        sbatch(['--job-name=nbf-pilot', '--export=ALL,NBF_COHORT=data/cohort-318.csv,NBF_RUN_NAME=pilot-318',
                'run_array.sbatch'], dry)


def autopilot(dry=False):
    """Submit the unattended driver (study_driver.sbatch), unless one is queued or running."""
    if 'nbf-driver' in running_jobs():
        print('the driver is already queued or running (results/study/driver.log; stop: scancel --name=nbf-driver)')
        return
    RESULTS.mkdir(exist_ok=True)
    sbatch(['study_driver.sbatch'], dry)
    print('driver submitted: it runs `next` every 30 minutes until the study is complete.\n'
          '  progress: bash study.sh status; tail results/study/driver.log; errors: results/study/driver-errors.log')


def classify(dry=False):
    if not (STUDY / 'merged.sqlite').exists():
        sys.exit('no results/study/merged.sqlite yet')
    sbatch(['classify.sbatch', str(STUDY.relative_to(BUNDLE))], dry)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'status'
    dry = '--dry-run' in sys.argv
    {'status': lambda d: status_report(), 'next': next_stage, 'pilot': pilot, 'classify': classify,
     'autopilot': autopilot}.get(
        command, lambda d: sys.exit(__doc__))(dry)


if __name__ == '__main__':
    main()
