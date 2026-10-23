#!/usr/bin/env python3
"""Dependency evidence and install plan for one acquired resource.

Usage: dependencies.py REPO_DIR --notebooks "a.ipynb;b/c.ipynb" --python TARGET_PYTHON [--json OUT]

Reads (never writes) the resource and reports:
  sources   every dependency file found, its kind, whether it is used and why:
            requirements*.txt / .in (incl. -r/-c includes), environment.yml (pip section and
            conda packages mapped to PyPI names), pyproject.toml ([project] and Poetry),
            setup.cfg / setup.py install_requires, Pipfile, %pip / !pip install lines
  requirements  the deduplicated install plan: spec, distribution, origin, decision
  imports   every module imported by each notebook (Python's ast on the code cells, IPython
            magics transformed; a line-based fallback for cells that do not parse, e.g.
            Python 2) with its classification:
              stdlib          in the standard library of TARGET_PYTHON
              python2-stdlib  a Python 2 standard-library name (urllib2, cPickle, ...)
              local           a module or package found in the resource (path recorded)
              not-installable a runtime-only module (google.colab, pyodide, ...)
              third-party     mapped to a PyPI distribution, with the mapping source
                              (override / pipreqs / same name)
  import_path  folders to put on the kernel's import path so local modules resolve
  python2   notebooks with Python 2 syntax

Which requirements files are used: requirements.txt (or requirements.in when there is no
.txt next to it) in the resource root, binder/, .binder/, and every folder from the root
to an analysed notebook. Other variants (requirements-dev.txt, docs/requirements.txt, ...)
are listed with decision "not used". Lines pointing at the resource itself (".", "-e .",
local paths) are not installed: the resource's own code is used from its folder through
the import path, so that the analysed version is the one that runs.
Python 3.10+ standard library; IPython is used when available.
"""
from __future__ import annotations

import argparse
import ast
import configparser
import json
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path, PurePosixPath

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

DATA = Path(__file__).resolve().parent / 'data'
REQ_FILE = re.compile(r'^requirements([\w.-]*)\.(txt|in)$', re.I)
SKIP_DIRS = {'.git', '.ipynb_checkpoints', '__MACOSX', 'node_modules', '.venv', 'venv', 'env', '.tox',
             'site-packages', '__pycache__'}
PY2_STDLIB = {'urllib2', 'urlparse', 'ConfigParser', 'cPickle', 'cStringIO', 'StringIO', 'Queue', 'Tkinter',
              'tkMessageBox', 'tkFileDialog', 'httplib', 'HTMLParser', 'commands', 'exceptions', '__builtin__',
              'thread', 'md5', 'sets', 'copy_reg', 'SocketServer', 'xmlrpclib', 'Cookie', 'cookielib',
              'dummy_thread', 'anydbm', 'dbhash', 'htmlentitydefs', 'robotparser', 'UserDict', 'UserList',
              'UserString', 'urllib2_kerberos', 'string_tools'} - {'urllib2_kerberos', 'string_tools'}
# conda package -> PyPI distribution ('' = conda-only, not installable with pip)
CONDA_TO_PYPI = {'pytorch': 'torch', 'py-opencv': 'opencv-python', 'opencv': 'opencv-python',
                 'python-graphviz': 'graphviz', 'py-xgboost': 'xgboost', 'matplotlib-base': 'matplotlib',
                 'pytorch-cuda': '', 'cudatoolkit': '', 'cudnn': '', 'mkl': '', 'blas': '', 'openblas': '',
                 'nodejs': '', 'git': '', 'graphviz': '', 'gdal': 'GDAL', 'libgdal': '', 'proj': '', 'geos': '',
                 'compilers': '', 'cxx-compiler': '', 'c-compiler': '', 'make': '', 'cmake': '', 'pip': '',
                 'python': '', 'ipykernel': 'ipykernel', 'r-base': '', 'r-essentials': '', 'jupyter': 'jupyter',
                 'tensorflow-gpu': 'tensorflow', 'pyqt': 'PyQt5', 'qt': '', 'ffmpeg': '', 'hdf5': '', 'netcdf4': 'netCDF4',
                 'libnetcdf': '', 'eccodes': '', 'cartopy': 'Cartopy', 'basemap': 'basemap', 'faiss-cpu': 'faiss-cpu'}
CONDA_ONLY_PREFIXES = ('r-', 'lib', 'cuda', 'nvidia', 'gcc', 'gxx', 'gfortran', 'clang', 'llvm', 'vs20', 'msys2-')


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


# --- import -> distribution mapping --------------------------------------------------------
@lru_cache(maxsize=1)
def mappings():
    pipreqs = {}
    for line in (DATA / 'pipreqs-mapping.txt').read_text().splitlines():
        if ':' in line:
            module, dist = line.strip().split(':', 1)
            pipreqs[module] = dist
    overrides = {}
    for line in (DATA / 'import-overrides.txt').read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and ':' in line:
            module, dist = line.split(':', 1)
            overrides[module] = dist
    return overrides, pipreqs


def map_import(module):
    """(distribution or None, mapping source, reason) for a dotted module name."""
    overrides, pipreqs = mappings()
    parts = module.split('.')
    for length in range(len(parts), 0, -1):
        prefix = '.'.join(parts[:length])
        if prefix in overrides:
            value = overrides[prefix]
            if value.startswith('!'):
                return None, 'override', value[1:]
            return value, 'override', None
    top = parts[0]
    if top in pipreqs:
        return pipreqs[top], 'pipreqs', None
    return top, 'same name', None


@lru_cache(maxsize=8)
def target_stdlib(python):
    """Standard-library module names of the target interpreter."""
    probe = ('import sys, json, pkgutil, sysconfig\n'
             'names = set(getattr(sys, "stdlib_module_names", ()))\n'
             'if not names:\n'
             '    paths = [sysconfig.get_paths()["stdlib"]]\n'
             '    paths.append(paths[0] + "/lib-dynload")\n'
             '    names = {m.name for m in pkgutil.iter_modules(paths)} | set(sys.builtin_module_names)\n'
             'print(json.dumps(sorted(names)))\n')
    if python:
        try:
            result = subprocess.run([python, '-c', probe], capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                return frozenset(json.loads(result.stdout))
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
    return frozenset(sys.stdlib_module_names)


# --- notebooks -----------------------------------------------------------------------------
def code_cells(notebook):
    cells = notebook.get('cells')
    if cells is None:                                   # nbformat 3
        cells = [c for ws in notebook.get('worksheets', []) for c in ws.get('cells', [])]
    for index, cell in enumerate(cells):
        if cell.get('cell_type') == 'code':
            source = cell.get('source', cell.get('input', ''))
            yield index, ''.join(source) if isinstance(source, list) else str(source)


@lru_cache(maxsize=1)
def transformer():
    try:
        from IPython.core.inputtransformer2 import TransformerManager
        return TransformerManager()
    except ImportError:
        return None


def to_python(source):
    manager = transformer()
    if manager is not None:
        try:
            return manager.transform_cell(source)
        except Exception:  # noqa: BLE001 - fall back to dropping magic lines
            pass
    return '\n'.join(l for l in source.splitlines() if not l.lstrip().startswith(('%', '!')))


LINE_IMPORT = re.compile(r'^\s*(?:import\s+([\w.,\s]+?)(?:\s+as\s+\w+)?\s*(?:#.*)?$|from\s+([\w.]+)\s+import\b)')


def imports_in_cell(source):
    """(set of dotted module names, python2 syntax?) for one code cell."""
    code = to_python(source)
    try:
        tree = ast.parse(code)
    except SyntaxError:
        names = set()
        for line in source.splitlines():
            m = LINE_IMPORT.match(line)
            if m and m.group(2) and not m.group(2).startswith('.'):
                names.add(m.group(2))
            elif m and m.group(1):
                names.update(p.strip().split()[0] for p in m.group(1).split(',') if p.strip())
        python2 = bool(re.search(r'^\s*print\s+[^\s(=]', source, re.M) or re.search(r'except\s+[\w.]+\s*,\s*\w+\s*:', source)
                       or re.search(r'^\s*exec\s+["\']', source, re.M))
        return names, python2
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names, False


PIP_MAGIC = re.compile(r'^\s*[%!]\s*(?:python[\d.]*\s+-m\s+)?pip3?\s+install\s+(.+)$', re.M)


def pip_magic_specs(source):
    specs = []
    for args in PIP_MAGIC.findall(source):
        tokens = args.split('#', 1)[0].split()
        skip_next = False
        for token in tokens:
            if skip_next:
                skip_next = False
                continue
            if token in ('-r', '-c', '-e', '-i', '--index-url', '--extra-index-url', '-f', '--find-links', '-t', '--target'):
                skip_next = True
                continue
            if token.startswith('-') or token.startswith('$') or token.startswith('{'):
                continue
            token = token.strip('"\'')
            if re.match(r'^[A-Za-z0-9][\w.-]*(\[[\w,.-]+\])?\s*((==|>=|<=|~=|!=|<|>)[\w.*+!-]+(,\s*(==|>=|<=|~=|!=|<|>)[\w.*+!-]+)*)?$', token):
                specs.append(token)
    return specs


# --- local modules --------------------------------------------------------------------------
def local_index(root):
    """top-level importable name -> list of folders that contain it (as file or package)."""
    index = {}
    for path in root.rglob('*'):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix == '.py':
            index.setdefault(path.stem, set()).add(path.parent)
        elif path.is_dir() and (any(path.glob('*.py')) or (path / '__init__.py').exists()):
            index.setdefault(path.name, set()).add(path.parent)
    return index


def resolve_local(top, notebook_dir, root, index):
    """Folder from which `top` would be imported for this notebook, or None."""
    folders = index.get(top)
    if not folders:
        return None
    for preferred in (notebook_dir, root, root / 'src'):
        if preferred in folders:
            return preferred
    # nearest folder: shortest relative path from the notebook's folder, then from the root
    return min(folders, key=lambda f: (len(f.relative_to(root).parts), str(f)))


# --- declared dependency files ----------------------------------------------------------------
def is_local_requirement(line):
    stripped = line.strip()
    target = stripped[2:].strip() if stripped.startswith('-e ') else stripped.split('--editable', 1)[-1].strip() \
        if stripped.startswith('--editable') else stripped
    return target in ('.', './') or target.startswith(('./', '../', '/', 'file:')) or target.endswith(('.whl', '.tar.gz')) \
        and not target.startswith(('http://', 'https://'))


def read_requirements(path, root, seen=None):
    """(specs, options, notes) from a requirements file, following -r/-c includes inside the resource."""
    seen = seen or set()
    seen.add(path.resolve())
    specs, options, notes = [], [], []
    for raw in path.read_text(errors='replace').splitlines():
        line = raw.split(' #', 1)[0].strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r'^(-r|--requirement|-c|--constraint)\s*=?\s*(.+)$', line)
        if m:
            include = (path.parent / m.group(2).strip()).resolve()
            try:
                include.relative_to(root.resolve())
            except ValueError:
                notes.append(f'{line}: outside the resource, ignored')
                continue
            if include in seen or not include.is_file():
                notes.append(f'{line}: not found' if not include.is_file() else f'{line}: already read')
                continue
            s, o, n = read_requirements(include, root, seen)
            if m.group(1) in ('-c', '--constraint'):
                options.extend(f'# constraint from {include.relative_to(root.resolve())}: {x}' for x in s)
            else:
                specs.extend(s)
            options.extend(o)
            notes.extend(n)
            continue
        if is_local_requirement(line):
            notes.append(f'{line}: the resource itself / local path, used from the resource folder')
            continue
        if line.startswith(('--index-url', '-i ', '--extra-index-url', '--find-links', '-f ', '--pre', '--trusted-host')):
            options.append(line)
            continue
        if line.startswith('-'):
            notes.append(f'{line}: pip option not used')
            continue
        specs.append(line)
    return specs, options, notes


def conda_spec(entry):
    """PyPI spec for a conda dependency string, '' if conda-only, None if not parseable."""
    m = re.match(r'^(?:[\w-]+::)?([A-Za-z0-9][\w.-]*)\s*(.*)$', entry.strip())
    if not m:
        return None
    name, version = m.group(1).lower(), m.group(2).strip()
    if name in CONDA_TO_PYPI:
        name = CONDA_TO_PYPI[name]
        if not name:
            return ''
    elif name.startswith(CONDA_ONLY_PREFIXES):
        return ''
    if not version:
        return name
    exact = re.match(r'^=?=?\s*([\w.]+?)(\.\*)?(=[\w.]+)?$', version)       # =1.2 / ==1.2.3 / =1.2.3=build
    if exact and not version.startswith(('>', '<', '!', '~')):
        v = exact.group(1)
        return f'{name}=={v}' if version.startswith('==') or v.count('.') >= 2 else f'{name}=={v}.*'
    return f'{name}{version.replace(" ", "")}' if re.match(r'^[<>!~=]', version) else name


def poetry_spec(name, value):
    version = value.get('version', '*') if isinstance(value, dict) else str(value)
    if version in ('*', ''):
        return name
    m = re.match(r'^\^(\d+)(?:\.(\d+))?(?:\.(\d+))?$', version)
    if m:
        major, minor = int(m.group(1)), int(m.group(2) or 0)
        upper = f'{major + 1}.0' if major > 0 else f'0.{minor + 1}'
        return f'{name}>={version[1:]},<{upper}'
    m = re.match(r'^~(\d+)\.(\d+)(?:\.\d+)?$', version)
    if m:
        return f'{name}>={version[1:]},<{m.group(1)}.{int(m.group(2)) + 1}'
    return f'{name}{version}' if re.match(r'^[<>=!~]', version) else f'{name}=={version}'


def declared_sources(root, notebook_dirs, supplied=()):
    """Every dependency file with its parsed specs and whether it is used."""
    used_dirs = {root, root / 'binder', root / '.binder'}
    for folder in notebook_dirs:
        current = folder
        while True:
            used_dirs.add(current)
            if current == root or root not in current.parents:
                break
            current = current.parent
    sources = []
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if not path.is_file() or any(part in SKIP_DIRS for part in rel.parts) or path.stat().st_size > 2 * 2**20:
            continue
        name = path.name
        entry = dict(path=str(rel), kind=None, specs=[], options=[], notes=[], used=False, reason='')
        try:
            if str(rel) in supplied and not REQ_FILE.match(name):
                entry['kind'] = 'requirements'
                entry['specs'], entry['options'], entry['notes'] = read_requirements(path, root)
                entry['used'], entry['reason'] = True, 'requirements file supplied with the resource'
            elif REQ_FILE.match(name):
                entry['kind'] = 'requirements'
                variant = REQ_FILE.match(name).group(1)
                compiled = name.lower().endswith('.in') and (path.with_suffix('.txt')).exists()
                entry['specs'], entry['options'], entry['notes'] = read_requirements(path, root)
                if str(rel) in supplied:
                    entry['used'], entry['reason'] = True, 'requirements file supplied with the resource'
                elif variant:
                    entry['reason'] = 'variant file (dev/test/docs/extra) not used'
                elif compiled:
                    entry['reason'] = 'pip-tools input; the compiled .txt next to it is used'
                elif path.parent not in used_dirs:
                    entry['reason'] = 'not in the root, binder/ or a folder of an analysed notebook'
                else:
                    entry['used'], entry['reason'] = True, 'requirements file'
            elif name in ('environment.yml', 'environment.yaml'):
                entry['kind'] = 'conda environment'
                section = None
                for raw in path.read_text(errors='replace').splitlines():
                    line = raw.split('#', 1)[0].rstrip()
                    if re.match(r'^\S', line):
                        section = line.split(':')[0].strip()
                        continue
                    m = re.match(r'^\s*-\s*pip\s*:\s*$', line)
                    if m:
                        section = 'pip'
                        continue
                    item = re.match(r'^\s*-\s*(.+)$', line)
                    if not item or section not in ('dependencies', 'pip'):
                        continue
                    value = item.group(1).strip().strip('"\'')
                    if section == 'pip' and re.match(r'^\s{4,}-', line):
                        if is_local_requirement(value) or value.startswith('-'):
                            entry['notes'].append(f'pip: {value}: not used')
                        else:
                            entry['specs'].append(value)
                    else:
                        spec = conda_spec(value)
                        if spec:
                            entry['specs'].append(spec)
                        elif spec == '':
                            entry['notes'].append(f'conda: {value}: conda-only or interpreter, not installed with pip')
                entry['used'] = path.parent in used_dirs
                entry['reason'] = 'conda environment (pip section + conda packages mapped to PyPI)' if entry['used'] \
                    else 'not in the root, binder/ or a folder of an analysed notebook'
            elif name == 'pyproject.toml':
                entry['kind'] = 'pyproject.toml'
                data = tomllib.loads(path.read_text(errors='replace')) if tomllib else {}
                entry['specs'] = list((data.get('project') or {}).get('dependencies') or [])
                poetry = (((data.get('tool') or {}).get('poetry') or {}).get('dependencies') or {})
                entry['specs'] += [poetry_spec(k, v) for k, v in poetry.items() if k.lower() != 'python']
                entry['used'] = path.parent == root and bool(entry['specs'])
                entry['reason'] = 'project dependencies' if entry['used'] else \
                    ('no dependencies declared' if not entry['specs'] else 'not in the resource root')
            elif name == 'setup.cfg':
                entry['kind'] = 'setup.cfg'
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(path.read_text(errors='replace'))
                value = parser.get('options', 'install_requires', fallback='')
                entry['specs'] = [l.strip() for l in value.splitlines() if l.strip() and not l.strip().startswith('#')]
                entry['used'] = path.parent == root and bool(entry['specs'])
                entry['reason'] = 'project dependencies' if entry['used'] else \
                    ('no install_requires' if not entry['specs'] else 'not in the resource root')
            elif name == 'setup.py':
                entry['kind'] = 'setup.py'
                text = path.read_text(errors='replace')
                m = re.search(r'install_requires\s*=\s*\[(.*?)\]', text, re.S)
                entry['specs'] = re.findall(r'["\']([^"\']+)["\']', m.group(1)) if m else []
                if 'install_requires' in text and not m:
                    entry['notes'].append('install_requires is computed, not a literal list; not read')
                entry['used'] = path.parent == root and bool(entry['specs'])
                entry['reason'] = 'project dependencies (literal install_requires)' if entry['used'] else \
                    ('no literal install_requires' if not entry['specs'] else 'not in the resource root')
            elif name == 'Pipfile':
                entry['kind'] = 'Pipfile'
                data = tomllib.loads(path.read_text(errors='replace')) if tomllib else {}
                for package, value in (data.get('packages') or {}).items():
                    version = value.get('version', '*') if isinstance(value, dict) else str(value)
                    entry['specs'].append(package if version in ('*', '') else f'{package}{version}')
                entry['used'] = path.parent in used_dirs and bool(entry['specs'])
                entry['reason'] = '[packages] section' if entry['used'] else 'not used'
        except Exception as error:  # noqa: BLE001 - a broken file is evidence, not a crash
            entry['notes'].append(f'could not parse: {error}')
            entry['reason'] = 'parse error'
            try:
                if re.search(r'^(<{7}|>{7}) ', path.read_text(errors='replace'), re.M):
                    entry['reason'] = 'parse error: unresolved git merge conflict markers'
            except OSError:
                pass
        if entry['kind']:
            entry['pinned'] = sum(bool(re.search(r'===?\s*[\w.]', s)) for s in entry['specs'])
            sources.append(entry)
    return sources


def spec_name(spec):
    m = re.match(r'^\s*([A-Za-z0-9][\w.-]*)', spec)
    return normalize(m.group(1)) if m else None


PRECEDENCE = {'requirements': 0, 'conda environment': 1, 'Pipfile': 2, 'pyproject.toml': 3, 'setup.cfg': 4,
              'setup.py': 5, '%pip': 6}


def analyse(root, notebooks, python=None, supplied=()):
    root = Path(root).resolve()
    stdlib = target_stdlib(python)
    index = local_index(root)
    notebook_paths = [root / n for n in notebooks if n and (root / n).is_file()]
    imports, magic_sources, python2_notebooks, import_path = [], [], [], set()
    for path in notebook_paths:
        relative = str(path.relative_to(root))
        try:
            notebook = json.loads(path.read_text(encoding='utf-8', errors='replace'))
        except json.JSONDecodeError as error:
            imports.append(dict(notebook=relative, module=None, classification='unreadable notebook', note=str(error)))
            continue
        found, py2 = {}, False
        for cell, source in code_cells(notebook):
            names, cell_py2 = imports_in_cell(source)
            py2 |= cell_py2
            for name in names:
                found.setdefault(name, []).append(cell)
            specs = pip_magic_specs(source)
            if specs:
                magic_sources.append(dict(notebook=relative, cell=cell, specs=specs))
        if py2:
            python2_notebooks.append(relative)
        for module, cells in sorted(found.items()):
            top = module.split('.')[0]
            row = dict(notebook=relative, module=module, top=top, cells=cells, distribution=None, mapping=None,
                       local_path=None, note=None)
            local = resolve_local(top, path.parent, root, index)
            if top in stdlib:
                row['classification'] = 'stdlib'
            elif top in PY2_STDLIB:
                row['classification'] = 'python2-stdlib'
            elif local is not None:
                row['classification'] = 'local'
                row['local_path'] = str(local.relative_to(root)) or '.'
                if local != path.parent:
                    import_path.add(str(local.relative_to(root)) or '.')
            else:
                distribution, source, reason = map_import(module)
                if distribution is None:
                    row.update(classification='not-installable', mapping=source, note=reason)
                else:
                    row.update(classification='third-party', distribution=distribution, mapping=source)
            imports.append(row)

    sources = declared_sources(root, {p.parent for p in notebook_paths}, {s.strip() for s in supplied if s.strip()})
    for magic in magic_sources:
        sources.append(dict(path=f"{magic['notebook']} (cell {magic['cell']})", kind='%pip', specs=magic['specs'],
                            options=[], notes=[], used=True, reason='install command in the notebook',
                            pinned=sum('==' in s for s in magic['specs'])))
    # packages defined at the top of the resource (or in src/): installed from the folder, not PyPI
    own_packages = {normalize(name) for name, folders in index.items() if root in folders or root / 'src' in folders}
    requirements, by_name = [], {}
    for source in sorted(sources, key=lambda s: (PRECEDENCE.get(s['kind'], 9), s['path'])):
        for spec in source['specs']:
            name = spec_name(spec)
            if name is None:
                continue
            if not source['used']:
                continue
            if name in by_name:
                first = by_name[name]
                versioned = lambda x: bool(re.search(r'[<>=!~]', x))
                if versioned(spec) and versioned(first['spec']) and spec.replace(' ', '') != first['spec'].replace(' ', ''):
                    first.setdefault('conflicts', []).append(f"{source['path']}: {spec}")
                first['origin_files'].append(source['path'])
                continue
            entry = dict(spec=spec, name=name, origin='declared', origin_kind=source['kind'],
                         origin_files=[source['path']], decision='install', reason=source['reason'])
            if name in own_packages:
                entry.update(decision='skip', reason="the resource's own package: its code is used from the "
                                                     'resource folder (import path), not from PyPI')
            by_name[name] = entry
            requirements.append(entry)
    options = sorted({o for s in sources if s['used'] for o in s.get('options', []) if not o.startswith('#')})
    return dict(root=str(root), python=python, sources=sources, requirements=requirements, pip_options=options,
                imports=imports, import_path=sorted(import_path), python2_notebooks=python2_notebooks,
                stdlib_source='target interpreter' if python else 'pipeline interpreter')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('repo_dir', type=Path)
    parser.add_argument('--notebooks', default='')
    parser.add_argument('--python', help='target interpreter (for its standard-library names)')
    parser.add_argument('--json', type=Path, help='write the report here (default: stdout)')
    parser.add_argument('--requirements', default='', help='requirement files supplied with the resource, separated by ;')
    args = parser.parse_args(argv)
    report = analyse(args.repo_dir, args.notebooks.split(';'), args.python, args.requirements.split(';'))
    text = json.dumps(report, indent=1)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text)
    else:
        print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
