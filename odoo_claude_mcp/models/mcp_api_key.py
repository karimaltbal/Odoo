# -*- coding: utf-8 -*-
import hashlib
import secrets

from odoo import api, fields, models


class McpApiKey(models.Model):
    _name = 'mcp.api.key'
    _description = 'Claude MCP API Key'
    _order = 'create_date desc'

    name = fields.Char(
        string='Label',
        required=True,
        help="A human-readable name, e.g. 'Claude - Sales Read Only'",
    )
    user_id = fields.Many2one(
        'res.users',
        string='Run As User',
        required=True,
        help="All tool calls made with this key will execute with this "
             "user's permissions. Use a dedicated, restricted user - never "
             "an Administrator account.",
    )
    key_hash = fields.Char(string='Key Hash', copy=False, readonly=True)
    key_preview = fields.Char(
        string='Key Preview',
        readonly=True,
        copy=False,
        help="Last 6 characters of the key, for identification purposes only.",
    )
    active = fields.Boolean(default=True)
    last_used = fields.Datetime(string='Last Used', readonly=True, copy=False)

    tool_access = fields.Selection(
        [('all', 'All tools'), ('custom', 'Selected tools only')],
        string='Tool Access', default='all', required=True,
        help="With 'Selected tools only', Claude will only SEE and be able to "
             "call the tools chosen below. Everything else is hidden and refused.",
    )
    allowed_tool_ids = fields.Many2many(
        'mcp.tool', 'mcp_api_key_tool_rel', 'key_id', 'tool_id',
        string='Allowed Tools')
    allowed_model_ids = fields.Many2many(
        'mcp.allowed.model', 'mcp_api_key_model_rel', 'key_id', 'model_id',
        string='Queryable Models',
        help="Models the generic tools (list_models / describe_model / "
             "query_data) may read for this key. Empty = no generic access. "
             "Odoo permissions of the 'Run As User' still apply on top.")
    all_models = fields.Boolean(
        string='All whitelisted models', default=False,
        help="When checked, this key may query EVERY active model in "
             "'Queryable Models' (including ones added later), instead of only "
             "the models chosen in the list. Blocked fields and the Odoo "
             "permissions of the 'Run As User' still apply.")
    write_mode = fields.Selection(
        [('read', 'Read only'),
         ('create', 'Read + create draft records'),
         ('full', 'Read + create drafts + business actions')],
        string='Write Access', default='read', required=True,
        help="Read only: Claude can only query data.\n"
             "Create: Claude can also create DRAFT records (create_record, create_records_bulk) "
             "on models where 'Allow Create' is ticked.\n"
             "Full: Claude can also run business actions (confirm, post, validate, won/lost...) "
             "with execute_action on models where 'Allow Business Actions' is ticked.\n"
             "The Odoo access rights of the 'Run As User' always apply on top.")
    user_is_admin = fields.Boolean(compute='_compute_user_is_admin')

    @api.depends('user_id')
    def _compute_user_is_admin(self):
        for rec in self:
            rec.user_is_admin = bool(
                rec.user_id and rec.user_id.has_group('base.group_system'))

    def _tool_allowed(self, tool_name):
        self.ensure_one()
        if self.tool_access == 'all':
            return True
        return tool_name in self.allowed_tool_ids.filtered('active').mapped('name')

    @staticmethod
    def _hash_key(raw_key):
        return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()

    def action_generate_key(self):
        """Generates a brand new key, overwriting any previous one for this
        record, and shows it once to the user via a transient wizard."""
        self.ensure_one()
        raw_key = 'mcp_' + secrets.token_urlsafe(32)
        self.write({
            'key_hash': self._hash_key(raw_key),
            'key_preview': raw_key[-6:],
        })
        wizard = self.env['mcp.api.key.show.wizard'].create({
            'api_key_id': self.id,
            'raw_key': raw_key,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mcp.api.key.show.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }

    @api.model
    def _authenticate(self, raw_key):
        """Given a raw key from an Authorization header, return the matching
        active mcp.api.key record (sudo), or an empty recordset if invalid."""
        if not raw_key:
            return self.env['mcp.api.key']
        key_hash = self._hash_key(raw_key)
        record = self.sudo().search(
            [('key_hash', '=', key_hash), ('active', '=', True)], limit=1
        )
        if record:
            record.sudo().write({'last_used': fields.Datetime.now()})
        return record


class McpApiKeyShowWizard(models.TransientModel):
    _name = 'mcp.api.key.show.wizard'
    _description = 'Show Generated MCP API Key (one time)'

    api_key_id = fields.Many2one('mcp.api.key', required=True)
    raw_key = fields.Char(readonly=True)

    def action_confirm_copied(self):
        return {'type': 'ir.actions.act_window_close'}
