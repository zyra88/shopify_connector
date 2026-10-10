from odoo import _, fields, models

SHOPIFY_LINE_TYPES = [
    ('product', 'Product'),
    ('shipping', 'Shipping'),
    ('discount', 'Discount'),
    ('fee', 'Fee'),
]


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    shopify_store_id = fields.Many2one(
        'shopify.store', string='Shopify Store', copy=False, readonly=True, index='btree_not_null')
    shopify_order_id = fields.Char(
        string='Shopify Order ID', copy=False, readonly=True, index='btree_not_null')
    shopify_order_name = fields.Char(string='Shopify Order', copy=False, readonly=True)
    shopify_order_number = fields.Integer(string='Shopify Order Number', copy=False, readonly=True)
    shopify_order_ref_id = fields.Many2one(
        'shopify.order', string='Shopify Sync Record', copy=False, readonly=True, ondelete='set null')

    shopify_financial_status = fields.Char(string='Financial Status', copy=False, readonly=True)
    shopify_fulfillment_status = fields.Char(string='Fulfillment Status', copy=False, readonly=True)
    shopify_payment_gateway = fields.Char(string='Payment Method', copy=False, readonly=True)
    shopify_tags = fields.Char(string='Shopify Tags', copy=False, readonly=True)
    shopify_currency_code = fields.Char(string='Shopify Currency', copy=False, readonly=True)
    shopify_created_at = fields.Datetime(string='Shopify Created At', copy=False, readonly=True)
    shopify_updated_at = fields.Datetime(string='Shopify Updated At', copy=False, readonly=True)

    _sql_constraints = [
        (
            'shopify_order_uniq',
            'UNIQUE(shopify_store_id, shopify_order_id)',
            'This Shopify order has already been imported for this store.',
        ),
    ]

    def action_view_shopify_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Shopify Order'),
            'res_model': 'shopify.order',
            'res_id': self.shopify_order_ref_id.id,
            'view_mode': 'form',
        }


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    shopify_line_id = fields.Char(string='Shopify Line ID', copy=False, index='btree_not_null')
    shopify_line_type = fields.Selection(SHOPIFY_LINE_TYPES, string='Shopify Line Type', copy=False)
    shopify_variant_id = fields.Char(string='Shopify Variant ID', copy=False)
    shopify_sku = fields.Char(string='Shopify SKU', copy=False)
