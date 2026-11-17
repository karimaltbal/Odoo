# -*- coding: utf-8 -*-
"""Direct record URLs built from Odoo's own window actions.

A record opened with the right window action shows the correct breadcrumb,
menu and form view (e.g. a vendor bill opens in Purchases > Bills, not as a
generic journal entry). The action is chosen in this order:
  1. a known action for the model, depending on the record (move type, state, lead type);
  2. any window action of the model that is reachable from a menu;
  3. any window action of the model;
  4. no action (the generic form view).
"""
from .mcp_common import ODOO_MAJOR

# model -> xmlid, list of xmlids (first installed wins) or callable(record) -> xmlid(s)
PREFERRED_ACTIONS = {
    'account.move': lambda rec: {
        'out_invoice': 'account.action_move_out_invoice_type',
        'out_refund': 'account.action_move_out_refund_type',
        'out_receipt': 'account.action_move_out_receipt_type',
        'in_invoice': 'account.action_move_in_invoice_type',
        'in_refund': 'account.action_move_in_refund_type',
        'in_receipt': 'account.action_move_in_receipt_type',
    }.get(rec.move_type, 'account.action_move_journal_line'),
    'account.payment': lambda rec: (
        'account.action_account_payments' if rec.payment_type == 'inbound'
        else 'account.action_account_payments_payable'),
    'sale.order': lambda rec: (
        'sale.action_orders' if rec.state in ('sale', 'done')
        else ['sale.action_quotations_with_onboarding', 'sale.action_quotations']),
    'purchase.order': lambda rec: (
        'purchase.purchase_form_action' if rec.state in ('purchase', 'done')
        else 'purchase.purchase_rfq'),
    'crm.lead': lambda rec: (
        'crm.crm_lead_action_pipeline' if rec.type == 'opportunity'
        else 'crm.crm_lead_all_leads'),
    'stock.picking': 'stock.action_picking_tree_all',
    'res.partner': ['contacts.action_contacts', 'account.res_partner_action_customer',
                    'base.action_partner_form'],
    'product.template': ['stock.product_template_action_product', 'sale.product_template_action',
                         'purchase.product_normal_action_puchased', 'product.product_template_action'],
    'product.product': ['stock.stock_product_normal_action', 'product.product_normal_action_sell',
                        'product.product_normal_action'],
    'mrp.production': 'mrp.mrp_production_action',
    'project.task': 'project.action_view_all_task',
    'project.project': 'project.open_view_project_all',
    'hr.employee': 'hr.open_view_employee_list_my',
    'hr.leave': 'hr_holidays.hr_leave_action_action_approve_department',
}

# (registry id, cache key) -> action id or 0; registry id changes on reload.
_ACTION_CACHE = {}


def _cache_get(env, key, compute):
    cache_key = (id(env.registry), env.cr.dbname, key)
    if cache_key not in _ACTION_CACHE:
        if len(_ACTION_CACHE) > 2000:
            _ACTION_CACHE.clear()
        _ACTION_CACHE[cache_key] = compute()
    return _ACTION_CACHE[cache_key]


def _action_from_xmlids(env, model_name, xmlids):
    if isinstance(xmlids, str):
        xmlids = [xmlids]

    def compute():
        for xmlid in xmlids:
            action = env.ref(xmlid, raise_if_not_found=False)
            if action and action._name == 'ir.actions.act_window' and action.sudo().res_model == model_name:
                return action.id
        return 0
    return _cache_get(env, ('xmlid', model_name, tuple(xmlids)), compute)


def _default_action(env, model_name):
    def compute():
        actions = env['ir.actions.act_window'].sudo().search(
            [('res_model', '=', model_name)], order='id')
        if not actions:
            return 0
        refs = [f'ir.actions.act_window,{a.id}' for a in actions]
        menus = env['ir.ui.menu'].sudo().with_context(active_test=False).search([('action', 'in', refs)])
        with_menu = {int(m.action.id) for m in menus if m.action}
        for action in actions:
            if action.id in with_menu:
                return action.id
        return actions[0].id
    return _cache_get(env, ('model', model_name), compute)


def _action_id(env, record):
    preferred = PREFERRED_ACTIONS.get(record._name)
    if preferred is not None:
        try:
            xmlids = preferred(record) if callable(preferred) else preferred
        except Exception:  # noqa: BLE001 - e.g. no read access on the deciding field
            xmlids = None
        if xmlids:
            action_id = _action_from_xmlids(env, record._name, xmlids)
            if action_id:
                return action_id
    return _default_action(env, record._name)


def base_url(env):
    return (env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')


def record_url(env, record):
    """Direct URL opening `record` (a single record) in its form view."""
    if not record:
        return ''
    record = record[:1]
    action_id = _action_id(env, record)
    base = base_url(env)
    if ODOO_MAJOR >= 18:
        if action_id:
            return f"{base}/odoo/action-{action_id}/{record.id}"
        return f"{base}/odoo/{record._name}/{record.id}"
    url = f"{base}/web#id={record.id}&model={record._name}&view_type=form"
    if action_id:
        url += f"&action={action_id}"
    return url


def url_for(env, model_name, record_id):
    if not record_id or model_name not in env.registry:
        return ''
    return record_url(env, env[model_name].browse(record_id))
