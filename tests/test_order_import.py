from unittest.mock import patch

from odoo.tests import tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestOrderImport(ShopifyConnectorCase):

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_order_becomes_a_sale_order(self):
        record = self.import_payload(self.order_payload())

        self.assertEqual(record.state, 'imported')
        self.assertTrue(record.sale_order_id)

        sale_order = record.sale_order_id
        self.assertEqual(sale_order.partner_id, record.partner_id)
        self.assertEqual(sale_order.shopify_order_id, '55000000000001')
        self.assertEqual(sale_order.shopify_order_name, '#1001')
        self.assertEqual(sale_order.shopify_order_number, 1001)
        self.assertEqual(sale_order.shopify_payment_gateway, 'shopify_payments')
        self.assertEqual(sale_order.client_order_ref, '#1001')
        self.assertNotEqual(sale_order.name, 'SH/#1001')
        self.assertEqual(sale_order.warehouse_id, self.warehouse)
        self.assertEqual(len(sale_order.order_line), 1)

        line = sale_order.order_line
        self.assertEqual(line.product_id, self.product_by_sku)
        self.assertEqual(line.product_uom_qty, 2)
        self.assertEqual(line.price_unit, 100.0)
        self.assertEqual(line.shopify_line_type, 'product')
        self.assertEqual(line.shopify_sku, 'SKU-RUN-01')

        self.assertEqual(sale_order.amount_total, 200.0)
        self.assertTrue(record.is_reconciled)

    def test_custom_sale_order_prefix_is_optional(self):
        self.store.write({
            'use_custom_sale_order_prefix': True,
            'sale_order_prefix': 'SH/',
        })
        record = self.import_payload(self.order_payload(shopify_id='55000000000099'))

        self.assertEqual(record.sale_order_id.name, 'SH/#1001')

    def test_customer_is_resolved_before_the_sale_order_exists(self):
        """The partner must already exist when ``sale.order`` is created."""
        created_order_partners = []
        original_create = type(self.env['sale.order']).create

        def spy_create(self_model, vals_list):
            entries = vals_list if isinstance(vals_list, list) else [vals_list]
            created_order_partners.extend(vals.get('partner_id') for vals in entries)
            return original_create(self_model, vals_list)

        with patch.object(type(self.env['sale.order']), 'create', spy_create):
            record = self.import_payload(self.order_payload())

        self.assertTrue(created_order_partners)
        self.assertTrue(all(created_order_partners))
        self.assertEqual(created_order_partners[0], record.partner_id.id)

    def test_shopify_fields_are_captured_on_the_mirror(self):
        record = self.import_payload(self.order_payload())

        self.assertEqual(record.shopify_order_name, '#1001')
        self.assertEqual(record.financial_status, 'paid')
        self.assertEqual(record.fulfillment_status, 'unfulfilled')
        self.assertEqual(record.tags, 'web, priority')
        self.assertEqual(record.note, 'Leave at the door')
        self.assertEqual(record.payment_gateway, 'shopify_payments')
        self.assertIn('12 Analytical Street', record.billing_address)
        self.assertIn('London', record.shipping_address)
        self.assertEqual(record.currency_id, self.currency)
        self.assertTrue(record.payload)

    # ------------------------------------------------------------------
    # Discounts, shipping, multiple lines
    # ------------------------------------------------------------------

    def test_line_discount_is_kept_as_a_percentage(self):
        payload = self.order_payload(line_items=[
            self.line_item(price='100.00', quantity=2, current_quantity=2,
                           total_discount='20.00', discounted_unit_price='90.00'),
        ])
        record = self.import_payload(payload)

        line = record.sale_order_id.order_line
        self.assertEqual(line.price_unit, 100.0)
        self.assertEqual(line.discount, 10.0)
        self.assertEqual(line.price_subtotal, 180.0)
        self.assertTrue(record.is_reconciled)

    def test_repeating_discount_percentage_still_reconciles(self):
        # 1.00 off 30.00 is 3.3333...%; rounded to 3.33% Odoo still lands on
        # 29.00, so the discount stays visible as a percentage.
        payload = self.order_payload(line_items=[
            self.line_item(price='10.00', quantity=3, current_quantity=3,
                           total_discount='1.00', discounted_unit_price='9.67'),
        ])
        record = self.import_payload(payload)

        line = record.sale_order_id.order_line
        self.assertEqual(line.discount, 3.33)
        self.assertAlmostEqual(line.price_subtotal, 29.0, places=2)
        self.assertTrue(record.is_reconciled)

    def test_discount_falls_back_to_the_net_unit_price_when_percent_cannot_match(self):
        # 3.33 off 1000.00 is 0.333%, which rounds to 0.33% and would bill
        # 996.70 instead of 996.67. The net unit price is exact, so it wins.
        payload = self.order_payload(line_items=[
            self.line_item(price='1000.00', quantity=1, current_quantity=1,
                           total_discount='3.33', discounted_unit_price='996.67'),
        ])
        record = self.import_payload(payload)

        line = record.sale_order_id.order_line
        self.assertEqual(line.discount, 0.0)
        self.assertEqual(line.price_unit, 996.67)
        self.assertIn('Shopify discount', line.name)
        self.assertAlmostEqual(line.price_subtotal, 996.67, places=2)
        self.assertTrue(record.is_reconciled)

    def test_shipping_becomes_its_own_line(self):
        payload = self.order_payload(shipping_lines=[{
            'id': '99000000000001', 'title': 'Standard Shipping',
            'code': 'standard', 'source': 'shopify',
            'price': '12.50', 'discounted_price': '12.50', 'tax_lines': [],
        }])
        record = self.import_payload(payload)

        shipping_lines = record.sale_order_id.order_line.filtered(
            lambda line: line.shopify_line_type == 'shipping')
        self.assertEqual(len(shipping_lines), 1)
        self.assertEqual(shipping_lines.product_id, self.shipping_product)
        self.assertEqual(shipping_lines.name, 'Standard Shipping')
        self.assertEqual(shipping_lines.price_unit, 12.5)
        self.assertEqual(record.sale_order_id.amount_total, 212.5)
        self.assertTrue(record.is_reconciled)

    def test_free_shipping_line_is_skipped(self):
        payload = self.order_payload(shipping_lines=[{
            'id': '99000000000002', 'title': 'Free Shipping',
            'price': '0.00', 'discounted_price': '0.00', 'tax_lines': [],
        }])
        record = self.import_payload(payload)
        self.assertFalse(record.sale_order_id.order_line.filtered(
            lambda line: line.shopify_line_type == 'shipping'))

    def test_multiple_lines_and_refunded_quantity(self):
        payload = self.order_payload(line_items=[
            self.line_item(id='11000000000001', variant_id='44000000000001'),
            self.line_item(id='11000000000002', variant_id='44000000000002',
                           sku='SKU-POLE-01', name='Hiking Pole',
                           price='40.00', quantity=1, current_quantity=0,
                           variant_barcode=False, total_discount='0.00'),
        ])
        record = self.import_payload(payload)

        # Both lines are mirrored for audit, but the fully-refunded one is not
        # put on the sale order.
        self.assertEqual(len(record.line_ids), 2)
        self.assertEqual(len(record.sale_order_id.order_line), 1)
        self.assertTrue(all(line.resolution_state == 'matched' for line in record.line_ids))

    # ------------------------------------------------------------------
    # Product mapping failures
    # ------------------------------------------------------------------

    def test_unmatched_product_blocks_the_sale_order(self):
        payload = self.order_payload(line_items=[
            self.line_item(variant_id='44999999999999', sku='GHOST-SKU',
                           variant_barcode=False, product_id='77999999999999'),
        ])
        record = self.import_payload(payload)

        self.assertEqual(record.state, 'needs_review')
        self.assertFalse(record.sale_order_id)
        self.assertIn('GHOST-SKU', record.review_reason)
        self.assertEqual(record.line_ids.resolution_state, 'unmatched')

    def test_one_unmatched_line_blocks_the_whole_order(self):
        payload = self.order_payload(line_items=[
            self.line_item(),
            self.line_item(id='11000000000002', variant_id='44999999999999',
                           sku='GHOST-SKU', variant_barcode=False,
                           product_id='77999999999999'),
        ])
        record = self.import_payload(payload)

        self.assertEqual(record.state, 'needs_review')
        self.assertFalse(record.sale_order_id)
        self.assertEqual(record.unmatched_line_count, 1)

    def test_mapping_the_variant_then_retrying_succeeds(self):
        payload = self.order_payload(line_items=[
            self.line_item(variant_id='44999999999999', sku='GHOST-SKU',
                           variant_barcode=False, product_id='77999999999999'),
        ])
        record = self.import_payload(payload)
        self.assertEqual(record.state, 'needs_review')

        self.env['shopify.variant.mapping'].create({
            'store_id': self.store.id,
            'shopify_variant_id': '44999999999999',
            'product_id': self.product_by_sku.id,
        })
        record.action_retry_import()

        self.assertEqual(record.state, 'imported')
        self.assertTrue(record.sale_order_id)

    # ------------------------------------------------------------------
    # Reconciliation, cancellation, isolation
    # ------------------------------------------------------------------

    def test_total_mismatch_flags_needs_review_but_keeps_the_order(self):
        payload = self.order_payload()
        payload['total_price'] = '999.00'
        record = self.import_payload(payload)

        self.assertEqual(record.state, 'needs_review')
        self.assertTrue(record.sale_order_id)
        self.assertIn('do not reconcile', record.review_reason)
        self.assertFalse(record.is_reconciled)

    def test_cancelled_order_cancels_the_draft_quotation(self):
        record = self.import_payload(self.order_payload())
        sale_order = record.sale_order_id

        cancelled = self.order_payload()
        cancelled['cancelled_at'] = '2026-01-06T09:00:00Z'
        record = self.import_payload(cancelled)

        self.assertEqual(record.state, 'cancelled')
        self.assertEqual(sale_order.state, 'cancel')

    def test_confirmed_order_is_not_rewritten(self):
        record = self.import_payload(self.order_payload())
        sale_order = record.sale_order_id
        sale_order.action_confirm()
        original_line_count = len(sale_order.order_line)

        updated = self.order_payload(line_items=[
            self.line_item(quantity=99, current_quantity=99)])
        updated['fulfillment_status'] = 'fulfilled'
        self.import_payload(updated)

        self.assertEqual(sale_order.state, 'sale')
        self.assertEqual(len(sale_order.order_line), original_line_count)
        self.assertEqual(sale_order.order_line[0].product_uom_qty, 2)
        self.assertEqual(sale_order.shopify_fulfillment_status, 'fulfilled')

    def test_a_failing_order_does_not_stop_the_others(self):
        good_one = self.order_payload(id='55000000000001', name='#1001')
        broken = self.order_payload(id='55000000000002', name='#1002')
        good_two = self.order_payload(id='55000000000003', name='#1003')

        queue = self.env['shopify.sync.queue']
        jobs = queue.browse()
        for payload in (good_one, broken, good_two):
            jobs |= queue.enqueue_order(self.store, payload)

        original_resolve = type(self.env['shopify.customer.resolver']).resolve

        def explode_on_1002(self_model, store, payload):
            if payload.get('name') == '#1002':
                raise ValueError('Simulated Shopify data corruption')
            return original_resolve(self_model, store, payload)

        with patch.object(type(self.env['shopify.customer.resolver']), 'resolve', explode_on_1002):
            summary = jobs.process()

        self.assertEqual(summary['done'], 2)
        self.assertEqual(summary['failed'], 1)

        failed_job = jobs.filtered(lambda job: job.shopify_order_name == '#1002')
        self.assertEqual(failed_job.state, 'pending')  # retried later
        self.assertIn('Simulated Shopify data corruption', failed_job.error_message)

        imported = self.env['shopify.order'].search([
            ('store_id', '=', self.store.id), ('state', '=', 'imported')])
        self.assertEqual(set(imported.mapped('shopify_order_name')), {'#1001', '#1003'})

    def test_failure_is_logged_against_the_store(self):
        jobs = self.env['shopify.sync.queue'].enqueue_order(
            self.store, self.order_payload())

        with patch.object(type(self.env['shopify.customer.resolver']), 'resolve',
                          side_effect=ValueError('boom')):
            jobs.process()

        logs = self.env['shopify.sync.log'].search([
            ('store_id', '=', self.store.id), ('level', '=', 'error')])
        self.assertTrue(logs)
        self.assertIn('boom', logs[0].message)

    def test_auto_confirm_respects_needs_review(self):
        self.store.auto_confirm_order = True

        record = self.import_payload(self.order_payload())
        self.assertEqual(record.sale_order_id.state, 'sale')

        mismatched = self.order_payload(id='55000000000002', name='#1002')
        mismatched['total_price'] = '999.00'
        record = self.import_payload(mismatched)
        self.assertEqual(record.state, 'needs_review')
        self.assertEqual(record.sale_order_id.state, 'draft')


@tagged('post_install', '-at_install', 'shopify')
class TestOrderImportTaxes(ShopifyConnectorCase):

    def setUp(self):
        super().setUp()
        self.store.tax_mode = 'shopify'
        self.tax_group = self.env['account.tax.group'].search(
            [('company_id', '=', self.company.id)], limit=1)
        if not self.tax_group:
            self.tax_group = self.env['account.tax.group'].create({
                'name': 'Shopify Test Group', 'company_id': self.company.id})

    def _tax_line(self, rate, price, title='VAT'):
        return {'title': title, 'rate': rate, 'price': price}

    def test_tax_excluded_order_creates_and_applies_a_tax(self):
        payload = self.order_payload(line_items=[
            self.line_item(price='100.00', quantity=2, current_quantity=2,
                           tax_lines=[self._tax_line(0.2, '40.00')]),
        ])
        payload['total_tax'] = '40.00'
        payload['total_price'] = '240.00'

        record = self.import_payload(payload)

        line = record.sale_order_id.order_line
        self.assertEqual(len(line.tax_id), 1)
        self.assertEqual(line.tax_id.amount, 20.0)
        self.assertFalse(line.tax_id.price_include)
        self.assertEqual(record.sale_order_id.amount_total, 240.0)
        self.assertTrue(record.is_reconciled)

    def test_tax_included_order_keeps_the_shopify_total(self):
        payload = self.order_payload(line_items=[
            self.line_item(price='120.00', quantity=1, current_quantity=1,
                           tax_lines=[self._tax_line(0.2, '20.00')]),
        ])
        payload['taxes_included'] = True
        payload['total_tax'] = '20.00'
        payload['subtotal_price'] = '120.00'
        payload['total_price'] = '120.00'

        record = self.import_payload(payload)

        line = record.sale_order_id.order_line
        self.assertTrue(line.tax_id.price_include)
        self.assertEqual(record.sale_order_id.amount_total, 120.0)
        self.assertAlmostEqual(record.sale_order_id.amount_tax, 20.0, places=2)
        self.assertTrue(record.is_reconciled)

    def test_an_existing_odoo_tax_is_reused(self):
        existing = self.env['account.tax'].create({
            'name': 'Existing VAT 20%',
            'amount': 20.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
            'price_include_override': 'tax_excluded',
            'tax_group_id': self.tax_group.id,
        })
        payload = self.order_payload(line_items=[
            self.line_item(price='100.00', quantity=2, current_quantity=2,
                           tax_lines=[self._tax_line(0.2, '40.00')]),
        ])
        payload['total_tax'] = '40.00'
        payload['total_price'] = '240.00'

        record = self.import_payload(payload)
        self.assertEqual(record.sale_order_id.order_line.tax_id, existing)

    def test_tax_mode_none_strips_product_taxes(self):
        self.store.tax_mode = 'none'
        tax = self.env['account.tax'].create({
            'name': 'Default sale tax',
            'amount': 20.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
            'tax_group_id': self.tax_group.id,
        })
        self.product_by_sku.taxes_id = tax

        record = self.import_payload(self.order_payload())
        self.assertFalse(record.sale_order_id.order_line.tax_id)
        self.assertEqual(record.sale_order_id.amount_total, 200.0)
