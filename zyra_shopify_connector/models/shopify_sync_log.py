import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

LEVEL_SEVERITY = {'info': 10, 'warning': 20, 'error': 30}


class ShopifySyncLog(models.Model):
    _name = 'shopify.sync.log'
    _description = 'Shopify Sync Log'
    _order = 'create_date desc, id desc'
    _rec_name = 'display_name'

    store_id = fields.Many2one(
        'shopify.store', string='Store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True, index=True)

    level = fields.Selection(
        [('info', 'Info'), ('warning', 'Warning'), ('error', 'Error')],
        required=True, default='info', index=True)
    operation = fields.Selection(
        [
            ('test_connection', 'Test Connection'),
            ('order_fetch', 'Order Fetch'),
            ('order_import', 'Order Import'),
            ('customer_resolve', 'Customer Resolution'),
            ('product_resolve', 'Product Resolution'),
            ('product_sync', 'Product Sync'),
            ('tax_resolve', 'Tax Resolution'),
            ('reconciliation', 'Amount Reconciliation'),
            ('queue', 'Queue'),
        ],
        required=True, default='order_import', index=True)

    message = fields.Text(required=True)
    shopify_reference = fields.Char(
        string='Shopify Reference', index=True,
        help="Shopify order name or resource id this entry is about.")
    shopify_order_id = fields.Char(index=True)

    shopify_order_ref_id = fields.Many2one('shopify.order', string='Shopify Order Record', ondelete='cascade')
    sale_order_id = fields.Many2one('sale.order', string='Sale Order', ondelete='set null')
    partner_id = fields.Many2one('res.partner', string='Customer', ondelete='set null')
    queue_id = fields.Many2one('shopify.sync.queue', string='Queue Job', ondelete='cascade')

    exception_info = fields.Text(
        string='Technical Details',
        help="Python traceback, kept for support. Not shown to end users by default.")

    @api.depends('operation', 'shopify_reference')
    def _compute_display_name(self):
        labels = dict(self._fields['operation']._description_selection(self.env))
        for log in self:
            label = labels.get(log.operation, log.operation or '')
            log.display_name = '%s - %s' % (label, log.shopify_reference) if log.shopify_reference else label

    @api.model
    def log(self, store, level, operation, message, **values):
        """Create one log entry and mirror it to the server log.

        Returns the created record so callers can chain, but callers should
        never depend on it: logging must not be able to break an import.
        """
        values.update({
            'store_id': store.id if hasattr(store, 'id') else store,
            'level': level,
            'operation': operation,
            'message': message or '',
        })
        severity = LEVEL_SEVERITY.get(level, 10)
        _logger.log(
            logging.ERROR if severity >= 30 else logging.WARNING if severity >= 20 else logging.INFO,
            "[shopify:%s] %s %s", operation, values.get('shopify_reference') or '', message,
        )
        return self.sudo().create(values)

    @api.model
    def _gc_logs(self, days=90):
        """Delete info/warning entries older than ``days``. Errors are kept."""
        limit_date = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.sudo().search([
            ('create_date', '<', limit_date),
            ('level', 'in', ('info', 'warning')),
        ])
        stale.unlink()
        return True
