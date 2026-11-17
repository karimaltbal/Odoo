# -*- coding: utf-8 -*-
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

RETENTION_PARAM = 'odoo_claude_mcp.log_retention_days'
DEFAULT_RETENTION_DAYS = 90
PURGE_BATCH_SIZE = 5000


class McpToolLog(models.Model):
    _name = 'mcp.tool.log'
    _description = 'Claude MCP Call Log'
    _order = 'create_date desc, id desc'

    api_key_id = fields.Many2one('mcp.api.key', string='API Key', ondelete='set null', index=True)
    user_id = fields.Many2one('res.users', string='Ran As')
    method = fields.Char(string='JSON-RPC Method')
    tool_name = fields.Char(string='Tool Name', index=True)
    access = fields.Selection(
        [('read', 'Read'), ('create', 'Create'), ('action', 'Business Action')],
        string='Access', default='read', index=True)
    arguments = fields.Text(string='Arguments (JSON)')
    result_summary = fields.Text(string='Result Summary')
    success = fields.Boolean(default=True)
    error_message = fields.Text()

    @api.model
    def _retention_days(self):
        """Days to keep call logs. 0 (or a negative value) keeps them forever."""
        raw = self.env['ir.config_parameter'].sudo().get_param(RETENTION_PARAM, DEFAULT_RETENTION_DAYS)
        try:
            return int(raw)
        except (TypeError, ValueError):
            _logger.warning("Invalid %s=%r, using %s days", RETENTION_PARAM, raw, DEFAULT_RETENTION_DAYS)
            return DEFAULT_RETENTION_DAYS

    @api.model
    def _purge_old_logs(self, days=None):
        """Delete logs older than the retention period, in batches. Returns the
        number of deleted rows."""
        days = self._retention_days() if days is None else days
        if days <= 0:
            return 0
        cutoff = fields.Datetime.now() - timedelta(days=days)
        deleted = 0
        while True:
            batch = self.sudo().search([('create_date', '<', cutoff)], limit=PURGE_BATCH_SIZE)
            if not batch:
                break
            deleted += len(batch)
            batch.unlink()
            if len(batch) < PURGE_BATCH_SIZE:
                break
        if deleted:
            _logger.info("Claude MCP: purged %s call log(s) older than %s days", deleted, days)
        return deleted

    @api.model
    def _cron_purge_old_logs(self):
        self._purge_old_logs()

    @api.model
    def action_purge_old_logs(self):
        """Server action behind the 'Purge Old Logs' menu entry."""
        days = self._retention_days()
        if days <= 0:
            message = (f"Retention is disabled ({RETENTION_PARAM} = {days}); nothing was deleted. "
                       "Set it to a number of days under Settings > Technical > System Parameters.")
        else:
            deleted = self._purge_old_logs(days)
            message = f"{deleted} call log(s) older than {days} days deleted."
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Claude MCP Call Log',
                'message': message,
                'type': 'success',
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }
