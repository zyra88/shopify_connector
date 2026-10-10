from odoo import _, api, fields, models


class ShopifyProductSync(models.AbstractModel):
    """Pull Shopify product and variant ids into Odoo by SKU or barcode."""

    _name = 'shopify.product.sync'
    _description = 'Shopify Product Sync'

    @api.model
    def sync(self, store, updated_at_min=None, max_products=None):
        """Fetch Shopify products and map variant ids onto Odoo products.

        Returns ``{'fetched', 'mapped', 'updated', 'unmatched', 'conflicts'}``.
        """
        client = store.get_client()
        stats = {
            'fetched': 0,
            'variants': 0,
            'mapped': 0,
            'updated': 0,
            'unmatched': 0,
            'conflicts': 0,
        }
        log = self.env['shopify.sync.log']

        for product_payload in client.iter_products(
                updated_at_min=updated_at_min,
                page_size=store.page_size or 50,
                max_products=max_products or None):
            stats['fetched'] += 1
            for variant in product_payload.get('variants') or []:
                stats['variants'] += 1
                outcome = self._sync_variant(store, product_payload, variant)
                stats[outcome] += 1

        message = _(
            "%(mapped)s variant(s) mapped, %(updated)s already mapped, "
            "%(unmatched)s unmatched, %(conflicts)s conflict(s) "
            "from %(variants)s variant(s) in %(fetched)s Shopify product(s).",
            **stats,
        )
        log.log(
            store,
            'warning' if stats['unmatched'] or stats['conflicts'] else 'info',
            'product_sync',
            message,
        )
        return stats

    @api.model
    def _sync_variant(self, store, product_payload, variant):
        variant_id = variant.get('id') and str(variant['id']) or None
        if not variant_id:
            return 'unmatched'

        mapping_model = self.env['shopify.variant.mapping']
        existing = mapping_model.search([
            ('store_id', '=', store.id),
            ('shopify_variant_id', '=', variant_id),
        ], limit=1)
        if existing:
            existing.write({
                'sku': variant.get('sku'),
                'barcode': variant.get('barcode'),
                'shopify_product_id': product_payload.get('id'),
                'last_synced_at': fields.Datetime.now(),
            })
            existing.write_back_identifiers()
            self._apply_match_metadata(store, existing.product_id, variant)
            return 'updated'

        product, match_source, conflict = self._find_odoo_product(store, variant)
        if conflict:
            self.env['shopify.sync.log'].log(
                store, 'warning', 'product_sync',
                _("Shopify variant %(variant)s (%(sku)s): %(reason)s",
                  variant=variant_id,
                  sku=variant.get('sku') or '-',
                  reason=conflict))
            return 'conflicts'
        if not product:
            return 'unmatched'

        if product.shopify_variant_id and product.shopify_variant_id != variant_id:
            self.env['shopify.sync.log'].log(
                store, 'warning', 'product_sync',
                _("Shopify variant %(variant)s (SKU %(sku)s) matches %(product)s, "
                  "but that product already carries Shopify variant %(existing)s.",
                  variant=variant_id,
                  sku=variant.get('sku') or '-',
                  product=product.display_name,
                  existing=product.shopify_variant_id))
            return 'conflicts'

        mapping = mapping_model.upsert(
            store, variant_id, product,
            shopify_product_id=product_payload.get('id'),
            sku=variant.get('sku'),
            barcode=variant.get('barcode'),
            match_source=match_source or 'sku',
        )
        mapping.write_back_identifiers()
        self._apply_match_metadata(store, product, variant)
        return 'mapped'

    @api.model
    def _apply_match_metadata(self, store, product, variant):
        if not store.write_sku_on_match:
            return
        sku = (variant.get('sku') or '').strip()
        if sku and not product.default_code:
            product.sudo().default_code = sku

    @api.model
    def _find_odoo_product(self, store, variant):
        sku = (variant.get('sku') or '').strip()
        barcode = (variant.get('barcode') or '').strip()
        Product = self.env['product.product']

        if store.match_product_by_sku and sku:
            products = Product.search([('default_code', '=', sku)], limit=2)
            if len(products) == 1:
                return products, 'sku', None
            if len(products) > 1:
                return Product.browse(), None, _(
                    "SKU '%s' matches more than one Odoo product.", sku)

        if store.match_product_by_attributes and sku:
            product, conflict = self._find_by_sku_attributes(sku)
            if product:
                return product, 'sku', None
            if conflict:
                return Product.browse(), None, conflict

        if store.match_product_by_barcode and barcode:
            products = Product.search([('barcode', '=', barcode)])
            products = products.filtered(lambda p: not p.shopify_variant_id)
            if len(products) == 1:
                return products, 'barcode', None
            if len(products) > 1:
                return Product.browse(), None, _(
                    "Barcode '%s' matches more than one unmapped Odoo product.",
                    barcode)

        return Product.browse(), None, None

    @api.model
    def _parse_shopify_sku(self, sku):
        """Split ``MODEL-Colour-6`` style SKUs into model, colour and size."""
        sku = (sku or '').strip()
        if not sku:
            return None
        parts = sku.split('-')
        if len(parts) < 3:
            return None
        size = parts[-1]
        if not size.isdigit():
            return None
        return {
            'model': '-'.join(parts[:-2]),
            'color': parts[-2],
            'size': size,
        }

    @api.model
    def _variant_attribute_map(self, product):
        attrs = {}
        for ptav in product.product_template_attribute_value_ids:
            name = (ptav.attribute_id.name or '').strip().lower()
            value = (ptav.product_attribute_value_id.name or '').strip()
            if name in ('colour', 'color'):
                attrs['color'] = value
            elif name == 'size':
                attrs['size'] = value
        return attrs

    @api.model
    def _find_by_sku_attributes(self, sku):
        parsed = self._parse_shopify_sku(sku)
        if not parsed:
            return self.env['product.product'], None

        templates = self.env['product.template'].search([
            ('name', 'ilike', parsed['model']),
        ])
        templates = templates.filtered(
            lambda tmpl: parsed['model'].upper() in (tmpl.name or '').upper())

        matches = self.env['product.product']
        for template in templates:
            for product in template.product_variant_ids:
                attrs = self._variant_attribute_map(product)
                if not attrs.get('color') or not attrs.get('size'):
                    continue
                if attrs['color'].lower() != parsed['color'].lower():
                    continue
                if str(attrs['size']) != str(parsed['size']):
                    continue
                matches |= product

        if not matches:
            return self.env['product.product'], None
        if len(matches) == 1:
            return matches, None

        unmapped = matches.filtered(lambda product: not product.shopify_variant_id)
        if len(unmapped) == 1:
            return unmapped, None
        pool = unmapped or matches

        def template_weight(product):
            return len(product.product_tmpl_id.product_variant_ids)

        best_weight = max(template_weight(product) for product in pool)
        finalists = pool.filtered(lambda product: template_weight(product) == best_weight)
        if len(finalists) == 1:
            return finalists, None

        return self.env['product.product'], _(
            "SKU '%(sku)s' matches %(count)s Odoo variants for %(color)s / "
            "%(size)s.",
            sku=sku, count=len(finalists),
            color=parsed['color'], size=parsed['size'])
