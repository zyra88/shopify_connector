from odoo import _, api, fields, models

MATCH_SOURCES = [
    ('mapping', 'Existing Mapping'),
    ('variant_id', 'Shopify Variant ID'),
    ('product_id', 'Shopify Product ID'),
    ('sku', 'SKU / Internal Reference'),
    ('barcode', 'Barcode'),
    ('manual', 'Manual'),
]


class ShopifyProductMapping(models.Model):
    """Shopify product <-> Odoo product template."""

    _name = 'shopify.product.mapping'
    _description = 'Shopify Product Mapping'
    _order = 'id desc'
    _rec_name = 'display_name'

    store_id = fields.Many2one(
        'shopify.store', string='Store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True, index=True)

    shopify_product_id = fields.Char(string='Shopify Product ID', required=True, index=True)
    shopify_title = fields.Char(string='Shopify Product', index=True)
    product_tmpl_id = fields.Many2one(
        'product.template', string='Odoo Product Template', ondelete='set null', index=True)

    variant_mapping_ids = fields.One2many(
        'shopify.variant.mapping', 'product_mapping_id', string='Variants')
    variant_mapping_count = fields.Integer(compute='_compute_variant_mapping_count')
    variant_total_count = fields.Integer(readonly=True)
    variant_mapped_count = fields.Integer(readonly=True)
    connection_state = fields.Selection(
        [
            ('connected', 'Connected'),
            ('partial', 'Partially Connected'),
            ('not_connected', 'Not Connected'),
        ],
        string='Status', readonly=True, index=True)
    last_synced_at = fields.Datetime(readonly=True)

    _store_product_uniq = models.Constraint(
        'UNIQUE(store_id, shopify_product_id)',
        "This Shopify product is already mapped for this store.",
    )

    @api.depends('shopify_title', 'product_tmpl_id', 'shopify_product_id')
    def _compute_display_name(self):
        for mapping in self:
            label = mapping.shopify_title or mapping.product_tmpl_id.display_name or _('Shopify Product')
            mapping.display_name = '%s (%s)' % (label, mapping.shopify_product_id or '-')

    def _compute_variant_mapping_count(self):
        for mapping in self:
            mapping.variant_mapping_count = len(mapping.variant_mapping_ids)

    def refresh_connection_state(self):
        catalog = self.env['shopify.product.catalog']
        for mapping in self:
            lines = catalog.search([
                ('store_id', '=', mapping.store_id.id),
                ('shopify_product_id', '=', mapping.shopify_product_id),
            ]) if mapping.store_id and mapping.shopify_product_id else catalog.browse()
            total = len(lines)
            mapped = len(lines.filtered(lambda line: line.state == 'mapped'))
            if mapping.product_tmpl_id and total and mapped == total:
                state = 'connected'
            elif mapping.product_tmpl_id or mapped:
                state = 'partial'
            else:
                state = 'not_connected'
            mapping.write({
                'variant_total_count': total,
                'variant_mapped_count': mapped,
                'connection_state': state,
            })

    def write(self, vals):
        res = super().write(vals)
        if 'product_tmpl_id' in vals:
            for mapping in self.filtered('product_tmpl_id'):
                if not mapping.product_tmpl_id.shopify_product_id:
                    mapping.product_tmpl_id.sudo().shopify_product_id = mapping.shopify_product_id
            self.refresh_connection_state()
        return res

    def action_open_fetch_wizard(self):
        return self.env['ir.actions.act_window']._for_xml_id(
            'shopify_connector.action_shopify_fetch_catalog_wizard')

    def action_open_variants(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'shopify_connector.action_shopify_product_catalog')
        action['domain'] = [
            ('store_id', '=', self.store_id.id),
            ('shopify_product_id', '=', self.shopify_product_id),
        ]
        action['context'] = dict(self.env.context, search_default_unmapped=1)
        action['name'] = _('Variants for %s', self.shopify_title or self.shopify_product_id)
        return action

    @api.model
    def upsert(self, store, shopify_product_id, product_tmpl, title=None):
        if not shopify_product_id or not product_tmpl:
            return self.browse()
        values = {
            'product_tmpl_id': product_tmpl.id,
            'shopify_title': title or product_tmpl.name,
            'last_synced_at': fields.Datetime.now(),
        }
        mapping = self.search([
            ('store_id', '=', store.id),
            ('shopify_product_id', '=', str(shopify_product_id)),
        ], limit=1)
        if mapping:
            mapping.write(values)
            return mapping
        values.update({'store_id': store.id, 'shopify_product_id': str(shopify_product_id)})
        return self.create(values)

    @api.model
    def upsert_catalog(self, store, shopify_product_id, title=None):
        """Create or refresh a Shopify product row before variants are linked."""
        if not shopify_product_id:
            return self.browse()
        values = {
            'shopify_title': title,
            'last_synced_at': fields.Datetime.now(),
        }
        mapping = self.search([
            ('store_id', '=', store.id),
            ('shopify_product_id', '=', str(shopify_product_id)),
        ], limit=1)
        if mapping:
            mapping.write(values)
            return mapping
        values.update({
            'store_id': store.id,
            'shopify_product_id': str(shopify_product_id),
        })
        return self.create(values)


class ShopifyVariantMapping(models.Model):
    """Shopify variant <-> Odoo product.product.

    This is the mapping the order importer actually resolves against: a
    Shopify line item always references a variant, even for products that have
    only the implicit default variant.
    """

    _name = 'shopify.variant.mapping'
    _description = 'Shopify Variant Mapping'
    _order = 'id desc'
    _rec_name = 'display_name'

    store_id = fields.Many2one(
        'shopify.store', string='Store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True, index=True)

    shopify_variant_id = fields.Char(string='Shopify Variant ID', required=True, index=True)
    shopify_product_id = fields.Char(string='Shopify Product ID', index=True)
    product_id = fields.Many2one(
        'product.product', string='Product Variant', required=True, ondelete='cascade', index=True)
    product_tmpl_id = fields.Many2one(related='product_id.product_tmpl_id', store=True)
    product_mapping_id = fields.Many2one(
        'shopify.product.mapping', string='Product Mapping', ondelete='set null')

    sku = fields.Char(string='Shopify SKU')
    barcode = fields.Char(string='Shopify Barcode')
    match_source = fields.Selection(
        MATCH_SOURCES, string='Matched By', default='manual', readonly=True)
    last_synced_at = fields.Datetime(readonly=True)

    _store_variant_uniq = models.Constraint(
        'UNIQUE(store_id, shopify_variant_id)',
        "This Shopify variant is already mapped for this store.",
    )

    @api.depends('product_id', 'shopify_variant_id')
    def _compute_display_name(self):
        for mapping in self:
            mapping.display_name = '%s (%s)' % (
                mapping.product_id.display_name or _('Unknown'), mapping.shopify_variant_id or '-')

    @api.model
    def upsert(self, store, shopify_variant_id, product, shopify_product_id=None,
               sku=None, barcode=None, match_source='manual'):
        if not shopify_variant_id or not product:
            return self.browse()
        product_mapping = self.env['shopify.product.mapping']
        if shopify_product_id:
            product_mapping = product_mapping.upsert(
                store, shopify_product_id, product.product_tmpl_id)

        values = {
            'product_id': product.id,
            'shopify_product_id': str(shopify_product_id) if shopify_product_id else False,
            'product_mapping_id': product_mapping.id if product_mapping else False,
            'sku': sku,
            'barcode': barcode,
            'match_source': match_source,
            'last_synced_at': fields.Datetime.now(),
        }
        mapping = self.search([
            ('store_id', '=', store.id),
            ('shopify_variant_id', '=', str(shopify_variant_id)),
        ], limit=1)
        if mapping:
            # Never silently repoint an existing mapping at a different product;
            # that would rewrite history on previously imported orders.
            if mapping.product_id != product:
                values.pop('product_id')
            mapping.write(values)
            return mapping
        values.update({'store_id': store.id, 'shopify_variant_id': str(shopify_variant_id)})
        return self.create(values)

    def write_back_identifiers(self):
        """Copy Shopify ids onto the product itself so they show in the UI.

        Only empty fields are filled. With several stores selling the same
        catalogue the product can hold one id at a time, and the per-store
        mapping rows remain the authoritative lookup.
        """
        for mapping in self:
            product = mapping.product_id
            if mapping.shopify_variant_id and not product.shopify_variant_id:
                product.sudo().shopify_variant_id = mapping.shopify_variant_id
            template = product.product_tmpl_id
            if mapping.shopify_product_id and not template.shopify_product_id:
                template.sudo().shopify_product_id = mapping.shopify_product_id
