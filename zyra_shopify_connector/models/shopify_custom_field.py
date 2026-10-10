"""Database-local sale order custom field defaults (no executable expressions)."""
import json
import math

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


SUPPORTED_TYPES = ('char', 'text', 'html', 'boolean', 'integer', 'float',
                   'monetary', 'selection', 'date', 'datetime', 'many2one')


SHOPIFY_VALUES = [
    ('name', 'Order number / name'),
    ('order_name_and_tracking', 'Order number + tracking number'),
    ('id', 'Shopify order ID'),
    ('email', 'Order email'),
    ('phone', 'Order phone'),
    ('note', 'Order note'),
    ('tags', 'Order tags'),
    ('gateway', 'Payment method'),
    ('financial_status', 'Payment status'),
    ('fulfillment_status', 'Delivery status'),
    ('currency', 'Currency code'),
    ('total_price', 'Order total'),
    ('subtotal_price', 'Subtotal'),
    ('total_discounts', 'Discount total'),
    ('total_shipping_price', 'Shipping total'),
    ('total_tax', 'Tax total'),
    ('primary_tracking_number', 'Tracking number'),
    ('customer.first_name', 'Customer first name'),
    ('customer.last_name', 'Customer last name'),
    ('customer.email', 'Customer email'),
    ('customer.phone', 'Customer phone'),
    ('shipping_address.city', 'Shipping city'),
    ('shipping_address.zip', 'Shipping postal code'),
    ('shipping_address.country', 'Shipping country'),
    ('billing_address.city', 'Billing city'),
    ('billing_address.zip', 'Billing postal code'),
    ('billing_address.country', 'Billing country'),
    ('taxes_included', 'Prices include tax'),
    ('test', 'Test order'),
]
VALUE_INPUTS = ('text_value', 'boolean_value', 'integer_value', 'number_value',
                'date_value', 'datetime_value', 'choice_id', 'record_value')


class ShopifyCustomField(models.Model):
    _name = 'shopify.custom.field'
    _description = 'Shopify Custom Sale Order Field'
    _order = 'id'

    store_id = fields.Many2one('shopify.store', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='store_id.company_id', store=True)
    active = fields.Boolean(default=True)
    available_field_ids = fields.Many2many('ir.model.fields', compute='_compute_available_fields')
    field_id = fields.Many2one('ir.model.fields', required=True, ondelete='cascade',
                             domain=lambda self: [('id', 'in', self._eligible_fields().ids)])
    field_help = fields.Text(compute='_compute_field_help')
    source = fields.Selection([('fixed', 'Use the same value for every order'), ('payload', 'Copy from Shopify')],
                              default='fixed', required=True)
    value_json = fields.Char(string='Value (JSON)', default='""',
                            help='Text: "Website"; boolean: true or false; number: 12; '
                                 'selection: "technical_key"; relation: existing Odoo record ID; '
                                 'date: "2026-10-09". Use false to clear an optional field.')
    payload_path = fields.Char(help='Dot-separated path in the normalized Shopify order, '
                                   'such as name, note, or customer.email. Missing/null values are skipped.')

    field_type = fields.Selection(related='field_id.ttype')
    relation_model = fields.Char(related='field_id.relation')
    shopify_value = fields.Selection(SHOPIFY_VALUES, string='Shopify information',
                                    compute='_compute_inputs', inverse='_inverse_inputs')
    legacy_source = fields.Boolean(compute='_compute_inputs')
    text_value = fields.Text(string='Value (Text)', compute='_compute_inputs', inverse='_inverse_inputs')
    boolean_value = fields.Boolean(string='Value (Yes/No)', compute='_compute_inputs', inverse='_inverse_inputs')
    integer_value = fields.Integer(string='Value (Whole Number)', compute='_compute_inputs', inverse='_inverse_inputs')
    number_value = fields.Float(string='Value (Number)', digits=(16, 6), compute='_compute_inputs', inverse='_inverse_inputs')
    date_value = fields.Date(string='Value (Date)', compute='_compute_inputs', inverse='_inverse_inputs')
    datetime_value = fields.Datetime(string='Value (Date and Time)', compute='_compute_inputs', inverse='_inverse_inputs')
    choice_id = fields.Many2one('ir.model.fields.selection', string='Value (Choice)',
                               domain="[('field_id', '=', field_id)]",
                               compute='_compute_inputs', inverse='_inverse_inputs')
    record_value = fields.Many2oneReference(string='Value (Record)', model_field='relation_model',
                                          compute='_compute_inputs', inverse='_inverse_inputs')
    value_summary = fields.Char(string='Configured value', compute='_compute_inputs')

    @api.depends('value_json', 'payload_path', 'source', 'field_id')
    def _compute_inputs(self):
        for mapping in self:
            try:
                value = json.loads(mapping.value_json or 'false')
            except (ValueError, TypeError):
                value = False
            kind = mapping.field_type
            mapping.text_value = value if kind in ('char', 'text', 'html') and isinstance(value, str) else False
            mapping.boolean_value = value if kind == 'boolean' else False
            mapping.integer_value = value if kind == 'integer' and type(value) is int else 0
            mapping.number_value = value if kind in ('float', 'monetary') and type(value) in (int, float) else 0
            mapping.date_value = value if kind == 'date' else False
            mapping.datetime_value = value if kind == 'datetime' else False
            mapping.record_value = value if kind == 'many2one' and type(value) is int else 0
            mapping.choice_id = self.env['ir.model.fields.selection'].search([
                ('field_id', '=', mapping.field_id.id), ('value', '=', value),
            ], limit=1) if kind == 'selection' and value is not False else False
            mapping.shopify_value = mapping.payload_path if mapping.payload_path in dict(SHOPIFY_VALUES) else False
            mapping.legacy_source = bool(mapping.payload_path and not mapping.shopify_value)
            if mapping.source == 'payload':
                summary = dict(SHOPIFY_VALUES).get(mapping.payload_path, _('Previously configured Shopify value'))
            elif kind == 'many2one' and mapping.record_value:
                record = self.env[mapping.relation_model].browse(mapping.record_value).exists()
                summary = record.display_name if record else _('Record no longer exists')
            elif kind == 'selection':
                summary = mapping.choice_id.name or _('No value')
            elif kind == 'boolean':
                summary = _('Yes') if value else _('No')
            else:
                summary = str(value) if value is not False else _('No value')
            mapping.value_summary = summary

    def _inverse_inputs(self):
        # create/write translate UI values before constraints execute. JSON and
        # paths remain the storage format, preserving existing configurations.
        pass

    @api.model
    def _prepare_input_values(self, values):
        values = dict(values)
        if 'shopify_value' in values:
            values['payload_path'] = values.pop('shopify_value') or False
        target = self.env['ir.model.fields'].browse(values.get('field_id')) if values.get('field_id') else (self.field_id if self else self.env['ir.model.fields'])
        expected = {'char': 'text_value', 'text': 'text_value', 'html': 'text_value',
                    'boolean': 'boolean_value', 'integer': 'integer_value',
                    'float': 'number_value', 'monetary': 'number_value',
                    'date': 'date_value', 'datetime': 'datetime_value',
                    'selection': 'choice_id', 'many2one': 'record_value'}.get(target.ttype)
        for key in VALUE_INPUTS:
            if key not in values:
                continue
            value = values.pop(key)
            if key != expected:
                continue
            if key == 'choice_id':
                choice = self.env['ir.model.fields.selection'].browse(value).exists() if value else False
                field_id = values.get('field_id') or (self.field_id.id if self else False)
                if choice and choice.field_id.id != field_id:
                    raise ValidationError(_('Choose a value from the selected custom field.'))
                value = choice.value if choice else False
            elif key == 'date_value':
                value = fields.Date.to_string(fields.Date.to_date(value)) if value else False
            elif key == 'datetime_value':
                value = fields.Datetime.to_string(fields.Datetime.to_datetime(value)) if value else False
            elif key == 'record_value':
                value = value or False
            elif key == 'text_value':
                value = value or ''
            values['value_json'] = json.dumps(value)
        return values

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._prepare_input_values(vals) for vals in vals_list])

    def write(self, vals):
        for mapping in self:
            super(ShopifyCustomField, mapping).write(mapping._prepare_input_values(vals))
        return True

    @api.onchange('field_id')
    def _onchange_field_id(self):
        kind = self.field_type
        self.value_json = json.dumps(0 if kind in ('integer', 'float', 'monetary') else
                                     '' if kind in ('char', 'text', 'html') else False)
        self.source = 'fixed'
        self.payload_path = False

    _store_field_unique = models.Constraint(
        'unique(store_id, field_id)',
        'Configure each custom field only once per store.',
    )

    @api.model
    def _eligible_fields(self):
        metadata = self.env['ir.model.fields'].sudo().search([
            ('model', '=', 'sale.order'), ('ttype', 'in', SUPPORTED_TYPES)])
        # XML IDs identify the original owning modules, including fields whose
        # definitions have subsequently been extended by another addon.
        owners = {}
        for data in self.env['ir.model.data'].sudo().search([
                ('model', '=', 'ir.model.fields'), ('res_id', 'in', metadata.ids)]):
            owners.setdefault(data.res_id, set()).add(data.module)
        modules = self.env['ir.module.module'].sudo().search([('state', '=', 'installed')])
        custom_modules = {module.name for module in modules
                          if module.author and not any(marker in module.author.lower()
                                                       for marker in ('odoo', 'openerp'))
                          and module.name != 'zyra_shopify_connector'}
        allowed = self.env['ir.model.fields']
        for meta in metadata:
            field = self.env['sale.order']._fields.get(meta.name)
            if not field or field.compute or field.related or field.readonly or not field.store:
                continue
            if field.groups:
                continue
            owned_by = owners.get(meta.id, set())
            if meta.state == 'manual' or (owned_by and owned_by <= custom_modules):
                allowed |= meta
        return allowed

    @api.depends('field_id', 'store_id')
    def _compute_available_fields(self):
        eligible = self._eligible_fields()
        for mapping in self:
            mapping.available_field_ids = eligible

    @api.depends('field_id')
    def _compute_field_help(self):
        for mapping in self:
            field = self.env['sale.order']._fields.get(mapping.field_id.name)
            details = ''
            if field:
                details = '%s (%s)' % (mapping.field_id.name, field.type)
                if field.type == 'selection':
                    details += '\n' + '\n'.join('%s: %s' % item for item in field._description_selection(self.env))
                if field.type == 'many2one':
                    details += '\nExisting record ID from: ' + field.comodel_name
            mapping.field_help = details

    @api.constrains('field_id', 'source', 'value_json', 'payload_path', 'store_id', 'active')
    def _check_configuration(self):
        eligible = self._eligible_fields()
        for mapping in self:
            if not mapping.active:
                continue
            if mapping.field_id not in eligible:
                raise ValidationError(_('Choose a writable custom sale order field. Standard Odoo fields are not allowed.'))
            if mapping.source == 'payload':
                if not mapping.payload_path or any(not part.strip() for part in mapping.payload_path.split('.')):
                    raise ValidationError(_('Choose the Shopify information to copy.'))
            else:
                mapping._convert_value(mapping._fixed_value())

    def _fixed_value(self):
        try:
            return json.loads(self.value_json or '')
        except (ValueError, TypeError):
            raise ValidationError(_('Enter valid JSON for custom field %s.', self.field_id.field_description))

    def _convert_value(self, value):
        self.ensure_one()
        field = self.env['sale.order']._fields.get(self.field_id.name)
        valid = False
        if field and value is False:
            valid = not field.required or field.type == 'boolean'
        elif field:
            kind = field.type
            if kind in ('char', 'text', 'html', 'selection', 'date', 'datetime'):
                valid = isinstance(value, str)
                if valid and kind == 'selection':
                    valid = value in dict(field._description_selection(self.env))
                if valid and kind in ('date', 'datetime'):
                    try:
                        (fields.Date.to_date if kind == 'date' else fields.Datetime.to_datetime)(value)
                        valid = bool(value)
                    except (ValueError, TypeError):
                        valid = False
            elif kind == 'boolean':
                valid = type(value) is bool
            elif kind in ('integer', 'many2one'):
                valid = type(value) is int
                if valid and kind == 'many2one':
                    related = self.env[field.comodel_name].with_company(self.store_id.company_id)
                    domain = [('id', '=', value)]
                    if 'company_id' in related._fields:
                        domain += [('company_id', 'in', [False, self.store_id.company_id.id])]
                    valid = bool(related.search(domain, limit=1))
            elif kind in ('float', 'monetary'):
                valid = type(value) in (int, float) and math.isfinite(value)
        if not valid:
            raise ValidationError(_('Invalid value for custom field %s. Check its type and allowed values.',
                                    self.field_id.field_description))
        return value

    def get_sale_order_values(self, payload):
        self._check_configuration()
        values = {}
        for mapping in self.filtered('active'):
            if mapping.source == 'fixed':
                value = mapping._fixed_value()
            elif mapping.payload_path == 'order_name_and_tracking':
                name = str(payload.get('name') or '').strip()
                tracking = str(payload.get('primary_tracking_number') or
                               next(iter(payload.get('tracking_numbers') or []), '') or '').strip()
                value = '%s-%s' % (name, tracking) if name and tracking else name or str(payload.get('id') or '')
                if not value:
                    continue
            else:
                value = payload
                for part in mapping.payload_path.split('.'):
                    value = value.get(part) if isinstance(value, dict) else None
                if value is None:
                    continue
            if mapping.source == 'payload' and mapping.payload_path in dict(SHOPIFY_VALUES):
                # Shopify sends monetary amounts as strings, and IDs as strings
                # or numbers depending on the transport. Adapt these for Odoo.
                kind = mapping.field_type
                try:
                    if value is not False and kind in ('char', 'text', 'html'):
                        value = str(value)
                    elif value is not False and kind in ('float', 'monetary'):
                        value = float(value)
                    elif value is not False and kind == 'integer':
                        number = float(value)
                        if not number.is_integer():
                            raise ValueError()
                        value = int(number)
                except (ValueError, TypeError, OverflowError):
                    raise ValidationError(_('The selected Shopify information cannot be used for %s. Choose another value.',
                                            mapping.field_id.field_description))
            values[mapping.field_id.name] = mapping._convert_value(value)
        return values
