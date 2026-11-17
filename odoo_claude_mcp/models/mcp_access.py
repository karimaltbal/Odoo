# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..controllers.mcp_common import HARD_MAX_ROWS

_logger = logging.getLogger(__name__)

# Default write permissions proposed for new rules: (allow_create, allow_actions).
# Nothing is writable until the API key's Write Access is also switched on.
SEED_WRITE = {
    'sale.order': (True, True),
    'purchase.order': (True, True),
    'account.move': (True, True),
    'crm.lead': (True, True),
    'stock.picking': (True, True),
    'mrp.production': (True, True),
    'res.partner': (True, False),
    'project.task': (True, False),
}

# Remembers which default models were already proposed once, so a rule the
# administrator deletes is not silently re-created on the next upgrade.
SEEDED_PARAM = 'odoo_claude_mcp.seeded_models'

# model -> fields hidden from Claude by default (costs, margins, personal data).
# Field names that do not exist in the installed version are simply ignored.
SEED_MODELS = {
    # Sales / purchase
    'sale.order': ['margin'],
    'sale.order.line': ['purchase_price', 'margin', 'margin_percent'],
    'purchase.order': [],
    'purchase.order.line': [],
    # Products
    'product.template': ['standard_price'],
    'product.product': ['standard_price'],
    'product.category': [],
    'res.partner': ['comment'],
    # Inventory
    'stock.quant': ['value'],
    'stock.move': ['price_unit', 'value'],
    'stock.move.line': [],
    'stock.picking': [],
    'stock.warehouse': [],
    'stock.location': [],
    # Accounting
    'account.move': [],
    'account.move.line': [],
    'account.payment': [],
    'account.journal': [],
    'account.account': [],
    # Supporting models: needed so filters like user_id.name, stage_id.name,
    # team_id.name, country_id.name, company_id.name can be resolved.
    'res.users': ['login', 'signature', 'email'],
    'res.company': [],
    'res.currency': [],
    'res.country': [],
    'res.country.state': [],
    'res.partner.category': [],
    'uom.uom': [],
    'product.pricelist': [],
    'product.supplierinfo': [],
    'account.tax': [],
    'account.payment.term': [],
    'account.analytic.account': [],
    'account.analytic.line': [],
    'stock.lot': [],
    'stock.picking.type': [],
    'stock.warehouse.orderpoint': [],
    'sale.order.template': [],
    # Analysis / reporting views (fast for totals and trends)
    'sale.report': ['margin'],
    'purchase.report': [],
    'account.invoice.report': ['inventory_value'],
    # Rental (Enterprise sale_renting)
    'sale.rental.schedule': [],
    'product.pricing': [],
    'sale.temporal.recurrence': [],
    # CRM / projects / manufacturing / POS
    'crm.lead': [],
    'crm.stage': [],
    'crm.team': [],
    'crm.team.member': [],
    'crm.lost.reason': [],
    'crm.tag': [],
    'utm.source': [],
    'utm.medium': [],
    'utm.campaign': [],
    'project.project': [],
    'project.task': [],
    'mrp.production': [],
    'mrp.bom': [],
    'pos.order': [],
    'pos.order.line': [],
    # HR (personal and payroll data blocked by default)
    'hr.employee': [
        'private_street', 'private_street2', 'private_city', 'private_state_id',
        'private_zip', 'private_country_id', 'private_phone', 'private_email',
        'identification_id', 'passport_id', 'bank_account_id', 'ssnid', 'permit_no',
        'visa_no', 'visa_expire', 'work_permit_expiration_date', 'emergency_contact',
        'emergency_phone', 'birthday', 'place_of_birth', 'country_of_birth',
        'marital', 'spouse_complete_name', 'spouse_birthdate', 'children',
        'km_home_work', 'pin', 'barcode', 'notes'],
    'hr.contract': ['wage', 'hourly_wage', 'contract_wage'],
    'hr.department': [],
    'hr.leave': [],
    'hr.attendance': [],
}


class McpTool(models.Model):
    _name = 'mcp.tool'
    _description = 'Claude MCP Tool'
    _order = 'name'

    name = fields.Char(required=True, index=True, readonly=True)
    description = fields.Char(readonly=True)
    access = fields.Selection(
        [('read', 'Read'), ('create', 'Create'), ('action', 'Business Action')],
        string='Access', default='read', readonly=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'Tool name must be unique.'),
    ]

    @api.model
    def _sync_from_code(self):
        from ..controllers import mcp_tools  # lazy import: avoids load-order issues
        from ..controllers.mcp_common import tool_access
        definitions = {t['name']: t for t in mcp_tools.all_tool_definitions()}
        existing = {r.name: r for r in self.with_context(active_test=False).search([])}
        for name, definition in definitions.items():
            vals = {
                'description': (definition.get('description') or '').split('\n')[0][:250],
                'access': tool_access(name),
            }
            if name in existing:
                existing[name].write(dict(vals, active=True))
            else:
                self.create(dict(vals, name=name))
        for name, record in existing.items():
            if name not in definitions:
                record.active = False


class McpAllowedModel(models.Model):
    _name = 'mcp.allowed.model'
    _description = 'Claude MCP Queryable Model'
    _rec_name = 'model_name'
    _order = 'model_name'

    model_id = fields.Many2one(
        'ir.model', string='Model', required=True, ondelete='cascade',
        domain=[('transient', '=', False)])
    model_name = fields.Char(related='model_id.model', store=True, string='Technical Name')
    blocked_field_ids = fields.Many2many(
        'ir.model.fields', 'mcp_allowed_model_blocked_field_rel',
        'allowed_id', 'field_id', string='Blocked Fields',
        help="These fields can never be read, filtered, grouped or measured "
             "through Claude, even if the user has access in Odoo "
             "(e.g. cost prices, margins, personal notes).")
    allow_create = fields.Boolean(
        string='Allow Create',
        help="Claude may create DRAFT records of this model (create_record, create_records_bulk) "
             "when the API key's Write Access allows it. Lines of a document (e.g. order lines) "
             "only need their model to be in this list, not this flag.")
    allow_actions = fields.Boolean(
        string='Allow Business Actions',
        help="Claude may run this model's business actions (confirm, post, validate, won/lost...) "
             "with execute_action when the API key's Write Access is 'Full'.")
    max_rows = fields.Integer(
        string='Max Rows per Query', default=100,
        help=f"Upper bound of rows returned per query (hard cap {HARD_MAX_ROWS}).")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('model_uniq', 'unique(model_id)', 'This model is already in the whitelist.'),
    ]

    @api.constrains('max_rows')
    def _check_max_rows(self):
        for rec in self:
            if not 1 <= rec.max_rows <= HARD_MAX_ROWS:
                raise ValidationError(f"Max rows must be between 1 and {HARD_MAX_ROWS}.")

    @api.constrains('blocked_field_ids', 'model_id')
    def _check_blocked_fields_belong_to_model(self):
        for rec in self:
            if any(f.model_id != rec.model_id for f in rec.blocked_field_ids):
                raise ValidationError("Blocked fields must belong to the selected model.")

    @api.model
    def _seed_defaults(self, force=False):
        """Add the default rule of every SEED_MODELS entry whose app is installed.

        - Never touches a model that already has a rule (active or archived), so
          admin choices survive upgrades.
        - Each default is proposed only once: a rule the admin deleted is not
          re-created, unless force=True (the 'Add Default Models' menu).
        Returns the number of rules created."""
        IrModel = self.env['ir.model']
        IrFields = self.env['ir.model.fields']
        Param = self.env['ir.config_parameter'].sudo()
        seeded = set(filter(None, (Param.get_param(SEEDED_PARAM) or '').split(',')))
        existing = set(self.with_context(active_test=False).search([]).mapped('model_name'))
        created = 0
        for model_name, blocked in SEED_MODELS.items():
            if (model_name in seeded and not force) or model_name in existing:
                seeded.add(model_name)
                continue
            ir_model = IrModel.search([('model', '=', model_name)], limit=1)
            if not ir_model:
                continue  # app not installed (yet)
            blocked_fields = IrFields.search([
                ('model_id', '=', ir_model.id), ('name', 'in', blocked)])
            allow_create, allow_actions = SEED_WRITE.get(model_name, (False, False))
            self.create({
                'model_id': ir_model.id,
                'blocked_field_ids': [(6, 0, blocked_fields.ids)],
                'allow_create': allow_create,
                'allow_actions': allow_actions,
            })
            seeded.add(model_name)
            created += 1
        Param.set_param(SEEDED_PARAM, ','.join(sorted(seeded)))
        return created

    def _register_hook(self):
        """Runs after every registry load. When modules were installed or
        upgraded in this load (e.g. Sales installed after this module), add
        the default rules of the newly available models."""
        super()._register_hook()
        if not self.pool.updated_modules:
            return
        try:
            with self.env.cr.savepoint():
                created = self.sudo()._seed_defaults()
            if created:
                _logger.info("Claude MCP: added %s default Queryable Model(s)", created)
        except Exception:  # noqa: BLE001 - must never block the server start
            _logger.exception("Claude MCP: could not add default Queryable Models")

    @api.model
    def action_seed_defaults(self):
        """Server action: add the default models of apps installed after this
        module (e.g. Purchase or Inventory installed later)."""
        added = self._seed_defaults(force=True)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Queryable Models',
                'message': f"{added} default model(s) added." if added else "All default models are already listed.",
                'type': 'success',
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }
