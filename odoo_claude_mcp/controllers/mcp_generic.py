# -*- coding: utf-8 -*-
"""Generic, whitelist-driven read-only query tools.

Every model and field Claude touches here must be (a) in the key's Queryable
Models, (b) not blocked on that model rule, and (c) readable by the key's
Run As User. Record rules and multi-company rules apply because the
environment is never sudo.
"""
import datetime as dt
import json
import re
from contextlib import closing

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.osv import expression

from .mcp_common import (
    HARD_MAX_ROWS, ToolError, can_read, day_start_utc, field_readable,
    format_local_datetime, local_to_utc, readable_field_names, register,
    text_result, user_today,
)
from .mcp_urls import record_url, url_for

DEFAULT_LIMIT = 20
AGGREGATE_DEFAULT_LIMIT = 100
GROUP_FETCH_CAP = 5000
MAX_FILTERS = 10
MAX_DOMAIN_LEAVES = 15
MAX_PATH_HOPS = 3            # e.g. order_id.partner_id.country_id
MAX_GROUP_BY = 3
MAX_HAVING = 5
MAX_MULTI_QUERIES = 6
MAX_EXPAND = 3
MAX_IN_VALUES = 100
MAX_STRING_VALUE = 200
MAX_DESCRIBE_FIELDS = 150
MAX_OUTPUT_CHARS = 15000
MAX_X2MANY_NAMES = 10
MAX_OFFSET = 100000

LOGIC_OPS = {'&', '|', '!'}
OPERATORS = {'=', '!=', '>', '>=', '<', '<=', 'in', 'not in', 'ilike', 'not ilike'}
COMPARE_OPS = {'=', '!=', '>', '>=', '<', '<='}
NUMERIC_AGGS = {'sum', 'avg', 'min', 'max'}
AGGS = NUMERIC_AGGS | {'count_distinct'}
GRANULARITIES = {'day', 'week', 'month', 'quarter', 'year'}
NUMERIC_TYPES = {'integer', 'float', 'monetary'}
DATE_TYPES = {'date', 'datetime'}
X2MANY_TYPES = {'one2many', 'many2many'}
RELATIONAL_TYPES = {'many2one'} | X2MANY_TYPES
# Never exposed: heavy, unstructured or untyped links.
HIDDEN_TYPES = {'binary', 'properties', 'properties_definition', 'json', 'reference', 'html'}
GROUPABLE_TYPES = {'char', 'selection', 'boolean', 'integer', 'many2one', 'many2many'} | DATE_TYPES
DISTINCT_TYPES = {'char', 'selection', 'boolean', 'integer', 'many2one'} | DATE_TYPES
MINMAX_TYPES = NUMERIC_TYPES | DATE_TYPES

DATE_ONLY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
DATETIME_RE = re.compile(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$')


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _as_list(value, name):
    """Models sometimes send JSON as a string; accept both."""
    if value is None or value == '':
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ToolError(f"'{name}' must be a JSON list")
    if not isinstance(value, list):
        raise ToolError(f"'{name}' must be a list")
    return value


def _to_int(value, name, default):
    if value in (None, ''):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"'{name}' must be an integer")


def _offset(arguments):
    offset = _to_int(arguments.get('offset'), 'offset', 0)
    if offset < 0 or offset > MAX_OFFSET:
        raise ToolError(f"'offset' must be between 0 and {MAX_OFFSET}")
    return offset


def _scalar_ok(value):
    if value is None or isinstance(value, (bool, int, float)):
        return True
    return isinstance(value, str) and len(value) <= MAX_STRING_VALUE


def _cap_output(lines):
    text = "\n".join(lines)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n... (output truncated: narrow the query or use aggregate mode)"
    return text


def _allowed_rules(api_key):
    """{model_name: mcp.allowed.model record} for this key."""
    if api_key.all_models:
        rules = api_key.env['mcp.allowed.model'].sudo().search([('active', '=', True)])
    else:
        rules = api_key.allowed_model_ids.filtered('active')
    return {r.model_name: r for r in rules}


def _rule_for(env, rules, model_name):
    model_name = (model_name or '').strip()
    rule = rules.get(model_name)
    if not rule or model_name not in env.registry:
        raise ToolError(
            f"Model '{model_name}' is not available for this key. "
            "Call list_models to see the available models.")
    if not can_read(env[model_name]):
        raise ToolError(f"Your Odoo user has no permission to read '{model_name}'.")
    return rule


def _fields_map(env, model_name, rule):
    """Fields the current user may read, minus admin-blocked and hidden types."""
    model = env[model_name]
    blocked = set(rule.blocked_field_ids.mapped('name'))
    result = {}
    for name in readable_field_names(model):
        field = model._fields.get(name)
        if field is None or name in blocked or field.type in HIDDEN_TYPES:
            continue
        result[name] = field
    return result


def _x2many_allowed(env, rules, field):
    """An x2many field is usable only if its target model is whitelisted and readable."""
    return (field.type in X2MANY_TYPES and field.comodel_name in rules
            and field.comodel_name in env.registry and can_read(env[field.comodel_name]))


def _limit_for(rule, arguments, default):
    return max(1, min(_to_int(arguments.get('limit'), 'limit', default),
                      rule.max_rows or HARD_MAX_ROWS, HARD_MAX_ROWS))


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------
def _fmt(env, value, field=None):
    """Render one value for the model. Many2one keeps its id so Claude can
    drill down: 'Azure Interior [12]'. Datetimes are shown in the user's tz."""
    if isinstance(value, (tuple, list)) and len(value) == 2 and isinstance(value[0], int) \
            and isinstance(value[1], str):
        return f"{value[1]} [{value[0]}]"
    if field is not None and field.type == 'boolean':
        return 'yes' if value else 'no'
    if value is False or value is None:
        return '-'
    if field is not None and field.type == 'datetime':
        return format_local_datetime(env, value)
    if isinstance(value, dt.datetime):
        return format_local_datetime(env, value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:,.2f}"
    text = str(value)
    return text if len(text) <= 200 else text[:197] + '...'


def _x2many_names(env, comodel, ids):
    """{id: display_name} for related records the user may read (silently
    skipping ones hidden by record rules)."""
    if not ids:
        return {}
    try:
        records = env[comodel].search([('id', 'in', list(ids))])
        return {rec.id: rec.display_name for rec in records}
    except AccessError:
        return {}


def _fmt_x2many(ids, names):
    if not ids:
        return '-'
    shown = [f"{names.get(i, '?')} [{i}]" for i in ids[:MAX_X2MANY_NAMES] if i in names]
    hidden = len(ids) - len(shown)
    text = ", ".join(shown) or '-'
    if hidden > 0:
        text += f" (+{hidden} more)"
    return text


# ---------------------------------------------------------------------------
# Domain building
# ---------------------------------------------------------------------------
def _resolve_token(env, value):
    """Relative date tokens, computed in the user's time zone."""
    if not isinstance(value, str) or not value.startswith('@'):
        return value
    today = user_today(env)
    name, _sep, arg = value[1:].partition(':')
    try:
        n = int(arg) if arg else 0
    except ValueError:
        raise ToolError(f"Bad date token '{value}'")
    quarter_start = today.replace(month=3 * ((today.month - 1) // 3) + 1, day=1)
    tokens = {
        'today': lambda: today,
        'yesterday': lambda: today - dt.timedelta(days=1),
        'start_of_week': lambda: today - dt.timedelta(days=today.weekday()),
        'start_of_month': lambda: today.replace(day=1),
        'end_of_month': lambda: today.replace(day=1) + relativedelta(months=1, days=-1),
        'start_of_last_month': lambda: today.replace(day=1) - relativedelta(months=1),
        'end_of_last_month': lambda: today.replace(day=1) - dt.timedelta(days=1),
        'start_of_quarter': lambda: quarter_start,
        'end_of_quarter': lambda: quarter_start + relativedelta(months=3, days=-1),
        'start_of_year': lambda: today.replace(month=1, day=1),
        'end_of_year': lambda: today.replace(month=12, day=31),
        'start_of_last_year': lambda: today.replace(year=today.year - 1, month=1, day=1),
        'end_of_last_year': lambda: today.replace(year=today.year - 1, month=12, day=31),
        'days_ago': lambda: today - dt.timedelta(days=n),
        'days_ahead': lambda: today + dt.timedelta(days=n),
        'weeks_ago': lambda: today - dt.timedelta(weeks=n),
        'months_ago': lambda: today - relativedelta(months=n),
        'years_ago': lambda: today - relativedelta(years=n),
    }
    if name not in tokens:
        raise ToolError(
            f"Unknown date token '{value}'. Use one of: "
            + ", ".join('@' + t + (':N' if t.endswith(('_ago', '_ahead')) else '') for t in tokens))
    return tokens[name]().isoformat()


def _resolve_field_path(env, rules, model_name, path):
    """Validate a dotted path (up to MAX_PATH_HOPS relational hops) and return
    the final field. Every model crossed must be whitelisted and readable."""
    if not isinstance(path, str) or not path:
        raise ToolError("Each filter needs a 'field'")
    parts = path.split('.')
    if len(parts) > MAX_PATH_HOPS + 1:
        raise ToolError(
            f"Path '{path}' is too deep (max {MAX_PATH_HOPS} hops, e.g. order_id.partner_id.country_id)")
    current = model_name
    field = None
    for index, part in enumerate(parts):
        rule = rules.get(current)
        if not rule:
            raise ToolError(f"Model '{current}' (needed by '{path}') is not available for this key.")
        if index > 0 and not can_read(env[current]):
            raise ToolError(f"Your Odoo user has no permission to read '{current}'.")
        field = _fields_map(env, current, rule).get(part)
        if field is None:
            raise ToolError(f"Unknown or restricted field '{part}' on '{current}'.")
        if not (field.store or field.search):
            raise ToolError(f"Field '{part}' on '{current}' cannot be used in filters.")
        if field.type in X2MANY_TYPES and field.comodel_name not in rules:
            raise ToolError(f"'{part}' points to '{field.comodel_name}', which is not available for this key.")
        if index < len(parts) - 1:
            if field.type not in RELATIONAL_TYPES:
                raise ToolError(f"'{part}' is not a relational field, so '{path}' is not valid.")
            current = field.comodel_name
    return field


def _datetime_leaf(env, path, operator, value):
    """Turn a date or local datetime into UTC conditions on a datetime field.
    A date-only value means the whole local day."""
    if value is False or value is None:
        return [(path, operator, value)]
    if not isinstance(value, str):
        raise ToolError(f"Use a date 'YYYY-MM-DD' or datetime 'YYYY-MM-DD HH:MM' for '{path}'")
    value = value.strip()
    if DATE_ONLY_RE.match(value):
        try:
            day = dt.date.fromisoformat(value)
        except ValueError:
            raise ToolError(f"Invalid date '{value}'")
        start = day_start_utc(env, day)
        nxt = day_start_utc(env, day + dt.timedelta(days=1))
        mapping = {
            '>=': [(path, '>=', start)],
            '>': [(path, '>=', nxt)],
            '<': [(path, '<', start)],
            '<=': [(path, '<', nxt)],
            '=': ['&', (path, '>=', start), (path, '<', nxt)],
            '!=': ['|', (path, '<', start), (path, '>=', nxt)],
        }
        if operator not in mapping:
            raise ToolError(f"Operator '{operator}' is not supported on date values of '{path}'")
        return mapping[operator]
    if DATETIME_RE.match(value):
        if operator not in COMPARE_OPS:
            raise ToolError(f"Operator '{operator}' is not supported on datetime values of '{path}'")
        try:
            local = dt.datetime.fromisoformat(value.replace('T', ' '))
        except ValueError:
            raise ToolError(f"Invalid datetime '{value}'")
        return [(path, operator, fields.Datetime.to_string(local_to_utc(env, local)))]
    raise ToolError(f"Use a date 'YYYY-MM-DD' or datetime 'YYYY-MM-DD HH:MM' for '{path}'")


def _validated_leaf(env, rules, model_name, path, operator, value):
    """Return a list of prefix-notation domain terms forming ONE condition."""
    operator = str(operator or '=').lower().strip()
    if operator not in OPERATORS:
        raise ToolError(f"Operator '{operator}' is not allowed. Allowed: {', '.join(sorted(OPERATORS))}")
    field = _resolve_field_path(env, rules, model_name, path)
    if operator in ('in', 'not in'):
        if (not isinstance(value, list) or len(value) > MAX_IN_VALUES
                or not all(_scalar_ok(v) for v in value)):
            raise ToolError(f"'{operator}' needs a list of up to {MAX_IN_VALUES} simple values")
        if field.type == 'datetime':
            raise ToolError(f"Use '>=' and '<' (not '{operator}') on datetime field '{path}'")
        return [(path, operator, [_resolve_token(env, v) for v in value])]
    if not _scalar_ok(value):
        raise ToolError("Filter values must be simple (text, number, boolean, null)")
    value = _resolve_token(env, value)
    if field.type == 'datetime':
        return _datetime_leaf(env, path, operator, value)
    return [(path, operator, value)]


def _build_domain(env, rules, model_name, filters, domain=None):
    """AND of `filters` plus an optional Odoo prefix `domain` (supports
    '|', '&', '!'). Every field path is validated against the whitelist."""
    if len(filters) > MAX_FILTERS:
        raise ToolError(f"Too many filters (max {MAX_FILTERS})")
    parts = []
    for flt in filters:
        if not isinstance(flt, dict):
            raise ToolError("Each filter must be an object: {field, operator, value}")
        parts.append(_validated_leaf(
            env, rules, model_name, flt.get('field'), flt.get('operator'), flt.get('value')))

    extra = []
    leaves = 0
    for item in _as_list(domain, 'domain'):
        if isinstance(item, str):
            if item not in LOGIC_OPS:
                raise ToolError(f"Domain operator '{item}' is not allowed (use '&', '|', '!')")
            extra.append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 3:
            leaves += 1
            # A leaf may expand into a self-contained sub-domain ('&', a, b), which
            # is still exactly one term in prefix notation.
            extra.extend(_validated_leaf(env, rules, model_name, item[0], item[1], item[2]))
        else:
            raise ToolError("Each domain item must be '&', '|', '!' or [field, operator, value]")
    if leaves > MAX_DOMAIN_LEAVES:
        raise ToolError(f"Domain has too many conditions (max {MAX_DOMAIN_LEAVES})")
    if extra:
        try:
            extra = expression.normalize_domain(extra)
            # normalize_domain does not detect missing operands; check arity.
            _check_prefix_arity(extra)
        except ToolError:
            raise
        except Exception:  # noqa: BLE001 - malformed prefix notation
            raise ToolError("Malformed domain: check the '|' / '&' / '!' structure")
        parts.append(extra)
    return expression.AND(parts) if parts else []


def _check_prefix_arity(domain):
    needed = 1
    for item in domain:
        if needed <= 0:
            raise ToolError("Malformed domain: too many conditions for the '|' / '&' operators")
        if item in ('&', '|'):
            needed += 1
        elif item == '!':
            pass
        else:
            needed -= 1
    if needed != 0:
        raise ToolError("Malformed domain: an operator is missing a condition")


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------
@register(
    'list_models',
    "List the Odoo models (tables) you are allowed to query with describe_model, "
    "find_fields, query_data and get_record. For any question not covered by a "
    "dedicated tool, start here or with find_fields.",
    {"type": "object", "properties": {}},
)
def tool_list_models(env, arguments, api_key):
    rules = _allowed_rules(api_key)
    if not rules:
        return text_result(
            "No models are assigned to this API key. Ask an administrator to tick "
            "'All whitelisted models' or pick models under 'Queryable Models' on the key "
            "(and make sure the MCP Queryable Models list has active records).",
            is_error=True)

    lines = []
    denied = []
    for name, rule in sorted(rules.items()):
        if name not in env.registry:
            continue
        if not can_read(env[name]):
            denied.append(name)
            continue
        lines.append(f"- {name} — {rule.model_id.name} (max {min(rule.max_rows, HARD_MAX_ROWS)} rows per query)")

    if not lines:
        return text_result(
            "Models are assigned to this key, but the Odoo user it runs as cannot read any "
            f"of them ({', '.join(denied[:10]) or 'none installed'}). "
            "Grant that user read access in Odoo.", is_error=True)
    companies = ", ".join(f"{c.name} [{c.id}]" for c in env.companies)
    return text_result(
        f"Companies in scope: {companies}\nAvailable models:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# describe_model / find_fields
# ---------------------------------------------------------------------------
def _capabilities(env, rules, field):
    caps = []
    if field.type in X2MANY_TYPES and not _x2many_allowed(env, rules, field):
        return 'target model not allowed'
    if field.store or field.search:
        caps.append('filter')
    if field.store and field.type in GROUPABLE_TYPES and field.type != 'one2many':
        caps.append('group_by')
    if field.store and field.type in NUMERIC_TYPES:
        caps.append('measure:sum/avg/min/max')
    elif field.store and field.type in DATE_TYPES:
        caps.append('measure:min/max')
    if field.store and field.type in DISTINCT_TYPES:
        caps.append('count_distinct')
    if field.type == 'one2many':
        caps.append('expand (get_record)')
    return ','.join(caps) or 'read only'


def _target(field):
    return f" -> {field.comodel_name}" if field.type in RELATIONAL_TYPES else ''


@register(
    'describe_model',
    "Show the queryable fields of a model: name, type, label and what each can be "
    "used for (filter, group_by, measure, count_distinct, expand). Use `search` "
    "to narrow down fields by name or label.",
    {
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Technical model name, e.g. 'sale.order'"},
            "search": {"type": "string", "description": "Optional text to match in field names/labels"},
        },
        "required": ["model"],
    },
)
def tool_describe_model(env, arguments, api_key):
    rules = _allowed_rules(api_key)
    model_name = arguments.get('model')
    rule = _rule_for(env, rules, model_name)
    needle = (arguments.get('search') or '').strip().lower()

    lines = []
    for name, field in sorted(_fields_map(env, model_name, rule).items()):
        label = field.string or ''
        if needle and needle not in name.lower() and needle not in label.lower():
            continue
        extra = _target(field)
        if field.type == 'selection':
            try:
                options = field._description_selection(env)
                extra = " values: " + ", ".join(f"{k}={v}" for k, v in options[:12])
            except Exception:  # noqa: BLE001 - dynamic selections may fail; skip silently
                extra = ''
        if field.readonly and (field.compute or field.related):
            write_flag = ' computed'
        elif field.readonly:
            write_flag = ' set-at-creation-only'
        else:
            write_flag = ''
        flags = ''.join([' required' if field.required else '', write_flag])
        lines.append(f"- {name} ({field.type}{flags}) '{label}' [{_capabilities(env, rules, field)}]{extra}")

    if not lines:
        return text_result(f"No fields matched on '{model_name}'.")
    if not needle:
        from .mcp_write import actions_help  # local import: mcp_write imports this module
        notes = []
        if rule.allow_create:
            notes.append("create_record is allowed on this model (as draft; required fields must be set; "
                         "computed fields and 'state' cannot be set).")
        if rule.allow_actions and actions_help(model_name):
            notes.append("Business actions (execute_action): " + "; ".join(
                line.split(': ', 1)[1] for line in actions_help(model_name)))
        lines += notes
    header = (f"Fields of {model_name}. Paths may cross up to {MAX_PATH_HOPS} relational "
              "fields, e.g. partner_id.country_id.code or tag_ids.name:")
    if len(lines) > MAX_DESCRIBE_FIELDS:
        lines = lines[:MAX_DESCRIBE_FIELDS] + [
            "... more fields exist; use the 'search' argument to narrow down."]
    return text_result(_cap_output([header] + lines))


@register(
    'find_fields',
    "Search ALL available models at once for fields/models matching one or more "
    "words (e.g. 'warehouse quantity', 'employee department', 'invoice due'). "
    "Use this first when you do not know which model holds the data.",
    {
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Words to look for in field names / labels / model names"},
            "model": {"type": "string", "description": "Optional: restrict the search to one model"},
        },
        "required": ["search"],
    },
)
def tool_find_fields(env, arguments, api_key):
    rules = _allowed_rules(api_key)
    terms = [t for t in (arguments.get('search') or '').lower().split() if len(t) >= 2]
    if not terms:
        raise ToolError("'search' needs at least one word of 2+ characters")
    only_model = (arguments.get('model') or '').strip()

    scored = []  # (score, line)
    for model_name, rule in sorted(rules.items()):
        if only_model and model_name != only_model:
            continue
        if model_name not in env.registry or not can_read(env[model_name]):
            continue
        model_text = f"{model_name} {rule.model_id.name or ''}".lower()
        model_score = sum(1 for t in terms if t in model_text)
        if model_score:
            scored.append((model_score + 1, f"[MODEL] {model_name} — {rule.model_id.name}"))
        for fname, field in _fields_map(env, model_name, rule).items():
            text = f"{fname} {field.string or ''}".lower()
            score = sum(1 for t in terms if t in text)
            if score:
                scored.append((score, f"{model_name}.{fname} ({field.type}) '{field.string}'"
                                      f" [{_capabilities(env, rules, field)}]{_target(field)}"))
    if not scored:
        return text_result(
            "No matching model or field among the models available for this key. "
            "Try another word (English technical names work best), or ask an "
            "administrator to whitelist the model.")
    scored.sort(key=lambda item: -item[0])
    lines = [line for _score, line in scored[:60]]
    if len(scored) > 60:
        lines.append(f"... {len(scored) - 60} more matches; narrow the search or pass 'model'.")
    return text_result(_cap_output(lines))


# ---------------------------------------------------------------------------
# query_data - aggregate mode
# ---------------------------------------------------------------------------
def _parse_group_by(env, rules, fmap, group_by):
    if len(group_by) > MAX_GROUP_BY:
        raise ToolError(f"At most {MAX_GROUP_BY} group_by fields are allowed")
    specs = []
    for item in group_by:
        if not isinstance(item, str):
            raise ToolError("group_by items must be strings like 'partner_id' or 'date_order:month'")
        name, _sep, granularity = item.partition(':')
        field = fmap.get(name)
        if field is None or not field.store:
            raise ToolError(f"Cannot group by '{name}' (unknown, restricted or not stored).")
        if field.type not in GROUPABLE_TYPES:
            raise ToolError(f"Cannot group by '{name}' ({field.type}).")
        if field.type == 'many2many' and not _x2many_allowed(env, rules, field):
            raise ToolError(f"Cannot group by '{name}': '{field.comodel_name}' is not available for this key.")
        if field.type in DATE_TYPES:
            granularity = granularity or 'month'
            if granularity not in GRANULARITIES:
                raise ToolError(f"Granularity must be one of {', '.join(sorted(GRANULARITIES))}")
            specs.append(f"{name}:{granularity}")
        else:
            if granularity:
                raise ToolError(f"Granularity only applies to date fields, not '{name}'")
            specs.append(name)
    return specs


def _parse_measures(fmap, measures):
    parsed = []  # (alias, read_group spec, field, agg)
    seen = set()
    for item in measures:
        if not isinstance(item, dict):
            raise ToolError("Each measure must be an object: {field, agg}")
        name = item.get('field')
        agg = str(item.get('agg') or 'sum').lower()
        field = fmap.get(name)
        if agg not in AGGS:
            raise ToolError(f"Aggregation '{agg}' not allowed. Allowed: {', '.join(sorted(AGGS))}")
        if field is None or not field.store:
            raise ToolError(f"'{name}' is not a stored field available for measures.")
        if agg == 'count_distinct':
            if field.type not in DISTINCT_TYPES:
                raise ToolError(f"count_distinct is not supported on '{name}' ({field.type}).")
        elif agg in ('min', 'max'):
            if field.type not in MINMAX_TYPES:
                raise ToolError(f"'{agg}' needs a numeric or date field, not '{name}' ({field.type}).")
        elif field.type not in NUMERIC_TYPES:
            raise ToolError(f"'{agg}' needs a numeric field, not '{name}' ({field.type}).")
        alias = f"{name}_{agg}"
        if alias in seen:
            continue
        seen.add(alias)
        parsed.append((alias, f"{alias}:{agg}({name})", field, agg))
    return parsed


def _parse_having(having, valid_names):
    parsed = []
    if len(having) > MAX_HAVING:
        raise ToolError(f"At most {MAX_HAVING} having conditions")
    for item in having:
        if not isinstance(item, dict):
            raise ToolError("Each having item must be {measure, operator, value}")
        measure = item.get('measure')
        operator = str(item.get('operator') or '>').strip()
        value = item.get('value')
        if measure not in valid_names:
            raise ToolError(f"having.measure must be one of: {', '.join(sorted(valid_names))}")
        if operator not in COMPARE_OPS:
            raise ToolError(f"having.operator must be one of: {', '.join(sorted(COMPARE_OPS))}")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ToolError("having.value must be a number")
        parsed.append((measure, operator, value))
    return parsed


def _having_ok(row, conditions):
    for measure, operator, value in conditions:
        current = row.get(measure) or 0
        if not isinstance(current, (int, float)):
            return False
        if not {
            '=': current == value, '!=': current != value,
            '>': current > value, '>=': current >= value,
            '<': current < value, '<=': current <= value,
        }[operator]:
            return False
    return True


def _group_sort_key(group, spec, field):
    """Sort key that keeps dates chronological and puts empty groups last."""
    value = group.get(spec)
    if field.type in DATE_TYPES:
        rng = (group.get('__range') or {}).get(spec) or {}
        start = rng.get('from')
        return (0, start) if start else (1, '')
    if not value and value != 0:
        return (1, '')
    if isinstance(value, (tuple, list)):
        return (0, str(value[1]).lower())
    if isinstance(value, str):
        return (0, value.lower())
    return (0, value)


def _run_aggregate(env, rules, model_name, rule, fmap, domain, arguments, limit, offset):
    group_by = _parse_group_by(env, rules, fmap, _as_list(arguments.get('group_by'), 'group_by'))
    measures = _parse_measures(fmap, _as_list(arguments.get('measures'), 'measures'))
    group_fields = {spec: fmap[spec.split(':')[0]] for spec in group_by}
    measure_names = {alias for alias, _s, _f, _a in measures} | {'count'}
    having = _parse_having(_as_list(arguments.get('having'), 'having'), measure_names)
    model = env[model_name]

    specs = [spec for _alias, spec, _f, _a in measures]
    groups = model.read_group(domain, specs, group_by, lazy=False,
                              limit=GROUP_FETCH_CAP if group_by else None)

    rows = []
    for group in groups:
        row = {'_group': group}
        for spec in group_by:
            row[spec] = group.get(spec)
        row['count'] = group.get('__count', 0)
        for alias, _spec, _field, _agg in measures:
            row[alias] = group.get(alias)
            if row[alias] is None or row[alias] is False:
                row[alias] = 0 if _field.type in NUMERIC_TYPES or _agg == 'count_distinct' else None
        rows.append(row)
    if having:
        rows = [row for row in rows if _having_ok(row, having)]

    # Default ordering: chronological when grouping by a date, otherwise by
    # the first measure (or count) descending.
    first_date = next((s for s in group_by if group_fields[s].type in DATE_TYPES), None)
    order_by = arguments.get('order_by')
    valid_orders = set(group_by) | measure_names
    if order_by and order_by not in valid_orders:
        raise ToolError(f"order_by must be one of: {', '.join(sorted(valid_orders))}")
    if not order_by:
        order_by = first_date or (measures[0][0] if measures else 'count')
        default_dir = 'asc' if first_date else 'desc'
    else:
        default_dir = 'asc' if order_by in group_fields else 'desc'
    descending = str(arguments.get('order_direction') or default_dir).lower() == 'desc'

    if order_by in group_fields:
        rows.sort(key=lambda r: _group_sort_key(r['_group'], order_by, group_fields[order_by]),
                  reverse=descending)
    else:
        present = [r for r in rows if r.get(order_by) is not None]
        missing = [r for r in rows if r.get(order_by) is None]
        present.sort(key=lambda r: r[order_by], reverse=descending)
        rows = present + missing

    total_groups = len(rows)
    shown = rows[offset:offset + limit]
    columns = list(group_by) + [alias for alias, _s, _f, _a in measures] + ['count']
    measure_fields = {alias: field for alias, _s, field, _a in measures}
    header = (f"{model_name} — aggregated by {', '.join(group_by) or 'all records'} "
              f"(sorted by {order_by} {'desc' if descending else 'asc'}")
    if having:
        header += "; having " + " and ".join(f"{m} {o} {v}" for m, o, v in having)
    lines = [header + "):"]
    for row in shown:
        parts = []
        for col in columns:
            if col in group_fields:
                field = group_fields[col]
                parts.append(f"{col}={_fmt(env, row.get(col), None if field.type in DATE_TYPES else field)}")
            elif col in measure_fields and measure_fields[col].type in DATE_TYPES:
                parts.append(f"{col}={_fmt(env, row.get(col), measure_fields[col])}")
            else:
                parts.append(f"{col}={_fmt(env, row.get(col))}")
        lines.append(" | ".join(parts))
    if not shown:
        lines.append("(no groups)" if not offset else f"(offset {offset} is beyond the {total_groups} groups)")
    elif total_groups > offset + len(shown):
        lines.append(f"... showing {offset + 1}-{offset + len(shown)} of {total_groups} groups. "
                     f"Use offset={offset + len(shown)} for more.")
    if group_by and len(groups) >= GROUP_FETCH_CAP:
        lines.append("WARNING: too many groups; results may be incomplete. Add filters.")
    return _cap_output(lines)


# ---------------------------------------------------------------------------
# query_data - records mode
# ---------------------------------------------------------------------------
def _check_read_fields(env, rules, model_name, fmap, requested, max_fields):
    if len(requested) > max_fields:
        raise ToolError(f"At most {max_fields} fields per request")
    for name in requested:
        field = fmap.get(name)
        if field is None:
            raise ToolError(f"Field '{name}' is unknown or restricted on '{model_name}'.")
        if field.type == 'one2many':
            raise ToolError(f"'{name}' is a list of lines; read it with get_record and `expand`.")
        if field.type == 'many2many' and not _x2many_allowed(env, rules, field):
            raise ToolError(f"'{name}' points to '{field.comodel_name}', which is not available for this key.")


def _render_values(env, fmap, rows, requested):
    """For each row, a list of (field name, formatted value). Many2many ids are
    resolved to names with one query per field."""
    names = {}
    for name in requested:
        field = fmap.get(name)
        if field is not None and field.type == 'many2many':
            ids = {i for row in rows for i in (row.get(name) or [])[:MAX_X2MANY_NAMES]}
            names[name] = _x2many_names(env, field.comodel_name, ids)
    out = []
    for row in rows:
        values = []
        for name in requested:
            if name == 'id':
                continue
            field = fmap.get(name)
            if field is not None and field.type == 'many2many':
                values.append((name, _fmt_x2many(row.get(name) or [], names[name])))
            else:
                values.append((name, _fmt(env, row.get(name), field)))
        out.append(values)
    return out


def _render_rows(env, fmap, rows, requested, model_name=None):
    """One 'id=.. | field=value | .. | url=..' line per row (url when model_name is given)."""
    out = []
    for row, values in zip(rows, _render_values(env, fmap, rows, requested)):
        parts = [f"id={row.get('id')}"] + [f"{name}={text}" for name, text in values]
        if model_name:
            parts.append(f"url={url_for(env, model_name, row.get('id'))}")
        out.append(" | ".join(parts))
    return out


def _run_records(env, rules, model_name, rule, fmap, domain, arguments, limit, offset):
    requested = _as_list(arguments.get('fields'), 'fields') or ['display_name']
    _check_read_fields(env, rules, model_name, fmap, requested, 15)

    order = None
    order_by = arguments.get('order_by')
    if order_by:
        field = fmap.get(order_by)
        if field is None or not field.store or field.type in X2MANY_TYPES:
            raise ToolError(f"Cannot order by '{order_by}'.")
        direction = 'asc' if str(arguments.get('order_direction') or 'desc').lower() == 'asc' else 'desc'
        order = f"{order_by} {direction}, id {direction}"

    model = env[model_name]
    total = model.search_count(domain)
    kwargs = {'limit': limit, 'offset': offset}
    if order:
        kwargs['order'] = order
    rows = model.search_read(domain, requested, **kwargs)

    if not rows:
        if offset and total:
            return f"{model_name} — offset {offset} is beyond the {total} matching records."
        return f"{model_name} — no matching records."
    lines = [f"{model_name} — records {offset + 1}-{offset + len(rows)} of {total} matching:"]
    include_urls = arguments.get('include_urls', True) not in (False, 'false', 0)
    lines += _render_rows(env, fmap, rows, requested, model_name if include_urls else None)
    if total > offset + len(rows):
        lines.append(f"... {total - offset - len(rows)} more records. Use offset={offset + len(rows)} "
                     "for the next page, or aggregate mode for totals.")
    return _cap_output(lines)


@register(
    'query_data',
    "Read-only structured query on an allowed model. Two modes: "
    "(1) RECORDS mode: pass `fields` (and optional filters/order/offset) to list records. "
    "(2) AGGREGATE mode: pass `group_by` and/or `measures` to summarize "
    "(e.g. total sales per month), optionally filtered on totals with `having`. "
    "Call describe_model or find_fields first so you use real field names. "
    "Paths may cross up to 3 relational fields (many2one, one2many, many2many), "
    "e.g. 'order_id.partner_id.country_id' or 'tag_ids.name'. Dates and datetimes "
    "you send are in the user's time zone; a date on a datetime field means the whole day.",
    {
        "type": "object",
        "properties": {
            "model": {"type": "string"},
            "domain": {
                "type": "array",
                "description": (
                    "Optional Odoo prefix domain for OR / NOT logic, e.g. "
                    "[\"|\", [\"state\", \"=\", \"sale\"], [\"state\", \"=\", \"done\"]]. "
                    "Combined with `filters` using AND."),
            },
            "filters": {
                "type": "array",
                "description": (
                    "List of conditions combined with AND. Date values accept tokens: "
                    "@today, @yesterday, @start_of_week, @start_of_month, @end_of_month, "
                    "@start_of_last_month, @end_of_last_month, @start_of_quarter, "
                    "@end_of_quarter, @start_of_year, @end_of_year, @start_of_last_year, "
                    "@end_of_last_year, @days_ago:N, @days_ahead:N, @weeks_ago:N, "
                    "@months_ago:N, @years_ago:N."),
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "operator": {"type": "string", "enum": sorted(OPERATORS)},
                        "value": {},
                    },
                    "required": ["field", "value"],
                },
            },
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "RECORDS mode: fields to return (default display_name). "
                                      "Many2one values come back as 'Name [id]'."},
            "group_by": {"type": "array", "items": {"type": "string"},
                         "description": f"AGGREGATE mode: up to {MAX_GROUP_BY} fields (many2one, many2many, "
                                        "selection, char, boolean, integer, date). Dates accept "
                                        "':day|week|month|quarter|year', e.g. 'date_order:month'."},
            "measures": {
                "type": "array",
                "description": (
                    "AGGREGATE mode: values to compute. sum/avg/min/max on numeric fields, "
                    "min/max on dates, count_distinct on many2one/char/selection/date fields "
                    "(e.g. {field: 'partner_id', agg: 'count_distinct'} = number of distinct customers). "
                    "Alias is '<field>_<agg>'. A record count is always included as 'count'."),
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "agg": {"type": "string", "enum": sorted(AGGS)},
                    },
                    "required": ["field"],
                },
            },
            "having": {
                "type": "array",
                "description": "AGGREGATE mode: keep only groups whose measure matches, e.g. "
                               "[{measure: 'amount_total_sum', operator: '>', value: 10000}] or "
                               "[{measure: 'count', operator: '>=', value: 5}].",
                "items": {
                    "type": "object",
                    "properties": {
                        "measure": {"type": "string"},
                        "operator": {"type": "string", "enum": sorted(COMPARE_OPS)},
                        "value": {"type": "number"},
                    },
                    "required": ["measure", "value"],
                },
            },
            "order_by": {"type": "string",
                         "description": "RECORDS: a stored field. AGGREGATE: a group_by spec, 'count', or a "
                                        "measure alias like 'amount_total_sum'. Date groups default to "
                                        "chronological order."},
            "order_direction": {"type": "string", "enum": ["asc", "desc"]},
            "limit": {"type": "integer",
                      "description": f"Default {DEFAULT_LIMIT} records / {AGGREGATE_DEFAULT_LIMIT} groups, "
                                     f"hard max {HARD_MAX_ROWS}."},
            "offset": {"type": "integer", "description": "Skip this many records/groups, to page through results."},
            "include_urls": {"type": "boolean",
                             "description": "RECORDS mode: add a direct Odoo URL per record (default true)."},
        },
        "required": ["model"],
    },
)
def tool_query_data(env, arguments, api_key):
    rules = _allowed_rules(api_key)
    model_name = arguments.get('model')
    rule = _rule_for(env, rules, model_name)
    fmap = _fields_map(env, model_name, rule)

    domain = _build_domain(
        env, rules, model_name, _as_list(arguments.get('filters'), 'filters'),
        arguments.get('domain'))
    aggregate = bool(arguments.get('group_by') or arguments.get('measures'))
    limit = _limit_for(rule, arguments, AGGREGATE_DEFAULT_LIMIT if aggregate else DEFAULT_LIMIT)
    offset = _offset(arguments)

    try:
        if aggregate:
            text = _run_aggregate(env, rules, model_name, rule, fmap, domain, arguments, limit, offset)
        else:
            if arguments.get('having'):
                raise ToolError("'having' only works in aggregate mode (with group_by or measures)")
            text = _run_records(env, rules, model_name, rule, fmap, domain, arguments, limit, offset)
    except (UserError, ValueError, KeyError) as exc:
        raise ToolError(f"Query failed: {exc}")
    return text_result(text)


# ---------------------------------------------------------------------------
# get_record
# ---------------------------------------------------------------------------
@register(
    'get_record',
    "Read one record by id, and optionally expand its lines "
    "(e.g. model=sale.order, expand=[{field: 'order_line', fields: "
    "['product_id','product_uom_qty','price_subtotal']}]).",
    {
        "type": "object",
        "properties": {
            "model": {"type": "string"},
            "id": {"type": "integer"},
            "fields": {"type": "array", "items": {"type": "string"}},
            "expand": {
                "type": "array",
                "description": f"Up to {MAX_EXPAND} one2many/many2many fields to list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "fields": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                    "required": ["field"],
                },
            },
        },
        "required": ["model", "id"],
    },
)
def tool_get_record(env, arguments, api_key):
    rules = _allowed_rules(api_key)
    model_name = arguments.get('model')
    rule = _rule_for(env, rules, model_name)
    fmap = _fields_map(env, model_name, rule)
    record_id = _to_int(arguments.get('id'), 'id', None)
    if not record_id:
        raise ToolError("'id' is required")

    requested = _as_list(arguments.get('fields'), 'fields') or ['display_name']
    _check_read_fields(env, rules, model_name, fmap, requested, 25)

    model = env[model_name]
    # Archived records (e.g. lost leads) are found too when asked for by id.
    record = model.with_context(active_test=False).search([('id', '=', record_id)], limit=1)
    if not record:
        raise ToolError(f"{model_name} #{record_id} was not found (or you cannot access it).")
    data = record.read(requested)
    lines = [f"{model_name} #{record_id}:", f"  url: {record_url(env, record)}"]
    for name, text in _render_values(env, fmap, data, requested)[0]:
        lines.append(f"  {name}: {text}")

    blocked = set(rule.blocked_field_ids.mapped('name'))
    for item in _as_list(arguments.get('expand'), 'expand')[:MAX_EXPAND]:
        if not isinstance(item, dict):
            raise ToolError("Each expand item must be {field, fields, limit, offset}")
        fname = item.get('field')
        field = model._fields.get(fname)
        if field is None or fname in blocked or field.type not in X2MANY_TYPES:
            raise ToolError(f"'{fname}' is not an expandable one2many/many2many field on '{model_name}'.")
        if not field_readable(model, fname):
            raise ToolError(f"Your Odoo user cannot read '{fname}'.")
        sub_rule = _rule_for(env, rules, field.comodel_name)
        sub_map = _fields_map(env, field.comodel_name, sub_rule)
        sub_fields = _as_list(item.get('fields'), 'fields') or ['display_name']
        _check_read_fields(env, rules, field.comodel_name, sub_map, sub_fields, 12)
        sub_limit = _limit_for(sub_rule, item, DEFAULT_LIMIT)
        sub_offset = _offset(item)
        related = record[fname]
        try:
            rows = related[sub_offset:sub_offset + sub_limit].read(sub_fields)
        except AccessError:
            raise ToolError(f"Your Odoo user cannot read '{field.comodel_name}'.")
        lines.append(f"  {fname} — lines {sub_offset + 1}-{sub_offset + len(rows)} of "
                     f"{len(related)} ({field.comodel_name}):")
        for row_text in _render_rows(env, sub_map, rows, sub_fields, field.comodel_name):
            lines.append("    " + row_text)
    return text_result(_cap_output(lines))


# ---------------------------------------------------------------------------
# multi_query
# ---------------------------------------------------------------------------
@register(
    'multi_query',
    f"Run up to {MAX_MULTI_QUERIES} query_data requests in ONE call (each item takes the same "
    "arguments as query_data plus an optional `label`). Use it for comparisons, ratios and "
    "period-over-period questions, then compute the result yourself.",
    {
        "type": "object",
        "properties": {
            "queries": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["queries"],
    },
)
def tool_multi_query(env, arguments, api_key):
    queries = _as_list(arguments.get('queries'), 'queries')
    if not queries or len(queries) > MAX_MULTI_QUERIES:
        raise ToolError(f"'queries' needs 1 to {MAX_MULTI_QUERIES} query_data requests")
    out = []
    for index, query in enumerate(queries, 1):
        if not isinstance(query, dict):
            raise ToolError("Each query must be an object with the same arguments as query_data")
        label = query.get('label') or f"Query {index}"
        try:
            # Each sub-query gets its own savepoint so a database error in one
            # does not abort the transaction for the others.
            with closing(env.cr.savepoint()):
                text = tool_query_data(env, query, api_key)['content'][0]['text']
        except ToolError as exc:
            text = f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001 - reported per query, others still run
            text = f"ERROR: query failed ({type(exc).__name__})"
        out.append(f"### {label}\n{text}")
    return text_result(_cap_output(out))
