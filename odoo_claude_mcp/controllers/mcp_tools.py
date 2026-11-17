# -*- coding: utf-8 -*-
"""Dedicated business tools plus the public entry points used by the controller.

Sales figures come from sale.report, which is Odoo's own analysis view:
- amounts are converted to the current company's currency (no mixing of currencies),
- quantities are in each product's default unit of measure (no mixing of UoMs),
- confirmed POS orders are included when pos_sale is installed.
"""
import datetime as dt

from dateutil.relativedelta import relativedelta

from . import mcp_generic  # noqa: F401 - importing registers the generic tools
from .mcp_common import (
    TOOLS, ToolError, access_allowed, company_currency_name, datetime_day_range,
    format_local_datetime, register, text_result, tool_access, tool_installed,
    user_today, utc_to_local,
)
from .mcp_urls import record_url, url_for
from . import mcp_business  # noqa: E402,F401 - registers the business tools
from . import mcp_write  # noqa: E402,F401 - registers the write tools

_text_result = text_result  # kept for backwards compatibility with older imports

MAX_ORDERS_RETURNED = 20

VALID_PERIODS = {
    'last_7_days', 'last_30_days', 'last_3_months',
    'last_6_months', 'last_year', 'this_month', 'this_year',
}

PERIOD_PROPERTIES = {
    "period": {"type": "string", "enum": sorted(VALID_PERIODS),
               "description": "Shorthand period, in the user's time zone. Default 'last_3_months'."},
    "date_from": {"type": "string", "description": "Explicit start date YYYY-MM-DD (inclusive)"},
    "date_to": {"type": "string", "description": "Explicit end date YYYY-MM-DD (inclusive)"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _int_arg(arguments, name, default, minimum=0, maximum=None):
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


def _parse_date(value, name):
    try:
        return dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ToolError(f"'{name}' must be a date in the format YYYY-MM-DD")


def _resolve_period(env, arguments, default_period='last_3_months'):
    """(date_from, date_to) as local dates, both inclusive."""
    date_from = arguments.get('date_from')
    date_to = arguments.get('date_to')
    if date_from or date_to:
        if not date_from or not date_to:
            raise ToolError("Both date_from and date_to are required when using explicit dates")
        start, end = _parse_date(date_from, 'date_from'), _parse_date(date_to, 'date_to')
        if end < start:
            raise ToolError("date_to must be on or after date_from")
        return start, end

    period = arguments.get('period') or default_period
    if period not in VALID_PERIODS:
        raise ToolError(f"Unknown period '{period}'. Valid values: {', '.join(sorted(VALID_PERIODS))}")
    today = user_today(env)
    start = {
        'last_7_days': lambda: today - dt.timedelta(days=7),
        'last_30_days': lambda: today - dt.timedelta(days=30),
        'last_3_months': lambda: today - relativedelta(months=3),
        'last_6_months': lambda: today - relativedelta(months=6),
        'last_year': lambda: today - relativedelta(years=1),
        'this_month': lambda: today.replace(day=1),
        'this_year': lambda: today.replace(month=1, day=1),
    }[period]()
    return start, today


def _sale_states(env):
    """Confirmed states in sale.report (includes POS states when pos_sale is installed)."""
    return env['sale.report']._get_done_states()


def _sales_domain(env, date_from=None, date_to=None):
    domain = [('state', 'in', _sale_states(env))]
    if date_from and date_to:
        domain += datetime_day_range(env, 'date', date_from, date_to)
    return domain


def _sales_totals(env, domain):
    """DB-side totals over ALL matching sale.report rows, in company currency."""
    groups = env['sale.report'].read_group(
        domain,
        ['untaxed:sum(price_subtotal)', 'total:sum(price_total)',
         'orders:count_distinct(order_reference)', 'customers:count_distinct(commercial_partner_id)'],
        [], lazy=False)
    group = groups[0] if groups else {}
    return {
        'untaxed': group.get('untaxed') or 0.0,
        'total': group.get('total') or 0.0,
        'orders': group.get('orders') or 0,
        'customers': group.get('customers') or 0,
    }


def _products_ranking(env, model, domain, qty_field, amount_field, order, limit):
    """[(product_id, name, qty, amount)] ranked by 'qty' or 'amount'."""
    order_field = 'qty' if order == 'qty' else 'amount'
    groups = env[model].read_group(
        domain + [('product_id', '!=', False)],
        [f'qty:sum({qty_field})', f'amount:sum({amount_field})'], ['product_id'],
        orderby=f'{order_field} desc', limit=limit, lazy=False)
    return [(g['product_id'][0], g['product_id'][1], g.get('qty') or 0.0, g.get('amount') or 0.0)
            for g in groups if g.get('product_id')]


def _money(value):
    return f"{value:,.2f}"


def _qty(value):
    return f"{value:,.2f}".rstrip('0').rstrip('.')


# ---------------------------------------------------------------------------
# get_customer_sales
# ---------------------------------------------------------------------------
@register(
    'get_customer_sales',
    "Sales of one customer (the company and all its contacts): confirmed totals in "
    "company currency and the latest orders. Pass customer_name, or customer_id when "
    "several customers match.",
    {
        "type": "object",
        "properties": {
            "customer_name": {"type": "string", "description": "Full or partial customer name"},
            "customer_id": {"type": "integer", "description": "Exact customer id (from search_customers)"},
            "date_from": {"type": "string", "description": "Optional start date YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "Optional end date YYYY-MM-DD"},
        },
    },
    requires=('sale.report', 'sale.order'),
)
def tool_get_customer_sales(env, arguments, api_key):
    Partner = env['res.partner']
    customer_id = _int_arg(arguments, 'customer_id', None, minimum=1)
    name = (arguments.get('customer_name') or '').strip()
    if customer_id:
        partner = Partner.search([('id', '=', customer_id)], limit=1)
        if not partner:
            raise ToolError(f"No customer with id {customer_id}.")
        partner = partner.commercial_partner_id
    elif name:
        commercial = Partner.search([('name', 'ilike', name)], limit=50).commercial_partner_id
        if not commercial:
            return text_result(f"No customer found matching '{name}'.", is_error=True)
        if len(commercial) > 1:
            listing = "\n".join(f"- {p.display_name} (id {p.id}, {p.city or 'no city'})"
                                for p in commercial[:15])
            return text_result(
                "Several customers match this name. Call again with customer_id:\n" + listing,
                is_error=True)
        partner = commercial
    else:
        raise ToolError("customer_name or customer_id is required")

    date_from = arguments.get('date_from')
    date_to = arguments.get('date_to')
    d_from = _parse_date(date_from, 'date_from') if date_from else dt.date(1900, 1, 1)
    d_to = _parse_date(date_to, 'date_to') if date_to else user_today(env) + dt.timedelta(days=36500)

    report_domain = [('commercial_partner_id', '=', partner.id)] + _sales_domain(env, d_from, d_to)
    totals = _sales_totals(env, report_domain)
    currency = company_currency_name(env)

    order_domain = [('partner_id', 'child_of', partner.id)] + datetime_day_range(env, 'date_order', d_from, d_to)
    all_count = env['sale.order'].search_count(order_domain)

    period = f" from {date_from or 'the beginning'} to {date_to or 'today'}" if (date_from or date_to) else ""
    lines = [f"Customer: {partner.display_name} (id {partner.id}, including its contacts){period}",
             f"URL: {record_url(env, partner)}"]
    if totals['orders']:
        lines += [
            f"Confirmed sales (company currency {currency}): untaxed {_money(totals['untaxed'])}, "
            f"total incl. taxes {_money(totals['total'])}, {totals['orders']} confirmed orders.",
        ]
    else:
        lines.append("No confirmed sales (only quotations or cancelled orders, if any).")
    lines.append(f"Sales orders in any status: {all_count}")

    if all_count:
        lines += ["", "Latest orders (amount in the order's own currency):"]
        orders = env['sale.order'].search(order_domain, order='date_order desc, id desc',
                                          limit=MAX_ORDERS_RETURNED)
        for order in orders:
            lines.append(
                f"- {order.name} [{order.id}] | {format_local_datetime(env, order.date_order)} | "
                f"{_money(order.amount_total)} {order.currency_id.name or ''} | status: {order.state}"
                + (f" | contact: {order.partner_id.name}" if order.partner_id != partner else "")
                + f" | {record_url(env, order)}")
        if all_count > MAX_ORDERS_RETURNED:
            lines.append(f"... {all_count - MAX_ORDERS_RETURNED} more orders not listed "
                         "(the totals above include them).")
    return text_result("\n".join(lines))


# ---------------------------------------------------------------------------
# get_sales_analysis
# ---------------------------------------------------------------------------
@register(
    'get_sales_analysis',
    "Overall confirmed sales over a period, in company currency: untaxed and total revenue, "
    "order count, average order value, distinct customers, top 5 products by revenue, and "
    "growth against the previous period of the same length. For month-by-month or any custom "
    "breakdown, use query_data on sale.report in aggregate mode instead of calling this repeatedly.",
    {"type": "object", "properties": dict(PERIOD_PROPERTIES)},
    requires=('sale.report',),
)
def tool_get_sales_analysis(env, arguments, api_key):
    date_from, date_to = _resolve_period(env, arguments)
    totals = _sales_totals(env, _sales_domain(env, date_from, date_to))

    prev_to = date_from - dt.timedelta(days=1)
    prev_from = prev_to - (date_to - date_from)
    prev = _sales_totals(env, _sales_domain(env, prev_from, prev_to))
    currency = company_currency_name(env)

    lines = [f"Confirmed sales from {date_from} to {date_to} (computed by the database, "
             f"amounts in {currency}):", ""]
    if not totals['orders']:
        lines.append("No confirmed sales in this period.")
        return text_result("\n".join(lines))

    avg_order = totals['untaxed'] / totals['orders'] if totals['orders'] else 0.0
    if prev['untaxed']:
        growth = (totals['untaxed'] - prev['untaxed']) / prev['untaxed'] * 100
        growth_txt = (f"{'+' if growth >= 0 else '-'}{abs(growth):.1f}% versus {prev_from} to {prev_to} "
                      f"(untaxed {_money(prev['untaxed'])})")
    else:
        growth_txt = f"not available (no sales from {prev_from} to {prev_to})"
    lines += [
        f"Untaxed revenue: {_money(totals['untaxed'])}",
        f"Total incl. taxes: {_money(totals['total'])}",
        f"Confirmed orders: {totals['orders']}",
        f"Average order value (untaxed): {_money(avg_order)}",
        f"Distinct customers: {totals['customers']}",
        f"Growth: {growth_txt}",
        "",
    ]

    top = _products_ranking(env, 'sale.report', _sales_domain(env, date_from, date_to),
                            'product_uom_qty', 'price_subtotal', 'amount', 5)
    if top:
        lines.append("Top 5 products by untaxed revenue:")
        for i, (pid, pname, qty, amount) in enumerate(top, 1):
            lines.append(f"{i}. {pname} [{pid}] — qty {_qty(qty)} | revenue {_money(amount)} | "
                         f"{url_for(env, 'product.product', pid)}")
    return text_result("\n".join(lines))


# ---------------------------------------------------------------------------
# get_top_selling_products
# ---------------------------------------------------------------------------
@register(
    'get_top_selling_products',
    "Best-selling products over a period from confirmed sales (and POS, if installed). "
    "Quantities are in each product's unit of measure; revenue is untaxed, in company currency.",
    {
        "type": "object",
        "properties": dict(PERIOD_PROPERTIES, **{
            "order_by": {"type": "string", "enum": ["qty", "revenue"],
                         "description": "Rank by quantity (default) or by revenue."},
            "limit": {"type": "integer", "description": "Max products to return, default 10, max 50"},
        }),
    },
    requires=('sale.report',),
)
def tool_get_top_selling_products(env, arguments, api_key):
    date_from, date_to = _resolve_period(env, arguments)
    limit = _int_arg(arguments, 'limit', 10, minimum=1, maximum=50)
    order = 'amount' if arguments.get('order_by') == 'revenue' else 'qty'
    ranking = _products_ranking(env, 'sale.report', _sales_domain(env, date_from, date_to),
                                'product_uom_qty', 'price_subtotal', order, limit)
    if not ranking:
        return text_result(f"No confirmed sales between {date_from} and {date_to}.")

    lines = [f"Best-selling products by {'revenue' if order == 'amount' else 'quantity'} from "
             f"{date_from} to {date_to} (revenue in {company_currency_name(env)}):", ""]
    for i, (pid, pname, qty, amount) in enumerate(ranking, 1):
        lines.append(f"{i}. {pname} [{pid}] — qty sold {_qty(qty)} | revenue {_money(amount)} | "
                     f"{url_for(env, 'product.product', pid)}")
    return text_result("\n".join(lines))


# ---------------------------------------------------------------------------
# get_top_purchased_products
# ---------------------------------------------------------------------------
@register(
    'get_top_purchased_products',
    "Products purchased in the largest quantities over a period (confirmed purchase orders), "
    "with how much of each was sold in the same period. Quantities are in each product's "
    "unit of measure.",
    {
        "type": "object",
        "properties": dict(PERIOD_PROPERTIES, **{
            "limit": {"type": "integer", "description": "Max products to return, default 10, max 50"},
        }),
    },
    requires=('purchase.report',),
)
def tool_get_top_purchased_products(env, arguments, api_key):
    date_from, date_to = _resolve_period(env, arguments)
    limit = _int_arg(arguments, 'limit', 10, minimum=1, maximum=50)

    purchase_domain = [('state', 'in', ['purchase', 'done'])] + \
        datetime_day_range(env, 'date_order', date_from, date_to)
    purchased = _products_ranking(env, 'purchase.report', purchase_domain,
                                  'qty_ordered', 'untaxed_total', 'qty', limit)
    if not purchased:
        return text_result(f"No confirmed purchases between {date_from} and {date_to}.")

    sold_map = {}
    if 'sale.report' in env.registry:
        groups = env['sale.report'].read_group(
            _sales_domain(env, date_from, date_to) + [('product_id', 'in', [p[0] for p in purchased])],
            ['qty:sum(product_uom_qty)'], ['product_id'], lazy=False)
        sold_map = {g['product_id'][0]: g.get('qty') or 0.0 for g in groups if g.get('product_id')}

    lines = [f"Most purchased products from {date_from} to {date_to} "
             f"(untaxed purchase value in {company_currency_name(env)}):", ""]
    for i, (pid, pname, qty, amount) in enumerate(purchased, 1):
        sold = f" | sold {_qty(sold_map.get(pid, 0.0))}" if 'sale.report' in env.registry else ""
        lines.append(f"{i}. {pname} [{pid}] — purchased {_qty(qty)} | value {_money(amount)}{sold} | "
                     f"{url_for(env, 'product.product', pid)}")
    return text_result("\n".join(lines))


# ---------------------------------------------------------------------------
# get_slow_moving_products
# ---------------------------------------------------------------------------
@register(
    'get_slow_moving_products',
    "Slow-moving / stagnant products: items with stock on hand but little or no outgoing "
    "movement in the recent period. Outgoing movement = done stock moves to customers "
    "(sales deliveries and POS) or consumed by manufacturing.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "How many recent days to check, default 90"},
            "limit": {"type": "integer", "description": "Max products to return, default 10, max 50"},
        },
    },
    requires=('stock.quant', 'stock.move'),
)
def tool_get_slow_moving_products(env, arguments, api_key):
    days = _int_arg(arguments, 'days', 90, minimum=1, maximum=3650)
    limit = _int_arg(arguments, 'limit', 10, minimum=1, maximum=50)
    date_to = user_today(env)
    date_from = date_to - dt.timedelta(days=days)

    stock_groups = env['stock.quant'].read_group(
        [('location_id.usage', '=', 'internal')],
        ['quantity:sum'], ['product_id'], lazy=False)
    stock_map = {g['product_id'][0]: (g['product_id'][1], g.get('quantity') or 0.0)
                 for g in stock_groups if g.get('product_id')}
    stock_map = {pid: v for pid, v in stock_map.items() if v[1] > 0}
    if not stock_map:
        return text_result("No products have stock on hand.")

    outgoing = [
        ('state', '=', 'done'),
        ('location_id.usage', '=', 'internal'),
        ('location_dest_id.usage', 'in', ['customer', 'production']),
        ('product_id', 'in', list(stock_map)),
    ]
    recent = env['stock.move'].read_group(
        outgoing + datetime_day_range(env, 'date', date_from, date_to),
        ['qty:sum(product_qty)'], ['product_id'], lazy=False)
    moved_map = {g['product_id'][0]: g.get('qty') or 0.0 for g in recent if g.get('product_id')}

    ranked = sorted(stock_map.items(), key=lambda item: (moved_map.get(item[0], 0.0), -item[1][1]))[:limit]
    last = env['stock.move'].read_group(
        outgoing[:3] + [('product_id', 'in', [pid for pid, _v in ranked])],
        ['last:max(date)'], ['product_id'], lazy=False)
    last_map = {g['product_id'][0]: g.get('last') for g in last if g.get('product_id')}

    lines = [f"Slowest-moving products over the last {days} days ({date_from} to {date_to}), "
             f"out of {len(stock_map)} products with stock on hand:", ""]
    for i, (pid, (pname, qty)) in enumerate(ranked, 1):
        last_date = last_map.get(pid)
        last_txt = utc_to_local(env, last_date).date().isoformat() if last_date else 'never'
        lines.append(f"{i}. {pname} [{pid}] — on hand {_qty(qty)} | moved out in period "
                     f"{_qty(moved_map.get(pid, 0.0))} | last outgoing move: {last_txt} | "
                     f"{url_for(env, 'product.product', pid)}")
    return text_result("\n".join(lines))


# ---------------------------------------------------------------------------
# search_customers
# ---------------------------------------------------------------------------
@register(
    'search_customers',
    "Search customers (companies and individuals) by partial name or internal reference. "
    "Use this first when a name may be ambiguous; contacts are reported under their company.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Partial customer name or reference"},
        },
        "required": ["query"],
    },
)
def tool_search_customers(env, arguments, api_key):
    query = (arguments.get('query') or '').strip()
    if not query:
        raise ToolError("query is required")
    partners = env['res.partner'].search(
        ['|', ('name', 'ilike', query), ('ref', '=ilike', query)], limit=50)
    commercial = partners.commercial_partner_id[:15]
    if not commercial:
        return text_result(f"No customers match '{query}'.")
    lines = [f"Customers matching '{query}':"]
    for p in commercial:
        kind = 'company' if p.is_company else 'individual'
        place = ", ".join(x for x in (p.city, p.country_id.name) if x) or 'no address'
        lines.append(f"- {p.display_name} (id {p.id}, {kind}, {place}) | {record_url(env, p)}")
    return text_result("\n".join(lines))


# ---------------------------------------------------------------------------
# get_customers_by_last_purchase
# ---------------------------------------------------------------------------
@register(
    'get_customers_by_last_purchase',
    "List customers by the date of their most recent confirmed sale, with order count and "
    "total spent (company currency). Use min_months_since_last_purchase alone for 'has not "
    "bought for N months' (win-back), or together with max_months_since_last_purchase for "
    "'last purchase about N months ago'.",
    {
        "type": "object",
        "properties": {
            "min_months_since_last_purchase": {
                "type": "integer", "description": "Last order must be at least this many months old. Default 3."},
            "max_months_since_last_purchase": {
                "type": "integer", "description": "Optional. Last order must be at most this many months old."},
            "limit": {"type": "integer", "description": "Max customers per call (default 50, max 200)."},
            "offset": {"type": "integer", "description": "Skip this many customers, to page through. Default 0."},
            "sort_by": {
                "type": "string", "enum": ["last_purchase", "total_spent"],
                "description": "last_purchase (default, newest first) or total_spent (biggest first)."},
        },
    },
    requires=('sale.report',),
)
def tool_get_customers_by_last_purchase(env, arguments, api_key):
    min_months = _int_arg(arguments, 'min_months_since_last_purchase', 3)
    max_months = _int_arg(arguments, 'max_months_since_last_purchase', None)
    limit = _int_arg(arguments, 'limit', 50, minimum=1, maximum=200)
    offset = _int_arg(arguments, 'offset', 0)
    sort_by = (arguments.get('sort_by') or 'last_purchase').strip()
    if sort_by not in ('last_purchase', 'total_spent'):
        raise ToolError("sort_by must be 'last_purchase' or 'total_spent'")
    if max_months is not None and max_months < min_months:
        raise ToolError("max_months_since_last_purchase must be >= min_months_since_last_purchase")

    today = user_today(env)
    newest_allowed = today - relativedelta(months=min_months)
    oldest_allowed = today - relativedelta(months=max_months) if max_months is not None else None

    groups = env['sale.report'].read_group(
        [('state', 'in', _sale_states(env)), ('commercial_partner_id', '!=', False)],
        ['last:max(date)', 'spent:sum(price_total)', 'orders:count_distinct(order_reference)'],
        ['commercial_partner_id'], lazy=False)

    matched = []
    for group in groups:
        last = group.get('last')
        if not last:
            continue
        last_local = utc_to_local(env, last).date()
        if last_local > newest_allowed or (oldest_allowed and last_local < oldest_allowed):
            continue
        partner_id, partner_name = group['commercial_partner_id']
        matched.append({'id': partner_id, 'name': partner_name, 'last': last_local,
                        'orders': group.get('orders') or 0, 'spent': group.get('spent') or 0.0})
    if sort_by == 'total_spent':
        matched.sort(key=lambda c: c['spent'], reverse=True)
    else:
        matched.sort(key=lambda c: c['last'], reverse=True)

    if not matched:
        return text_result("No customers match this last-purchase window.")
    if offset >= len(matched):
        return text_result(f"Offset {offset} is beyond the {len(matched)} matching customers.")

    shown = matched[offset:offset + limit]
    contacts = {p['id']: p for p in env['res.partner'].search_read(
        [('id', 'in', [c['id'] for c in shown])], ['phone', 'email'])}
    window = f">= {min_months} months ago" + (f" and <= {max_months} months ago" if max_months is not None else "")
    lines = [f"Customers whose last confirmed sale was {window} ({len(matched)} found, showing "
             f"{offset + 1}-{offset + len(shown)}), sorted by "
             f"{'total spent' if sort_by == 'total_spent' else 'last purchase, newest first'}; "
             f"amounts incl. taxes in {company_currency_name(env)}:"]
    for c in shown:
        contact = contacts.get(c['id'], {})
        lines.append(
            f"- {c['name']} [{c['id']}] | last sale {c['last']} | {c['orders']} orders | "
            f"{_money(c['spent'])} | phone: {contact.get('phone') or '-'} | email: {contact.get('email') or '-'} | "
            f"{url_for(env, 'res.partner', c['id'])}")
    remaining = len(matched) - (offset + len(shown))
    if remaining > 0:
        lines.append(f"... {remaining} more customers. Call again with offset={offset + len(shown)} "
                     "(same filters and sort_by).")
    return text_result("\n".join(lines))


# ---------------------------------------------------------------------------
# Public API used by the controller and by mcp.tool
# ---------------------------------------------------------------------------
def all_tool_definitions():
    """Every registered tool, installed or not (used to sync mcp.tool)."""
    return [tool['definition'] for tool in TOOLS.values()]


# Kept for backwards compatibility: some code reads TOOL_DEFINITIONS directly.
TOOL_DEFINITIONS = all_tool_definitions()


def visible_tool_definitions(env, api_key):
    """Tools this key may use (tool list + write mode) AND whose apps are installed."""
    return [tool['definition'] for name, tool in TOOLS.items()
            if tool_installed(env, name) and api_key._tool_allowed(name)
            and access_allowed(api_key, tool['access'])]


def call_tool(env, tool_name, arguments, api_key):
    """Run a tool. Returns None for an unknown tool name."""
    tool = TOOLS.get(tool_name)
    if tool is None:
        return None
    if not tool_installed(env, tool_name):
        raise ToolError(f"The tool '{tool_name}' needs an Odoo app that is not installed in this database.")
    if not access_allowed(api_key, tool_access(tool_name)):
        raise ToolError(
            f"The tool '{tool_name}' changes data, and this API key is read-only for that. An "
            "administrator can change 'Write Access' on the key under Claude Connector > API Keys.")
    if not isinstance(arguments, dict):
        raise ToolError("Tool arguments must be a JSON object")
    return tool['handler'](env, arguments, api_key)

