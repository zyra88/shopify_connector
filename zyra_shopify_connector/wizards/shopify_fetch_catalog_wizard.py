from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ShopifyFetchCatalogWizard(models.TransientModel):
    _name = 'shopify.fetch.catalog.wizard'
    _description = 'Fetch Shopify Product Catalog'
    _rec_name = 'title'

    title = fields.Char(default='Fetch Shopify Products', readonly=True)
    store_id = fields.Many2one(
        'shopify.store', string='Store', required=True,
        domain=[('connection_state', '=', 'connected')])

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        store = self.env['shopify.store'].search([
            ('connection_state', '=', 'connected'),
        ], limit=1)
        if store:
            values['store_id'] = store.id
        return values

    def action_fetch_and_open(self):
        self.ensure_one()
        if not self.store_id:
            raise UserError(_("Select a connected Shopify store."))
        stats = self.env['shopify.product.catalog'].fetch_for_store(self.store_id)
        action = self.env['ir.actions.act_window']._for_xml_id(
            'zyra_shopify_connector.action_shopify_product_catalog')
        action['target'] = 'current'
        action['effect'] = {
            'fadeout': 'slow',
            'message': _("%(created)s new, %(updated)s updated from %(fetched)s Shopify product(s).",
                         **stats),
            'type': 'rainbow_man',
        }
        return action
