#!/usr/bin/env python3
"""Upload an XLSX to the Odoo historical import staging endpoint.

Usage: python3 historical_upload.py /path/to/file.xlsx contracted
The Cloud network proxy must inject X-Historical-Upload-Key for the target domain.
This script creates a staging session; approval/execution use Odoo MCP tools.
"""
import hashlib
import json
import sys
from pathlib import Path

import requests

# URL = 'https://example.odoo.com/mcp/historical/upload'
URL = 'url/mcp/historical/upload'
MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in ('contracted', 'reserved'):
        sys.exit('Usage: historical_upload.py FILE.xlsx contracted|reserved')
    path = Path(sys.argv[1])
    if not path.is_file() or path.suffix.lower() != '.xlsx':
        sys.exit('ERROR: XLSX file not found')
    if len(path.name) > 180 or any(ord(c) < 32 for c in path.name):
        sys.exit('ERROR: invalid XLSX filename')
    raw = path.read_bytes()
    if not 0 < len(raw) <= 10 * 1024 * 1024:
        sys.exit('ERROR: file size outside supported range (1-10 MB)')
    sha = hashlib.sha256(raw).hexdigest()
    try:
        result = requests.post(URL, data=raw, headers={
            'X-Filename': path.name,
            'X-Import-Type': sys.argv[2],
            'X-Content-SHA256': sha,
            'Content-Type': MIME,
        }, timeout=120)
    except requests.RequestException as exc:
        sys.exit('UPLOAD UNCERTAIN: network error; do not retry blindly: ' + str(exc))
    if result.status_code != 201:
        sys.exit(f'UPLOAD FAILED HTTP {result.status_code}: {result.text[:300]}')
    try:
        payload = result.json()
    except ValueError:
        sys.exit('UPLOAD UNCERTAIN: unexpected response; check sessions before retrying')
    if payload.get('sha256') != sha or not payload.get('session_id'):
        sys.exit('UPLOAD UNCERTAIN: missing session ID or mismatched hash; do not retry blindly')
    print(json.dumps({'session_id': payload['session_id'], 'state': payload.get('state'), 'sha256': sha}))


if __name__ == '__main__':
    main()
