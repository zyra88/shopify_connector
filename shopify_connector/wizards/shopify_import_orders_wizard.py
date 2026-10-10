import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.shopify_client import ShopifyError


class ShopifyImportOrdersWizard(models.TransientModel):
    """Manual order import with an explicit scope.

    The store's own *Import Orders* button covers the routine case. This
    wizard exists for the two situations that button cannot express: pulling a
    specific set of orders again, and backfilling from an arbitrary date
    without disturbing the sync cursor.
    """

    _name = 'shopify.import.orders.wizard'
    _description = 'Import Shopify Orders'

    store_id = fields.Many2one(
        'shopify.store', string='Store', required=True,
        default=lambda self: self._default_store())
    mode = fields.Selection(
        [
            ('incremental', 'New and updated since last sync'),
            ('from_date', 'Updated since a date'),
            ('specific', 'Specific order IDs'),
        ],
        default='incremental', required=True)

    updated_at_min = fields.Datetime(
        string='Updated Since',
        default=lambda self: fields.Datetime.subtract(fields.Datetime.now(), days=7))
    order_ids_text = fields.Char(
        string='Shopify Order IDs',
        help="Comma or space separated numeric Shopify order IDs.")
    max_orders = fields.Integer(string='Maximum Orders', default=0, help="0 means no limit.")

    last_sync_date = fields.Datetime(related='store_id.last_order_sync_date', readonly=True)
    connection_state = fields.Selection(related='store_id.connection_state', readonly=True)

    @api.model
    def _default_store(self):
        if self.env.context.get('default_store_id'):
            return self.env.context['default_store_id']
        return self.env['shopify.store'].search(
            [('company_id', 'in', self.env.companies.ids)], limit=1)

    def action_import(self):
        self.ensure_one()
        kwargs = {'max_orders': self.max_orders or None}

        if self.mode == 'from_date':
            if not self.updated_at_min:
                raise UserError(_("Choose the date to import from."))
            kwargs['updated_at_min'] = self.updated_at_min
        elif self.mode == 'specific':
            order_ids = [token for token in re.split(r'[\s,;]+', self.order_ids_text or '') if token]
            if not order_ids:
                raise UserError(_("Enter at least one Shopify order ID."))
            non_numeric = [token for token in order_ids if not token.isdigit()]
            if non_numeric:
                raise UserError(_(
                    "Shopify order IDs are numeric. These are not: %s", ', '.join(non_numeric)))
            kwargs['shopify_order_ids'] = order_ids

        try:
            summary = self.store_id.sync_orders(**kwargs)
        except (ShopifyError, UserError) as exc:
            raise UserError(_("Import failed: %s", exc)) from exc

        message = _(
            "%(fetched)s order(s) fetched, %(done)s imported, %(review)s need review, %(failed)s failed.",
            fetched=summary.get('fetched', 0), done=summary.get('done', 0),
            review=summary.get('needs_review', 0), failed=summary.get('failed', 0))
        return self.store_id._notify(
            message,
            warning=bool(summary.get('failed') or summary.get('needs_review')),
            sticky=bool(summary.get('failed')))
