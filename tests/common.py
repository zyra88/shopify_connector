"""Shared fixtures and Shopify payload builders for the connector tests.

Payloads are built in the *normalised* shape the client emits, so the tests
exercise the importer without going near the network.
"""

from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class ShopifyConnectorCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.currency = cls.company.currency_id

        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1)
        cls.store = cls.env['shopify.store'].create({
            'name': 'Test Shopify Store',
            'shop_domain': 'test-store.myshopify.com',
            'client_id': 'test-client-id',
            'client_secret': 'test-client-secret',
            'access_token': 'shpat_testtoken',
            'access_token_expires_at': fields.Datetime.now() + timedelta(days=1),
            'api_version': '2026-07',
            'company_id': cls.company.id,
            'currency_id': cls.currency.id,
            'warehouse_id': cls.warehouse.id,
            # Most tests are about resolution and idempotency, not accounting:
            # taxes are covered explicitly in their own test.
            'tax_mode': 'none',
            'connection_state': 'connected',
        })

        cls.product_by_sku = cls.env['product.product'].create({
            'name': 'Trail Runner Shoe',
            'default_code': 'SKU-RUN-01',
            'barcode': '5901234123457',
            'list_price': 100.0,
            'type': 'consu',
            'taxes_id': [(5, 0, 0)],
        })
        cls.product_by_variant_id = cls.env['product.product'].create({
            'name': 'Hiking Pole',
            'default_code': 'SKU-POLE-01',
            'shopify_variant_id': '44000000000002',
            'list_price': 40.0,
            'type': 'consu',
            'taxes_id': [(5, 0, 0)],
        })
        cls.shipping_product = cls.env['product.product'].create({
            'name': 'Shopify Shipping',
            'type': 'service',
            'invoice_policy': 'order',
            'taxes_id': [(5, 0, 0)],
        })
        cls.store.shipping_product_id = cls.shipping_product

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------

    @classmethod
    def address(cls, **overrides):
        address = {
            'first_name': 'Ada',
            'last_name': 'Lovelace',
            'name': 'Ada Lovelace',
            'company': False,
            'address1': '12 Analytical Street',
            'address2': False,
            'city': 'London',
            'province': False,
            'province_code': False,
            'zip': 'EC1A 1BB',
            'country': 'United Kingdom',
            'country_code': 'GB',
            'phone': '+44 20 7946 0958',
        }
        address.update(overrides)
        return address

    @classmethod
    def line_item(cls, **overrides):
        line = {
            'id': '11000000000001',
            'name': 'Trail Runner Shoe',
            'title': 'Trail Runner Shoe',
            'variant_title': False,
            'sku': 'SKU-RUN-01',
            'vendor': 'Trekiva',
            'quantity': 2,
            'current_quantity': 2,
            'requires_shipping': True,
            'taxable': True,
            'price': '100.00',
            'discounted_unit_price': '100.00',
            'total_discount': '0.00',
            'product_id': '77000000000001',
            'variant_id': '44000000000001',
            'variant_barcode': '5901234123457',
            'tax_lines': [],
        }
        line.update(overrides)
        return line

    @classmethod
    def order_payload(cls, **overrides):
        """A single-line, tax-free, shipping-free order that reconciles."""
        line_items = overrides.pop('line_items', [cls.line_item()])
        shipping_lines = overrides.pop('shipping_lines', [])
        customer = overrides.pop('customer', {
            'id': '66000000000001',
            'first_name': 'Ada',
            'last_name': 'Lovelace',
            'email': 'ada@example.com',
            'phone': '+44 20 7946 0958',
            'tags': 'vip',
            'note': False,
            'default_address': cls.address(),
        })

        subtotal = sum(
            float(line['price']) * line['quantity'] - float(line['total_discount'])
            for line in line_items)
        shipping = sum(float(ship.get('discounted_price', ship['price'])) for ship in shipping_lines)
        discounts = sum(float(line['total_discount']) for line in line_items)

        payload = {
            'id': '55000000000001',
            'admin_graphql_api_id': 'gid://shopify/Order/55000000000001',
            'name': '#1001',
            'order_number': 1001,
            'created_at': '2026-01-05T10:00:00Z',
            'updated_at': '2026-01-05T10:05:00Z',
            'processed_at': '2026-01-05T10:00:00Z',
            'cancelled_at': None,
            'closed_at': None,
            'currency': cls.currency.name,
            'presentment_currency': cls.currency.name,
            'financial_status': 'paid',
            'fulfillment_status': None,
            'taxes_included': False,
            'tax_exempt': False,
            'test': False,
            'note': 'Leave at the door',
            'tags': 'web, priority',
            'email': 'ada@example.com',
            'phone': '+44 20 7946 0958',
            'gateway': 'shopify_payments',
            'payment_gateway_names': ['shopify_payments'],
            'discount_codes': [],
            'subtotal_price': '%.2f' % subtotal,
            'total_discounts': '%.2f' % discounts,
            'total_shipping_price': '%.2f' % shipping,
            'total_tax': '0.00',
            'total_price': '%.2f' % (subtotal + shipping),
            'customer': customer,
            'billing_address': cls.address(),
            'shipping_address': cls.address(),
            'tax_lines': [],
            'line_items': line_items,
            'shipping_lines': shipping_lines,
            'transactions': [],
        }
        payload.update(overrides)
        return payload

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def import_payload(self, payload, store=None):
        """Run one payload through the queue, as a real sync would."""
        store = store or self.store
        job = self.env['shopify.sync.queue'].enqueue_order(store, payload)
        job.process()
        return self.env['shopify.order'].search([
            ('store_id', '=', store.id),
            ('shopify_order_id', '=', str(payload['id'])),
        ], limit=1)
