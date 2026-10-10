from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestProductCatalog(ShopifyConnectorCase):

    def _product_payload(self):
        return {
            'id': '99000000000001',
            'title': 'Trail Runner Shoe',
            'variants': [{
                'id': '88000000000001',
                'product_id': '99000000000001',
                'title': 'Default Title',
                'sku': 'SKU-RUN-01',
                'barcode': '5901234123457',
            }],
        }

    def test_fetch_creates_catalog_rows(self):
        catalog = self.env['shopify.product.catalog']
        client = MagicMock()
        client.iter_products.return_value = iter([self._product_payload()])
        with patch.object(type(self.store), 'get_client', return_value=client):
            stats = catalog.fetch_for_store(self.store)

        self.assertEqual(stats['created'], 1)
        row = catalog.search([
            ('store_id', '=', self.store.id),
            ('shopify_variant_id', '=', '88000000000001'),
        ])
        self.assertEqual(row.sku, 'SKU-RUN-01')
        self.assertEqual(row.suggested_product_id, self.product_by_sku)

    def test_selecting_odoo_product_creates_mapping(self):
        row = self.env['shopify.product.catalog'].create({
            'store_id': self.store.id,
            'shopify_product_id': '99000000000001',
            'shopify_variant_id': '88000000000002',
            'shopify_product_title': 'Trail Runner Shoe',
            'shopify_variant_title': 'Default Title',
            'sku': 'SKU-RUN-01',
        })
        row.product_id = self.product_by_sku

        self.assertEqual(row.state, 'mapped')
        self.assertTrue(row.mapping_id)
        self.assertEqual(self.product_by_sku.shopify_variant_id, '88000000000002')

    def test_fetch_creates_template_rows_and_status(self):
        catalog = self.env['shopify.product.catalog']
        client = MagicMock()
        client.iter_products.return_value = iter([self._product_payload()])
        with patch.object(type(self.store), 'get_client', return_value=client):
            catalog.fetch_for_store(self.store)

        template = self.env['shopify.product.mapping'].search([
            ('store_id', '=', self.store.id),
            ('shopify_product_id', '=', '99000000000001'),
        ], limit=1)
        self.assertTrue(template)
        self.assertEqual(template.shopify_title, 'Trail Runner Shoe')
        self.assertEqual(template.connection_state, 'not_connected')
        self.assertEqual(template.variant_total_count, 1)
        self.assertEqual(template.variant_mapped_count, 0)

    def test_template_status_updates_when_variant_linked(self):
        catalog = self.env['shopify.product.catalog']
        client = MagicMock()
        client.iter_products.return_value = iter([self._product_payload()])
        with patch.object(type(self.store), 'get_client', return_value=client):
            catalog.fetch_for_store(self.store)

        template = self.env['shopify.product.mapping'].search([
            ('store_id', '=', self.store.id),
            ('shopify_product_id', '=', '99000000000001'),
        ], limit=1)
        template.product_tmpl_id = self.product_by_sku.product_tmpl_id
        row = catalog.search([
            ('store_id', '=', self.store.id),
            ('shopify_variant_id', '=', '88000000000001'),
        ], limit=1)
        row.product_id = self.product_by_sku

        self.assertEqual(template.connection_state, 'connected')
        self.assertEqual(template.variant_mapped_count, 1)

    def test_variant_title_is_split_into_color_and_size(self):
        row = self.env['shopify.product.catalog'].create({
            'store_id': self.store.id,
            'shopify_product_id': '99000000000001',
            'shopify_variant_id': '88000000000003',
            'shopify_product_title': 'Breezy Waterproof Unisex Clogs | TCG-6005',
            'shopify_variant_title': 'Beige / 10',
            'sku': 'TCG-6005-Beige-10',
        })
        self.assertEqual(row.shopify_color, 'Beige')
        self.assertEqual(row.shopify_size, '10')
        self.assertEqual(row.shopify_model_code, 'TCG-6005')
