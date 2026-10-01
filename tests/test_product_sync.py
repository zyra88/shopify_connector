from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestProductSync(ShopifyConnectorCase):

    def _product_payload(self, **overrides):
        payload = {
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
        payload.update(overrides)
        return payload

    def _mock_client(self, products):
        client = MagicMock()
        client.iter_products.side_effect = lambda **_kwargs: iter(products)
        return client

    def test_sync_maps_shopify_ids_by_sku(self):
        product = self.product_by_sku
        self.assertFalse(product.shopify_variant_id)

        with patch.object(type(self.store), 'get_client',
                          return_value=self._mock_client([self._product_payload()])):
            stats = self.store.sync_products()

        self.assertEqual(stats['mapped'], 1)
        self.assertEqual(product.shopify_variant_id, '88000000000001')
        self.assertEqual(product.product_tmpl_id.shopify_product_id, '99000000000001')

        mapping = self.env['shopify.variant.mapping'].search([
            ('store_id', '=', self.store.id),
            ('shopify_variant_id', '=', '88000000000001'),
        ], limit=1)
        self.assertEqual(mapping.product_id, product)
        self.assertEqual(mapping.match_source, 'sku')

    def test_second_sync_counts_as_updated(self):
        with patch.object(type(self.store), 'get_client',
                          return_value=self._mock_client([self._product_payload()])):
            self.store.sync_products()
            stats = self.store.sync_products()

        self.assertEqual(stats['updated'], 1)
        self.assertEqual(stats['mapped'], 0)

    def test_unmatched_variant_when_sku_is_missing_in_odoo(self):
        payload = self._product_payload(variants=[{
            'id': '88000000000002',
            'product_id': '99000000000001',
            'title': 'Ghost',
            'sku': 'GHOST-SKU',
            'barcode': None,
        }])
        with patch.object(type(self.store), 'get_client',
                          return_value=self._mock_client([payload])):
            stats = self.store.sync_products()

        self.assertEqual(stats['unmatched'], 1)

    def test_sync_maps_by_sku_attributes_when_default_code_is_empty(self):
        color_attr = self.env['product.attribute'].create({'name': 'Color'})
        size_attr = self.env['product.attribute'].create({'name': 'Size'})
        black = self.env['product.attribute.value'].create({
            'name': 'Black', 'attribute_id': color_attr.id,
        })
        size_6 = self.env['product.attribute.value'].create({
            'name': '6', 'attribute_id': size_attr.id,
        })
        template = self.env['product.template'].create({
            'name': 'Connector Attribute Test | ZZZ-ATTR-01',
            'type': 'consu',
            'attribute_line_ids': [
                (0, 0, {'attribute_id': color_attr.id, 'value_ids': [(6, 0, [black.id])]}),
                (0, 0, {'attribute_id': size_attr.id, 'value_ids': [(6, 0, [size_6.id])]}),
            ],
        })
        product = template.product_variant_ids
        self.assertEqual(len(product), 1)
        payload = self._product_payload(variants=[{
            'id': '88000000000099',
            'product_id': '99000000000001',
            'title': 'Black / 6',
            'sku': 'ZZZ-ATTR-01-Black-6',
            'barcode': '5061999999999',
        }])
        with patch.object(type(self.store), 'get_client',
                          return_value=self._mock_client([payload])):
            stats = self.store.sync_products()

        self.assertEqual(stats['mapped'], 1)
        self.assertEqual(product.shopify_variant_id, '88000000000099')
        self.assertEqual(product.default_code, 'ZZZ-ATTR-01-Black-6')

    def test_conflict_when_product_already_has_another_variant_id(self):
        product = self.product_by_sku
        product.shopify_variant_id = '77000000000001'
        with patch.object(type(self.store), 'get_client',
                          return_value=self._mock_client([self._product_payload()])):
            stats = self.store.sync_products()

        self.assertEqual(stats['conflicts'], 1)
        self.assertEqual(product.shopify_variant_id, '77000000000001')
