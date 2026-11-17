# -*- coding: utf-8 -*-
"""Authenticated raw XLSX ingress for Claude's code execution environment.

No financial execution takes place here. The endpoint only creates a session.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import zipfile
from io import BytesIO

from odoo import http
from odoo.http import request
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)
_MAX_BYTES = 10 * 1024 * 1024
_DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')


def _reply(payload, status=200):
    return request.make_response(
        json.dumps(payload), status=status,
        headers=[('Content-Type', 'application/json'), ('Cache-Control', 'no-store')],
    )


class HistoricalDirectUpload(http.Controller):
    @http.route('/mcp/historical/client.py', type='http', auth='none', methods=['GET'], csrf=False)
    def historical_upload_client(self, **kwargs):
        """Distribute the credential-free upload client to fresh Claude sessions.

        No authentication token, Odoo record or user data is returned.
        """
        from pathlib import Path
        client = Path(__file__).resolve().parent.parent / 'static' / 'tools' / 'historical_upload.py'
        return request.make_response(
            client.read_bytes(),
            headers=[('Content-Type', 'text/x-python; charset=utf-8'),
                     ('Cache-Control', 'public, max-age=300'),
                     ('X-Content-Type-Options', 'nosniff')],
        )

    @http.route('/mcp/historical/upload', type='http', auth='none', methods=['POST'], csrf=False)
    def upload_xlsx(self, **kwargs):
        # Dedicated revocable token. The Odoo database stores only its SHA-256 hash.
        params = request.env['ir.config_parameter'].sudo()
        configured_hash = params.get_param('odoo_claude_mcp.direct_upload_token_sha256', '')
        user_id_text = params.get_param('odoo_claude_mcp.direct_upload_user_id', '')
        supplied = request.httprequest.headers.get('X-Historical-Upload-Key', '')
        if (not configured_hash or not _DIGEST_RE.fullmatch(configured_hash)
                or not supplied or len(supplied) > 256
                or not hmac.compare_digest(hashlib.sha256(supplied.encode('utf-8')).hexdigest(), configured_hash)):
            return _reply({'error': 'unauthorized'}, 401)
        try:
            user_id = int(user_id_text)
        except (TypeError, ValueError):
            return _reply({'error': 'upload_not_configured'}, 503)
        user = request.env['res.users'].sudo().browse(user_id).exists()
        if not user or not user.active or not user.has_group('odoo_claude_mcp.group_historical_mcp_import'):
            return _reply({'error': 'upload_user_not_authorized'}, 403)

        filename = request.httprequest.headers.get('X-Filename', '').strip()
        import_type = request.httprequest.headers.get('X-Import-Type', '').strip()
        expected_sha = request.httprequest.headers.get('X-Content-SHA256', '').lower().strip()
        if (not filename or len(filename) > 180 or not filename.lower().endswith('.xlsx')
                or '/' in filename or '\\' in filename or any(ord(c) < 32 for c in filename)
                or import_type not in ('contracted', 'reserved') or not _DIGEST_RE.fullmatch(expected_sha)):
            return _reply({'error': 'invalid_metadata'}, 400)
        length = request.httprequest.content_length
        if length is None or length < 1 or length > _MAX_BYTES:
            return _reply({'error': 'invalid_file_size', 'max_bytes': _MAX_BYTES}, 413)
        # Werkzeug bounds reading to Content-Length. Reject mismatch and non-XLSX.
        raw = request.httprequest.get_data(cache=False)
        if len(raw) != length or len(raw) > _MAX_BYTES:
            return _reply({'error': 'invalid_file_size'}, 413)
        actual_sha = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(actual_sha, expected_sha):
            return _reply({'error': 'sha256_mismatch'}, 422)
        if not zipfile.is_zipfile(BytesIO(raw)):
            return _reply({'error': 'invalid_xlsx_container'}, 422)
        try:
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                names = set(archive.namelist())
                if '[Content_Types].xml' not in names or 'xl/workbook.xml' not in names:
                    return _reply({'error': 'invalid_xlsx_structure'}, 422)
                if sum(info.file_size for info in archive.infolist()) > 80 * 1024 * 1024:
                    return _reply({'error': 'expanded_file_too_large'}, 422)
        except (OSError, zipfile.BadZipFile):
            return _reply({'error': 'invalid_xlsx_container'}, 422)

        # Create using the configured operator's normal ACLs and company context.
        # Never sudo the session creation; never call approve/execute here.
        env = request.env(user=user.id, su=False, context=dict(
            request.env.context, allowed_company_ids=[user.company_id.id],
        ))
        try:
            rec = env['mcp.historical.import.session'].create({
                'name': filename,
                'filename': filename,
                'import_type': import_type,
                'file': base64.b64encode(raw).decode('ascii'),
                'company_id': user.company_id.id,
                'owner_id': user.id,
            })
            return _reply({'session_id': rec.id, 'state': rec.state,
                           'sha256': actual_sha, 'size_bytes': len(raw)}, 201)
        except (AccessError, UserError, ValidationError) as exc:
            _logger.warning('Historical direct upload rejected: %s', type(exc).__name__)
            return _reply({'error': 'session_creation_rejected', 'reason': str(exc)}, 422)
