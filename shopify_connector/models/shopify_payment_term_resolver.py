from odoo import api, models


class ShopifyPaymentTermResolver(models.AbstractModel):
    """Map Shopify payment gateways to Odoo payment terms."""

    _name = 'shopify.payment.term.resolver'
    _description = 'Shopify Payment Term Resolver'

    @api.model
    def resolve(self, store, payment_gateway, financial_status=None):
        """Return the ``account.payment.term`` for a Shopify order."""
        term_name = self._gateway_to_term_name(payment_gateway, financial_status)
        if term_name:
            term = self._find_payment_term(term_name)
            if term:
                return term
        return store.payment_term_id or self.env['account.payment.term']

    @api.model
    def _gateway_to_term_name(self, payment_gateway, financial_status):
        gateway = (payment_gateway or '').strip().lower()
        financial = (financial_status or '').lower()

        # Cash on delivery — payment is collected on delivery, not upfront.
        if gateway and ('cod' in gateway or 'cash on delivery' in gateway):
            return 'COD'

        # Online payment: paid at checkout, or an online gateway (Razorpay, UPI, etc.).
        if financial == 'paid':
            return 'Prepaid'
        if gateway and any(marker in gateway for marker in (
            'razorpay', 'shopify_payments', 'prepaid', 'cards', 'upi',
            'wallet', ' nb', 'netbanking',
        )):
            return 'Prepaid'
        return None

    @api.model
    def _find_payment_term(self, name):
        return self.env['account.payment.term'].search([('name', '=ilike', name)], limit=1)
