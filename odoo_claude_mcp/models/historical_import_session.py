import base64
import hashlib
import io
import json
import zipfile
from openpyxl import load_workbook
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

MAX_SIZE = 10 * 1024 * 1024
MAX_UNCOMPRESSED = 80 * 1024 * 1024
MAX_ROWS = 100000
EXPECTED = ('Developer Units', 'Units Customers', 'Units Sales', 'Check Wallets', 'Installments', 'Payments', 'Reconciliations')

class HistoricalMCPSession(models.Model):
    _name = 'mcp.historical.import.session'
    _description = 'Claude Historical Import Session'
    _order = 'id desc'

    name = fields.Char(required=True)
    import_type = fields.Selection([('contracted','Contracted'),('reserved','Reserved')], required=True, default='contracted')
    state = fields.Selection([('uploaded','Uploaded'),('validated','Validated'),('approved','Approved'),('done','Done'),('failed','Failed')], default='uploaded', required=True)
    file = fields.Binary(attachment=True, required=True)
    filename = fields.Char()
    file_hash = fields.Char(readonly=True)
    validation_json = fields.Text(readonly=True)
    approved_hash = fields.Char(readonly=True)
    approved_by = fields.Many2one('res.users', readonly=True)
    approved_at = fields.Datetime(readonly=True)
    execution_result = fields.Text(readonly=True)
    company_id = fields.Many2one('res.company', default=lambda self:self.env.company, required=True)
    owner_id = fields.Many2one('res.users', default=lambda self:self.env.user, required=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('file'):
                try:
                    raw = base64.b64decode(vals['file'], validate=True)
                except (ValueError, TypeError) as exc:
                    raise UserError('Invalid XLSX file encoding') from exc
                if len(raw) > MAX_SIZE:
                    raise UserError('Workbook exceeds 10MB')
                if not zipfile.is_zipfile(io.BytesIO(raw)):
                    raise UserError('The uploaded file is not a valid XLSX container')
                vals['file_hash'] = hashlib.sha256(raw).hexdigest()
            filename = vals.get('filename') or vals.get('name') or ''
            if filename and not filename.lower().endswith('.xlsx'):
                raise UserError('Only XLSX workbooks are supported')
        return super().create(vals_list)

    def _raw(self):
        self.ensure_one()
        data = base64.b64decode(self.file)
        if len(data) > MAX_SIZE: raise UserError('Workbook exceeds 10MB')
        return data

    def _open(self):
        data = self._raw()
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise UserError('Not a valid XLSX ZIP container')
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_UNCOMPRESSED:
                raise UserError('Workbook expands beyond 80MB')
            if any(i.filename.startswith(('xl/externalLinks/', 'xl/vbaProject')) for i in z.infolist()):
                raise UserError('External links or macros are not supported')
        try:
            return load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        except Exception as exc:
            raise UserError('Could not read XLSX: %s' % exc)

    def action_validate(self):
        self.ensure_one()
        if self.state == 'done': raise UserError('Completed imports cannot be revalidated')
        wb = self._open()
        issues=[]; sheets=[]
        try:
            required = 7 if self.import_type == 'contracted' else 6
            if len(wb.worksheets) < required:
                issues.append({'code':'MISSING_SHEETS','message':'Expected at least %s worksheets' % required})
            for index, sheet in enumerate(wb.worksheets):
                sheets.append({'index':index+1,'name':sheet.title,'rows':sheet.max_row,'columns':sheet.max_column})
                if sheet.max_row > MAX_ROWS: issues.append({'sheet':sheet.title,'code':'TOO_MANY_ROWS'})
                if index < min(required, len(wb.worksheets)) and sheet.max_row < 2:
                    issues.append({'sheet':sheet.title,'code':'EMPTY_REQUIRED_SHEET'})
            if self.import_type == 'contracted':
                for i, expected in enumerate(EXPECTED):
                    if i < len(wb.worksheets) and wb.worksheets[i].title.strip().lower() != expected.lower():
                        issues.append({'sheet':wb.worksheets[i].title,'code':'SHEET_ORDER_WARNING','expected':expected,'severity':'warning'})
            result={'sheets':sheets,'issues':issues,'note':'Structural preflight only; full Odoo business validation runs during approved execution.'}
            blocking=[i for i in issues if i.get('severity')!='warning']
            self.write({'validation_json':json.dumps(result),'state':'validated' if not blocking else 'uploaded','approved_hash':False,'approved_by':False,'approved_at':False})
            return result
        finally:
            wb.close()

    def action_approve(self):
        self.ensure_one()
        if not self.env.user.has_group('odoo_claude_mcp.group_historical_mcp_approve'):
            raise UserError('Approver group is required')
        if self.state != 'validated': raise UserError('Validate the workbook first')
        self.write({'state':'approved','approved_hash':self.file_hash,'approved_by':self.env.user.id,'approved_at':fields.Datetime.now()})

    def _historical_import_company(self):
        """Resolve the company from the developer units in the XLSX.

        This is transport-level company selection only. All financial and
        historical business rules remain in the existing import wizard.
        """
        self.ensure_one()
        workbook = self._open()
        try:
            if not workbook.worksheets:
                raise UserError('Historical workbook has no Developer Units sheet')
            sheet = workbook.worksheets[0]
            helper = self.env['import.export.helper']
            unit_codes = set()
            for row in sheet.iter_rows(min_row=2, values_only=True):
                if not row or row[0] is None or str(row[0]).strip() == '':
                    continue
                # Same code normalization as the existing import_file method.
                raw_code = str(row[0])
                number = helper.get_float_value(raw_code)
                unit_codes.add(str(int(number)) if number != 0.0 else raw_code)
            if not unit_codes:
                raise UserError('No developer unit codes found in the workbook')

            units = self.env['rs.dev.unit'].search([('unit_code', 'in', list(unit_codes))])
            matched_codes = set(units.mapped('unit_code'))
            missing = unit_codes - matched_codes
            if missing:
                raise UserError('Developer unit(s) not found or inaccessible: %s' % ', '.join(sorted(missing)))
            if len(units) != len(unit_codes):
                raise UserError('Ambiguous developer unit codes across companies; import manually or resolve duplicates')
            companies = units.mapped('company_id')
            if len(companies) != 1:
                raise UserError('Workbook contains developer units from multiple companies; single-company MCP execution only')
            company = companies[0]
            if not company or company not in self.env.user.company_ids:
                raise UserError('Execution user does not have access to the developer unit company')
            return company
        finally:
            workbook.close()

    def action_execute(self):
        self.ensure_one()
        if not self.env.user.has_group('odoo_claude_mcp.group_historical_mcp_import'):
            raise UserError('Operator group is required')
        self.env.cr.execute('SELECT id FROM mcp_historical_import_session WHERE id = %s FOR UPDATE NOWAIT', (self.id,))
        if self.state != 'approved' or self.approved_hash != hashlib.sha256(self._raw()).hexdigest():
            raise UserError('Workbook must be validated and approved without modification')
        # The existing wizard remains the only authority for business and financial rules.
        company = self._historical_import_company()
        # Same wizard/method as the manual import, but with the developer
        # unit's company active (important for check wallet company_id).
        try:
            # Roll back the entire original wizard operation if it raises a
            # business validation error. Persist only its message afterward.
            with self.env.cr.savepoint():
                wizard_model = self.env['import.historical.dev.unit.wizard'].with_company(company)
                wizard = wizard_model.create({'file': self.file, 'import_type': self.import_type})
                wizard.import_file()
        except (UserError, ValidationError) as exc:
            message = str(exc) or type(exc).__name__
            self.write({'execution_result': message})
            return {
                'success': False,
                'state': self.state,
                'session_id': self.id,
                'error_type': type(exc).__name__,
                'error_message': message,
            }
        self.write({'state': 'done', 'execution_result': 'Existing historical import completed successfully (company: %s)' % company.display_name})
        return {'success': True, 'state': 'done', 'session_id': self.id, 'company_id': company.id}
