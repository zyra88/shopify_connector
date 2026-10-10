from odoo import _, api, models


class ShopifyProductResolver(models.AbstractModel):
    """Resolve a Shopify line item to an Odoo ``product.product``.

    Matching is by identifier only, in this order:

    1. an existing ``shopify.variant.mapping`` for the store
    2. ``product.product.shopify_variant_id``
    3. SKU (``default_code``)
    4. barcode
    5. a ``shopify.product.mapping`` on a single-variant template, but only
       for lines that carry no variant id at all

    Matching by product *name* is deliberately not implemented: two Shopify
    products can easily share a title, and a wrong line silently invoices the
    wrong item. An unresolvable line sends the order to Needs Review instead.
    """

    _name = 'shopify.product.resolver'
    _description = 'Shopify Product Resolver'

    @api.model
    def resolve(self, store, line):
        """Return ``(product, match_source, message)``.

        ``product`` is an empty recordset when nothing matched; ``message``
        then explains exactly which identifiers were tried.
        """
        variant_id = line.get('variant_id') and str(line['variant_id']) or None
        product_id = line.get('product_id') and str(line['product_id']) or None
        sku = (line.get('sku') or '').strip() or None
        barcode = (line.get('variant_barcode') or '').strip() or None

        if variant_id:
            mapping = self.env['shopify.variant.mapping'].search([
                ('store_id', '=', store.id),
                ('shopify_variant_id', '=', variant_id),
            ], limit=1)
            if mapping and mapping.product_id:
                return mapping.product_id, 'mapping', None

            product = self.env['product.product'].search([
                ('shopify_variant_id', '=', variant_id),
            ], limit=1)
            if product:
                return product, 'variant_id', None

        if store.match_product_by_sku and sku:
            products = self.env['product.product'].search([('default_code', '=', sku)], limit=2)
            if len(products) == 1:
                return products, 'sku', None
            if len(products) > 1:
                return self.env['product.product'], None, _(
                    "SKU '%s' matches more than one Odoo product. Map the Shopify "
                    "variant explicitly, or make the internal reference unique.", sku)

        if store.match_product_by_barcode and barcode:
            products = self.env['product.product'].search([('barcode', '=', barcode)], limit=2)
            if len(products) == 1:
                return products, 'barcode', None
            if len(products) > 1:
                return self.env['product.product'], None, _(
                    "Barcode '%s' matches more than one Odoo product.", barcode)

        # Product-level fallback, only for lines with no variant id. When a
        # line does name a variant, an unknown variant means Shopify has a
        # variant Odoo does not -- falling back to the template's single
        # variant would quietly sell the wrong item.
        if product_id and not variant_id:
            template = self.env['shopify.product.mapping'].search([
                ('store_id', '=', store.id),
                ('shopify_product_id', '=', product_id),
            ], limit=1).product_tmpl_id
            if not template:
                template = self.env['product.template'].search([
                    ('shopify_product_id', '=', product_id),
                ], limit=1)
            if template and len(template.product_variant_ids) == 1:
                return template.product_variant_id, 'product_id', None

        return self.env['product.product'], None, self._unmatched_message(line, variant_id, sku, barcode)

    @api.model
    def _unmatched_message(self, line, variant_id, sku, barcode):
        tried = []
        if variant_id:
            tried.append(_("Shopify Variant ID %s", variant_id))
        if sku:
            tried.append(_("SKU %s", sku))
        if barcode:
            tried.append(_("barcode %s", barcode))
        if not tried:
            return _(
                "Shopify line '%s' carries no variant ID, SKU or barcode, so it "
                "cannot be matched to an Odoo product.", line.get('name') or line.get('title') or '?')
        return _(
            "No Odoo product found for Shopify line '%(name)s'. Tried: %(tried)s. "
            "Map the variant under Shopify > Mappings > Variants, or set the "
            "Shopify Variant ID on the product.",
            name=line.get('name') or line.get('title') or '?', tried=', '.join(tried))

    @api.model
    def remember_match(self, store, line, product, match_source):
        """Persist a successful match so the next import is a single lookup."""
        variant_id = line.get('variant_id') and str(line['variant_id']) or None
        if not variant_id or not product:
            return self.env['shopify.variant.mapping']
        mapping = self.env['shopify.variant.mapping'].upsert(
            store, variant_id, product,
            shopify_product_id=line.get('product_id') and str(line['product_id']) or None,
            sku=line.get('sku'),
            barcode=line.get('variant_barcode'),
            match_source=match_source or 'manual',
        )
        mapping.write_back_identifiers()
        return mapping
