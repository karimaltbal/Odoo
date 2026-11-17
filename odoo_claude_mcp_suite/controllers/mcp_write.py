# -*- coding: utf-8 -*-
"""Write tools: create draft records (one or in bulk) and run curated business
actions (confirm, validate, mark won, ...).

Safety layers, all of which must allow the operation:
1. The API key's Write Access mode (read / create / full).
2. The Queryable Model rule: 'Allow Create' / 'Allow Business Actions'.
3. The Run As user's Odoo access rights and record rules (never sudo).
4. Field checks: blocked, read-only, computed and workflow fields ('state')
   can never be set, so records always start in draft.
5. Actions come from a fixed catalogue (ACTIONS); no arbitrary method calls.
Every created record and every executed action is noted in the record's chatter.
"""
import datetime as dt
import json

from markupsafe import Markup

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError

from .mcp_common import (
    ACCESS_ACTION, ACCESS_CREATE, ToolError, can_access, field_writable,
    local_to_utc, register, text_result,
)
from .mcp_generic import (
    DATE_ONLY_RE, DATETIME_RE, _allowed_rules, _as_list, _cap_output,
    _resolve_token, _rule_for,
)
from .mcp_urls import record_url

MAX_BULK_RECORDS = 200
MAX_ACTION_RECORDS = 200
MAX_LINES_PER_RECORD = 200
MAX_VALUES_PER_RECORD = 60
MAX_TEXT_LENGTH = 20000

# Never settable from Claude, whatever the model.
# Workflow, sequence-protection and audit fields: setting them would bypass
# the draft-first rule or Odoo's controls.
FORBIDDEN_FIELDS = {
    'id', 'state', 'create_uid', 'create_date', 'write_uid', 'write_date',
    'display_name', '__last_update', 'message_ids', 'message_follower_ids',
    'activity_ids', 'website_message_ids', 'access_token', 'access_url', 'company_ids',
    'payment_state', 'posted_before', 'inalterable_hash', 'secure_sequence_number',
    'locked', 'is_move_sent', 'invoice_status', 'receipt_status', 'delivery_status',
    'date_done', 'date_closed', 'date_approve', 'won_status',
}
WRITABLE_TYPES = {
    'char', 'text', 'html', 'selection', 'boolean', 'integer', 'float', 'monetary',
    'date', 'datetime', 'many2one', 'many2many', 'one2many',
}
EXPECTED_ERRORS = (UserError, ValidationError, AccessError, ValueError, ToolError)


# ---------------------------------------------------------------------------
# Value conversion (Claude JSON -> Odoo create values)
# ---------------------------------------------------------------------------
class _Converter:
    """Validates and converts values for create(), with a lookup memo shared
    across the records of one call (bulk imports repeat the same names)."""

    def __init__(self, env, rules):
        self.env = env
        self.rules = rules
        self.memo = {}

    def _writable_fields(self, model_name, rule):
        model = self.env[model_name]
        blocked = set(rule.blocked_field_ids.mapped('name'))
        return model, blocked

    def _check_field(self, model, blocked, fname, line_inverse=None):
        field = model._fields.get(fname)
        if field is None:
            raise ToolError(f"Unknown field '{fname}' on '{model._name}'. Use describe_model to see the fields.")
        if fname in FORBIDDEN_FIELDS or fname in blocked or fname == line_inverse:
            raise ToolError(f"Field '{fname}' on '{model._name}' cannot be set through Claude.")
        if field.type not in WRITABLE_TYPES:
            raise ToolError(f"Field '{fname}' ({field.type}) on '{model._name}' cannot be set through Claude.")
        # Computed / related read-only fields are managed by Odoo. Plain read-only
        # fields (e.g. account.move.move_type) are "set at creation only" and allowed.
        if field.readonly and (field.compute or field.related):
            raise ToolError(f"Field '{fname}' on '{model._name}' is computed by Odoo and cannot be set.")
        if not field_writable(model, fname):
            raise ToolError(f"Your Odoo user may not set '{fname}' on '{model._name}'.")
        return field

    def convert(self, model_name, values, allow_lines=True, line_inverse=None):
        if not isinstance(values, dict) or not values:
            raise ToolError("Each record must be a non-empty object of {field: value}")
        if len(values) > MAX_VALUES_PER_RECORD:
            raise ToolError(f"At most {MAX_VALUES_PER_RECORD} fields per record")
        rule = self.rules.get(model_name)
        if rule is None:
            raise ToolError(f"Model '{model_name}' is not available for this key.")
        model, blocked = self._writable_fields(model_name, rule)
        result = {}
        for fname, value in values.items():
            field = self._check_field(model, blocked, fname, line_inverse)
            result[fname] = self._convert_value(model, field, value, allow_lines)
        return result

    # -- per type ------------------------------------------------------------
    def _convert_value(self, model, field, value, allow_lines):
        label = f"'{field.name}' on '{model._name}'"
        if value is None:
            value = False
        if field.type in ('char', 'text', 'html'):
            if value is False:
                return False
            if not isinstance(value, (str, int, float)):
                raise ToolError(f"{label} needs text")
            value = str(value)
            if len(value) > MAX_TEXT_LENGTH:
                raise ToolError(f"{label} is too long (max {MAX_TEXT_LENGTH} characters)")
            return value
        if field.type == 'boolean':
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ('true', 'yes', '1'):
                    return True
                if lowered in ('false', 'no', '0', ''):
                    return False
                raise ToolError(f"{label} needs true or false")
            return bool(value)
        if field.type in ('integer', 'float', 'monetary'):
            if value is False:
                return 0
            try:
                number = float(str(value).replace(',', '')) if isinstance(value, str) else float(value)
            except (TypeError, ValueError):
                raise ToolError(f"{label} needs a number, got {value!r}")
            return int(number) if field.type == 'integer' else number
        if field.type == 'selection':
            if value is False:
                return False
            options = field._description_selection(self.env)
            for key, text in options:
                if str(value) == key:
                    return key
            for key, text in options:
                if str(value).strip().lower() in (str(key).lower(), str(text).lower()):
                    return key
            raise ToolError(f"{label} must be one of: " + ", ".join(f"{k} ({t})" for k, t in options[:20]))
        if field.type == 'date':
            if value is False:
                return False
            value = _resolve_token(self.env, value)
            if not isinstance(value, str) or not DATE_ONLY_RE.match(value.strip()[:10]):
                raise ToolError(f"{label} needs a date YYYY-MM-DD")
            try:
                return dt.date.fromisoformat(value.strip()[:10]).isoformat()
            except ValueError:
                raise ToolError(f"{label}: invalid date {value!r}")
        if field.type == 'datetime':
            if value is False:
                return False
            value = _resolve_token(self.env, value)
            if not isinstance(value, str):
                raise ToolError(f"{label} needs a datetime 'YYYY-MM-DD HH:MM' (your local time)")
            value = value.strip().replace('T', ' ')
            try:
                if DATE_ONLY_RE.match(value):
                    local = dt.datetime.combine(dt.date.fromisoformat(value), dt.time(0, 0))
                elif DATETIME_RE.match(value):
                    local = dt.datetime.fromisoformat(value)
                else:
                    raise ValueError
            except ValueError:
                raise ToolError(f"{label} needs a datetime 'YYYY-MM-DD HH:MM' (your local time)")
            return fields.Datetime.to_string(local_to_utc(self.env, local))
        if field.type == 'many2one':
            if value is False:
                return False
            return self._resolve_record(field.comodel_name, value, label)
        if field.type == 'many2many':
            items = value if isinstance(value, list) else ([] if value is False else [value])
            if len(items) > 100:
                raise ToolError(f"{label}: at most 100 values")
            return [Command.set([self._resolve_record(field.comodel_name, item, label) for item in items])]
        if field.type == 'one2many':
            if not allow_lines:
                raise ToolError(f"{label}: nested lines inside lines are not supported")
            if field.comodel_name not in self.rules:
                raise ToolError(f"{label}: lines model '{field.comodel_name}' is not available for this key.")
            lines = value if isinstance(value, list) else []
            if not lines and value not in (False, []):
                raise ToolError(f"{label} needs a list of line objects")
            if len(lines) > MAX_LINES_PER_RECORD:
                raise ToolError(f"{label}: at most {MAX_LINES_PER_RECORD} lines")
            commands = []
            for index, line in enumerate(lines, 1):
                try:
                    commands.append(Command.create(self.convert(
                        field.comodel_name, line, allow_lines=False, line_inverse=field.inverse_name)))
                except ToolError as exc:
                    raise ToolError(f"{label}, line {index}: {exc}")
            return commands
        raise ToolError(f"{label} cannot be set through Claude.")

    def _resolve_record(self, comodel, value, label):
        """id, {'id': n} or a name -> record id (exact match first, then partial)."""
        if isinstance(value, dict):
            value = value.get('id', value.get('name'))
        key = (comodel, value if not isinstance(value, float) else int(value))
        if key in self.memo:
            return self.memo[key]
        Model = self.env[comodel]
        if isinstance(value, bool) or value is None:
            raise ToolError(f"{label} needs an id or a name")
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            record_id = int(value)
            if not Model.search([('id', '=', record_id)], limit=1):
                raise ToolError(f"{label}: {comodel} id {record_id} does not exist or is not accessible.")
            self.memo[key] = record_id
            return record_id
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"{label} needs an id or a name")
        if comodel not in self.rules:
            raise ToolError(f"{label}: '{comodel}' is not available for this key, so it can only be "
                            "set by numeric id (ask an administrator to add it to Queryable Models).")
        name = value.strip()
        matches = Model.name_search(name, operator='=ilike', limit=5)
        if not matches:
            matches = Model.name_search(name, operator='ilike', limit=6)
        if not matches:
            raise ToolError(f"{label}: no {comodel} named '{name}'.")
        if len(matches) > 1:
            listing = "; ".join(f"{text} [{rid}]" for rid, text in matches[:6])
            raise ToolError(f"{label}: '{name}' matches several {comodel} records ({listing}). Use the id.")
        self.memo[key] = matches[0][0]
        return matches[0][0]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _key_label(api_key):
    return f"{api_key.name} (MCP key #{api_key.id})"


def _note(record, text):
    """Log an internal note in the record's chatter when the model has one.
    _message_log does not notify followers and does not need the user to have
    an email address (message_post would fail for a Run As user without one)."""
    if hasattr(record, '_message_log'):
        record._message_log(body=text)
    elif hasattr(record, 'message_post'):
        record.message_post(body=text, subtype_xmlid='mail.mt_note')


def _draft_check(record):
    """Records must start in draft when the model has a draft state."""
    field = record._fields.get('state')
    if field is None or field.type != 'selection':
        return
    keys = [key for key, _label in field._description_selection(record.env)]
    if 'draft' in keys and record.state != 'draft':
        raise ToolError(
            f"Odoo created this {record._name} in state '{record.state}' instead of draft; "
            "it was not kept. Create it manually in Odoo.")


def _summary(record):
    parts = [f"{record.display_name} [id {record.id}]"]
    for fname in ('state', 'amount_total', 'expected_revenue'):
        if fname in record._fields:
            value = record[fname]
            if fname == 'state':
                parts.append(f"state: {value}")
            elif value:
                currency = record.currency_id.name if 'currency_id' in record._fields and record.currency_id else ''
                parts.append(f"{fname}: {value:,.2f} {currency}".rstrip())
    return " | ".join(parts)


def _error_text(exc):
    if isinstance(exc, EXPECTED_ERRORS):
        message = exc.args[0] if exc.args else str(exc)
    else:
        message = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    return str(message).strip().replace("\n", " ")[:500]


def _check_create_allowed(env, rules, model_name):
    rule = _rule_for(env, rules, model_name)
    if not rule.allow_create:
        raise ToolError(
            f"Creating '{model_name}' records is not enabled. An administrator can tick "
            "'Allow Create' on this model under Claude Connector > Queryable Models.")
    if not can_access(env[model_name], 'create'):
        raise ToolError(f"Your Odoo user has no permission to create '{model_name}' records.")
    return rule


def _create_one(env, converter, model_name, values, api_key):
    vals = converter.convert(model_name, values)
    record = env[model_name].create(vals)
    _draft_check(record)
    _note(record, f"Created as draft by Claude through {_key_label(api_key)}, "
                  f"running as {env.user.name}. Please review before confirming.")
    return record


# ---------------------------------------------------------------------------
# create_record
# ---------------------------------------------------------------------------
@register(
    'create_record',
    "Create ONE record (invoice, quotation, lead, purchase order, contact, task...) as a DRAFT "
    "for review. `values` maps real field names (see describe_model; fields marked 'required' "
    "must be set) to values: many2one/many2many accept an id or an exact name, one2many lines "
    "take a list of objects (e.g. sale.order: {partner_id: 'Azure Interior', order_line: "
    "[{product_id: 'Office Chair', product_uom_qty: 2}]}; customer invoice: account.move with "
    "move_type 'out_invoice' and invoice_line_ids). 'state' can never be set. Only use when the "
    "user asks to create something. Returns the new record with its URL.",
    {
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Technical model name, e.g. 'sale.order'"},
            "values": {"type": "object", "description": "Field values of the new record"},
        },
        "required": ["model", "values"],
    },
    access=ACCESS_CREATE,
)
def tool_create_record(env, arguments, api_key):
    rules = _allowed_rules(api_key)
    model_name = (arguments.get('model') or '').strip()
    _check_create_allowed(env, rules, model_name)
    values = arguments.get('values')
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            raise ToolError("'values' must be a JSON object")
    record = _create_one(env, _Converter(env, rules), model_name, values, api_key)
    return text_result(
        f"Created draft {model_name}: {_summary(record)}\nURL: {record_url(env, record)}")


# ---------------------------------------------------------------------------
# create_records_bulk
# ---------------------------------------------------------------------------
@register(
    'create_records_bulk',
    f"Import up to {MAX_BULK_RECORDS} records of ONE model in a single call, e.g. rows pasted from "
    "a spreadsheet. Each item of `records` has the same format as create_record `values`. Every "
    "record is created as a draft in its own transaction step: the result lists success (id + URL) "
    "or the exact error per row. Set all_or_nothing=true to keep nothing if any row fails. Map "
    "columns to real field names first (describe_model) and confirm the mapping with the user "
    "when it is not obvious.",
    {
        "type": "object",
        "properties": {
            "model": {"type": "string"},
            "records": {"type": "array", "items": {"type": "object"},
                        "description": f"1 to {MAX_BULK_RECORDS} value objects"},
            "all_or_nothing": {"type": "boolean",
                               "description": "Default false: keep the rows that succeed."},
        },
        "required": ["model", "records"],
    },
    access=ACCESS_CREATE,
)
def tool_create_records_bulk(env, arguments, api_key):
    rules = _allowed_rules(api_key)
    model_name = (arguments.get('model') or '').strip()
    _check_create_allowed(env, rules, model_name)
    records = _as_list(arguments.get('records'), 'records')
    if not records or len(records) > MAX_BULK_RECORDS:
        raise ToolError(f"'records' needs 1 to {MAX_BULK_RECORDS} items")
    all_or_nothing = bool(arguments.get('all_or_nothing'))

    converter = _Converter(env, rules)
    lines, ok, failed = [], 0, 0
    for index, values in enumerate(records, 1):
        savepoint = env.cr.savepoint()
        try:
            record = _create_one(env, converter, model_name, values, api_key)
            savepoint.close(rollback=False)
            ok += 1
            lines.append(f"#{index} OK — {_summary(record)} | {record_url(env, record)}")
        except Exception as exc:  # noqa: BLE001 - reported per row
            savepoint.close(rollback=True)
            failed += 1
            lines.append(f"#{index} FAILED — {_error_text(exc)}")

    header = f"Bulk create {model_name}: {ok} created, {failed} failed (of {len(records)})."
    if all_or_nothing and failed:
        # Raising rolls back the whole call (see the controller savepoint).
        raise ToolError(_cap_output([
            f"Bulk create {model_name}: {failed} of {len(records)} rows failed, so NOTHING was "
            "created (all_or_nothing). Fix these rows and try again:"]
            + [line for line in lines if 'FAILED' in line]))
    return text_result(_cap_output([header] + lines))


# ---------------------------------------------------------------------------
# execute_action
# ---------------------------------------------------------------------------
def _lost_reason(env, params):
    reason = params.get('lost_reason')
    values = {}
    if reason not in (None, '', False):
        Reason = env['crm.lost.reason']
        if isinstance(reason, int) or (isinstance(reason, str) and reason.isdigit()):
            found = Reason.search([('id', '=', int(reason))], limit=1)
        else:
            found = Reason.search([('name', '=ilike', str(reason))], limit=1) or \
                Reason.search([('name', 'ilike', str(reason))], limit=2)
        if len(found) != 1:
            names = ", ".join(Reason.search([], limit=15).mapped('name'))
            raise ToolError(f"Unknown or ambiguous lost reason '{reason}'. Available: {names}")
        values['lost_reason_id'] = found.id
    return values


def _run_mark_lost(record, params):
    """Same as Odoo's 'Mark Lost' wizard: the feedback goes to the chatter
    (tracked with the stage change) and the reason onto the lead."""
    feedback = str(params.get('lost_feedback') or '').strip()[:2000]
    if feedback and hasattr(record, '_track_set_log_message'):
        record._track_set_log_message(
            Markup('<div style="margin-bottom: 4px;"><p>%s:</p>%s<br /></div>') % ('Lost Comment', feedback))
    return record.action_set_lost(**_lost_reason(record.env, params))


def _run_convert_to_opportunity(record, params):
    if record.type == 'opportunity':
        raise UserError("This record is already an opportunity.")
    return record.convert_opportunity(record.partner_id)


def _run_validate_picking(record, params):
    create_backorder = params.get('create_backorder')
    if create_backorder is None:
        return record.button_validate()
    ctx = {'skip_backorder': True}
    if not create_backorder:
        ctx['picking_ids_not_to_backorder'] = record.ids
    return record.with_context(**ctx).button_validate()


# model -> action name -> spec. 'method' is called on one record; 'run' is a
# function(record, params) for actions needing parameters.
ACTIONS = {
    'sale.order': {
        'confirm': {'method': 'action_confirm', 'help': 'Confirm a quotation into a sales order'},
        'cancel': {'method': 'action_cancel', 'context': {'disable_cancel_warning': True},
                   'help': 'Cancel the order (no cancellation email)'},
        'set_to_quotation': {'method': 'action_draft', 'help': 'Set a cancelled order back to quotation'},
        'lock': {'method': 'action_lock', 'help': 'Lock a confirmed order'},
        'unlock': {'method': 'action_unlock', 'help': 'Unlock a locked order'},
    },
    'purchase.order': {
        'confirm': {'method': 'button_confirm', 'help': 'Confirm an RFQ into a purchase order'},
        'approve': {'method': 'button_approve', 'help': 'Approve an order waiting for approval'},
        'cancel': {'method': 'button_cancel', 'help': 'Cancel the order'},
        'set_to_draft': {'method': 'button_draft', 'help': 'Set a cancelled order back to RFQ'},
    },
    'account.move': {
        'confirm': {'method': 'action_post', 'help': 'Post (confirm) a draft invoice, bill or entry'},
        'reset_to_draft': {'method': 'button_draft', 'help': 'Reset a posted or cancelled move to draft'},
        'cancel': {'method': 'button_cancel', 'help': 'Cancel a draft move'},
    },
    'crm.lead': {
        'mark_won': {'method': 'action_set_won', 'help': 'Mark the opportunity as won'},
        'mark_lost': {'run': _run_mark_lost, 'params': ['lost_reason', 'lost_feedback'],
                      'help': 'Mark as lost; params: lost_reason (name or id), lost_feedback (text)'},
        'convert_to_opportunity': {'run': _run_convert_to_opportunity,
                                   'help': 'Convert a lead into an opportunity'},
        'restore': {'method': 'action_unarchive', 'help': 'Restore a lost (archived) lead'},
    },
    'stock.picking': {
        'mark_as_todo': {'method': 'action_confirm', 'help': 'Confirm a draft transfer'},
        'check_availability': {'method': 'action_assign', 'changes_state': False,
                               'help': 'Reserve the products'},
        'validate': {'run': _run_validate_picking, 'params': ['create_backorder'],
                     'help': 'Validate the transfer; params: create_backorder (true/false) for partial quantities'},
        'cancel': {'method': 'action_cancel', 'help': 'Cancel the transfer'},
    },
    'mrp.production': {
        'confirm': {'method': 'action_confirm', 'help': 'Confirm a draft manufacturing order'},
        'check_availability': {'method': 'action_assign', 'changes_state': False,
                               'help': 'Reserve the components'},
        'mark_done': {'method': 'button_mark_done', 'help': 'Produce and mark as done'},
        'cancel': {'method': 'action_cancel', 'help': 'Cancel the manufacturing order'},
    },
}


def actions_help(model_name=None):
    models = [model_name] if model_name else sorted(ACTIONS)
    lines = []
    for name in models:
        for action, spec in ACTIONS.get(name, {}).items():
            lines.append(f"{name}: {action} — {spec['help']}")
    return lines


def _wizard_hint(result):
    model = result.get('res_model') or ''
    if model == 'stock.backorder.confirmation':
        return ("quantities are partial: call again with params.create_backorder = true "
                "(keep the rest as a backorder) or false (no backorder)")
    return f"Odoo asks for a confirmation in '{result.get('name') or model}'; open the record to continue"


def _is_wizard(result):
    return isinstance(result, dict) and result.get('type') == 'ir.actions.act_window' \
        and result.get('target') == 'new'


def _state(record):
    if 'state' in record._fields:
        locked = ' (locked)' if 'locked' in record._fields and record.locked else ''
        return f"{record.state}{locked}"
    if record._name == 'crm.lead':
        return f"{record.type} / {record.stage_id.name or '-'}{'' if record.active else ' (lost)'}"
    return '-'


@register(
    'execute_action',
    "Run a business action on up to 200 records of one model, e.g. confirm quotations, post "
    "invoices, validate deliveries, mark opportunities won/lost. Each record is processed and "
    "reported separately (success, error, or 'needs a decision in Odoo' with its URL), and each "
    "success is noted in the record's chatter. Only run this on the user's explicit request "
    "for these specific records. Available actions:\n" + "\n".join(actions_help()),
    {
        "type": "object",
        "properties": {
            "model": {"type": "string", "enum": sorted(ACTIONS)},
            "action": {"type": "string", "description": "Action name from the list, e.g. 'confirm'"},
            "ids": {"type": "array", "items": {"type": "integer"},
                    "description": f"Record ids, 1 to {MAX_ACTION_RECORDS}"},
            "params": {"type": "object", "description": "Optional action parameters (see the list)"},
        },
        "required": ["model", "action", "ids"],
    },
    access=ACCESS_ACTION,
)
def tool_execute_action(env, arguments, api_key):
    rules = _allowed_rules(api_key)
    model_name = (arguments.get('model') or '').strip()
    action_name = (arguments.get('action') or '').strip()
    catalogue = ACTIONS.get(model_name)
    if not catalogue:
        raise ToolError(f"No business actions are defined for '{model_name}'. "
                        f"Models with actions: {', '.join(sorted(ACTIONS))}")
    spec = catalogue.get(action_name)
    if not spec:
        raise ToolError(f"Unknown action '{action_name}' for {model_name}. Available:\n"
                        + "\n".join(actions_help(model_name)))
    rule = _rule_for(env, rules, model_name)
    if not rule.allow_actions:
        raise ToolError(
            f"Business actions on '{model_name}' are not enabled. An administrator can tick "
            "'Allow Business Actions' on this model under Claude Connector > Queryable Models.")
    if not can_access(env[model_name], 'write'):
        raise ToolError(f"Your Odoo user has no permission to modify '{model_name}' records.")

    ids = []
    for value in _as_list(arguments.get('ids'), 'ids'):
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            raise ToolError("'ids' must be a list of integers")
    ids = list(dict.fromkeys(ids))
    if not ids or len(ids) > MAX_ACTION_RECORDS:
        raise ToolError(f"'ids' needs 1 to {MAX_ACTION_RECORDS} record ids")
    params = arguments.get('params') or {}
    if not isinstance(params, dict):
        raise ToolError("'params' must be an object")
    unknown = set(params) - set(spec.get('params', []))
    if unknown:
        raise ToolError(f"Unknown params for {action_name}: {', '.join(sorted(unknown))}")

    records = env[model_name].with_context(active_test=False).search([('id', 'in', ids)])
    found = set(records.ids)
    lines, counts = [], {'done': 0, 'no change': 0, 'failed': 0, 'needs decision': 0, 'not found': 0}
    for record_id in ids:
        if record_id not in found:
            counts['not found'] += 1
            lines.append(f"[{record_id}] NOT FOUND or not accessible")
            continue
        record = records.browse(record_id)
        name = record.display_name
        before = _state(record)
        savepoint = env.cr.savepoint()
        try:
            target = record.with_context(**spec.get('context', {}))
            if 'run' in spec:
                result = spec['run'](target, params)
            else:
                result = getattr(target, spec['method'])()
            if _is_wizard(result):
                savepoint.close(rollback=True)
                counts['needs decision'] += 1
                lines.append(f"{name} [{record_id}] NEEDS A DECISION IN ODOO "
                             f"({_wizard_hint(result)}); nothing was changed | {record_url(env, record)}")
                continue
            record.invalidate_recordset()
            after = _state(record)
            if after == before and spec.get('changes_state', True):
                savepoint.close(rollback=True)
                counts['no change'] += 1
                lines.append(f"{name} [{record_id}] NO CHANGE — still {after} (the action does not apply "
                             f"in this state) | {record_url(env, record)}")
                continue
            _note(record, f"Action '{action_name}' executed by Claude through {_key_label(api_key)}, "
                          f"running as {env.user.name}.")
            savepoint.close(rollback=False)
            counts['done'] += 1
            lines.append(f"{name} [{record_id}] DONE — {before} -> {after} | {record_url(env, record)}")
        except Exception as exc:  # noqa: BLE001 - reported per record
            savepoint.close(rollback=True)
            counts['failed'] += 1
            lines.append(f"{name} [{record_id}] FAILED — {_error_text(exc)} | {record_url(env, record)}")

    header = (f"{model_name} / {action_name}: " +
              ", ".join(f"{count} {label}" for label, count in counts.items() if count))
    return text_result(_cap_output([header] + lines))
