from odoo.tests import tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestCustomerResolver(ShopifyConnectorCase):

    def setUp(self):
        super().setUp()
        self.resolver = self.env['shopify.customer.resolver']

    def test_deprecated_timezone_from_context_is_normalized(self):
        payload = self.order_payload()
        payload['customer']['id'] = '66000000000099'
        payload['customer']['email'] = 'tz.test@example.com'
        result = self.resolver.with_context(tz='Asia/Calcutta').resolve(self.store, payload)
        self.assertEqual(result['partner'].tz, 'Asia/Kolkata')

    def test_new_customer_is_created_and_mapped(self):
        payload = self.order_payload()
        result = self.resolver.resolve(self.store, payload)

        partner = result['partner']
        self.assertTrue(partner)
        self.assertEqual(partner.name, 'Ada Lovelace')
        self.assertEqual(partner.email, 'ada@example.com')
        self.assertEqual(partner.country_id.code, 'GB')
        self.assertFalse(result['is_guest'])

        mapping = self.env['shopify.customer.mapping'].search([
            ('store_id', '=', self.store.id),
            ('shopify_customer_id', '=', '66000000000001'),
        ])
        self.assertEqual(len(mapping), 1)
        self.assertEqual(mapping.partner_id, partner)
        self.assertEqual(partner.shopify_customer_id, '66000000000001')

    def test_existing_mapping_wins_over_everything_else(self):
        partner = self.env['res.partner'].create({'name': 'Already Known'})
        self.env['shopify.customer.mapping'].create({
            'store_id': self.store.id,
            'shopify_customer_id': '66000000000001',
            'partner_id': partner.id,
        })
        # A different email on the order must not create a second partner.
        payload = self.order_payload()
        payload['customer']['email'] = 'someone.else@example.com'

        result = self.resolver.resolve(self.store, payload)
        self.assertEqual(result['partner'], partner)

    def test_matched_by_email_does_not_duplicate(self):
        existing = self.env['res.partner'].create({
            'name': 'Ada L.', 'email': 'ADA@example.com'})
        payload = self.order_payload()
        payload['customer']['id'] = None

        result = self.resolver.resolve(self.store, payload)
        self.assertEqual(result['partner'], existing)

    def test_matched_by_phone_ignores_formatting(self):
        existing = self.env['res.partner'].create({
            'name': 'Ada by phone', 'phone': '02079460958'})
        payload = self.order_payload(customer={}, email=False)
        payload['phone'] = '+44 (0) 20 7946 0958'
        payload['billing_address']['phone'] = '+44 (0) 20 7946 0958'

        result = self.resolver.resolve(self.store, payload)
        self.assertEqual(result['partner'], existing)

    def test_email_takes_priority_over_phone(self):
        by_email = self.env['res.partner'].create({
            'name': 'Right one', 'email': 'ada@example.com'})
        self.env['res.partner'].create({
            'name': 'Wrong one', 'phone': '+44 20 7946 0958'})
        payload = self.order_payload()
        payload['customer']['id'] = None

        self.assertEqual(self.resolver.resolve(self.store, payload)['partner'], by_email)

    def test_guest_order_without_shopify_customer(self):
        payload = self.order_payload(customer={})
        result = self.resolver.resolve(self.store, payload)

        self.assertTrue(result['is_guest'])
        self.assertTrue(result['partner'])
        self.assertEqual(result['partner'].name, 'Ada Lovelace')
        self.assertFalse(result['mapping'])
        self.assertFalse(self.env['shopify.customer.mapping'].search([
            ('store_id', '=', self.store.id)]))

    def test_guest_order_with_no_identity_at_all_is_rejected(self):
        from ..models.shopify_customer_resolver import ShopifyCustomerResolutionError

        payload = self.order_payload(
            customer={}, email=None, phone=None,
            billing_address={}, shipping_address={})
        with self.assertRaises(ShopifyCustomerResolutionError):
            self.resolver.resolve(self.store, payload)

    def test_guest_fallback_partner_is_used_when_configured(self):
        fallback = self.env['res.partner'].create({'name': 'Shopify Guest'})
        self.store.guest_partner_id = fallback
        payload = self.order_payload(
            customer={}, email=None, phone=None,
            billing_address={}, shipping_address={})

        self.assertEqual(self.resolver.resolve(self.store, payload)['partner'], fallback)

    # -- addresses -------------------------------------------------------

    def test_same_billing_and_shipping_reuse_the_customer(self):
        result = self.resolver.resolve(self.store, self.order_payload())
        self.assertEqual(result['partner_invoice'], result['partner'])
        self.assertEqual(result['partner_shipping'], result['partner'])

    def test_different_shipping_address_creates_a_delivery_child(self):
        payload = self.order_payload(
            shipping_address=self.address(
                first_name='Charles', last_name='Babbage',
                name='Charles Babbage', address1='1 Difference Way',
                city='Cambridge', zip='CB2 1TN'))

        result = self.resolver.resolve(self.store, payload)
        shipping = result['partner_shipping']

        self.assertNotEqual(shipping, result['partner'])
        self.assertEqual(shipping.parent_id, result['partner'])
        self.assertEqual(shipping.type, 'delivery')
        self.assertEqual(shipping.shopify_address_type, 'shipping')
        self.assertEqual(shipping.street, '1 Difference Way')
        self.assertEqual(result['partner_invoice'], result['partner'])

    def test_repeated_import_reuses_the_same_address_child(self):
        payload = self.order_payload(
            shipping_address=self.address(address1='1 Difference Way', city='Cambridge'))

        first = self.resolver.resolve(self.store, payload)['partner_shipping']
        second = self.resolver.resolve(self.store, payload)['partner_shipping']

        self.assertEqual(first, second)
        self.assertEqual(len(first.parent_id.child_ids), 1)

    def test_existing_customer_fields_are_not_clobbered(self):
        partner = self.env['res.partner'].create({
            'name': 'Ada Lovelace',
            'email': 'ada@example.com',
            'phone': '+44 1234 000000',
            'ref': 'LEGACY-0042',
            'comment': 'Do not touch',
        })
        self.store.update_existing_customer = True
        self.resolver.resolve(self.store, self.order_payload())

        partner.invalidate_recordset()
        self.assertEqual(partner.ref, 'LEGACY-0042')
        self.assertIn('Do not touch', partner.comment)
        # The partner became Shopify-owned during this import, so Shopify's
        # phone number is allowed to win from the next import onwards.
        self.assertEqual(partner.shopify_customer_id, '66000000000001')

    def test_guest_order_never_overwrites_a_matched_customer(self):
        """A guest checkout is matched on one signal; it is not a correction."""
        registered = self.resolver.resolve(self.store, self.order_payload())['partner']
        self.assertEqual(registered.email, 'ada@example.com')

        guest = self.order_payload(customer={}, email='someone-elses-laptop@example.com')
        guest['email'] = 'someone-elses-laptop@example.com'
        result = self.resolver.resolve(self.store, guest)

        self.assertEqual(result['partner'], registered)
        registered.invalidate_recordset()
        self.assertEqual(registered.email, 'ada@example.com')

    def test_phone_key_is_stored_for_indexed_matching(self):
        partner = self.env['res.partner'].create({
            'name': 'Keyed', 'phone': '+44 (0) 20 7946 0958'})
        self.assertEqual(partner.shopify_phone_key, '079460958')

        partner.phone = '020 7946 0958'
        self.assertEqual(partner.shopify_phone_key, '079460958')

        partner.phone = '123'
        self.assertFalse(partner.shopify_phone_key)

    def test_non_shopify_customer_keeps_its_phone(self):
        partner = self.env['res.partner'].create({
            'name': 'Ada Lovelace',
            'email': 'ada@example.com',
            'phone': '+44 1234 000000',
        })
        payload = self.order_payload()
        payload['customer']['id'] = None

        self.resolver.resolve(self.store, payload)
        partner.invalidate_recordset()
        self.assertEqual(partner.phone, '+44 1234 000000')
