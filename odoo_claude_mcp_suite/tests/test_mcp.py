# -*- coding: utf-8 -*-
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger

from ..controllers import mcp_common, mcp_generic, mcp_tools
from ..controllers.mcp_urls import record_url as mcp_urls_record_url
from ..controllers.mcp_common import ToolError, register


class McpCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env['res.users'].create({
            'name': 'Claude MCP Test User',
            'login': 'claude_mcp_test_user',
            'tz': 'Africa/Cairo',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.api_key = cls.env['mcp.api.key'].create({
            'name': 'Test key',
            'user_id': cls.user.id,
            'all_models': True,
        })
        cls.env['mcp.allowed.model']._seed_defaults()
        cls.tag_a = cls.env['res.partner.category'].create({'name': 'MCP Tag A'})
        cls.tag_b = cls.env['res.partner.category'].create({'name': 'MCP Tag B'})
        cls.country = cls.env.ref('base.eg')
        Partner = cls.env['res.partner']
        cls.partners = Partner.create([
            {'name': 'MCP P1', 'date': '2026-03-10', 'country_id': cls.country.id,
             'category_id': [(6, 0, [cls.tag_a.id])], 'color': 1},
            {'name': 'MCP P2', 'date': '2026-01-05', 'country_id': cls.country.id,
             'category_id': [(6, 0, [cls.tag_a.id, cls.tag_b.id])], 'color': 2},
            {'name': 'MCP P3', 'date': '2026-02-20', 'country_id': cls.env.ref('base.fr').id,
             'category_id': [(6, 0, [cls.tag_b.id])], 'color': 3},
        ])

    def _env(self):
        return self.env(user=self.user.id, context=dict(self.env.context, tz='Africa/Cairo'), su=False)

    def _call(self, tool, **arguments):
        result = mcp_tools.call_tool(self._env(), tool, arguments, self.api_key)
        return result['content'][0]['text']


@tagged('post_install', '-at_install')
class TestTimeZone(McpCommon):

    def test_date_on_datetime_field_is_whole_local_day(self):
        env = self._env()
        rules = mcp_generic._allowed_rules(self.api_key)
        domain = mcp_generic._build_domain(
            env, rules, 'res.partner',
            [{'field': 'create_date', 'operator': '=', 'value': '2026-10-01'}])
        # Cairo is UTC+3 on 2026-10-01: the local day starts at 21:00 UTC the day before.
        self.assertIn(('create_date', '>=', '2026-09-30 21:00:00'), domain)
        self.assertIn(('create_date', '<', '2026-10-01 21:00:00'), domain)

    def test_le_on_datetime_includes_whole_day(self):
        env = self._env()
        rules = mcp_generic._allowed_rules(self.api_key)
        domain = mcp_generic._build_domain(
            env, rules, 'res.partner',
            [{'field': 'create_date', 'operator': '<=', 'value': '2026-10-01'}])
        self.assertEqual(domain, [('create_date', '<', '2026-10-01 21:00:00')])

    def test_local_datetime_value_converted_to_utc(self):
        env = self._env()
        rules = mcp_generic._allowed_rules(self.api_key)
        domain = mcp_generic._build_domain(
            env, rules, 'res.partner',
            [{'field': 'create_date', 'operator': '>=', 'value': '2026-10-01 10:00'}])
        self.assertEqual(domain, [('create_date', '>=', '2026-10-01 07:00:00')])

    def test_today_token_uses_user_timezone(self):
        env = self._env()
        with patch.object(mcp_generic, 'user_today', return_value=fields.Date.to_date('2026-10-12')):
            self.assertEqual(mcp_generic._resolve_token(env, '@today'), '2026-10-12')
            self.assertEqual(mcp_generic._resolve_token(env, '@start_of_last_month'), '2026-09-01')
            self.assertEqual(mcp_generic._resolve_token(env, '@end_of_last_month'), '2026-09-30')
        self.assertEqual(mcp_common.user_tz(env).zone, 'Africa/Cairo')

    def test_prefix_domain_with_expanded_date_leaf(self):
        env = self._env()
        rules = mcp_generic._allowed_rules(self.api_key)
        domain = mcp_generic._build_domain(
            env, rules, 'res.partner', [],
            ['|', ['create_date', '=', '2026-10-01'], ['name', '=', 'x']])
        self.assertEqual(domain[0], '|')
        self.assertEqual(len([d for d in domain if isinstance(d, tuple)]), 3)

    def test_malformed_domain_rejected(self):
        env = self._env()
        rules = mcp_generic._allowed_rules(self.api_key)
        with self.assertRaises(ToolError):
            mcp_generic._build_domain(env, rules, 'res.partner', [], ['|', ['name', '=', 'x']])


@tagged('post_install', '-at_install')
class TestGenericLayer(McpCommon):

    def test_group_by_date_is_chronological(self):
        text = self._call('query_data', model='res.partner',
                          filters=[{'field': 'name', 'operator': 'ilike', 'value': 'MCP P'}],
                          group_by=['date:month'])
        jan, feb, mar = text.index('January'), text.index('February'), text.index('March')
        self.assertTrue(jan < feb < mar, text)

    def test_many2one_shows_id(self):
        text = self._call('query_data', model='res.partner', fields=['name', 'country_id'],
                          filters=[{'field': 'name', 'operator': '=', 'value': 'MCP P1'}])
        self.assertIn(f"[{self.country.id}]", text)

    def test_many2many_filter_path_group_and_read(self):
        text = self._call('query_data', model='res.partner', fields=['name', 'category_id'],
                          filters=[{'field': 'category_id.name', 'operator': '=', 'value': 'MCP Tag B'}],
                          order_by='name', order_direction='asc')
        self.assertIn('MCP P2', text)
        self.assertIn('MCP P3', text)
        self.assertNotIn('MCP P1', text)
        self.assertIn('MCP Tag A', text)  # many2many values are shown by name
        grouped = self._call('query_data', model='res.partner', group_by=['category_id'],
                             filters=[{'field': 'name', 'operator': 'ilike', 'value': 'MCP P'}])
        self.assertIn(f"MCP Tag A [{self.tag_a.id}] | count=2", grouped)

    def test_count_distinct_on_many2one_and_having(self):
        text = self._call('query_data', model='res.partner',
                          filters=[{'field': 'name', 'operator': 'ilike', 'value': 'MCP P'}],
                          measures=[{'field': 'country_id', 'agg': 'count_distinct'}])
        self.assertIn('country_id_count_distinct=2', text)
        having = self._call('query_data', model='res.partner', group_by=['country_id'],
                            filters=[{'field': 'name', 'operator': 'ilike', 'value': 'MCP P'}],
                            having=[{'measure': 'count', 'operator': '>=', 'value': 2}])
        self.assertIn(self.country.name, having)
        self.assertNotIn('France', having)

    def test_records_offset(self):
        args = dict(model='res.partner', fields=['name'], limit=1, order_by='name', order_direction='asc',
                    filters=[{'field': 'name', 'operator': 'ilike', 'value': 'MCP P'}])
        first = self._call('query_data', **args)
        second = self._call('query_data', offset=1, **args)
        self.assertIn('MCP P1', first)
        self.assertIn('MCP P2', second)
        self.assertIn('offset=2', second)

    def test_blocked_field_rejected(self):
        rule = self.env['mcp.allowed.model'].search([('model_name', '=', 'res.partner')])
        field = self.env['ir.model.fields']._get('res.partner', 'color')
        rule.blocked_field_ids = [(4, field.id)]
        with self.assertRaises(ToolError):
            self._call('query_data', model='res.partner', fields=['color'])
        with self.assertRaises(ToolError):
            self._call('query_data', model='res.partner',
                       filters=[{'field': 'color', 'operator': '=', 'value': 1}])

    def test_x2many_to_non_whitelisted_model_rejected(self):
        rule = self.env['mcp.allowed.model'].search([('model_name', '=', 'res.partner.category')])
        rule.active = False
        with self.assertRaises(ToolError):
            self._call('query_data', model='res.partner',
                       filters=[{'field': 'category_id.name', 'operator': '=', 'value': 'MCP Tag B'}])

    def test_multi_query_survives_one_failure(self):
        text = self._call('multi_query', queries=[
            {'label': 'bad', 'model': 'no.such.model'},
            {'label': 'good', 'model': 'res.partner', 'measures': [{'field': 'color', 'agg': 'sum'}],
             'filters': [{'field': 'name', 'operator': 'ilike', 'value': 'MCP P'}]},
        ])
        self.assertIn('### bad\nERROR', text)
        self.assertIn('color_sum=6', text)


@tagged('post_install', '-at_install')
class TestRegistryAndHousekeeping(McpCommon):

    def test_single_registry(self):
        names = [d['name'] for d in mcp_tools.all_tool_definitions()]
        self.assertEqual(len(names), len(set(names)))
        for name in ('query_data', 'get_record', 'multi_query', 'search_customers'):
            self.assertIn(name, names)
        synced = self.env['mcp.tool'].search([]).mapped('name')
        self.assertEqual(set(synced), set(names))

    def test_tools_of_missing_apps_are_hidden(self):
        visible = {d['name'] for d in mcp_tools.visible_tool_definitions(self.env, self.api_key)}
        for name, tool in mcp_common.TOOLS.items():
            installed = all(m in self.env.registry for m in tool['requires'])
            allowed = mcp_common.access_allowed(self.api_key, tool['access'])
            self.assertEqual(name in visible, installed and allowed, name)

    def test_seed_respects_deleted_rules(self):
        Rule = self.env['mcp.allowed.model']
        rule = Rule.search([('model_name', '=', 'res.country')])
        self.assertTrue(rule)
        rule.unlink()
        Rule._seed_defaults()
        self.assertFalse(Rule.search([('model_name', '=', 'res.country')]))
        self.assertEqual(Rule._seed_defaults(force=True), 1)
        self.assertTrue(Rule.search([('model_name', '=', 'res.country')]))

    def test_log_purge(self):
        Log = self.env['mcp.tool.log']
        old, recent = Log.create([{'tool_name': 'old'}, {'tool_name': 'recent'}])
        self.env.cr.execute("UPDATE mcp_tool_log SET create_date = %s WHERE id = %s",
                            (fields.Datetime.now() - timedelta(days=200), old.id))
        self.env['ir.config_parameter'].sudo().set_param('odoo_claude_mcp_suite.log_retention_days', '90')
        Log.invalidate_model()
        self.assertGreaterEqual(Log._purge_old_logs(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.env['ir.config_parameter'].sudo().set_param('odoo_claude_mcp_suite.log_retention_days', '0')
        self.assertEqual(Log._purge_old_logs(), 0)


@tagged('post_install', '-at_install')
class TestSalesTools(McpCommon):

    def test_sales_tools_run(self):
        if 'sale.report' not in self.env.registry:
            self.skipTest('Sales is not installed')
        self.user.groups_id = [(4, self.env.ref('sales_team.group_sale_manager').id)]
        self._call('get_sales_analysis', period='this_year')
        self._call('get_top_selling_products', period='last_year', order_by='revenue')
        self._call('get_customers_by_last_purchase', min_months_since_last_purchase=0)
        self._call('search_customers', query='MCP')
        text = self._call('get_customer_sales', customer_name='MCP P1')
        self.assertIn('MCP P1', text)


@tagged('post_install', '-at_install')
class TestControllerRobustness(HttpCase):

    def setUp(self):
        super().setUp()
        user = self.env['res.users'].create({
            'name': 'Claude MCP Http User', 'login': 'claude_mcp_http_user', 'tz': 'Africa/Cairo',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.api_key = self.env['mcp.api.key'].create({'name': 'Http key', 'user_id': user.id, 'all_models': True})
        client = self.env['mcp.oauth.client'].create({'client_id': 'test', 'redirect_uris': '[]'})
        Token = self.env['mcp.oauth.token']
        self.raw_token = Token._new_raw_token()
        Token.create({
            'access_token_hash': Token._hash_token(self.raw_token),
            'refresh_token_hash': Token._hash_token(Token._new_raw_token()),
            'client_id_ref': client.id,
            'api_key_id': self.api_key.id,
            'access_expires_at': fields.Datetime.now() + timedelta(hours=1),
        })

    def _post(self, payload):
        response = self.url_open('/mcp', data=json.dumps(payload), headers={
            'Content-Type': 'application/json', 'Authorization': f'Bearer {self.raw_token}'})
        return response

    def test_database_error_is_logged_and_rolled_back(self):
        @register('_test_db_error', 'test only', {'type': 'object', 'properties': {}})
        def _boom(env, arguments, api_key):
            env['res.partner'].sudo().create({'name': 'MCP must be rolled back'})
            env.cr.execute("SELECT * FROM table_that_does_not_exist")

        try:
            with mute_logger('odoo.sql_db', 'odoo.addons.odoo_claude_mcp_suite.controllers.mcp_controller'):
                response = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                       'params': {'name': '_test_db_error', 'arguments': {}}})
        finally:
            mcp_common.TOOLS.pop('_test_db_error', None)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['result']['isError'])
        log = self.env['mcp.tool.log'].search([('tool_name', '=', '_test_db_error')])
        self.assertEqual(len(log), 1)
        self.assertFalse(log.success)
        self.assertIn('UndefinedTable', log.error_message)
        self.assertFalse(self.env['res.partner'].search([('name', '=', 'MCP must be rolled back')]))

    def test_initialize_reports_version_and_timezone(self):
        body = self._post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                           'params': {'protocolVersion': '2025-06-18'}}).json()
        self.assertEqual(body['result']['serverInfo']['version'], self.env['ir.module.module'].search(
            [('name', '=', 'odoo_claude_mcp_suite')]).installed_version)
        self.assertIn('Africa/Cairo', body['result']['instructions'])

    def test_batch_body_rejected_cleanly(self):
        response = self._post([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}])
        self.assertEqual(response.status_code, 400)


BUSINESS_GROUPS = [
    'sales_team.group_sale_manager', 'account.group_account_invoice', 'account.group_account_readonly',
    'purchase.group_purchase_manager', 'stock.group_stock_manager', 'mrp.group_mrp_manager',
]


@tagged('post_install', '-at_install')
class TestWriteTools(McpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groups = [cls.env.ref(x, raise_if_not_found=False) for x in BUSINESS_GROUPS]
        cls.user.groups_id = [(4, g.id) for g in groups if g]
        cls.user.groups_id = [(4, cls.env.ref('base.group_partner_manager').id)]

    def test_read_only_key_cannot_write(self):
        self.api_key.write_mode = 'read'
        with self.assertRaises(ToolError):
            self._call('create_record', model='res.partner', values={'name': 'MCP nope'})
        visible = {d['name'] for d in mcp_tools.visible_tool_definitions(self.env, self.api_key)}
        self.assertNotIn('create_record', visible)
        self.assertNotIn('execute_action', visible)

    def test_create_record_resolves_names_and_returns_url(self):
        self.api_key.write_mode = 'create'
        text = self._call('create_record', model='res.partner',
                          values={'name': 'MCP Created', 'country_id': 'Egypt', 'category_id': ['MCP Tag A']})
        partner = self.env['res.partner'].search([('name', '=', 'MCP Created')])
        self.assertEqual(partner.country_id, self.country)
        self.assertEqual(partner.category_id, self.tag_a)
        self.assertIn(f"/web#id={partner.id}&model=res.partner", text)

    def test_create_record_rejects_unsafe_fields(self):
        self.api_key.write_mode = 'create'
        for values in ({'name': 'x', 'create_date': '2020-01-01'}, {'name': 'x', 'no_such_field': 1},
                       {'name': 'x', 'commercial_partner_id': 1}, {'name': 'x', 'country_id': 'Atlantis'}):
            with self.assertRaises(ToolError, msg=values):
                self._call('create_record', model='res.partner', values=values)

    def test_create_needs_allow_create_on_model(self):
        self.api_key.write_mode = 'create'
        rule = self.env['mcp.allowed.model'].search([('model_name', '=', 'res.partner')])
        rule.allow_create = False
        with self.assertRaises(ToolError):
            self._call('create_record', model='res.partner', values={'name': 'MCP nope'})

    def test_bulk_reports_each_row(self):
        self.api_key.write_mode = 'create'
        text = self._call('create_records_bulk', model='res.partner', records=[
            {'name': 'MCP Bulk 1'}, {'name': 'MCP Bulk 2', 'country_id': 'Atlantis'}, {'name': 'MCP Bulk 3'}])
        self.assertIn('2 created, 1 failed', text)
        self.assertIn('#2 FAILED', text)
        self.assertEqual(self.env['res.partner'].search_count([('name', 'like', 'MCP Bulk')]), 2)

    def test_bulk_all_or_nothing(self):
        self.api_key.write_mode = 'create'
        with self.assertRaises(ToolError):
            with self.env.cr.savepoint():  # the controller wraps write tools the same way
                self._call('create_records_bulk', model='res.partner', all_or_nothing=True, records=[
                    {'name': 'MCP AON 1'}, {'name': 'MCP AON 2', 'country_id': 'Atlantis'}])
        self.assertFalse(self.env['res.partner'].search([('name', 'like', 'MCP AON')]))

    def test_sale_order_draft_then_confirm(self):
        if 'sale.order' not in self.env.registry:
            self.skipTest('Sales is not installed')
        product = self.env['product.product'].create({'name': 'MCP Widget', 'list_price': 10})
        self.api_key.write_mode = 'create'
        self._call('create_record', model='sale.order', values={
            'partner_id': 'MCP P1', 'order_line': [{'product_id': 'MCP Widget', 'product_uom_qty': 3}]})
        order = self.env['sale.order'].search([('partner_id', '=', self.partners[0].id)])
        self.assertEqual(order.state, 'draft')
        self.assertEqual(order.order_line.product_uom_qty, 3)
        self.assertEqual(order.order_line.product_id, product)
        with self.assertRaises(ToolError):  # actions need write mode 'full'
            self._call('execute_action', model='sale.order', action='confirm', ids=[order.id])
        self.api_key.write_mode = 'full'
        text = self._call('execute_action', model='sale.order', action='confirm', ids=[order.id, 999999])
        self.assertEqual(order.state, 'sale')
        self.assertIn('1 done', text)
        self.assertIn('1 not found', text)
        self.assertIn(f"action={self.env.ref('sale.action_orders').id}", text)
        self.assertTrue(order.message_ids.filtered(lambda m: 'executed by Claude' in (m.body or '')))

    def test_customer_invoice_draft_then_post(self):
        if 'account.move' not in self.env.registry or not self.env['account.journal'].search(
                [('type', '=', 'sale'), ('company_id', '=', self.env.company.id)], limit=1):
            self.skipTest('No sales journal')
        self.api_key.write_mode = 'full'
        text = self._call('create_record', model='account.move', values={
            'move_type': 'out_invoice', 'partner_id': self.partners[0].id, 'invoice_date': '@today',
            'invoice_line_ids': [{'name': 'MCP service', 'quantity': 2, 'price_unit': 50}]})
        move = self.env['account.move'].search([('partner_id', '=', self.partners[0].id)], limit=1)
        self.assertEqual((move.move_type, move.state), ('out_invoice', 'draft'))
        self.assertIn(f"action={self.env.ref('account.action_move_out_invoice_type').id}", text)
        self._call('execute_action', model='account.move', action='confirm', ids=[move.id])
        self.assertEqual(move.state, 'posted')

    def test_state_cannot_be_forced(self):
        if 'sale.order' not in self.env.registry:
            self.skipTest('Sales is not installed')
        self.api_key.write_mode = 'full'
        with self.assertRaises(ToolError):
            self._call('create_record', model='sale.order', values={'partner_id': 'MCP P1', 'state': 'sale'})

    def test_unknown_action_rejected(self):
        self.api_key.write_mode = 'full'
        with self.assertRaises(ToolError):
            self._call('execute_action', model='res.partner', action='unlink', ids=[self.partners[0].id])


@tagged('post_install', '-at_install')
class TestBusinessTools(McpCommon):

    def test_business_tools_run(self):
        groups = [self.env.ref(x, raise_if_not_found=False) for x in BUSINESS_GROUPS]
        self.user.groups_id = [(4, g.id) for g in groups if g]
        calls = {
            'get_overdue_invoices': {}, 'get_upcoming_due': {'days': 30}, 'get_unpaid_bills': {},
            'get_upcoming_vendor_bills': {'days': 30}, 'get_open_orders': {}, 'get_top_customers': {},
            'get_pending_purchases': {}, 'get_open_leads': {}, 'get_hot_opportunities': {},
            'get_low_stock': {}, 'get_stock_value': {},
        }
        ran = 0
        for name, arguments in calls.items():
            if not mcp_common.tool_installed(self.env, name):
                continue
            text = self._call(name, **arguments)
            self.assertTrue(text, name)
            ran += 1
        if not ran:
            self.skipTest('No business app installed')

    def test_url_uses_window_action(self):
        url = mcp_urls_record_url(self.env, self.partners[0])
        self.assertIn(f"id={self.partners[0].id}&model=res.partner&view_type=form", url)
