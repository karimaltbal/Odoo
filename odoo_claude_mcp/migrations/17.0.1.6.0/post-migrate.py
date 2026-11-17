# -*- coding: utf-8 -*-
"""17.0.1.6.0 adds write tools. Existing Queryable Model rules get the default
write flags of the main business documents. Nothing becomes writable until an
administrator also changes the API key's Write Access (default: Read only)."""
from odoo import SUPERUSER_ID, api

from odoo.addons.odoo_claude_mcp.models.mcp_access import SEED_WRITE


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Rule = env['mcp.allowed.model'].with_context(active_test=False)
    for model_name, (allow_create, allow_actions) in SEED_WRITE.items():
        Rule.search([('model_name', '=', model_name)]).write({
            'allow_create': allow_create,
            'allow_actions': allow_actions,
        })
