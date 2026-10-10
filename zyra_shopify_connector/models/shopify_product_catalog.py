from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.shopify_client import ShopifyError


class ShopifyProductCatalog(models.Model):
    """Shopify variants fetched for manual mapping to Odoo products."""

    _name = 'shopify.product.catalog'
    _description = 'Shopify Product Catalog'
    _order = 'shopify_product_title, shopify_variant_title, id'
    _rec_name = 'display_name'

    store_id = fields.Many2one(
        'shopify.store', string='Store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True, index=True)

    shopify_product_id = fields.Char(string='Shopify Product ID', required=True, index=True)
    shopify_variant_id = fields.Char(string='Shopify Variant ID', required=True, index=True)
    shopify_product_title = fields.Char(string='Shopify Product', readonly=True)
    shopify_variant_title = fields.Char(string='Shopify Variant Label', readonly=True,
                                         help="Raw label from Shopify, usually “Color / Size”.")
    shopify_option1 = fields.Char(string='Option 1', readonly=True)
    shopify_option2 = fields.Char(string='Option 2', readonly=True)
    shopify_option3 = fields.Char(string='Option 3', readonly=True)
    shopify_color = fields.Char(string='Color', compute='_compute_variant_attributes', store=True)
    shopify_size = fields.Char(string='Size', compute='_compute_variant_attributes', store=True)
    shopify_model_code = fields.Char(
        string='Model Code', compute='_compute_model_code', store=True, index=True)
    sku = fields.Char(string='Shopify SKU', readonly=True, index=True)
    barcode = fields.Char(string='Shopify Barcode', readonly=True)

    suggested_product_id = fields.Many2one(
        'product.product', string='Suggested Odoo Product', readonly=True,
        help="Automatic match by SKU attributes or barcode. Pick your own "
             "product in the next column if this is wrong.")
    product_id = fields.Many2one(
        'product.product', string='Odoo Product', index=True,
        help="Select the Odoo product variant that represents this Shopify variant.")
    mapping_id = fields.Many2one(
        'shopify.variant.mapping', string='Mapping', readonly=True, copy=False)
    state = fields.Selection(
        [('unmapped', 'Not Linked'), ('mapped', 'Linked')],
        string='Status', compute='_compute_state', store=True, index=True)
    display_name = fields.Char(compute='_compute_display_name')

    _sql_constraints = [
        (
            'store_variant_uniq',
            'UNIQUE(store_id, shopify_variant_id)',
            'This Shopify variant is already listed for this store.',
        ),
    ]

    @api.depends('shopify_product_title', 'shopify_color', 'shopify_size', 'sku')
    def _compute_display_name(self):
        for record in self:
            parts = [record.shopify_product_title or _('Shopify Product')]
            attrs = [value for value in (record.shopify_color, record.shopify_size) if value]
            if attrs:
                parts.append(' / '.join(attrs))
            elif record.shopify_variant_title and record.shopify_variant_title != 'Default Title':
                parts.append(record.shopify_variant_title)
            if record.sku:
                parts.append('[%s]' % record.sku)
            record.display_name = ' / '.join(parts)

    @api.depends('shopify_product_title')
    def _compute_model_code(self):
        for record in self:
            title = record.shopify_product_title or ''
            record.shopify_model_code = title.split('|')[-1].strip() if '|' in title else ''

    @api.depends('shopify_option1', 'shopify_option2', 'shopify_variant_title')
    def _compute_variant_attributes(self):
        for record in self:
            color = (record.shopify_option1 or '').strip()
            size = (record.shopify_option2 or '').strip()
            if (not color or not size) and record.shopify_variant_title:
                title = record.shopify_variant_title.strip()
                if title and title != 'Default Title' and '/' in title:
                    parts = [part.strip() for part in title.split('/') if part.strip()]
                    if not color and parts:
                        color = parts[0]
                    if not size and len(parts) > 1:
                        size = parts[1]
                elif not color and title and title != 'Default Title':
                    color = title
            record.shopify_color = color
            record.shopify_size = size

    @api.depends('mapping_id', 'product_id')
    def _compute_state(self):
        for record in self:
            record.state = 'mapped' if record.mapping_id else 'unmapped'

    def write(self, vals):
        res = super().write(vals)
        if 'product_id' in vals:
            self.filtered('product_id')._apply_mapping()
            self.filtered(lambda row: not row.product_id).mapping_id = False
        return res

    def _apply_mapping(self):
        mapping_model = self.env['shopify.variant.mapping']
        for record in self:
            if not record.product_id:
                continue
            mapping = mapping_model.upsert(
                record.store_id,
                record.shopify_variant_id,
                record.product_id,
                shopify_product_id=record.shopify_product_id,
                sku=record.sku,
                barcode=record.barcode,
                match_source='manual',
            )
            mapping.write_back_identifiers()
            if (record.store_id.write_sku_on_match and record.sku
                    and not record.product_id.default_code):
                record.product_id.sudo().default_code = record.sku
            record.mapping_id = mapping.id
        self._refresh_template_status()

    def _refresh_template_status(self):
        templates = self.env['shopify.product.mapping'].search([
            ('store_id', 'in', self.mapped('store_id').ids),
            ('shopify_product_id', 'in', self.mapped('shopify_product_id')),
        ])
        templates.refresh_connection_state()

    def action_open_fetch_wizard(self):
        return self.env['ir.actions.act_window']._for_xml_id(
            'zyra_shopify_connector.action_shopify_fetch_catalog_wizard')

    @api.model
    def fetch_for_store(self, store):
        client = store.get_client()
        sync = self.env['shopify.product.sync']
        mapping_model = self.env['shopify.variant.mapping']
        stats = {'fetched': 0, 'created': 0, 'updated': 0}

        product_mapping = self.env['shopify.product.mapping']
        for product_payload in client.iter_products(page_size=store.page_size or 50):
            stats['fetched'] += 1
            product_mapping.upsert_catalog(
                store, product_payload.get('id'), title=product_payload.get('title'))
            for variant in product_payload.get('variants') or []:
                variant_id = variant.get('id') and str(variant['id']) or None
                if not variant_id:
                    continue
                vals = {
                    'shopify_product_id': product_payload.get('id'),
                    'shopify_product_title': product_payload.get('title'),
                    'shopify_variant_title': variant.get('title'),
                    'shopify_option1': variant.get('option1'),
                    'shopify_option2': variant.get('option2'),
                    'shopify_option3': variant.get('option3'),
                    'sku': variant.get('sku'),
                    'barcode': variant.get('barcode'),
                }
                mapping = mapping_model.search([
                    ('store_id', '=', store.id),
                    ('shopify_variant_id', '=', variant_id),
                ], limit=1)
                if mapping:
                    vals.update({
                        'product_id': mapping.product_id.id,
                        'mapping_id': mapping.id,
                        'suggested_product_id': False,
                    })
                else:
                    suggested, _source, _conflict = sync._find_odoo_product(store, variant)
                    vals['suggested_product_id'] = suggested.id if suggested else False

                row = self.search([
                    ('store_id', '=', store.id),
                    ('shopify_variant_id', '=', variant_id),
                ], limit=1)
                if row:
                    row.write(vals)
                    stats['updated'] += 1
                else:
                    vals.update({
                        'store_id': store.id,
                        'shopify_variant_id': variant_id,
                    })
                    self.create(vals)
                    stats['created'] += 1

        templates = product_mapping.search([('store_id', '=', store.id)])
        templates.refresh_connection_state()
        self.env['shopify.sync.log'].log(
            store, 'info', 'product_sync',
            _("Fetched %(fetched)s Shopify product(s) into the manual catalog "
              "(%(created)s new, %(updated)s updated).", **stats))
        return stats

    def action_use_suggestion(self):
        for record in self.filtered(lambda row: row.suggested_product_id and not row.mapping_id):
            record.product_id = record.suggested_product_id
        return True
