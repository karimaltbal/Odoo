# -*- coding: utf-8 -*-
"""Ready-made business tools for Accounting, Sales, Purchase, CRM and Inventory.

All of them are read-only, computed by the database over all matching rows,
use the user's time zone for "today", and include a direct URL per record.
Each tool is only listed when its app is installed (see `requires`).
"""
import datetime as dt

from odoo import fields

from .mcp_common import (
    ToolError, datetime_day_range, day_start_utc, format_local_datetime, register,
    text_result, user_today,
)
from .mcp_generic import _cap_output
from .mcp_urls import record_url, url_for

MAX_ROWS = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _int(arguments, name, default, minimum=0, maximum=None):
    value = arguments.get(name)
    if value in (None, ''):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"'{name}' must be an integer")
    if number < minimum:
        raise ToolError(f"'{name}' must be >= {minimum}")
    return min(number, maximum) if maximum is not None else number


def _money(value):
    return f"{value or 0.0:,.2f}"


def _qty(value):
    return f"{value or 0.0:,.2f}".rstrip('0').rstrip('.')


def _partner_domain(arguments, name_path):
    """Optional partner filter: partner_id (that partner and all its contacts)
    or partner_name (partial match on the company name)."""
    partner_id = arguments.get('partner_id')
    if partner_id not in (None, ''):
        try:
            return [('partner_id', 'child_of', int(partner_id))]
        except (TypeError, ValueError):
            raise ToolError("'partner_id' must be an integer")
    name = (arguments.get('partner_name') or '').strip()
    if name:
        return [(name_path, 'ilike', name)]
    return []


PARTNER_PROPERTIES = {
    "partner_id": {"type": "integer", "description": "Optional exact customer/vendor id"},
    "partner_name": {"type": "string", "description": "Optional partial customer/vendor name"},
}
LIMIT_PROPERTY = {"limit": {"type": "integer", "description": f"Rows to list, default 20, max {MAX_ROWS}"}}


def _company_totals(env, model, domain, amount_field, sign=1):
    """Totals per company (each in its own currency), computed by the database."""
    groups = env[model].read_group(domain, [f'total:sum({amount_field})'], ['company_id'], lazy=False)
    parts = []
    for g in groups:
        company = env['res.company'].browse(g['company_id'][0]) if g.get('company_id') else env.company
        parts.append(f"{_money(sign * (g.get('total') or 0.0))} {company.currency_id.name}"
                     + (f" ({company.name})" if len(groups) > 1 else ""))
    count = sum(g.get('__count', 0) for g in groups)
    return count, ", ".join(parts) or "0.00"


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------
UNPAID = ('not_paid', 'partial')


def _invoice_report(env, arguments, move_type, title, due_from=None, due_to=None,
                    aging=False, sort_default='due_date'):
    customer = move_type == 'out_invoice'
    sign = 1 if customer else -1  # amount_residual_signed is negative on vendor bills
    domain = [('move_type', '=', move_type), ('state', '=', 'posted'),
              ('payment_state', 'in', UNPAID)]
    if due_from:
        domain.append(('invoice_date_due', '>=', due_from.isoformat()))
    if due_to:
        domain.append(('invoice_date_due', '<=', due_to.isoformat()))
    domain += _partner_domain(arguments, 'commercial_partner_id.name')
    limit = _int(arguments, 'limit', 20, minimum=1, maximum=MAX_ROWS)
    sort_by = arguments.get('sort_by') or sort_default
    if sort_by not in ('due_date', 'amount'):
        raise ToolError("sort_by must be 'due_date' or 'amount'")

    Move = env['account.move']
    count, total = _company_totals(env, 'account.move', domain, 'amount_residual_signed', sign)
    if not count:
        return text_result(f"{title}: none found.")
    today = user_today(env)
    lines = [f"{title}: {count} documents, amount due {total} (company currency)."]

    if aging:
        buckets = [('1-30 days', 1, 30), ('31-60 days', 31, 60), ('61-90 days', 61, 90), ('over 90 days', 91, None)]
        parts = []
        for label, low, high in buckets:
            bucket = list(domain) + [('invoice_date_due', '<=', (today - dt.timedelta(days=low)).isoformat())]
            if high:
                bucket.append(('invoice_date_due', '>=', (today - dt.timedelta(days=high)).isoformat()))
            n, amount = _company_totals(env, 'account.move', bucket, 'amount_residual_signed', sign)
            if n:
                parts.append(f"{label}: {n} docs, {amount}")
        if parts:
            lines.append("Aging of the overdue part: " + "; ".join(parts))

    by_partner = Move.read_group(domain, ['due:sum(amount_residual_signed)'], ['commercial_partner_id'],
                                 orderby='due desc' if customer else 'due asc', limit=5, lazy=False)
    if by_partner:
        lines.append("Largest " + ("customers" if customer else "vendors") + ": " + "; ".join(
            f"{g['commercial_partner_id'][1]} [{g['commercial_partner_id'][0]}] {_money(sign * (g.get('due') or 0))}"
            f" ({g['__count']} docs)" for g in by_partner if g.get('commercial_partner_id')))

    order = 'invoice_date_due asc, id asc' if sort_by == 'due_date' else \
        ('amount_residual_signed desc, id' if customer else 'amount_residual_signed asc, id')
    moves = Move.search(domain, order=order, limit=limit)
    lines += ["", f"{'Oldest due first' if sort_by == 'due_date' else 'Largest first'} "
                  f"(amounts in the document currency):"]
    for move in moves:
        due = move.invoice_date_due
        days = (today - due).days if due else 0
        when = f"{days} days overdue" if days > 0 else ("due today" if days == 0 else f"due in {-days} days")
        lines.append(
            f"- {move.name} [{move.id}] | {move.partner_id.display_name} | dated {move.invoice_date or '-'} | "
            f"due {due or '-'} ({when}) | total {_money(move.amount_total)} | due amount "
            f"{_money(move.amount_residual)} {move.currency_id.name} | {move.payment_state} | {record_url(env, move)}")
    if count > len(moves):
        lines.append(f"... {count - len(moves)} more not listed (totals above include them); "
                     "raise `limit` or filter by partner.")
    return text_result(_cap_output(lines))


@register(
    'get_overdue_invoices',
    "Customer invoices past their due date and not fully paid: total due, aging buckets "
    "(1-30/31-60/61-90/90+ days), largest debtors and the list with days overdue and URLs.",
    {"type": "object", "properties": dict(PARTNER_PROPERTIES, **LIMIT_PROPERTY, **{
        "min_days_overdue": {"type": "integer", "description": "Only invoices at least N days overdue, default 1"},
        "sort_by": {"type": "string", "enum": ["due_date", "amount"], "description": "Default due_date (oldest first)"},
    })},
    requires=('account.move',),
)
def tool_get_overdue_invoices(env, arguments, api_key):
    min_days = _int(arguments, 'min_days_overdue', 1, minimum=1)
    due_to = user_today(env) - dt.timedelta(days=min_days)
    return _invoice_report(env, arguments, 'out_invoice', "Overdue customer invoices",
                           due_to=due_to, aging=True)


@register(
    'get_upcoming_due',
    "Customer invoices not yet paid that fall due in the next N days (default 7), with URLs.",
    {"type": "object", "properties": dict(PARTNER_PROPERTIES, **LIMIT_PROPERTY, **{
        "days": {"type": "integer", "description": "Look-ahead window in days, default 7, max 365"},
        "sort_by": {"type": "string", "enum": ["due_date", "amount"]},
    })},
    requires=('account.move',),
)
def tool_get_upcoming_due(env, arguments, api_key):
    days = _int(arguments, 'days', 7, minimum=0, maximum=365)
    today = user_today(env)
    return _invoice_report(env, arguments, 'out_invoice',
                           f"Customer invoices due from {today} to {today + dt.timedelta(days=days)}",
                           due_from=today, due_to=today + dt.timedelta(days=days))


@register(
    'get_unpaid_bills',
    "Vendor bills posted and not fully paid (overdue and not yet due), with totals, aging of "
    "the overdue part, largest vendors and URLs.",
    {"type": "object", "properties": dict(PARTNER_PROPERTIES, **LIMIT_PROPERTY, **{
        "overdue_only": {"type": "boolean", "description": "Only bills past their due date"},
        "sort_by": {"type": "string", "enum": ["due_date", "amount"]},
    })},
    requires=('account.move',),
)
def tool_get_unpaid_bills(env, arguments, api_key):
    due_to = user_today(env) - dt.timedelta(days=1) if arguments.get('overdue_only') else None
    return _invoice_report(env, arguments, 'in_invoice',
                           "Overdue vendor bills" if due_to else "Unpaid vendor bills",
                           due_to=due_to, aging=True)


@register(
    'get_upcoming_vendor_bills',
    "Vendor bills not yet paid that fall due in the next N days (default 7): what has to be paid soon.",
    {"type": "object", "properties": dict(PARTNER_PROPERTIES, **LIMIT_PROPERTY, **{
        "days": {"type": "integer", "description": "Look-ahead window in days, default 7, max 365"},
        "sort_by": {"type": "string", "enum": ["due_date", "amount"]},
    })},
    requires=('account.move',),
)
def tool_get_upcoming_vendor_bills(env, arguments, api_key):
    days = _int(arguments, 'days', 7, minimum=0, maximum=365)
    today = user_today(env)
    return _invoice_report(env, arguments, 'in_invoice',
                           f"Vendor bills due from {today} to {today + dt.timedelta(days=days)}",
                           due_from=today, due_to=today + dt.timedelta(days=days))


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------
@register(
    'get_open_orders',
    "Confirmed sales orders that are not fully invoiced yet, with the amount still to invoice, "
    "invoice and delivery status, and URLs.",
    {"type": "object", "properties": dict(PARTNER_PROPERTIES, **LIMIT_PROPERTY, **{
        "invoice_status": {"type": "string", "enum": ["to invoice", "no", "upselling"],
                           "description": "Optional: 'to invoice' = ready to invoice now; "
                                          "'no' = nothing to invoice yet (e.g. not delivered)"},
        "sort_by": {"type": "string", "enum": ["date", "amount_to_invoice"], "description": "Default date (oldest first)"},
    })},
    requires=('sale.order',),
)
def tool_get_open_orders(env, arguments, api_key):
    Order = env['sale.order']
    domain = [('state', '=', 'sale'), ('invoice_status', '!=', 'invoiced')] + \
        _partner_domain(arguments, 'partner_id.commercial_partner_id.name')
    if arguments.get('invoice_status'):
        domain.append(('invoice_status', '=', arguments['invoice_status']))
    limit = _int(arguments, 'limit', 20, minimum=1, maximum=MAX_ROWS)
    sort_by = arguments.get('sort_by') or 'date'
    if sort_by not in ('date', 'amount_to_invoice'):
        raise ToolError("sort_by must be 'date' or 'amount_to_invoice'")

    groups = Order.read_group(domain, ['due:sum(amount_to_invoice)', 'total:sum(amount_total)'],
                              ['currency_id'], lazy=False)
    count = sum(g['__count'] for g in groups)
    if not count:
        return text_result("No confirmed sales orders waiting to be invoiced.")
    totals = ", ".join(f"{_money(g.get('due'))} {g['currency_id'][1]} to invoice "
                       f"(orders total {_money(g.get('total'))})" for g in groups if g.get('currency_id'))
    status = Order.read_group(domain, [], ['invoice_status'], lazy=False)
    lines = [f"Open sales orders not fully invoiced: {count}. {totals}.",
             "By invoice status: " + ", ".join(f"{g['invoice_status']}: {g['__count']}" for g in status)]
    has_delivery = 'delivery_status' in Order._fields
    orders = Order.search(domain, limit=limit, order='date_order asc, id' if sort_by == 'date'
                          else 'amount_to_invoice desc, id')
    lines += ["", "Orders:"]
    for o in orders:
        lines.append(
            f"- {o.name} [{o.id}] | {o.partner_id.display_name} | {format_local_datetime(env, o.date_order)} | "
            f"total {_money(o.amount_total)} | to invoice {_money(o.amount_to_invoice)} {o.currency_id.name} | "
            f"invoice: {o.invoice_status}" + (f" | delivery: {o.delivery_status or '-'}" if has_delivery else "")
            + f" | {record_url(env, o)}")
    if count > len(orders):
        lines.append(f"... {count - len(orders)} more orders; raise `limit` or filter by partner.")
    return text_result(_cap_output(lines))


@register(
    'get_top_customers',
    "Top customers ranked by confirmed sales (untaxed, company currency) over a period, with "
    "order count, share of total sales and URLs. Includes sales made to their contacts.",
    {"type": "object", "properties": {
        "period": {"type": "string", "enum": ['last_7_days', 'last_30_days', 'last_3_months',
                                              'last_6_months', 'last_year', 'this_month', 'this_year', 'all_time'],
                   "description": "Default this_year"},
        "date_from": {"type": "string", "description": "Explicit start date YYYY-MM-DD"},
        "date_to": {"type": "string", "description": "Explicit end date YYYY-MM-DD"},
        "limit": {"type": "integer", "description": "Customers to list, default 10, max 100"},
    }},
    requires=('sale.report',),
)
def tool_get_top_customers(env, arguments, api_key):
    from .mcp_tools import _resolve_period, _sale_states  # local import: avoids an import cycle
    limit = _int(arguments, 'limit', 10, minimum=1, maximum=MAX_ROWS)
    domain = [('state', 'in', _sale_states(env)), ('commercial_partner_id', '!=', False)]
    if (arguments.get('period') or '') == 'all_time' and not arguments.get('date_from'):
        label = "all time"
    else:
        args = dict(arguments)
        args.setdefault('period', 'this_year')
        date_from, date_to = _resolve_period(env, args)
        domain += datetime_day_range(env, 'date', date_from, date_to)
        label = f"{date_from} to {date_to}"
    Report = env['sale.report']
    total = (Report.read_group(domain, ['s:sum(price_subtotal)'], [], lazy=False) or [{}])[0].get('s') or 0.0
    groups = Report.read_group(
        domain, ['sales:sum(price_subtotal)', 'orders:count_distinct(order_reference)'],
        ['commercial_partner_id'], orderby='sales desc', limit=limit, lazy=False)
    if not groups:
        return text_result(f"No confirmed sales for {label}.")
    currency = env.company.currency_id.name
    lines = [f"Top customers by untaxed sales, {label} (total {_money(total)} {currency}):"]
    for i, g in enumerate(groups, 1):
        pid, pname = g['commercial_partner_id']
        share = (g.get('sales') or 0) / total * 100 if total else 0
        lines.append(f"{i}. {pname} [{pid}] — {_money(g.get('sales'))} {currency} ({share:.1f}%) | "
                     f"{g.get('orders') or 0} orders | {url_for(env, 'res.partner', pid)}")
    return text_result(_cap_output(lines))


# ---------------------------------------------------------------------------
# Purchase
# ---------------------------------------------------------------------------
@register(
    'get_pending_purchases',
    "Purchase orders waiting for approval (state 'to approve') or for delivery (confirmed but not "
    "fully received), flagged late when the expected arrival date has passed, with URLs.",
    {"type": "object", "properties": dict(PARTNER_PROPERTIES, **LIMIT_PROPERTY, **{
        "status": {"type": "string", "enum": ["approval", "delivery", "all"], "description": "Default all"},
    })},
    requires=('purchase.order',),
)
def tool_get_pending_purchases(env, arguments, api_key):
    Order = env['purchase.order']
    status = arguments.get('status') or 'all'
    if status not in ('approval', 'delivery', 'all'):
        raise ToolError("status must be 'approval', 'delivery' or 'all'")
    limit = _int(arguments, 'limit', 20, minimum=1, maximum=MAX_ROWS)
    partner = _partner_domain(arguments, 'partner_id.commercial_partner_id.name')
    has_receipt = 'receipt_status' in Order._fields
    sections = []
    if status in ('approval', 'all'):
        sections.append(("Waiting for approval", [('state', '=', 'to approve')] + partner))
    if status in ('delivery', 'all'):
        if has_receipt:
            sections.append(("Waiting for delivery", [('state', 'in', ['purchase', 'done']),
                                                      ('receipt_status', 'in', ['pending', 'partial'])] + partner))
        elif status == 'delivery':
            raise ToolError("Receipt tracking needs the Inventory app (purchase_stock) to be installed.")

    now = fields.Datetime.now()
    lines = []
    for title, domain in sections:
        groups = Order.read_group(domain, ['total:sum(amount_total)'], ['currency_id'], lazy=False)
        count = sum(g['__count'] for g in groups)
        totals = ", ".join(f"{_money(g.get('total'))} {g['currency_id'][1]}" for g in groups if g.get('currency_id'))
        lines.append(f"{title}: {count} orders" + (f", {totals}" if totals else "") + ".")
        for o in Order.search(domain, order='date_planned asc, id', limit=limit):
            late = " | LATE" if o.date_planned and o.date_planned < now and title.endswith('delivery') else ""
            receipt = f" | received: {o.receipt_status}" if has_receipt and title.endswith('delivery') else ""
            lines.append(
                f"- {o.name} [{o.id}] | {o.partner_id.display_name} | ordered "
                f"{format_local_datetime(env, o.date_approve or o.date_order)} | expected "
                f"{format_local_datetime(env, o.date_planned)} | {_money(o.amount_total)} {o.currency_id.name}"
                f"{receipt}{late} | {record_url(env, o)}")
        if count > limit:
            lines.append(f"... {count - limit} more; raise `limit` or filter by vendor.")
        lines.append("")
    return text_result(_cap_output(lines))


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------
def _lead_line(env, lead):
    contact = lead.partner_id.display_name or lead.partner_name or lead.contact_name or '-'
    deadline = lead.date_deadline
    late = " (past)" if deadline and deadline < user_today(env) else ""
    return (f"- {lead.name} [{lead.id}] | {lead.type} | {contact} | stage: {lead.stage_id.name or '-'} | "
            f"salesperson: {lead.user_id.name or '-'} | expected {_money(lead.expected_revenue)} "
            f"{lead.company_currency.name or ''} | probability {lead.probability:.0f}% | "
            f"deadline {deadline or '-'}{late} | {record_url(env, lead)}")


def _crm_filters(arguments):
    domain = []
    if arguments.get('salesperson'):
        domain.append(('user_id.name', 'ilike', str(arguments['salesperson'])))
    if arguments.get('team'):
        domain.append(('team_id.name', 'ilike', str(arguments['team'])))
    return domain


CRM_FILTER_PROPERTIES = {
    "salesperson": {"type": "string", "description": "Optional partial salesperson name"},
    "team": {"type": "string", "description": "Optional partial sales team name"},
}


@register(
    'get_open_leads',
    "Open CRM leads and opportunities (not won, not lost): summary per stage and the list with "
    "expected revenue, probability, deadline and URLs.",
    {"type": "object", "properties": dict(CRM_FILTER_PROPERTIES, **LIMIT_PROPERTY, **{
        "type": {"type": "string", "enum": ["lead", "opportunity", "all"], "description": "Default all"},
        "sort_by": {"type": "string", "enum": ["newest", "expected_revenue", "deadline"], "description": "Default newest"},
    })},
    requires=('crm.lead',),
)
def tool_get_open_leads(env, arguments, api_key):
    Lead = env['crm.lead']
    kind = arguments.get('type') or 'all'
    domain = [('active', '=', True), ('probability', '<', 100)] + _crm_filters(arguments)
    if kind in ('lead', 'opportunity'):
        domain.append(('type', '=', kind))
    limit = _int(arguments, 'limit', 20, minimum=1, maximum=MAX_ROWS)
    order = {'newest': 'create_date desc, id desc', 'expected_revenue': 'expected_revenue desc, id',
             'deadline': 'date_deadline asc, id'}.get(arguments.get('sort_by') or 'newest')
    if not order:
        raise ToolError("sort_by must be 'newest', 'expected_revenue' or 'deadline'")
    count = Lead.search_count(domain)
    if not count:
        return text_result("No open leads or opportunities.")
    stages = Lead.read_group(domain, ['rev:sum(expected_revenue)', 'pro:sum(prorated_revenue)'],
                             ['stage_id'], lazy=False)
    currency = env.company.currency_id.name
    lines = [f"Open {'leads and opportunities' if kind == 'all' else kind + 's'}: {count}.", "By stage:"]
    for g in stages:
        lines.append(f"  {g['stage_id'][1] if g.get('stage_id') else 'No stage'}: {g['__count']} | expected "
                     f"{_money(g.get('rev'))} {currency} | weighted {_money(g.get('pro'))}")
    lines += ["", "List:"] + [_lead_line(env, lead) for lead in Lead.search(domain, order=order, limit=limit)]
    if count > limit:
        lines.append(f"... {count - limit} more; raise `limit` or filter.")
    return text_result(_cap_output(lines))


@register(
    'get_hot_opportunities',
    "Open opportunities with a high probability of winning (default >= 70%), biggest expected "
    "revenue first, with weighted revenue, deadline and URLs.",
    {"type": "object", "properties": dict(CRM_FILTER_PROPERTIES, **LIMIT_PROPERTY, **{
        "min_probability": {"type": "integer", "description": "Minimum probability %, default 70"},
    })},
    requires=('crm.lead',),
)
def tool_get_hot_opportunities(env, arguments, api_key):
    Lead = env['crm.lead']
    min_probability = _int(arguments, 'min_probability', 70, minimum=0, maximum=99)
    domain = [('type', '=', 'opportunity'), ('active', '=', True), ('probability', '<', 100),
              ('probability', '>=', min_probability)] + _crm_filters(arguments)
    limit = _int(arguments, 'limit', 20, minimum=1, maximum=MAX_ROWS)
    totals = Lead.read_group(domain, ['rev:sum(expected_revenue)', 'pro:sum(prorated_revenue)'], [], lazy=False)
    count = totals[0]['__count'] if totals else 0
    if not count:
        return text_result(f"No open opportunities with probability >= {min_probability}%.")
    currency = env.company.currency_id.name
    lines = [f"Hot opportunities (probability >= {min_probability}%): {count} | expected "
             f"{_money(totals[0].get('rev'))} {currency} | weighted {_money(totals[0].get('pro'))} {currency}", ""]
    lines += [_lead_line(env, lead) for lead in
              Lead.search(domain, order='expected_revenue desc, probability desc, id', limit=limit)]
    if count > limit:
        lines.append(f"... {count - limit} more; raise `limit`.")
    return text_result(_cap_output(lines))


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
MAX_ORDERPOINTS = 5000


@register(
    'get_low_stock',
    "Products below their reorder point (reordering rules): forecast (default) or on-hand "
    "quantity under the rule's minimum, with the quantity to order and URLs.",
    {"type": "object", "properties": dict(LIMIT_PROPERTY, **{
        "basis": {"type": "string", "enum": ["forecast", "on_hand"],
                  "description": "Compare the minimum with the forecast (default) or the on-hand quantity"},
        "warehouse": {"type": "string", "description": "Optional partial warehouse name"},
    })},
    requires=('stock.warehouse.orderpoint',),
)
def tool_get_low_stock(env, arguments, api_key):
    basis = arguments.get('basis') or 'forecast'
    if basis not in ('forecast', 'on_hand'):
        raise ToolError("basis must be 'forecast' or 'on_hand'")
    limit = _int(arguments, 'limit', 20, minimum=1, maximum=MAX_ROWS)
    domain = [('active', '=', True), ('product_id.active', '=', True)]
    if arguments.get('warehouse'):
        domain.append(('warehouse_id.name', 'ilike', str(arguments['warehouse'])))
    Orderpoint = env['stock.warehouse.orderpoint']
    total_rules = Orderpoint.search_count(domain)
    if not total_rules:
        return text_result("No reordering rules are configured, so there is no reorder point to compare with. "
                           "Set min/max rules in Inventory > Configuration > Reordering Rules, or ask for "
                           "products with low on-hand quantity instead.")
    orderpoints = Orderpoint.search(domain, limit=MAX_ORDERPOINTS)
    low = []
    for op in orderpoints:  # qty_on_hand / qty_forecast are computed, not stored
        current = op.qty_forecast if basis == 'forecast' else op.qty_on_hand
        if current < op.product_min_qty or (basis == 'forecast' and current < 0):
            low.append((current - op.product_min_qty, op, current))
    low.sort(key=lambda item: item[0])
    lines = [f"Products below their reorder point ({basis.replace('_', '-')} basis): {len(low)} of "
             f"{len(orderpoints)} reordering rules."]
    if len(orderpoints) >= MAX_ORDERPOINTS:
        lines.append(f"WARNING: only the first {MAX_ORDERPOINTS} rules were checked; filter by warehouse.")
    for gap, op, current in low[:limit]:
        lines.append(
            f"- {op.product_id.display_name} [{op.product_id.id}] | {op.warehouse_id.name or '-'} / "
            f"{op.location_id.display_name} | on hand {_qty(op.qty_on_hand)} | forecast {_qty(op.qty_forecast)} | "
            f"min {_qty(op.product_min_qty)} | max {_qty(op.product_max_qty)} | to order {_qty(op.qty_to_order)} "
            f"{op.product_uom.name or ''} | trigger {op.trigger} | {record_url(env, op.product_id)}")
    if len(low) > limit:
        lines.append(f"... {len(low) - limit} more; raise `limit` or filter by warehouse.")
    return text_result(_cap_output(lines))


@register(
    'get_stock_value',
    "Inventory valuation by product category from the stock valuation layers (quantity and "
    "value in company currency), optionally as of a past date.",
    {"type": "object", "properties": {
        "at_date": {"type": "string", "description": "Optional date YYYY-MM-DD: value at the end of that day"},
        "category": {"type": "string", "description": "Optional partial category name"},
        "limit": {"type": "integer", "description": f"Categories to list, default 50, max {MAX_ROWS}"},
    }},
    requires=('stock.valuation.layer',),
)
def tool_get_stock_value(env, arguments, api_key):
    domain = []
    label = "today"
    if arguments.get('at_date'):
        try:
            at = dt.date.fromisoformat(str(arguments['at_date'])[:10])
        except ValueError:
            raise ToolError("'at_date' must be YYYY-MM-DD")
        domain += [('create_date', '<', day_start_utc(env, at + dt.timedelta(days=1)))]
        label = f"end of {at}"
    if arguments.get('category'):
        domain.append(('categ_id.complete_name', 'ilike', str(arguments['category'])))
    limit = _int(arguments, 'limit', 50, minimum=1, maximum=MAX_ROWS)
    Layer = env['stock.valuation.layer']
    groups = Layer.read_group(domain, ['value:sum(value)', 'qty:sum(quantity)'],
                              ['company_id', 'categ_id'], lazy=False)
    groups = [g for g in groups if abs(g.get('value') or 0) > 0.005 or abs(g.get('qty') or 0) > 0.0001]
    if not groups:
        return text_result(f"No stock value found ({label}).")
    by_company = {}
    for g in groups:
        by_company.setdefault(g['company_id'][0] if g.get('company_id') else 0, []).append(g)
    lines = [f"Stock valuation by product category ({label}):"]
    for company_id, rows in by_company.items():
        company = env['res.company'].browse(company_id) if company_id else env.company
        currency = company.currency_id.name
        total = sum(r.get('value') or 0 for r in rows)
        rows.sort(key=lambda r: -(r.get('value') or 0))
        lines.append(f"{company.name}: total {_money(total)} {currency}")
        for r in rows[:limit]:
            share = (r.get('value') or 0) / total * 100 if total else 0
            category = r['categ_id'][1] if r.get('categ_id') else 'No category'
            cat_url = url_for(env, 'product.category', r['categ_id'][0]) if r.get('categ_id') else ''
            lines.append(f"- {category} | qty {_qty(r.get('qty'))} | value {_money(r.get('value'))} {currency} "
                         f"({share:.1f}%)" + (f" | {cat_url}" if cat_url else ""))
        if len(rows) > limit:
            lines.append(f"... {len(rows) - limit} more categories.")
    return text_result(_cap_output(lines))
