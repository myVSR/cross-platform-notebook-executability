#!/usr/bin/env python3
"""Install a resource's dependencies into its venv and report what happened to every package.

Usage: install_env.py --python VENV_PYTHON --plan DEPENDENCIES_JSON --out-dir DIR [--timeout SECONDS]

Time travel (--resource-date): every pip call that resolves packages (declared ranges,
extras, setuptools/wheel, ipykernel) and every isolated build environment only sees
releases uploaded before the cutoff, via PIP_UPLOADED_PRIOR_TO (pip >= 26). The cutoff is
the end of the resource's version date (Git commit / Zenodo deposit). If the selected
Python is newer than the requested one (raised into the supported range) or was released
after the resource date, the cutoff is moved to one year after that Python's release, so
that wheels for it exist; the reason is recorded.

Order (each step recorded):
  1. declared   the install plan of src/dependencies.py (requirements files, environment.yml,
                pyproject, setup.cfg/py, Pipfile, %pip lines), in ONE pip call so pip resolves
                them together. When pip names the requirement it cannot satisfy ("No matching
                distribution found for X"), X is set aside and the joint call is repeated (up
                to 10 times); what remains unresolvable is installed one package at a time
  2. probe      which third-party modules imported by the notebooks are still not importable
                (importlib.util.find_spec on the top-level name; nothing is imported)
  3. extra      for each still-missing module, its mapped distribution, one pip call each
                ("inferred": imported by the notebooks but declared nowhere, or declared
                and failed under another name)
  4. kernel     ipykernel, last, so the resource's pins win; packages it changes are recorded
  5. inspect    pip freeze --all, pip check, and the installed version of every package
Writes to DIR: declared-requirements.txt, extra-requirements.txt, pip-freeze.txt, pip-check.txt,
install.log and install-report.json. Nothing is written into the resource.

Package index: pip is isolated from the machine's pip configuration (PIP_CONFIG_FILE=/dev/null, all other
PIP_* variables of the caller dropped) and uses PyPI only (NBF_PIP_INDEX_URL, default https://pypi.org/simple),
which provides the upload-time metadata the cutoff needs. Index options of the resource (-i, --extra-index-url,
--find-links, --trusted-host in a requirements file) are not used while a cutoff applies, for the same reason;
they are recorded. Proxy variables (https_proxy, ...) are kept. The effective index is recorded in the report.
(Study run 2026-10: the cluster's pip config pointed at a mirror without upload-time metadata, behind a proxy
that often answered 500; 51% of the runs lost packages that way.)

Network failures: pip retries every request (PIP_RETRIES, default 10). An install that still fails because the
index could not be reached is retried after a pause (NBF_PIP_NETWORK_RETRIES, default 3: 30 s, 90 s, 270 s). If it
still fails, the index counts as unreachable for this run: the remaining installs are skipped and the exit status
is 75, which the pipeline records as INSTALL_NETWORK_FAIL (a pipeline error: the resource is run again).
Exit status 0 even when packages fail (failures are data); 1 only if pip itself is unusable; 75 as above.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# First final release of each CPython minor version (python.org)
PYTHON_RELEASES = {(3, 8): date(2019, 10, 14), (3, 9): date(2020, 10, 5), (3, 10): date(2021, 10, 4),
                   (3, 11): date(2022, 10, 24), (3, 12): date(2023, 10, 2), (3, 13): date(2024, 10, 7),
                   (3, 14): date(2025, 10, 7)}


def minor_of(version):
    m = re.match(r'^(\d+)\.(\d+)', version or '')
    return (int(m.group(1)), int(m.group(2))) if m else None


def compute_cutoff(resource_date, selected, requested):
    """(ISO cutoff or None, reason)."""
    if not resource_date:
        return None, 'no resource date recorded; packages as of today'
    try:
        moment = datetime.fromisoformat(resource_date.replace('Z', '+00:00'))
        day = (moment if moment.tzinfo is None else moment.astimezone(timezone.utc)).date()   # no zone: as given
    except ValueError:
        return None, f'resource date {resource_date!r} not understood; packages as of today'
    cutoff, reason = day, f'end of the resource version date {day.isoformat()}'
    sel, req = minor_of(selected), minor_of(requested)
    release = PYTHON_RELEASES.get(sel)
    if release and (day < release or (req and sel and sel > req)):
        floor = release + timedelta(days=365)
        if floor > cutoff:
            why = (f'Python {selected} runs instead of the requested {requested}' if req and sel > req
                   else f'Python {selected} was released after the resource date')
            cutoff, reason = floor, f'{why}; cutoff moved to one year after its release ({release.isoformat()}) so that wheels exist'
    return f'{cutoff.isoformat()}T23:59:59Z', reason

PROBE = ('import importlib.util, json, sys\n'
         'result = {}\n'
         'for name in json.loads(sys.argv[1]):\n'
         '    try:\n'
         '        result[name] = importlib.util.find_spec(name) is not None\n'
         '    except Exception:\n'
         '        result[name] = False\n'
         'print(json.dumps(result))\n')


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


# The pipeline's own PYTHONPATH (project root) must not influence pip or the import probe.
CLEAN_ENV = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
INDEX_URL = os.environ.get('NBF_PIP_INDEX_URL', 'https://pypi.org/simple')
NETWORK_EXIT = 75
RESOURCE_INDEX_OPTIONS = ('--index-url', '-i', '--extra-index-url', '--find-links', '-f', '--trusted-host')
# pip's messages when the index could not be reached (after its own retries)
NETWORK_HINT = re.compile(r'Tunnel connection failed|connection broken|Read timed out|ProxyError|NewConnectionError|'
                          r'Temporary failure in name resolution|Max retries exceeded|Connection reset by peer|'
                          r'Network is unreachable|ConnectTimeoutError|SSLError', re.I)
NETWORK_FINAL = re.compile(r'\(from versions: none\)|ERROR:.*(Max retries exceeded|ProxyError|Tunnel connection failed|'
                           r'ConnectionError|Read timed out|connection broken)', re.I)
INDEX_PROBLEM = 'does not provide upload-time metadata'


def pip_environment(base=None):
    """The environment for every pip call: no machine pip config, PyPI only, generous retries, proxies kept."""
    base = base or CLEAN_ENV
    env = {k: v for k, v in base.items() if not k.startswith('PIP_')}
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_INDEX_URL=INDEX_URL,
               PIP_RETRIES=os.environ.get('NBF_PIP_RETRIES', base.get('PIP_RETRIES', '10')),
               PIP_DEFAULT_TIMEOUT=os.environ.get('NBF_PIP_TIMEOUT', base.get('PIP_DEFAULT_TIMEOUT', '60')))
    return env


def network_failure(code, output):
    """True if a failed pip call failed because the index could not be reached (not because of the package)."""
    if code == 0:
        return False
    return INDEX_PROBLEM in output or bool(NETWORK_HINT.search(output) and NETWORK_FINAL.search(output))


LOCAL_VERSION = re.compile(r'(===?\s*[A-Za-z0-9_.!*-]+)\+[A-Za-z0-9_.]+')


def without_local_version(spec):
    """(spec, note): PyPI hosts no local versions (torch==2.5.1+cu124, lz4==3.1.3+dfsg), so a pin with a local label
    cannot be installed from it; the label is removed and recorded (study run 2026-10: 9 runs)."""
    stripped = LOCAL_VERSION.sub(r'\1', spec)
    if stripped == spec:
        return spec, None
    labels = ', '.join(m.group(0).split('+', 1)[1] for m in LOCAL_VERSION.finditer(spec))
    return stripped, f'local version label +{labels} removed: PyPI has no local versions'


def split_resource_options(options, cutoff):
    """(options used, options set aside): resource indexes are not used while a cutoff applies."""
    if not cutoff:
        return list(options), []
    used, dropped = [], []
    for option in options:
        flag = option.strip().split(None, 1)[0].split('=', 1)[0] if option.strip() else ''
        (dropped if flag in RESOURCE_INDEX_OPTIONS else used).append(option)
    return used, dropped


def proxy_hosts(env):
    """Proxy settings without credentials, for the report."""
    out = {}
    for key in ('https_proxy', 'HTTPS_PROXY', 'http_proxy', 'HTTP_PROXY', 'no_proxy', 'NO_PROXY'):
        if env.get(key):
            out[key] = re.sub(r'//[^@/]*@', '//***@', env[key])
    return out


class Installer:
    def __init__(self, python, out_dir, timeout, options, cutoff=None, network_retries=None, pause=30):
        self.python, self.out, self.timeout, self.options = python, out_dir, timeout, options
        self.env = pip_environment()
        if cutoff:   # also reaches pip's isolated build environments
            self.env['PIP_UPLOADED_PRIOR_TO'] = cutoff
        self.network_retries = int(os.environ.get('NBF_PIP_NETWORK_RETRIES', '3')) if network_retries is None \
            else network_retries
        self.pause = pause
        self.network_down, self.network_events = False, []
        self.log = (out_dir / 'install.log').open('a', encoding='utf-8')

    def pip(self, *args, timeout=None):
        command = [self.python, '-m', 'pip', *args]
        self.log.write(f'\n$ {" ".join(command)}\n')
        self.log.flush()
        started = time.monotonic()
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout or self.timeout, env=self.env)
            output, code = result.stdout + result.stderr, result.returncode
        except subprocess.TimeoutExpired as error:
            output, code = f'{error.stdout or ""}{error.stderr or ""}\npip timed out after {timeout or self.timeout}s', 124
        self.log.write(output if isinstance(output, str) else output.decode(errors='replace'))
        self.log.flush()
        return code, output if isinstance(output, str) else output.decode(errors='replace'), round(time.monotonic() - started, 3)

    def install(self, *targets):
        if self.network_down:
            return NETWORK_EXIT, 'skipped: the package index could not be reached earlier in this run', 0.0
        args = ['install', '--no-input', '--disable-pip-version-check', '--progress-bar', 'off']
        for option in self.options:
            args += option.split(None, 1)
        total = 0.0
        for attempt in range(self.network_retries + 1):
            code, output, seconds = self.pip(*args, *targets)
            total += seconds
            if not network_failure(code, output):
                return code, output, round(total, 3)
            event = dict(targets=list(targets), attempt=attempt + 1, at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                         error=error_excerpt(output))
            self.network_events.append(event)
            if attempt < self.network_retries:
                wait = self.pause * 3 ** attempt
                self.log.write(f'\n[network] the index could not be reached; retrying in {wait} s\n')
                self.log.flush()
                time.sleep(wait)
        self.network_down = True
        self.log.write('\n[network] the index is unreachable: the remaining installs of this run are skipped\n')
        self.log.flush()
        return code, output, round(total, 3)

    def probe(self, modules):
        if not modules:
            return {}
        result = subprocess.run([self.python, '-c', PROBE, json.dumps(sorted(modules))], capture_output=True,
                                text=True, timeout=300, env=CLEAN_ENV)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {m: False for m in modules}

    def freeze(self):
        code, output, _ = self.pip('freeze', '--all', timeout=300)
        versions = {}
        for line in output.splitlines():
            m = re.match(r'^([A-Za-z0-9][\w.-]*)\s*(?:==|\s@\s)\s*(.+)$', line.strip())
            if m:
                versions[normalize(m.group(1))] = m.group(2).strip()
        return output, versions


def error_excerpt(output):
    lines = [l for l in output.splitlines() if re.search(r'error|Error|ERROR|No matching|conflict|Could not', l)]
    return ' | '.join(lines[-4:])[:600] if lines else output.strip()[-300:]


def run(python, plan, out, timeout, resource_date=None, selected=None, requested=None):
    out.mkdir(parents=True, exist_ok=True)
    cutoff, cutoff_reason = compute_cutoff(resource_date, selected, requested)
    installer = Installer(python, out, timeout, [])
    code, version, _ = installer.pip('--version', timeout=60)
    if code != 0:
        return dict(status='pip unusable', error=version[-300:]), 1
    if cutoff:
        _, help_text, _ = installer.pip('install', '--help', timeout=60)
        if '--uploaded-prior-to' not in help_text:
            # the venv's pip is too old (its upgrade by pyenv.sh may have failed): upgrade it here, with the network
            # retries, so that a passing outage does not silently switch the time travel off. Python 3.8 cannot run
            # pip >= 26, so it stays without a cutoff (recorded).
            _, upgrade_output, _ = installer.install('--upgrade', 'pip')
            # pip exits 0 ("Requirement already satisfied") when it cannot reach the index: look at its warnings
            if not installer.network_down and NETWORK_HINT.search(upgrade_output) and minor_of(selected) != (3, 8):
                installer.network_down = True
                installer.network_events.append(dict(targets=['--upgrade', 'pip'], attempt=1, error=error_excerpt(upgrade_output),
                                                     at=datetime.now(timezone.utc).isoformat(timespec='seconds')))
            if installer.network_down:
                report = dict(status='network failure', error='pip could not be upgraded: the package index could '
                              'not be reached', packages=[], uploaded_prior_to=None, cutoff_reason=cutoff_reason,
                              index=dict(index_url=INDEX_URL, config_file=os.devnull, proxies=proxy_hosts(installer.env)),
                              network=dict(unreachable=True, events=installer.network_events))
                return report, NETWORK_EXIT
            code, version, _ = installer.pip('--version', timeout=60)
            _, help_text, _ = installer.pip('install', '--help', timeout=60)
        if '--uploaded-prior-to' in help_text:
            installer.env['PIP_UPLOADED_PRIOR_TO'] = cutoff
        else:
            cutoff, cutoff_reason = None, f'pip {version.split()[1:2]} has no --uploaded-prior-to; packages as of today'
    pip_options, dropped_options = split_resource_options(plan.get('pip_options', []), cutoff)
    installer.options = [o for o in pip_options if not o.lstrip().startswith('#')]
    _, config_debug, _ = installer.pip('config', 'debug', timeout=60)
    index = dict(index_url=INDEX_URL, config_file=os.devnull, retries=installer.env['PIP_RETRIES'],
                 timeout=installer.env['PIP_DEFAULT_TIMEOUT'], proxies=proxy_hosts(installer.env),
                 caller_pip_variables=sorted(k for k in CLEAN_ENV if k.startswith('PIP_')),
                 resource_options_not_used=dropped_options, pip_config_debug=config_debug.strip()[:2000])

    # 0. build tools as of the cutoff (setuptools still with pkg_resources for old projects)
    build_tools = []
    for tool in ('setuptools', 'wheel'):
        code, output, seconds = installer.install('--upgrade', tool)
        build_tools.append(dict(name=tool, spec=tool, origin='build tools', origin_kind='pipeline', origin_files=[],
                                imports=[], decision='install', reason='build tools for source distributions'
                                + (f', as of {cutoff[:10]}' if cutoff else ''), method='before the resource packages',
                                status='installed' if code == 0 else 'failed', error=None if code == 0 else error_excerpt(output),
                                seconds=seconds, conflicts=[]))

    packages = []
    for req in plan.get('requirements', []):
        spec, note = without_local_version(req['spec'])
        packages.append(dict(name=req['name'], spec=spec, origin=req['origin'], origin_kind=req['origin_kind'],
                             origin_files=req['origin_files'], conflicts=req.get('conflicts', []),
                             decision=req['decision'], reason=req['reason'], method=None, status='skipped'
                             if req['decision'] != 'install' else None, error=None, seconds=None, imports=[],
                             **({'declared_spec': req['spec'], 'note': note} if note else {})))
    imports = [i for i in plan.get('imports', []) if i.get('classification') == 'third-party']
    by_dist = {}
    for item in imports:
        by_dist.setdefault(normalize(item['distribution']), []).append(item)
    for package in packages:
        package['imports'] = sorted({i['module'] for i in by_dist.get(package['name'], [])})

    # 1. declared, jointly
    declared = [p for p in packages if p['decision'] == 'install']
    (out / 'declared-requirements.txt').write_text('\n'.join(pip_options + [p['spec'] for p in declared]) + '\n')
    if declared:
        remaining, set_aside = list(declared), []
        for attempt in range(1, 12):
            requirements = out / f'declared-requirements.attempt{attempt}.txt'
            requirements.write_text('\n'.join(pip_options + [p['spec'] for p in remaining]) + '\n')
            code, output, seconds = installer.install('-r', str(requirements))
            if code == 0:
                for p in remaining:
                    p.update(method='joint' if attempt == 1 else f'joint (attempt {attempt}, after setting aside '
                             f'{", ".join(x["name"] for x in set_aside)})', status='installed', seconds=seconds)
                break
            culprits = {normalize(m) for m in re.findall(r'No matching distribution found for ([A-Za-z0-9][\w.-]*)', output)}
            culprits |= {normalize(m) for m in re.findall(r'Could not find a version that satisfies the requirement '
                                                            r'([A-Za-z0-9][\w.-]*)', output)}
            failing = [p for p in remaining if p['name'] in culprits]
            if not failing or attempt == 11:
                break
            for p in failing:
                p.update(method='set aside from the joint install', status='failed', error=error_excerpt(output),
                         seconds=seconds)
            set_aside += failing
            remaining = [p for p in remaining if p not in failing]
        else:
            code = 1
        if code != 0:                    # unresolvable as a whole (conflicts, build failures): one at a time
            joint_error = error_excerpt(output)
            for p in remaining:
                code, output, seconds = installer.install(p['spec'])
                p.update(method='one by one (joint install failed)', status='installed' if code == 0 else 'failed',
                         error=None if code == 0 else error_excerpt(output), seconds=seconds, joint_error=joint_error)

    # 2. probe, 3. extras
    tops = {i['top'] for i in imports}
    available = installer.probe(tops)
    attempted = {p['name'] for p in packages}
    extras = []
    for top in sorted(t for t, ok in available.items() if not ok):
        items = [i for i in imports if i['top'] == top]
        distribution = items[0]['distribution']
        name = normalize(distribution)
        reason = (f"imported as {', '.join(sorted({i['module'] for i in items}))} in "
                  f"{', '.join(sorted({i['notebook'] for i in items}))}")
        if name in attempted:
            for p in packages:
                if p['name'] == name and p['status'] == 'installed':
                    p['note'] = f'installed, but module {top} is still not importable'
            continue
        attempted.add(name)
        code, output, seconds = installer.install(distribution)
        extra = dict(name=name, spec=distribution, origin='inferred', origin_kind='notebook imports',
                     origin_files=sorted({i['notebook'] for i in items}), conflicts=[], decision='install',
                     reason=reason + '; not provided by the declared packages',
                     method='one by one', status='installed' if code == 0 else 'failed',
                     error=None if code == 0 else error_excerpt(output), seconds=seconds,
                     imports=sorted({i['module'] for i in items}), mapping=items[0].get('mapping'))
        extras.append(extra)
    packages += extras + build_tools
    (out / 'extra-requirements.txt').write_text('\n'.join(e['spec'] for e in extras) + ('\n' if extras else ''))
    available_after = installer.probe(tops)

    # 4. kernel
    _, before = installer.freeze()
    kernel = dict(package='ipykernel', status='already installed')
    if not installer.probe({'ipykernel'}).get('ipykernel'):
        code, output, seconds = installer.install('ipykernel')
        kernel.update(status='installed' if code == 0 else 'failed', error=None if code == 0 else error_excerpt(output),
                      seconds=seconds)
    freeze_text, after = installer.freeze()
    kernel['changed'] = sorted(f'{n} {before.get(n, "-")} -> {after.get(n, "-")}'
                               for n in set(before) | set(after) if before.get(n) != after.get(n))
    kernel['version'] = after.get('ipykernel')

    # 5. inspect
    (out / 'pip-freeze.txt').write_text(freeze_text)
    code, check_output, _ = installer.pip('check', timeout=300)
    (out / 'pip-check.txt').write_text(check_output)
    for p in packages:
        p['installed_version'] = after.get(p['name'])
        if p['status'] == 'failed' and p['installed_version']:
            p['note'] = (p.get('note') or '') + 'another version is installed (pulled in by other packages)'
    for p in packages:
        error = p.get('error') or ''
        if p['status'] == 'failed' and (network_failure(1, error) or error.startswith('skipped: the package index')):
            p['failure_kind'] = 'network'
    report = dict(status='network failure' if installer.network_down else 'done',
                  pip_version=version.split()[1] if version.split()[1:] else version.strip(),
                  uploaded_prior_to=cutoff, cutoff_reason=cutoff_reason, index=index,
                  network=dict(unreachable=installer.network_down, events=installer.network_events,
                               retries_after_pip=installer.network_retries),
                  packages=packages, modules_available_before_extras=available, modules_available=available_after,
                  kernel=kernel, pip_check_ok=code == 0, pip_check=check_output.strip()[:5000],
                  freeze=freeze_text, summary=dict(
                      declared=len(declared), declared_failed=sum(p['status'] == 'failed' for p in declared),
                      skipped=sum(p['status'] == 'skipped' for p in packages), extras=len(extras),
                      extras_failed=sum(e['status'] == 'failed' for e in extras),
                      modules_missing=sorted(m for m, ok in available_after.items() if not ok)))
    return report, NETWORK_EXIT if installer.network_down else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--python', required=True, help="the resource venv's python")
    parser.add_argument('--plan', type=Path, required=True, help='JSON from src/dependencies.py')
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=3600, help='seconds per pip call')
    parser.add_argument('--resource-date', help='ISO date of the resource version (enables time travel)')
    parser.add_argument('--python-selected', help='Python version the venv runs (e.g. 3.8.20)')
    parser.add_argument('--python-requested', help='Python version the resource asked for (e.g. 3.6)')
    args = parser.parse_args(argv)
    report, status = run(args.python, json.loads(args.plan.read_text()), args.out_dir, args.timeout,
                         args.resource_date, args.python_selected, args.python_requested)
    (args.out_dir / 'install-report.json').write_text(json.dumps(report, indent=1))
    summary = report.get('summary', {})
    print(f"[INSTALL] packages uploaded before {report.get('uploaded_prior_to') or 'today'} "
          f"({report.get('cutoff_reason', '')})")
    print(f"[INSTALL] declared {summary.get('declared', 0)} (failed {summary.get('declared_failed', 0)}), "
          f"extras {summary.get('extras', 0)} (failed {summary.get('extras_failed', 0)}), "
          f"modules still missing: {', '.join(summary.get('modules_missing', [])) or 'none'}")
    print(f"[INSTALL] index {report.get('index', {}).get('index_url', '?')} (machine pip config not used)")
    if status == NETWORK_EXIT:
        print('[INSTALL] the package index could not be reached: INSTALL_NETWORK_FAIL (the resource is run again)')
    return status


if __name__ == '__main__':
    sys.exit(main())
