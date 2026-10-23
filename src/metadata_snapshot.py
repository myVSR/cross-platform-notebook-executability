#!/usr/bin/env python3
"""Store a platform API response exactly as received, with its provenance (table metadata_snapshots).

Every metadata request of a run (GitHub /repos/{o}/{r}, Codeberg /repos/{o}/{r}, Zenodo /records/{id})
is kept, also failed ones, so that the cross-platform normalisation can be redone or extended later
without querying the platforms again — and so that it is known what a platform said at that moment
(counts, dates and descriptions change; resources disappear).

Stored per request: run, repository, platform, purpose, method, URL, time of request and response
(UTC), HTTP status, attempts, whether a token was used (never the token), selected response headers
(Date, ETag, Last-Modified, rate-limit counters, ...), size and SHA-256 of the body, the body itself
(as JSON text when it parses — queryable with SQLite's json_extract — otherwise an excerpt), and the
HTTP client and its version.

Usage (called by src/repo.sh):
  metadata_snapshot.py --db DB --run-id N --platform github --url URL --status 200 \\
      --requested-at T0 --responded-at T1 --headers-file H --body-file B [--attempts 1] [--token-used] \\
      [--purpose 'repository metadata'] [--tool 'curl 7.61.1']
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_run_id INTEGER, repository_id INTEGER, platform TEXT, purpose TEXT,
    method TEXT, url TEXT, requested_at TEXT, responded_at TEXT, http_status INTEGER, attempts INTEGER,
    token_used INTEGER, response_headers_json TEXT, response_bytes INTEGER, response_sha256 TEXT,
    response_json TEXT, response_text_excerpt TEXT, tool TEXT, created_at TEXT,
    FOREIGN KEY (repository_run_id) REFERENCES repository_runs(id));
CREATE INDEX IF NOT EXISTS idx_metadata_snapshots_run ON metadata_snapshots(repository_run_id);
"""
KEEP_HEADERS = {'date', 'etag', 'last-modified', 'content-type', 'x-ratelimit-limit', 'x-ratelimit-remaining',
                'x-ratelimit-reset', 'x-ratelimit-used', 'x-ratelimit-resource', 'ratelimit', 'ratelimit-policy',
                'x-github-request-id', 'x-request-id', 'x-total-count', 'link', 'retry-after', 'location',
                'x-accepted-github-permissions', 'cache-control', 'server'}


def parse_headers(text):
    """Headers of the final response (curl -D writes one block per redirect/retry)."""
    blocks = [b for b in text.replace('\r\n', '\n').split('\n\n') if b.strip().startswith('HTTP/')]
    headers = {}
    for line in (blocks[-1] if blocks else '').splitlines()[1:]:
        if ':' in line:
            key, value = line.split(':', 1)
            if key.strip().lower() in KEEP_HEADERS:
                headers[key.strip().lower()] = value.strip()
    return headers


def record(db, run_id, platform, url, status, requested_at, responded_at, headers_text, body, attempts=1,
           token_used=False, purpose='repository metadata', method='GET', tool=None):
    connection = sqlite3.connect(db, timeout=60)
    connection.executescript(SCHEMA)
    row = connection.execute('SELECT repository_id FROM repository_runs WHERE id = ?', (run_id,)).fetchone()
    try:
        parsed = json.loads(body.decode('utf-8'))
        response_json, excerpt = json.dumps(parsed, ensure_ascii=False), None
    except (UnicodeDecodeError, ValueError):
        response_json, excerpt = None, body[:2000].decode('utf-8', 'replace')
    connection.execute(
        'INSERT INTO metadata_snapshots (repository_run_id, repository_id, platform, purpose, method, url, '
        'requested_at, responded_at, http_status, attempts, token_used, response_headers_json, response_bytes, '
        'response_sha256, response_json, response_text_excerpt, tool, created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (run_id, row[0] if row else None, platform, purpose, method, url, requested_at, responded_at,
         int(status) if str(status).isdigit() else None, attempts, int(bool(token_used)),
         json.dumps(parse_headers(headers_text), sort_keys=True), len(body), hashlib.sha256(body).hexdigest(),
         response_json, excerpt, tool, datetime.now(timezone.utc).isoformat(timespec='seconds')))
    connection.commit()
    connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', required=True)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--status', required=True)
    parser.add_argument('--requested-at', required=True)
    parser.add_argument('--responded-at', required=True)
    parser.add_argument('--headers-file', type=Path, required=True)
    parser.add_argument('--body-file', type=Path, required=True)
    parser.add_argument('--attempts', type=int, default=1)
    parser.add_argument('--token-used', action='store_true')
    parser.add_argument('--purpose', default='repository metadata')
    parser.add_argument('--method', default='GET')
    parser.add_argument('--tool')
    args = parser.parse_args(argv)
    headers = args.headers_file.read_text(errors='replace') if args.headers_file.exists() else ''
    body = args.body_file.read_bytes() if args.body_file.exists() else b''
    record(args.db, args.run_id, args.platform, args.url, args.status, args.requested_at, args.responded_at,
           headers, body, args.attempts, args.token_used, args.purpose, args.method, args.tool)
    return 0


if __name__ == '__main__':
    sys.exit(main())
