import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .shopify_product_mapping import MATCH_SOURCES
from .sale_order import SHOPIFY_LINE_TYPES


def format_address(address):
    """Render a Shopify address dict as a readable block, for audit only."""
    if not address:
        return ''
    name = ' '.join(filter(None, [address.get('first_name'), address.get('last_name')])) \
        or address.get('name') or ''
    province = ' '.join(filter(None, [address.get('province'), address.get('zip')]))
    lines = [
        name,
        address.get('company'),
        address.get('address1'),
        address.get('address2'),
        ' '.join(filter(None, [address.get('city'), province])),
        address.get('country'),
        address.get('phone'),
    ]
    return '\n'.join(line for line in lines if line)


# Shopify REST and GraphQL spell fulfillment statuses differently.
FULFILLMENT_ALIASES = {
    'partially_fulfilled': 'partial',
    'pending_fulfillment': 'in_progress',
    'scheduled': 'in_progress',
    'open': 'unfulfilled',
}

class ShopifyOrder(models.Model):
    """Mirror of a Shopify order and the audit trail of its import.

    Kept separate from ``sale.order`` on purpose: it survives even when no
    sale order could be created, which is what makes a failed import
    diagnosable instead of invisible.
    """

    _name = 'shopify.order'
    _description = 'Shopify Order'
    _order = 'shopify_created_at desc, id desc'
    _rec_name = 'shopify_order_name'

    store_id = fields.Many2one(
        'shopify.store', string='Store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True, index=True)

    # -- Shopify identity ----------------------------------------------
    shopify_order_id = fields.Char(string='Shopify Order ID', required=True, index=True, readonly=True)
    shopify_order_name = fields.Char(string='Shopify Order', readonly=True, index=True)
    shopify_order_number = fields.Integer(string='Order Number', readonly=True)
    shopify_created_at = fields.Datetime(string='Shopify Created At', readonly=True)
    shopify_updated_at = fields.Datetime(string='Shopify Updated At', readonly=True)
    shopify_processed_at = fields.Datetime(string='Processed At', readonly=True)
    shopify_cancelled_at = fields.Datetime(string='Cancelled At', readonly=True)

    # -- Customer -------------------------------------------------------
    shopify_customer_id = fields.Char(string='Shopify Customer ID', readonly=True, index='btree_not_null')
    is_guest_order = fields.Boolean(readonly=True)
    partner_id = fields.Many2one('res.partner', string='Customer', readonly=True)
    partner_invoice_id = fields.Many2one('res.partner', string='Invoice Address', readonly=True)
    partner_shipping_id = fields.Many2one('res.partner', string='Delivery Address', readonly=True)
    customer_mapping_id = fields.Many2one(
        'shopify.customer.mapping', string='Customer Mapping', readonly=True, ondelete='set null')
    email = fields.Char(readonly=True)
    phone = fields.Char(readonly=True)
    billing_address = fields.Text(readonly=True)
    shipping_address = fields.Text(readonly=True)

    # -- Commercials -----------------------------------------------------
    currency_code = fields.Char(string='Currency Code', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    financial_status = fields.Char(readonly=True)
    fulfillment_status = fields.Char(readonly=True)
    shopify_stage = fields.Selection(
        [
            ('unfulfilled', 'Unfulfilled'),
            ('in_progress', 'In Progress'),
            ('on_hold', 'On Hold'),
            ('partial', 'Partially Fulfilled'),
            ('fulfilled', 'Fulfilled'),
            ('restocked', 'Restocked'),
            ('cancelled', 'Cancelled'),
        ],
        string='Shopify Status', compute='_compute_shopify_status', store=True,
        help="Fulfillment status on Shopify, or Cancelled.")
    payment_status_label = fields.Char(
        string='Payment', compute='_compute_shopify_status', store=True)
    payment_gateway = fields.Char(string='Payment Method', readonly=True)
    note = fields.Text(readonly=True)
    tags = fields.Char(readonly=True)
    discount_codes = fields.Char(readonly=True)
    taxes_included = fields.Boolean(readonly=True)

    shopify_subtotal = fields.Monetary(string='Shopify Subtotal', currency_field='currency_id', readonly=True)
    shopify_total_discounts = fields.Monetary(string='Shopify Discounts', currency_field='currency_id', readonly=True)
    shopify_total_shipping = fields.Monetary(string='Shopify Shipping', currency_field='currency_id', readonly=True)
    shopify_total_tax = fields.Monetary(string='Shopify Tax', currency_field='currency_id', readonly=True)
    shopify_total = fields.Monetary(string='Shopify Total', currency_field='currency_id', readonly=True)

    odoo_total = fields.Monetary(
        string='Odoo Total', currency_field='currency_id',
        compute='_compute_reconciliation', store=True)
    amount_difference = fields.Monetary(
        string='Difference', currency_field='currency_id',
        compute='_compute_reconciliation', store=True)
    is_reconciled = fields.Boolean(compute='_compute_reconciliation', store=True)

    # -- Sync state -------------------------------------------------------
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('imported', 'Imported'),
            ('needs_review', 'Needs Review'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled'),
        ],
        default='draft', required=True, index=True, readonly=True)
    review_reason = fields.Text(readonly=True)
    error_message = fields.Text(readonly=True)
    last_sync_at = fields.Datetime(readonly=True)
    import_count = fields.Integer(string='Import Runs', default=0, readonly=True)

    sale_order_id = fields.Many2one('sale.order', string='Sale Order', readonly=True, ondelete='set null')
    sale_order_state = fields.Selection(related='sale_order_id.state', string='Sale Order Status')

    line_ids = fields.One2many('shopify.order.line', 'shopify_order_id', string='Lines', readonly=True)
    unmatched_line_count = fields.Integer(compute='_compute_unmatched_line_count')
    log_ids = fields.One2many('shopify.sync.log', 'shopify_order_ref_id', string='Logs')
    queue_ids = fields.One2many('shopify.sync.queue', 'shopify_order_ref_id', string='Queue Jobs')

    payload = fields.Text(
        string='Shopify Payload', readonly=True,
        help="Normalised payload of the last import, kept for audit and replay.")

    _sql_constraints = [
        (
            'store_order_uniq',
            'UNIQUE(store_id, shopify_order_id)',
            'This Shopify order is already recorded for this store.',
        ),
    ]

    @api.depends('fulfillment_status', 'shopify_cancelled_at', 'financial_status')
    def _compute_shopify_status(self):
        stages = dict(self._fields['shopify_stage'].selection)
        for order in self:
            if order.shopify_cancelled_at:
                stage = 'cancelled'
            else:
                stage = (order.fulfillment_status or 'unfulfilled').lower()
                stage = FULFILLMENT_ALIASES.get(stage, stage)
            order.shopify_stage = stage if stage in stages else False
            order.payment_status_label = (order.financial_status or '').replace('_', ' ').title()

    @api.depends('shopify_total', 'sale_order_id.amount_total', 'store_id.amount_tolerance')
    def _compute_reconciliation(self):
        for order in self:
            odoo_total = order.sale_order_id.amount_total if order.sale_order_id else 0.0
            order.odoo_total = odoo_total
            difference = odoo_total - order.shopify_total
            order.amount_difference = difference
            tolerance = order.store_id.amount_tolerance or 0.01
            order.is_reconciled = bool(order.sale_order_id) and abs(difference) <= tolerance

    def _compute_unmatched_line_count(self):
        for order in self:
            order.unmatched_line_count = len(
                order.line_ids.filtered(lambda line: line.resolution_state == 'unmatched'))

    @api.depends('shopify_order_name', 'shopify_order_id')
    def _compute_display_name(self):
        for order in self:
            order.display_name = order.shopify_order_name or order.shopify_order_id or _('New')

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_retry_import(self):
        """Re-queue the stored payload, without calling Shopify again."""
        queue_model = self.env['shopify.sync.queue']
        orders = self.filtered(lambda order: order.payload and order.state != 'imported')
        skipped = len(self) - len(orders)
        if not orders:
            raise UserError(_(
                "None of the selected orders can be retried. Already-imported "
                "orders and orders without a stored payload are skipped."))

        jobs = queue_model.browse()
        for order in orders:
            jobs |= queue_model.enqueue_order(order.store_id, json.loads(order.payload))
        summary = jobs.process()
        message = _("%(done)s imported, %(review)s need review, %(failed)s failed.",
                      done=summary['done'], review=summary['needs_review'],
                      failed=summary['failed'])
        if skipped:
            message = _("%(skipped)s skipped. ", skipped=skipped) + message
        return orders.store_id[:1]._notify(
            message, warning=bool(summary['failed'] or summary['needs_review'] or skipped))

    def action_refetch_and_import(self):
        """Pull a fresh copy from Shopify and import it again."""
        for order in self:
            order.store_id.sync_orders(shopify_order_ids=[order.shopify_order_id])
        return self.store_id[:1]._notify(_("%s order(s) re-fetched from Shopify.", len(self)))

    def action_view_sale_order(self):
        self.ensure_one()
        if not self.sale_order_id:
            raise UserError(_("No sale order has been created for %s yet.", self.display_name))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sale Order'),
            'res_model': 'sale.order',
            'res_id': self.sale_order_id.id,
            'view_mode': 'form',
        }

    def action_view_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sync Logs'),
            'res_model': 'shopify.sync.log',
            'view_mode': 'list,form',
            'domain': [('shopify_order_ref_id', '=', self.id)],
        }

    def action_mark_cancelled(self):
        self.write({'state': 'cancelled'})
        return True


class ShopifyOrderLine(models.Model):
    _name = 'shopify.order.line'
    _description = 'Shopify Order Line'
    _order = 'shopify_order_id, sequence, id'

    shopify_order_id = fields.Many2one(
        'shopify.order', string='Shopify Order', required=True, ondelete='cascade', index=True)
    store_id = fields.Many2one(related='shopify_order_id.store_id', store=True, index=True)
    currency_id = fields.Many2one(related='shopify_order_id.currency_id')
    sequence = fields.Integer(default=10)

    shopify_line_id = fields.Char(string='Shopify Line ID', index='btree_not_null')
    line_type = fields.Selection(SHOPIFY_LINE_TYPES, default='product', required=True)

    # -- What Shopify sent -------------------------------------------------
    shopify_product_id = fields.Char(string='Shopify Product ID')
    shopify_variant_id = fields.Char(string='Shopify Variant ID', index='btree_not_null')
    sku = fields.Char(string='SKU')
    barcode = fields.Char()
    name = fields.Char(string='Description', required=True)
    variant_title = fields.Char()

    quantity = fields.Float(default=1.0, digits='Product Unit of Measure')
    price_unit = fields.Monetary(string='Unit Price', currency_field='currency_id')
    discount_amount = fields.Monetary(string='Discount', currency_field='currency_id')
    tax_amount = fields.Monetary(string='Tax', currency_field='currency_id')
    price_subtotal = fields.Monetary(string='Subtotal', currency_field='currency_id')

    # -- How it resolved ----------------------------------------------------
    product_id = fields.Many2one('product.product', string='Odoo Product')
    variant_mapping_id = fields.Many2one('shopify.variant.mapping', string='Variant Mapping', ondelete='set null')
    match_source = fields.Selection(MATCH_SOURCES, string='Matched By', readonly=True)
    resolution_state = fields.Selection(
        [('matched', 'Matched'), ('unmatched', 'Not Matched')],
        default='unmatched', required=True, index=True)
    resolution_message = fields.Text(string='Resolution Note')

    sale_order_line_id = fields.Many2one('sale.order.line', string='Sale Order Line', ondelete='set null')

    def action_map_product(self):
        """Open the mapping wizard for an unmatched line."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Map Shopify Variant'),
            'res_model': 'shopify.variant.mapping',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_store_id': self.store_id.id,
                'default_shopify_variant_id': self.shopify_variant_id,
                'default_shopify_product_id': self.shopify_product_id,
                'default_sku': self.sku,
                'default_barcode': self.barcode,
                'default_match_source': 'manual',
            },
        }
