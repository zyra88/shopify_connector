from odoo.tests import tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestProductResolver(ShopifyConnectorCase):

    def setUp(self):
        super().setUp()
        self.resolver = self.env['shopify.product.resolver']

    def test_existing_mapping_is_used_first(self):
        other = self.env['product.product'].create({'name': 'Mapped explicitly', 'type': 'consu'})
        self.env['shopify.variant.mapping'].create({
            'store_id': self.store.id,
            'shopify_variant_id': '44000000000001',
            'product_id': other.id,
        })
        # SKU would point at a different product; the mapping must win.
        product, source, message = self.resolver.resolve(self.store, self.line_item())

        self.assertEqual(product, other)
        self.assertEqual(source, 'mapping')
        self.assertFalse(message)

    def test_match_by_shopify_variant_id_on_the_product(self):
        line = self.line_item(variant_id='44000000000002', sku=False, variant_barcode=False)
        product, source, _message = self.resolver.resolve(self.store, line)

        self.assertEqual(product, self.product_by_variant_id)
        self.assertEqual(source, 'variant_id')

    def test_match_by_sku(self):
        line = self.line_item(variant_id='44999999999999', variant_barcode=False)
        product, source, _message = self.resolver.resolve(self.store, line)

        self.assertEqual(product, self.product_by_sku)
        self.assertEqual(source, 'sku')

    def test_match_by_barcode_when_sku_is_absent(self):
        line = self.line_item(variant_id='44999999999999', sku=False)
        product, source, _message = self.resolver.resolve(self.store, line)

        self.assertEqual(product, self.product_by_sku)
        self.assertEqual(source, 'barcode')

    def test_ambiguous_sku_is_reported_not_guessed(self):
        self.env['product.product'].create({
            'name': 'Duplicate reference', 'default_code': 'SKU-RUN-01', 'type': 'consu'})
        line = self.line_item(variant_id='44999999999999', variant_barcode=False)

        product, source, message = self.resolver.resolve(self.store, line)
        self.assertFalse(product)
        self.assertFalse(source)
        self.assertIn('more than one', message)

    def test_never_matches_by_name(self):
        line = self.line_item(
            name='Trail Runner Shoe', title='Trail Runner Shoe',
            variant_id='44999999999999', sku=False, variant_barcode=False,
            product_id='77999999999999')
        product, _source, message = self.resolver.resolve(self.store, line)

        self.assertFalse(product)
        self.assertIn('No Odoo product found', message)

    def test_unmatched_message_names_the_identifiers_tried(self):
        line = self.line_item(
            variant_id='44999999999999', sku='UNKNOWN-SKU',
            variant_barcode='0000000000000', product_id='77999999999999')
        _product, _source, message = self.resolver.resolve(self.store, line)

        self.assertIn('44999999999999', message)
        self.assertIn('UNKNOWN-SKU', message)
        self.assertIn('0000000000000', message)

    def test_sku_matching_can_be_switched_off(self):
        self.store.match_product_by_sku = False
        self.store.match_product_by_barcode = False
        line = self.line_item(variant_id='44999999999999')

        product, _source, message = self.resolver.resolve(self.store, line)
        self.assertFalse(product)
        self.assertTrue(message)

    # -- mapping write-back ------------------------------------------------

    def test_a_sku_match_is_remembered_as_a_mapping(self):
        line = self.line_item(variant_id='44000000000007', variant_barcode=False)
        product, source, _message = self.resolver.resolve(self.store, line)
        mapping = self.resolver.remember_match(self.store, line, product, source)

        self.assertEqual(mapping.product_id, self.product_by_sku)
        self.assertEqual(mapping.match_source, 'sku')
        self.assertEqual(mapping.shopify_variant_id, '44000000000007')
        # The identifiers are mirrored onto the product, barcode-style.
        self.assertEqual(self.product_by_sku.shopify_variant_id, '44000000000007')
        self.assertEqual(
            self.product_by_sku.product_tmpl_id.shopify_product_id, '77000000000001')

    def test_remember_match_never_repoints_an_existing_mapping(self):
        other = self.env['product.product'].create({'name': 'Other', 'type': 'consu'})
        mapping = self.env['shopify.variant.mapping'].create({
            'store_id': self.store.id,
            'shopify_variant_id': '44000000000001',
            'product_id': other.id,
        })
        self.resolver.remember_match(
            self.store, self.line_item(), self.product_by_sku, 'sku')

        self.assertEqual(mapping.product_id, other)

    def test_variantless_line_falls_back_to_the_shopify_product_id(self):
        template = self.product_by_sku.product_tmpl_id
        template.shopify_product_id = '77000000000123'
        line = self.line_item(
            variant_id=False, product_id='77000000000123',
            sku=False, variant_barcode=False)

        product, source, _message = self.resolver.resolve(self.store, line)
        self.assertEqual(product, self.product_by_sku)
        self.assertEqual(source, 'product_id')

    def test_unknown_variant_does_not_fall_back_to_the_template(self):
        """An unknown variant means Shopify has one Odoo does not.

        Falling back to the template's only variant would quietly sell the
        wrong item, so the line stays unmatched.
        """
        template = self.product_by_sku.product_tmpl_id
        template.shopify_product_id = '77000000000123'
        line = self.line_item(
            variant_id='44999999999999', product_id='77000000000123',
            sku=False, variant_barcode=False)

        product, _source, message = self.resolver.resolve(self.store, line)
        self.assertFalse(product)
        self.assertIn('44999999999999', message)
