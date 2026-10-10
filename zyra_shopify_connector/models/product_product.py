from odoo import fields, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    shopify_variant_id = fields.Char(
        string='Shopify Variant ID', copy=False, index='btree_not_null',
        help="Identifier of the Shopify variant this product represents. "
             "Shopify line items always reference a variant, so this is what "
             "the order importer matches on first.")
    shopify_product_id = fields.Char(
        string='Shopify Product ID', related='product_tmpl_id.shopify_product_id',
        store=False, readonly=True)

    shopify_variant_mapping_ids = fields.One2many(
        'shopify.variant.mapping', 'product_id', string='Shopify Variant Mappings')

    _shopify_variant_id_uniq = models.Constraint(
        'UNIQUE(shopify_variant_id)',
        "Another product already carries this Shopify Variant ID.",
    )
