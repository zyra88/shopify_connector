from odoo.tests import tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestPaymentTermResolver(ShopifyConnectorCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.term_cod = cls._ensure_payment_term('COD')
        cls.term_prepaid = cls._ensure_payment_term('Prepaid')
        cls.resolver = cls.env['shopify.payment.term.resolver']

    @classmethod
    def _ensure_payment_term(cls, name):
        term = cls.env['account.payment.term'].search([('name', '=ilike', name)], limit=1)
        if term:
            return term
        return cls.env['account.payment.term'].create({
            'name': name,
            'line_ids': [(0, 0, {'value': 'percent', 'value_amount': 100.0, 'nb_days': 0})],
        })

    def test_cod_gateway_maps_to_cod_term(self):
        term = self.resolver.resolve(self.store, 'Cash on Delivery (COD)', 'pending')
        self.assertEqual(term, self.term_cod)

    def test_razorpay_gateway_maps_to_prepaid_term(self):
        term = self.resolver.resolve(
            self.store,
            '01 Cards, UPI, NB, Wallets by Razorpay',
            'paid',
        )
        self.assertEqual(term, self.term_prepaid)

    def test_shopify_payments_maps_to_prepaid_term(self):
        term = self.resolver.resolve(self.store, 'shopify_payments', 'paid')
        self.assertEqual(term, self.term_prepaid)

    def test_paid_online_order_maps_to_prepaid_term(self):
        term = self.resolver.resolve(self.store, 'Some Online Gateway', 'paid')
        self.assertEqual(term, self.term_prepaid)

    def test_sale_order_gets_payment_term_from_gateway(self):
        payload = self.order_payload(payment_gateway_names=['Cash on Delivery (COD)'],
                                     financial_status='pending')
        record = self.import_payload(payload)
        self.assertEqual(record.sale_order_id.payment_term_id, self.term_cod)

    def test_store_default_is_used_when_gateway_is_unknown(self):
        fallback = self._ensure_payment_term('Connector Fallback')
        self.store.payment_term_id = fallback
        term = self.resolver.resolve(self.store, 'Mystery Gateway', 'pending')
        self.assertEqual(term, fallback)
