import re

from odoo import _, api, fields, models

_NON_DIGITS = re.compile(r'\D+')

# Shopify and Odoo write country prefixes and trunk zeros inconsistently, so
# numbers are compared on their last digits rather than character by character.
PHONE_KEY_DIGITS = 9
PHONE_MIN_DIGITS = 6

# Shopify imports run under the current user's context. Older Odoo databases
# still carry deprecated IANA aliases (e.g. Asia/Calcutta) on user records,
# but Odoo 18 only accepts the canonical names (Asia/Kolkata).
_DEPRECATED_TIMEZONES = {
    'Asia/Calcutta': 'Asia/Kolkata',
    'Asia/Katmandu': 'Asia/Kathmandu',
}


def phone_key(phone):
    """Comparable form of a phone number, or '' when it is too short."""
    digits = _NON_DIGITS.sub('', phone or '')
    if len(digits) < PHONE_MIN_DIGITS:
        return ''
    return digits[-PHONE_KEY_DIGITS:]


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.model
    def _sanitize_timezone(self, tz):
        tz = _DEPRECATED_TIMEZONES.get(tz, tz)
        if not tz:
            return False
        try:
            self._fields['tz'].convert_to_cache(tz, self)
        except ValueError:
            return False
        return tz

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('tz'):
                vals['tz'] = self._sanitize_timezone(vals['tz'])
            elif self.env.context.get('tz'):
                vals['tz'] = self._sanitize_timezone(self.env.context['tz'])
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('tz'):
            vals = dict(vals, tz=self._sanitize_timezone(vals['tz']))
        return super().write(vals)

    shopify_customer_id = fields.Char(
        string='Shopify Customer ID', copy=False, index='btree_not_null',
        help="Shopify customer this contact was created from. Guest "
             "checkouts leave this empty.")
    shopify_mapping_ids = fields.One2many(
        'shopify.customer.mapping', 'partner_id', string='Shopify Mappings')
    shopify_mapping_count = fields.Integer(compute='_compute_shopify_mapping_count')
    is_shopify_customer = fields.Boolean(
        compute='_compute_is_shopify_customer', store=True, string='From Shopify')
    # Stored and indexed so the order importer can look a customer up by
    # phone with an equality search. Matching on the raw column cannot work:
    # "+44 20 7946 0958" and "02079460958" share no usable substring.
    shopify_phone_key = fields.Char(
        compute='_compute_shopify_phone_keys', store=True, index='btree_not_null',
        string='Phone Match Key')
    shopify_mobile_key = fields.Char(
        compute='_compute_shopify_phone_keys', store=True, index='btree_not_null',
        string='Mobile Match Key')

    shopify_address_type = fields.Selection(
        [('billing', 'Shopify Billing Address'), ('shipping', 'Shopify Shipping Address')],
        copy=False, readonly=True,
        help="Set on the child contacts the connector creates so later "
             "imports reuse them instead of piling up duplicates.")

    @api.depends('phone')
    def _compute_shopify_phone_keys(self):
        for partner in self:
            partner.shopify_phone_key = phone_key(partner.phone) or False
            partner.shopify_mobile_key = phone_key(partner['mobile'] if 'mobile' in partner._fields else None) or False

    @api.depends('shopify_mapping_ids')
    def _compute_shopify_mapping_count(self):
        for partner in self:
            partner.shopify_mapping_count = len(partner.shopify_mapping_ids)

    @api.depends('shopify_mapping_ids', 'shopify_customer_id')
    def _compute_is_shopify_customer(self):
        for partner in self:
            partner.is_shopify_customer = bool(
                partner.shopify_mapping_ids or partner.shopify_customer_id)

    def action_view_shopify_mappings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Shopify Mappings'),
            'res_model': 'shopify.customer.mapping',
            'view_mode': 'list,form',
            'domain': [('partner_id', '=', self.id)],
        }
