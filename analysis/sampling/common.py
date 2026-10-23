"""Shared helpers for the sampling scripts (notes/sampling-design.md).

Every HTTP request is logged as one JSON line (time, method, URL without tokens, status,
bytes, elapsed seconds, rate-limit headers) to the request log of the step, so every frame
and every count can be traced back to the requests that produced it.
Tokens come from the environment or from a `.env` file in the repository root:
GITHUB_TOKEN, CODEBERG_TOKEN, ZENODO_TOKEN (all optional; GitHub needs one for the pilot).
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
SAMPLING_DIR = Path(os.environ.get('NBF_SAMPLING_DIR') or ROOT / 'data' / 'sampling')   # override for tests
CUTOFF = '2026-07-01'                 # D3: platform creation date before this day (UTC)
USER_AGENT = 'notebook-study-sampling/1.0 (an anonymised institution; research on notebook reproducibility)'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def load_env():
    """Read KEY=VALUE lines of ROOT/.env into os.environ (existing variables win)."""
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def token(name):
    load_env()
    return os.environ.get(name) or None


class ServerError(requests.HTTPError):
    """A server error (5xx) that persisted through all retries, e.g. one broken entry in a listing page."""


def rate_limited(response):
    """429, or a 403 that is a (primary or secondary) rate limit rather than a blocked resource."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    remaining = response.headers.get('X-RateLimit-Remaining')
    if remaining == '0':
        return True
    # Zenodo sends Retry-After with every 403, also "Permission denied" while quota remains: only a
    # Retry-After without a remaining-quota header counts on its own (GitHub secondary limits also say so).
    if 'Retry-After' in response.headers and remaining is None:
        return True
    try:
        return 'rate limit' in str(response.json().get('message', '')).lower()
    except ValueError:
        return False


def settled(status):
    """A final answer for one item: 200 or a client error (4xx; rate limits are waited out before). Server errors
    and unreachable servers (None) are not settled: they are recorded, and a rerun of the step retries them."""
    return isinstance(status, int) and (status == 200 or 400 <= status < 500)


def status_of(response):
    return response.status_code if response is not None else None


def json_of(response, default=None):
    """The JSON body of a 200 response, else `default` (also for a 200 whose body is not JSON)."""
    if response is None or response.status_code != 200:
        return default
    try:
        return response.json()
    except ValueError:
        return default


class Http:
    """requests.Session with pacing, retries (rate limits, 5xx, Retry-After) and a JSONL request log."""

    def __init__(self, log_path, min_interval=0.0, headers=None, max_retries=8):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT
        self.session.headers.update(headers or {})
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.last = 0.0
        self.count = 0
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        self.log = open(log_path, 'a', encoding='utf-8')

    def request(self, method, url, ok=(200,), stream=False, soft=False, **kwargs):
        """Return the response for a status in `ok`; retry 429/5xx and rate limits; raise on anything else.

        soft=True never raises: after the retries it returns the last response (any status), or None when the
        server could not be reached. Per-item loops use it to record the outcome and go on (see settled()).
        """
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            started = time.monotonic()
            error = None
            try:
                response = self.session.request(method, url, timeout=(20, 120), stream=stream, **kwargs)
            except requests.RequestException as exc:
                response, error = None, f'{type(exc).__name__}: {exc}'[:300]
            self.count += 1
            entry = dict(time=now(), method=method, url=response.url if response is not None else url,
                         params=kwargs.get('params'), attempt=attempt,
                         status=response.status_code if response is not None else None,
                         bytes=None if response is None or stream else len(response.content),
                         elapsed=round(time.monotonic() - started, 3), error=error)
            if response is not None:
                for header in ('X-RateLimit-Remaining', 'X-RateLimit-Reset', 'Retry-After', 'X-Total-Count'):
                    if header in response.headers:
                        entry[header.lower()] = response.headers[header]
            self.log.write(json.dumps(entry) + '\n')
            self.log.flush()
            limited = response is not None and rate_limited(response)
            if response is not None and response.status_code in ok and not limited:
                return response
            retry = response is None or limited or response.status_code in (500, 502, 503, 504)
            if not retry or attempt == self.max_retries:
                if soft:
                    return response
                if response is None:
                    raise requests.ConnectionError(error)
                if response.status_code >= 500:
                    raise ServerError(f'{response.status_code} after {attempt + 1} attempts: {response.url}',
                                      response=response)
                response.raise_for_status()
                raise requests.HTTPError(f'unexpected status {response.status_code} for {url}', response=response)
            time.sleep(self.backoff(response, attempt))
        raise AssertionError('unreachable')

    @staticmethod
    def backoff(response, attempt):
        if response is not None:
            if response.headers.get('Retry-After', '').isdigit():
                return int(response.headers['Retry-After']) + 1
            reset = response.headers.get('X-RateLimit-Reset')
            if response.headers.get('X-RateLimit-Remaining') == '0' and reset and reset.isdigit():
                return max(1, int(reset) - int(time.time()) + 2)
        return min(300, 5 * 2 ** attempt)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path, rows, fields):
    """Write rows (dicts) to CSV (gzip if the name ends in .gz); write <path>.sha256; return the digest."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'wt', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    digest = sha256_file(path)
    Path(f'{path}.sha256').write_text(f'{digest}  {path.name}\n')
    return digest


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + '\n')


def rng(seed):
    """Seeded random generator; the seed is always recorded in the output metadata."""
    return random.Random(seed)
