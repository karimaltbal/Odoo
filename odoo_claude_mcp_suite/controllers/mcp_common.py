# -*- coding: utf-8 -*-
"""Shared building blocks for the MCP tools: errors, result helpers, the single
tool registry, time-zone helpers and small Odoo-version compatibility shims.

This file has no dependency on odoo.http so it can also be imported from models.
"""
import datetime as dt
import re

import pytz

from odoo import fields, release
from odoo.exceptions import AccessError

# Absolute upper bound of rows any tool may return in one call.
HARD_MAX_ROWS = 200

ODOO_VERSION = release.major_version  # e.g. '17.0' or 'saas~17.4'
ODOO_MAJOR = int(re.search(r'\d+', ODOO_VERSION).group())


class ToolError(Exception):
    """Raised by a tool for an expected, user-facing problem (bad argument,
    missing access, ...). The message is sent back to Claude as-is."""


def text_result(text, is_error=False):
    result = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


# Backwards-compatible alias (older code imported the private name).
_text_result = text_result


# ---------------------------------------------------------------------------
# Single tool registry
# ---------------------------------------------------------------------------
# name -> {'definition': {...MCP tool definition...},
#          'handler': callable(env, arguments, api_key),
#          'requires': tuple of model names that must be installed,
#          'access': 'read' | 'create' | 'action'}
TOOLS = {}

# Access levels of tools, and the key write mode each one needs.
ACCESS_READ = 'read'
ACCESS_CREATE = 'create'
ACCESS_ACTION = 'action'
WRITE_MODES_FOR_ACCESS = {
    ACCESS_READ: ('read', 'create', 'full'),
    ACCESS_CREATE: ('create', 'full'),
    ACCESS_ACTION: ('full',),
}


def register(name, description, input_schema, requires=(), access=ACCESS_READ):
    """Decorator that registers a tool handler together with its MCP definition.
    Every handler has the signature handler(env, arguments, api_key)."""
    if access not in WRITE_MODES_FOR_ACCESS:
        raise ValueError(f"Unknown access level '{access}' for tool '{name}'")

    def decorator(func):
        if name in TOOLS:
            raise ValueError(f"MCP tool '{name}' is registered twice")
        annotations = {'readOnlyHint': access == ACCESS_READ}
        if access != ACCESS_READ:
            annotations['destructiveHint'] = access == ACCESS_ACTION
            annotations['idempotentHint'] = False
        TOOLS[name] = {
            'definition': {
                'name': name,
                'description': description,
                'inputSchema': input_schema,
                'annotations': annotations,
            },
            'handler': func,
            'requires': tuple(requires),
            'access': access,
        }
        return func
    return decorator


def tool_access(name):
    tool = TOOLS.get(name)
    return tool['access'] if tool else ACCESS_READ


def access_allowed(api_key, access):
    """Does this key's write mode allow tools of this access level?"""
    return (api_key.write_mode or 'read') in WRITE_MODES_FOR_ACCESS.get(access, ())


def tool_installed(env, name):
    """True when every model the tool needs exists in this database."""
    tool = TOOLS.get(name)
    return bool(tool) and all(model in env.registry for model in tool['requires'])


# ---------------------------------------------------------------------------
# Time-zone helpers
# ---------------------------------------------------------------------------
# Odoo stores datetimes in UTC. Users think in their own time zone, and
# read_group buckets datetimes by the context time zone, so every date the
# model sends is interpreted in the user's time zone and converted here.
def user_tz(env):
    name = env.context.get('tz') or env.user.tz
    if not name:
        return pytz.utc
    try:
        return pytz.timezone(name)
    except pytz.UnknownTimeZoneError:
        return pytz.utc


def user_now(env):
    """Current local time of the user, as a naive datetime."""
    return dt.datetime.now(user_tz(env)).replace(tzinfo=None)


def user_today(env):
    return user_now(env).date()


def local_to_utc(env, value):
    """Naive local datetime -> naive UTC datetime."""
    return user_tz(env).localize(value).astimezone(pytz.utc).replace(tzinfo=None)


def utc_to_local(env, value):
    """Naive UTC datetime (or 'YYYY-MM-DD HH:MM:SS' string) -> naive local datetime."""
    if isinstance(value, str):
        value = fields.Datetime.to_datetime(value)
    return pytz.utc.localize(value).astimezone(user_tz(env)).replace(tzinfo=None)


def day_start_utc(env, day):
    """UTC timestamp string of local midnight at the start of `day`."""
    return fields.Datetime.to_string(local_to_utc(env, dt.datetime.combine(day, dt.time.min)))


def datetime_day_range(env, field_name, date_from, date_to):
    """Domain covering whole local days date_from..date_to (inclusive) on a
    datetime field. Accepts date objects or 'YYYY-MM-DD' strings."""
    if isinstance(date_from, str):
        date_from = fields.Date.to_date(date_from)
    if isinstance(date_to, str):
        date_to = fields.Date.to_date(date_to)
    return [
        (field_name, '>=', day_start_utc(env, date_from)),
        (field_name, '<', day_start_utc(env, date_to + dt.timedelta(days=1))),
    ]


def format_local_datetime(env, value):
    """UTC datetime (or string) -> 'YYYY-MM-DD HH:MM' in the user's time zone."""
    if not value:
        return '-'
    if isinstance(value, str):
        value = fields.Datetime.to_datetime(value)
    return utc_to_local(env, value).strftime('%Y-%m-%d %H:%M')


# ---------------------------------------------------------------------------
# Odoo version compatibility (17 -> 18/19 renamed the access-check API)
# ---------------------------------------------------------------------------
def can_read(model):
    """Model-level read permission for the current user."""
    if hasattr(type(model), 'has_access'):  # Odoo 18+
        return model.has_access('read')
    return model.check_access_rights('read', raise_exception=False)


def readable_field_names(model):
    """Names of the fields the current user may read on `model`."""
    if hasattr(type(model), '_has_field_access'):  # Odoo 18+
        return [name for name, field in model._fields.items()
                if model._has_field_access(field, 'read')]
    try:
        return model.check_field_access_rights('read', None)
    except AccessError:
        return []


def field_readable(model, field_name):
    return field_name in readable_field_names(model)


def can_access(model, operation):
    """Model-level permission ('read', 'write', 'create', 'unlink')."""
    if hasattr(type(model), 'has_access'):  # Odoo 18+
        return model.has_access(operation)
    return model.check_access_rights(operation, raise_exception=False)


def field_writable(model, field_name):
    field = model._fields.get(field_name)
    if field is None:
        return False
    if hasattr(type(model), '_has_field_access'):  # Odoo 18+
        return model._has_field_access(field, 'write')
    try:
        model.check_field_access_rights('write', [field_name])
        return True
    except AccessError:
        return False


def company_currency_name(env):
    return env.company.currency_id.name or ''
