"""Error category with context: EXTERNAL_SERVICE_ERROR next to the type-based categories.

The thesis categorised a notebook's first error by its exception type only (summary.categorize_error_type).
A notebook that needs a remote service or credentials then often looks like a code error: e.g. the Georoc
notebook asks for an API key in a password widget; run unattended, nobody types it, the client stays None
and the first error is "AttributeError: 'NoneType' object has no attribute 'api_query'".

categorize() returns (category, category_by_type, evidence):
  category          EXTERNAL_SERVICE_ERROR if there is evidence below, else the type-based category
  category_by_type  the thesis mapping, kept for old-vs-new comparisons (error_category_by_type)
  evidence          short text why (error_evidence), or None
Evidence for EXTERNAL_SERVICE_ERROR (a remote service answered with an error, or credentials are needed):
  1. the exception type is a client/credential/rate-limit error (HTTPError, NoCredentialsError, ...)
  2. message or traceback names an HTTP 401/403/407/429/5xx answer, missing/invalid credentials, an
     API key or token, or a rate limit / quota
  3. KeyError for an environment variable that holds a secret (..._KEY, ..._TOKEN, ...)
  4. an interactive credential prompt (getpass, a Password widget, input() asking for a key, token or
     password) in a code cell at or before the failing cell, in its source or (for widgets created by
     the resource's own modules) in its output
Dependency errors keep their category (the import failure comes first). Connection failures without
an answer from the service (DNS, refused, timeout) stay NETWORK_ERROR.
"""
from __future__ import annotations

import re

from .summary import categorize_error_type

SERVICE_TYPES = {
    'HTTPError', 'NoCredentialsError', 'PartialCredentialsError', 'ClientError', 'Unauthorized', 'Forbidden',
    'AuthenticationError', 'PermissionDenied', 'Unauthenticated', 'RateLimitError', 'TooManyRequests',
    'RateLimitExceeded', 'APIError', 'ApiException', 'HfHubHTTPError', 'GatedRepoError', 'RepositoryNotFoundError',
    'InvalidToken', 'TokenExpiredError', 'BadCredentialsException', 'GoogleAuthError', 'DefaultCredentialsError',
    'ServiceUnavailable', 'ResourceExhausted', 'QuotaExceeded',
}
TEXT_SIGNALS = [
    (r'\b(?:HTTP(?: Error)?|status(?: code)?|response)\s*[:=]?\s*(401|403|407|429|5\d\d)\b', 'HTTP {0} answer'),
    (r'\b(401|403|407|429|5\d\d)\s+(?:Client|Server) Error\b', 'HTTP {0} answer'),
    (r'\b(unauthori[sz]ed|forbidden|too many requests|service unavailable|bad gateway|gateway time-?out)\b', '"{0}"'),
    (r'\b(api[ _-]?key|access[ _-]?token|auth(?:entication)?[ _-]?token|bearer token|invalid token|token (?:is )?(?:invalid|expired))\b', '"{0}"'),
    (r'\b(invalid|missing|no|expired) (credentials?|api key|token)\b', '"{0} {1}"'),
    (r'\b(rate[ -]?limit(?:ed)?|quota (?:exceeded|exhausted)|usage limit)\b', '"{0}"'),
]
SECRET_VARIABLE = re.compile(r"""^['"]?[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|AUTH)[A-Z0-9_]*['"]?$""")
PROMPTS = [
    (r'\bgetpass\s*\(', 'getpass()'),
    (r'\bPassword\s*\(', 'Password widget'),
    (r"""\binput\s*\([^)]*(?:key|token|password|passwd|secret|user ?name|login|credential)""", 'input() for credentials'),
    (r'\bnotebook_login\s*\(|\binteractive_login\s*\(|\blogin\s*\(\s*\)', 'interactive login'),
]
ANSI = re.compile(r'\x1b\[[0-9;]*m')


def _text_evidence(message, traceback):
    text = ANSI.sub('', f'{message or ""}\n{traceback or ""}')
    for pattern, label in TEXT_SIGNALS:
        match = re.search(pattern, text, re.I)
        if match:
            return label.format(*(g.lower() for g in match.groups()))
    return None


def _prompt_evidence(notebook, error_cell_index):
    if notebook is None:
        return None
    cells = notebook.get('cells', []) if isinstance(notebook, dict) else getattr(notebook, 'cells', [])
    last = error_cell_index if isinstance(error_cell_index, int) else len(cells) - 1
    for index, cell in enumerate(cells[:last + 1]):
        if cell.get('cell_type') != 'code':
            continue
        source = cell.get('source', '')
        source = ''.join(source) if isinstance(source, list) else source
        for pattern, label in PROMPTS:
            if re.search(pattern, source, re.I):
                return f'{label} in cell {index} (no input in unattended execution)'
        # widgets created by the resource's own modules show up only in the cell output
        for output in cell.get('outputs', []):
            shown = (output.get('data') or {}).get('text/plain', '')
            shown = ''.join(shown) if isinstance(shown, list) else str(shown)
            if re.search(r'\bPassword\(', shown):
                return f'Password widget shown by cell {index} (no input in unattended execution)'
    return None


def categorize(error_type, message=None, traceback=None, notebook=None, error_cell_index=None):
    by_type = categorize_error_type(error_type)
    if by_type == 'DEPENDENCY_ERROR':
        return by_type, by_type, None
    evidence = None
    if error_type in SERVICE_TYPES:
        evidence = f'{error_type} (client, credential or rate-limit error)'
    if evidence is None:
        found = _text_evidence(message, traceback)
        evidence = f'remote service: {found}' if found else None
    if evidence is None and error_type == 'KeyError' and SECRET_VARIABLE.match((message or '').strip()):
        evidence = f'credential missing: KeyError {message.strip()}'
    if evidence is None:
        evidence = _prompt_evidence(notebook, error_cell_index)
    return ('EXTERNAL_SERVICE_ERROR' if evidence else by_type), by_type, evidence


def ensure_columns(connection):
    """Add error_category_by_type and error_evidence to notebook_executions if missing."""
    present = {row[1] for row in connection.execute('PRAGMA table_info(notebook_executions)')}
    for column in ('error_category_by_type', 'error_evidence'):
        if column not in present:
            connection.execute(f'ALTER TABLE notebook_executions ADD COLUMN {column} TEXT')
