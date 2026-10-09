from odoo import fields
from odoo.tests import tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestOrderStatus(ShopifyConnectorCase):

    def order(self, **values):
        return self.env['shopify.order'].create(dict({
            'store_id': self.store.id, 'shopify_order_id': '9001', 'shopify_order_name': '#9001',
        }, **values))

    def test_status_follows_shopify(self):
        order = self.order(financial_status='partially_paid', fulfillment_status='partially_fulfilled')
        self.assertEqual(order.shopify_stage, 'partial')
        self.assertEqual(order.payment_status_label, 'Partially Paid')
        order.fulfillment_status = 'fulfilled'
        self.assertEqual(order.shopify_stage, 'fulfilled')
        order.shopify_cancelled_at = fields.Datetime.now()
        self.assertEqual(order.shopify_stage, 'cancelled')

    def test_missing_fulfillment_is_unfulfilled(self):
        self.assertEqual(self.order().shopify_stage, 'unfulfilled')
