from odoo import _, api, fields, models


class ShopifyCustomerMapping(models.Model):
    """Link between a registered Shopify customer and an Odoo partner.

    Guest orders deliberately produce no mapping row: there is no stable
    Shopify customer id to key on, so those partners are re-found by email or
    phone on the next import.
    """

    _name = 'shopify.customer.mapping'
    _description = 'Shopify Customer Mapping'
    _order = 'last_synced_at desc, id desc'
    _rec_name = 'display_name'

    store_id = fields.Many2one(
        'shopify.store', string='Store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True, index=True)

    shopify_customer_id = fields.Char(string='Shopify Customer ID', required=True, index=True)
    partner_id = fields.Many2one(
        'res.partner', string='Customer', required=True, ondelete='cascade', index=True)

    email = fields.Char()
    phone = fields.Char()
    shopify_tags = fields.Char(string='Shopify Tags')
    last_synced_at = fields.Datetime(readonly=True)
    order_count = fields.Integer(compute='_compute_order_count')

    _store_customer_uniq = models.Constraint(
        'UNIQUE(store_id, shopify_customer_id)',
        "This Shopify customer is already mapped for this store.",
    )

    @api.depends('partner_id', 'shopify_customer_id')
    def _compute_display_name(self):
        for mapping in self:
            mapping.display_name = '%s (%s)' % (
                mapping.partner_id.display_name or _('Unknown'), mapping.shopify_customer_id or '-')

    def _compute_order_count(self):
        if not self.ids:
            return
        data = self.env['shopify.order']._read_group(
            [('customer_mapping_id', 'in', self.ids)], ['customer_mapping_id'], ['__count'])
        counts = {mapping.id: count for mapping, count in data}
        for mapping in self:
            mapping.order_count = counts.get(mapping.id, 0)

    @api.model
    def upsert(self, store, shopify_customer_id, partner, email=None, phone=None, tags=None):
        """Create or refresh the mapping for one Shopify customer."""
        if not shopify_customer_id:
            return self.browse()
        values = {
            'partner_id': partner.id,
            'email': email or partner.email,
            'phone': phone or partner.phone,
            'shopify_tags': tags,
            'last_synced_at': fields.Datetime.now(),
        }
        mapping = self.search([
            ('store_id', '=', store.id),
            ('shopify_customer_id', '=', str(shopify_customer_id)),
        ], limit=1)
        if mapping:
            mapping.write(values)
            return mapping
        values.update({'store_id': store.id, 'shopify_customer_id': str(shopify_customer_id)})
        return self.create(values)

    def action_view_partner(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'res.partner',
            'res_id': self.partner_id.id,
            'view_mode': 'form',
        }
