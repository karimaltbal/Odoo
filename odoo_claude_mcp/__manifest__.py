# -*- coding: utf-8 -*-
{
    'name': 'Odoo Claude MCP Connector',
    'version': '18.0.1.9.0',
    'category': 'Productivity / Integrations',
    'author': 'Karim Ahmed',
    'summary': 'Secure Odoo MCP integration for AI clients, business insights and controlled actions',
    'description': """
        Claude MCP Server Integration

        This module exposes an MCP (Model Context Protocol) endpoint from within Odoo,
        allowing Claude to query business data through controlled, permission-respecting
        tools instead of raw SQL access.

        Key features:

        * Streamable HTTP MCP transport (self-hosted in your Odoo instance)
        * API Key authentication mapped to a specific Odoo user
        * Tool-based access (no free-form SQL exposed to the model)
        * Audit logging of every tool call, with a retention period and a daily purge
        * Generic query layer: cross-model field search, OR domains, paths of up to 3 relational hops (many2one, one2many, many2many), date tokens, count_distinct, HAVING-style filters on totals, paging, record + lines expansion, batched queries
        * Dates interpreted and shown in the user's time zone; data scoped to all of the user's companies
        * Read tools run in rolled-back savepoints; write actions require explicitly configured permissions
        * Write tools (off by default, enabled per API key and per model): create_record and
        create_records_bulk (up to 200 rows, per-row results) always create DRAFT records;
        execute_action runs curated business actions (confirm, post, validate, won/lost...) in bulk.
        Every change is noted in the record's chatter and in the Call Log
        * Direct record URLs in every result, built from Odoo's own window actions
        * Business tools: overdue / upcoming customer invoices, unpaid and upcoming vendor bills,
        open sales orders, top customers, pending purchases, open leads, hot opportunities,
        low stock vs reorder points, stock value by category
        * Sales, Purchase and Inventory tools appear only when those apps are installed; their default Queryable Models are added automatically when those apps are installed (or on demand with Claude Connector > Add Default Models)
    """,
    'license': 'LGPL-3',
    'price': 10.0,
    'currency': 'USD',
    'images': ['static/description/banner.png'],
    'depends': ['base', 'web'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        
        'views/mcp_api_key_views.xml',
        
        'data/mcp_cron.xml',
        'data/mcp_data.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
