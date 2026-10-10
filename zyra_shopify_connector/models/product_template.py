from odoo import _, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    shopify_product_id = fields.Char(
        string='Shopify Product ID', copy=False, index='btree_not_null', tracking=True,
        help="Identifier of the Shopify product this template represents.")

    # Mirrors the Barcode behaviour: a single-variant template exposes its
    # variant's Shopify id directly, multi-variant templates hide it because
    # each variant carries its own.
    shopify_variant_id = fields.Char(
        string='Shopify Variant ID',
        compute='_compute_shopify_variant_id',
        inverse='_set_shopify_variant_id',
        search='_search_shopify_variant_id')

    shopify_product_mapping_ids = fields.One2many(
        'shopify.product.mapping', 'product_tmpl_id', string='Shopify Product Mappings')
    shopify_variant_mapping_ids = fields.One2many(
        'shopify.variant.mapping', 'product_tmpl_id', string='Shopify Variant Mappings')
    shopify_mapping_count = fields.Integer(compute='_compute_shopify_mapping_count')

    def _compute_shopify_variant_id(self):
        self._compute_template_field_from_variant_field('shopify_variant_id')

    def _set_shopify_variant_id(self):
        self._set_product_variant_field('shopify_variant_id')

    def _search_shopify_variant_id(self, operator, value):
        subquery = self.with_context(active_test=False)._search([
            ('product_variant_ids.shopify_variant_id', operator, value),
        ])
        return [('id', 'in', subquery)]

    def _compute_shopify_mapping_count(self):
        for template in self:
            template.shopify_mapping_count = (
                len(template.shopify_product_mapping_ids) + len(template.shopify_variant_mapping_ids))

    def action_view_shopify_mappings(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Shopify Mappings'),
            'res_model': 'shopify.variant.mapping',
            'view_mode': 'list,form',
            'domain': [('product_tmpl_id', '=', self.id)],
            'context': {'default_product_id': self.product_variant_id.id},
        }
