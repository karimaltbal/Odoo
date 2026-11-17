"""Temporary, authenticated MCP chunk transfer; no historical business logic here."""
import base64
import binascii
import hashlib
import io
import zipfile
from odoo import fields, models
from odoo.exceptions import UserError

MAX_BYTES = 10 * 1024 * 1024
CHUNK_BYTES = 48 * 1024


class HistoricalUploadTransfer(models.Model):
    _name = 'mcp.historical.upload.transfer'
    _description = 'Temporary MCP Historical XLSX Transfer'

    name = fields.Char(required=True)
    import_type = fields.Selection([('contracted', 'Contracted'), ('reserved', 'Reserved')], required=True)
    owner_id = fields.Many2one('res.users', required=True, default=lambda self: self.env.user)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    expected_sha256 = fields.Char(required=True)
    expected_size = fields.Integer(required=True)
    chunk_count = fields.Integer(required=True)
    state = fields.Selection([('pending', 'Pending'), ('completed', 'Completed')], default='pending', required=True)
    session_id = fields.Many2one('mcp.historical.import.session', readonly=True)
    chunk_ids = fields.One2many('mcp.historical.upload.chunk', 'transfer_id')

    def _check_owner(self):
        self.ensure_one()
        if self.owner_id != self.env.user or self.company_id not in self.env.companies:
            raise UserError('Transfer is not accessible')
        if not self.env.user.has_group('odoo_claude_mcp.group_historical_mcp_import'):
            raise UserError('Historical import operator permission required')

    def append_chunk(self, index, encoded):
        self._check_owner()
        self.env.cr.execute('SELECT id FROM mcp_historical_upload_transfer WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset(['state'])
        if self.state != 'pending':
            raise UserError('Transfer is already completed')
        if index < 0 or index >= self.chunk_count:
            raise UserError('Chunk index out of range')
        if len(encoded) > 66000:
            raise UserError('Chunk is too large')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise UserError('Invalid base64 chunk')
        if not raw or len(raw) > CHUNK_BYTES:
            raise UserError('Invalid chunk length')
        existing = self.chunk_ids.filtered(lambda c: c.sequence == index)
        digest = hashlib.sha256(raw).hexdigest()
        if existing:
            if existing.sha256 != digest:
                raise UserError('Chunk already uploaded with different content')
            return {'received': index, 'duplicate': True, 'chunks_received': len(self.chunk_ids)}
        self.env['mcp.historical.upload.chunk'].create({
            'transfer_id': self.id, 'sequence': index, 'data_base64': encoded, 'sha256': digest,
        })
        return {'received': index, 'duplicate': False, 'chunks_received': len(self.chunk_ids)}

    def finish(self):
        self._check_owner()
        self.env.cr.execute('SELECT id FROM mcp_historical_upload_transfer WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset(['state', 'session_id'])
        if self.state == 'completed':
            return {'session_id': self.session_id.id, 'state': self.session_id.state, 'already_completed': True}
        chunks = self.chunk_ids.sorted('sequence')
        if len(chunks) != self.chunk_count or any(c.sequence != i for i, c in enumerate(chunks)):
            raise UserError('Missing chunks; upload all chunks before finishing')
        raw = b''.join(base64.b64decode(c.data_base64, validate=True) for c in chunks)
        if len(raw) != self.expected_size or hashlib.sha256(raw).hexdigest() != self.expected_sha256:
            raise UserError('Workbook size or SHA-256 mismatch; import session not created')
        if not zipfile.is_zipfile(io.BytesIO(raw)):
            raise UserError('Invalid XLSX container')
        rec = self.env['mcp.historical.import.session'].create({
            'name': self.name, 'filename': self.name, 'import_type': self.import_type,
            'file': base64.b64encode(raw), 'file_hash': self.expected_sha256,
        })
        self.write({'state': 'completed', 'session_id': rec.id})
        chunks.unlink()
        return {'session_id': rec.id, 'state': rec.state, 'sha256': self.expected_sha256}


class HistoricalUploadChunk(models.Model):
    _name = 'mcp.historical.upload.chunk'
    _description = 'Temporary MCP XLSX Chunk'

    transfer_id = fields.Many2one('mcp.historical.upload.transfer', required=True, ondelete='cascade')
    sequence = fields.Integer(required=True)
    data_base64 = fields.Text(required=True)
    sha256 = fields.Char(required=True)

    _sql_constraints = [('transfer_sequence_unique', 'unique(transfer_id, sequence)', 'Chunk index must be unique per transfer')]
