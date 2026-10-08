"""Duplicate manual vs imported sale order detection and resolution."""

from odoo.tests import tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestOrderConflict(ShopifyConnectorCase):

    def test_scan_finds_studio_order_id_duplicate(self):
        if 'x_studio_order_id' not in self.env['sale.order']._fields:
            self.skipTest('Studio Order ID field not installed')

        Conflict = self.env['shopify.sale.order.conflict']
        shopify_id = '5599998888777665'
        partner = self.env['res.partner'].create({'name': 'Dup Customer'})

        manual = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.store.company_id.id,
            'x_studio_order_id': shopify_id,
        })
        imported = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.store.company_id.id,
            'shopify_store_id': self.store.id,
            'shopify_order_id': shopify_id,
            'shopify_order_name': '#DUP-TEST',
        })
        mirror = self.env['shopify.order'].create({
            'store_id': self.store.id,
            'shopify_order_id': shopify_id,
            'shopify_order_name': '#DUP-TEST',
            'sale_order_id': imported.id,
            'payload': '{}',
        })

        Conflict.with_context(
            default_store_id=self.store.id,
            active_model='shopify.store',
            active_id=self.store.id,
        ).action_scan_duplicates()

        conflict = Conflict.search([
            ('store_id', '=', self.store.id),
            ('shopify_order_id', '=', shopify_id),
        ])
        self.assertEqual(len(conflict), 1)
        self.assertEqual(conflict.manual_sale_order_id, manual)
        self.assertEqual(conflict.imported_sale_order_id, imported)
        self.assertEqual(conflict.match_reason, 'studio_order_id')

        conflict.action_keep_manual_order()
        self.assertEqual(conflict.state, 'resolved')
        self.assertEqual(mirror.sale_order_id, manual)
        self.assertFalse(imported.shopify_order_id)
        self.assertEqual(imported.state, 'cancel')

    def test_keep_imported_leaves_confirmed_manual_open(self):
        if 'x_studio_order_id' not in self.env['sale.order']._fields:
            self.skipTest('Studio Order ID field not installed')

        Conflict = self.env['shopify.sale.order.conflict']
        shopify_id = '5599998888777666'
        partner = self.env['res.partner'].create({'name': 'Dup Customer 2'})

        manual = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.store.company_id.id,
            'x_studio_order_id': 'TR-001270-LUAC0001601959',
        })
        manual.action_confirm()
        imported = self.env['sale.order'].create({
            'partner_id': partner.id,
            'company_id': self.store.company_id.id,
            'shopify_store_id': self.store.id,
            'shopify_order_id': shopify_id,
            'shopify_order_name': 'TR-001270',
        })
        mirror = self.env['shopify.order'].create({
            'store_id': self.store.id,
            'shopify_order_id': shopify_id,
            'shopify_order_name': 'TR-001270',
            'sale_order_id': imported.id,
            'payload': '{}',
        })
        conflict = Conflict.create({
            'store_id': self.store.id,
            'shopify_order_id': shopify_id,
            'shopify_order_name': 'TR-001270',
            'shopify_mirror_id': mirror.id,
            'match_reason': 'shopify_order_ref',
            'manual_sale_order_id': manual.id,
            'imported_sale_order_id': imported.id,
        })

        conflict.action_keep_imported_order()
        self.assertEqual(conflict.state, 'resolved')
        self.assertEqual(mirror.sale_order_id, imported)
        self.assertFalse(manual.x_studio_order_id)
        self.assertEqual(manual.state, 'sale')
