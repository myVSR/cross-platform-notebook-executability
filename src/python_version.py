#!/usr/bin/env python3
"""Choose the Python version for a resource, and say why.

Usage: python_version.py REPO_DIR [--notebooks "a.ipynb;b/c.ipynb"] [--json]
Prints the chosen minor version (e.g. 3.10) or, with --json, the full decision.

Evidence (each recorded with its file and raw text):
  pins         runtime.txt (root, binder/, .binder/)  python-3.8[.10]
               .python-version                        3.8 / 3.8.10 (other text, e.g. a
                                                      virtualenv name, is rejected)
               environment.yml (root, binder/, .binder/)  python=3.8 / python>=3.8
               Pipfile [requires]                     python_version / python_full_version
  constraints  pyproject.toml                         requires-python, poetry python = "^3.8"
               setup.cfg / setup.py                   python_requires
  observed     notebooks' metadata.language_info.version and kernelspec (python2/python3);
               only notebooks whose language is Python (Julia/R/... notebooks also have a version)

Decision:
  1. the first pin (order above), if it satisfies the constraints;
  2. else the most common minor version among the notebooks, if it satisfies the constraints;
  3. else the default (PYTHON_DEFAULT_VERSION, 3.10) if allowed;
  4. else the allowed minor nearest to the default.
  The result is moved into the range this host can build
  (PYTHON_MIN_SUPPORTED .. PYTHON_MAX_SUPPORTED, default 3.8 .. 3.14); the move is
  recorded. Python 2 evidence is flagged (python2 = true); the pipeline cannot run it.
Python 3.10+ standard library only.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

VERSION = re.compile(r'^(\d+)\.(\d+)(?:\.(\d+))?$')
SPEC = re.compile(r'^\s*(~=|===|==|!=|<=|>=|<|>|\^|~)?\s*v?([0-9]+(?:\.[0-9*]+){0,2})\s*$')


def parse_version(text):
    match = VERSION.match(text.strip())
    return tuple(int(g) for g in match.groups() if g is not None) if match else None


def minor(version):
    return f'{version[0]}.{version[1]}'


def settings():
    def version(name, default):
        return parse_version(os.environ.get(name) or default)
    return dict(default=version('PYTHON_DEFAULT_VERSION', '3.10'), low=version('PYTHON_MIN_SUPPORTED', '3.8'),
                high=version('PYTHON_MAX_SUPPORTED', '3.14'))


# --- version specifiers (PEP 440 subset + Poetry ^ and ~) -------------------

def _spec_ok(op, target, candidate):
    """Does candidate (major, minor, patch) satisfy one specifier?"""
    if '*' in target:                       # ==3.8.*  / !=3.8.*
        prefix = tuple(int(p) for p in target.split('.') if p != '*')
        matches = candidate[:len(prefix)] == prefix
        return matches if op in (None, '==', '===') else (not matches if op == '!=' else False)
    parts = tuple(int(p) for p in target.split('.'))
    full = parts + (0,) * (3 - len(parts))
    if op in (None, '==', '==='):
        return candidate[:len(parts)] == parts
    if op == '!=':
        return candidate[:len(parts)] != parts
    if op == '>=':
        return candidate >= full
    if op == '<=':
        return candidate[:len(parts)] <= parts
    if op == '>':
        return candidate > full
    if op == '<':
        return candidate < full
    if op in ('~=', '~'):                   # ~=3.8 -> >=3.8,<4 ; ~=3.8.1 -> >=3.8.1,<3.9 ; poetry ~3.8 -> <3.9
        if op == '~=' and len(parts) == 2:
            upper = (parts[0] + 1, 0, 0)
        else:
            upper = (parts[0], parts[1] + 1, 0) if len(parts) >= 2 else (parts[0] + 1, 0, 0)
        return full <= candidate < upper
    if op == '^':                           # poetry ^3.8 -> >=3.8,<4
        upper = (parts[0] + 1, 0, 0) if parts[0] > 0 else (0, parts[1] + 1, 0)
        return full <= candidate < upper
    return True


def parse_specifier(text):
    """List of (op, version) or None if the text is not a specifier we understand."""
    specs = []
    for part in re.split(r'[,\s]+(?=[<>=!~^])|,', text.strip().strip('"\'')):
        if not part.strip():
            continue
        match = SPEC.match(part)
        if not match:
            return None
        specs.append((match.group(1), match.group(2)))
    return specs or None


def allows_minor(specs, version):
    """True if some patch release of this minor version satisfies every specifier."""
    return any(all(_spec_ok(op, target, (version[0], version[1], patch)) for op, target in specs)
               for patch in range(0, 40))


# --- evidence ----------------------------------------------------------------

def read(path, limit=2_000_000):
    try:
        if path.is_file() and path.stat().st_size <= limit:
            return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        pass
    return None


def pins_and_constraints(root):
    evidence = []

    def add(kind, file, raw, value, accepted=True, note=None):
        evidence.append(dict(kind=kind, file=str(file.relative_to(root)) if isinstance(file, Path) else file,
                             raw=raw[:200], value=value, accepted=accepted, note=note))

    for folder in ('binder', '.binder', ''):
        path = root / folder / 'runtime.txt'
        text = read(path)
        if text is None:
            continue
        match = re.search(r'python-(\d+\.\d+(?:\.\d+)?)', text)
        if match:
            add('pin', path, text.strip(), match.group(1))
        else:
            add('pin', path, text.strip(), None, False, 'no python-X.Y line')

    path = root / '.python-version'
    text = read(path)
    if text is not None:
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith('#')]
        valid = [l for l in lines if parse_version(l)]
        if valid:
            add('pin', path, text.strip(), valid[0])
        else:
            add('pin', path, text.strip(), None, False, 'not a version number (e.g. a virtualenv name)')

    for folder in ('binder', '.binder', ''):
        for name in ('environment.yml', 'environment.yaml'):
            path = root / folder / name
            text = read(path)
            if text is None:
                continue
            for line in text.splitlines():
                match = re.match(r'^\s*-\s*(?:conda-forge::)?python\s*([=<>!~][^#\s]*)?\s*(?:#.*)?$', line)
                if not match:
                    continue
                spec = (match.group(1) or '').strip()
                exact = re.fullmatch(r'==?(\d+\.\d+(?:\.\d+)?)(?:\.\*)?', spec)
                if exact:
                    add('pin', path, line.strip(), exact.group(1))
                elif spec and parse_specifier(spec.replace('=', '==', 1) if re.match(r'^=\d', spec) else spec):
                    add('constraint', path, line.strip(), spec)
                break

    path = root / 'Pipfile'
    text = read(path)
    if text is not None:
        match = re.search(r'python_(?:full_)?version\s*=\s*["\'](\d+\.\d+(?:\.\d+)?)["\']', text)
        if match:
            add('pin', path, match.group(0), match.group(1))

    path = root / 'pyproject.toml'
    text = read(path)
    if text is not None:
        data = None
        if tomllib:
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                data = None
        spec = None
        if data is not None:
            spec = (data.get('project') or {}).get('requires-python') or \
                ((data.get('tool') or {}).get('poetry') or {}).get('dependencies', {}).get('python')
            spec = spec if isinstance(spec, str) else None
        else:
            match = re.search(r'^\s*(?:requires-python|python)\s*=\s*["\']([^"\']+)["\']', text, re.M)
            spec = match.group(1) if match else None
        if spec:
            add('constraint', path, spec, spec) if parse_specifier(spec) else \
                add('constraint', path, spec, None, False, 'specifier not understood')

    path = root / 'setup.cfg'
    text = read(path)
    if text is not None:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
            spec = parser.get('options', 'python_requires', fallback=None)
        except configparser.Error:
            spec = None
        if spec:
            add('constraint', path, spec, spec.strip()) if parse_specifier(spec) else \
                add('constraint', path, spec, None, False, 'specifier not understood')

    path = root / 'setup.py'
    text = read(path)
    if text is not None:
        match = re.search(r'python_requires\s*=\s*["\']([^"\']+)["\']', text)
        if match:
            spec = match.group(1)
            add('constraint', path, match.group(0), spec) if parse_specifier(spec) else \
                add('constraint', path, match.group(0), None, False, 'specifier not understood')
    return evidence


def notebook_versions(root, notebooks):
    observed = []
    for relative in notebooks:
        text = read(root / relative, limit=200_000_000)
        if text is None:
            continue
        try:
            metadata = json.loads(text).get('metadata') or {}
        except (json.JSONDecodeError, AttributeError):
            continue
        info = metadata.get('language_info') or {}
        kernelspec = metadata.get('kernelspec') or {}
        kernel = str(kernelspec.get('name') or '')
        language = str(info.get('name') or kernelspec.get('language') or '').lower()
        # Julia, R, Scala, ... notebooks also carry language_info.version; only Python counts.
        if language != 'python' and not (not language and kernel.lower().startswith('python')):
            continue
        version = parse_version(str(info.get('version') or ''))
        if version is None and kernel.lower() == 'python2':
            version = (2, 7)
        if version:
            observed.append(dict(notebook=relative, version=minor(version), kernel=kernel or None))
    return observed


# --- decision ------------------------------------------------------------------

def decide(root, notebooks=(), config=None):
    config = config or settings()
    root = Path(root)
    evidence = pins_and_constraints(root)
    observed = notebook_versions(root, [n for n in notebooks if n])
    specs = [s for e in evidence if e['kind'] == 'constraint' and e['accepted']
             for s in parse_specifier(e['value'].replace('=', '==', 1) if re.match(r'^=\d', e['value']) else e['value'])]
    allowed = (lambda v: allows_minor(specs, v)) if specs else (lambda v: True)
    decision = dict(evidence=evidence, notebook_versions=dict(Counter(o['version'] for o in observed)),
                    constraints=', '.join(e['value'] for e in evidence if e['kind'] == 'constraint' and e['accepted']) or None,
                    python2=any(o['version'].startswith('2.') for o in observed) or
                            any((e['value'] or '').startswith('2.') for e in evidence if e['kind'] == 'pin'),
                    notes=[])

    requested, source = None, None
    for e in evidence:
        if e['kind'] == 'pin' and e['accepted']:
            version = parse_version(e['value'])
            if allowed(version):
                requested, source = e['value'], e['file']
                break
            decision['notes'].append(f"pin {e['value']} in {e['file']} conflicts with constraints {decision['constraints']}")
    if requested is None and observed:
        common, _ = Counter(o['version'] for o in observed).most_common(1)[0]
        if allowed(parse_version(common)):
            requested, source = common, 'notebook metadata'
    if requested is None and specs:
        if allowed(config['default']):
            requested, source = minor(config['default']), 'default (allowed by constraints)'
        else:
            candidates = [(3, m) for m in range(0, 30) if allowed((3, m))]
            if candidates:
                best = min(candidates, key=lambda v: (abs(v[1] - config['default'][1]), -v[1]))
                requested, source = minor(best), 'nearest to default allowed by constraints'
    if requested is None:
        requested, source = minor(config['default']), 'default'

    version = parse_version(requested)
    used = version
    if version[0] == 2:
        used = config['default']
        decision['notes'].append(f'Python {requested} requested; Python 2 is not supported, using {minor(used)}')
    elif version[:2] < config['low']:
        used = config['low']
        decision['notes'].append(f'Python {requested} is below the supported range, using {minor(used)}')
    elif version[:2] > config['high']:
        used = config['high']
        decision['notes'].append(f'Python {requested} is above the supported range, using {minor(used)}')
    decision.update(requested=requested, source=source,
                    use='.'.join(map(str, used)) if used == version else minor(used),
                    default=minor(config['default']))
    return decision


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('repo_dir', type=Path)
    parser.add_argument('--notebooks', default='', help='notebook paths relative to REPO_DIR, separated by ;')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    decision = decide(args.repo_dir, args.notebooks.split(';'))
    print(json.dumps(decision) if args.json else decision['use'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
