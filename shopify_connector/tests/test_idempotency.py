"""Re-running the import must never duplicate anything."""

from unittest.mock import patch

from odoo.tests import tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestIdempotency(ShopifyConnectorCase):

    def _partner_count(self, email='ada@example.com'):
        return self.env['res.partner'].search_count([('email', '=', email)])

    def test_links_manual_sale_order_by_client_order_ref(self):
        partner = self.env['res.partner'].create({'name': 'Manual Customer'})
        manual = self.env['sale.order'].create({
            'partner_id': partner.id,
            'client_order_ref': '#1001',
        })
        record = self.import_payload(self.order_payload())
        self.assertEqual(record.sale_order_id, manual)
        self.assertEqual(record.sale_order_id.shopify_order_id, '55000000000001')
        self.assertEqual(self.env['sale.order'].search_count([
            ('client_order_ref', '=', '#1001'),
        ]), 1)

    def test_links_manual_sale_order_by_studio_order_id(self):
        if 'x_studio_order_id' not in self.env['sale.order']._fields:
            self.skipTest('Studio Order ID field not installed')
        partner = self.env['res.partner'].create({'name': 'Studio Customer'})
        shopify_id = '5599998888777666'
        self.env['shopify.order'].search([
            ('store_id', '=', self.store.id),
            ('shopify_order_id', '=', shopify_id),
        ]).unlink()
        self.env['sale.order'].search([
            ('shopify_order_id', '=', shopify_id),
        ]).unlink()
        self.env['sale.order'].search([
            ('x_studio_order_id', '=', shopify_id),
        ]).unlink()
        manual = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.store.company_id.id,
            'x_studio_order_id': shopify_id,
        })
        record = self.import_payload(self.order_payload(
            id=shopify_id, name='#STUDIO-LINK-TEST'))
        self.assertEqual(record.sale_order_id, manual)
        self.assertEqual(self.env['sale.order'].search_count([
            ('shopify_order_id', '=', shopify_id),
        ]), 1)

    def test_reimporting_the_same_order_changes_nothing(self):
        payload = self.order_payload()

        first = self.import_payload(payload)
        sale_order = first.sale_order_id
        partner = first.partner_id

        second = self.import_payload(payload)

        self.assertEqual(first, second)
        self.assertEqual(second.sale_order_id, sale_order)
        self.assertEqual(second.partner_id, partner)
        self.assertEqual(second.import_count, 2)

        self.assertEqual(self.env['shopify.order'].search_count([
            ('store_id', '=', self.store.id),
            ('shopify_order_id', '=', '55000000000001')]), 1)
        self.assertEqual(self.env['sale.order'].search_count([
            ('shopify_store_id', '=', self.store.id),
            ('shopify_order_id', '=', '55000000000001')]), 1)
        self.assertEqual(self._partner_count(), 1)
        self.assertEqual(len(sale_order.order_line), 1)

    def test_reimport_does_not_duplicate_order_lines(self):
        payload = self.order_payload(
            line_items=[
                self.line_item(id='11000000000001', variant_id='44000000000001'),
                self.line_item(id='11000000000002', variant_id='44000000000002',
                               sku='SKU-POLE-01', name='Hiking Pole',
                               price='40.00', quantity=1, current_quantity=1,
                               variant_barcode=False),
            ],
            shipping_lines=[{
                'id': '99000000000001', 'title': 'Standard',
                'price': '5.00', 'discounted_price': '5.00', 'tax_lines': []}],
        )

        first = self.import_payload(payload)
        self.assertEqual(len(first.sale_order_id.order_line), 3)

        second = self.import_payload(payload)
        self.assertEqual(len(second.sale_order_id.order_line), 3)
        self.assertEqual(len(second.line_ids), 2)

    def test_reimport_picks_up_changes_while_the_order_is_a_quotation(self):
        first = self.import_payload(self.order_payload())
        self.assertEqual(first.sale_order_id.order_line.product_uom_qty, 2)

        updated = self.order_payload(line_items=[
            self.line_item(quantity=5, current_quantity=5)])
        second = self.import_payload(updated)

        self.assertEqual(second.sale_order_id, first.sale_order_id)
        self.assertEqual(len(second.sale_order_id.order_line), 1)
        self.assertEqual(second.sale_order_id.order_line.product_uom_qty, 5)

    def test_customer_mapping_is_not_duplicated(self):
        self.import_payload(self.order_payload(id='55000000000001', name='#1001'))
        self.import_payload(self.order_payload(id='55000000000002', name='#1002'))

        mappings = self.env['shopify.customer.mapping'].search([
            ('store_id', '=', self.store.id),
            ('shopify_customer_id', '=', '66000000000001')])
        self.assertEqual(len(mappings), 1)
        self.assertEqual(self._partner_count(), 1)

    def test_guest_orders_reuse_the_partner_found_by_email(self):
        self.import_payload(self.order_payload(id='55000000000001', name='#1001', customer={}))
        self.import_payload(self.order_payload(id='55000000000002', name='#1002', customer={}))

        self.assertEqual(self._partner_count(), 1)
        # Guests produce no mapping row: there is no stable id to key on.
        self.assertFalse(self.env['shopify.customer.mapping'].search(
            [('store_id', '=', self.store.id)]))

    def test_variant_mapping_is_not_duplicated(self):
        self.import_payload(self.order_payload(id='55000000000001', name='#1001'))
        self.import_payload(self.order_payload(id='55000000000002', name='#1002'))

        mappings = self.env['shopify.variant.mapping'].search([
            ('store_id', '=', self.store.id),
            ('shopify_variant_id', '=', '44000000000001')])
        self.assertEqual(len(mappings), 1)

    def test_queueing_the_same_order_twice_refreshes_one_job(self):
        queue = self.env['shopify.sync.queue']
        payload = self.order_payload()

        first = queue.enqueue_order(self.store, payload)
        second = queue.enqueue_order(self.store, dict(payload, name='#1001-renamed'))

        self.assertEqual(first, second)
        self.assertEqual(second.shopify_order_name, '#1001-renamed')
        self.assertEqual(queue.search_count([
            ('store_id', '=', self.store.id),
            ('shopify_order_id', '=', '55000000000001')]), 1)

    def test_full_sync_run_twice_is_stable(self):
        payloads = [
            self.order_payload(id='55000000000001', name='#1001'),
            self.order_payload(id='55000000000002', name='#1002'),
        ]

        def fake_iter_orders(*_args, **_kwargs):
            return iter(payloads)

        with patch('odoo.addons.shopify_connector.services.shopify_client.'
                   'ShopifyClient.iter_orders', fake_iter_orders):
            first = self.store.sync_orders()
            second = self.store.sync_orders()

        self.assertEqual(first['fetched'], 2)
        self.assertEqual(first['done'], 2)
        self.assertEqual(second['done'], 2)

        self.assertEqual(self.env['shopify.order'].search_count(
            [('store_id', '=', self.store.id)]), 2)
        self.assertEqual(self.env['sale.order'].search_count(
            [('shopify_store_id', '=', self.store.id)]), 2)
        self.assertEqual(self._partner_count(), 1)
        self.assertTrue(self.store.last_order_sync_date)

    def test_sync_cursor_applies_the_overlap_window(self):
        from odoo import fields

        self.store.last_order_sync_date = '2026-01-10 12:00:00'
        self.store.sync_overlap_minutes = 10

        cursor = self.store._get_sync_cursor()
        self.assertEqual(fields.Datetime.to_string(cursor), '2026-01-10 11:50:00')
