from datetime import timedelta
from unittest.mock import Mock, patch

from odoo import fields
from odoo.exceptions import ValidationError

from odoo.addons.zyra_shopify_connector.services.shopify_client import (
    ShopifyAuthError,
    ShopifyClient,
    exchange_client_credentials,
)
from odoo.addons.zyra_shopify_connector.tests.common import ShopifyConnectorCase
from odoo.addons.zyra_shopify_connector.tests.test_shopify_client import fake_response


class TestClientCredentialsExchange(ShopifyConnectorCase):

    def test_exchange_returns_token_and_expiry(self):
        session = Mock()
        session.post.return_value = fake_response(json_body={
            'access_token': 'oauth-token-abc',
            'expires_in': 86399,
            'scope': 'read_orders,read_products',
        })
        result = exchange_client_credentials(
            'test-store',
            'client-id',
            'client-secret',
            session=session,
        )
        self.assertEqual(result['access_token'], 'oauth-token-abc')
        self.assertEqual(result['expires_in'], 86399)
        session.post.assert_called_once()
        call = session.post.call_args
        self.assertEqual(
            call.args[0],
            'https://test-store.myshopify.com/admin/oauth/access_token',
        )
        self.assertEqual(call.kwargs['data']['grant_type'], 'client_credentials')

    def test_exchange_auth_error(self):
        session = Mock()
        session.post.return_value = fake_response(401, text='invalid client')
        with self.assertRaises(ShopifyAuthError):
            exchange_client_credentials(
                'test-store', 'bad-id', 'bad-secret', session=session)

    def test_client_uses_token_provider_and_refreshes_on_401(self):
        calls = {'count': 0}

        def provider(force_refresh=False):
            calls['count'] += 1
            return 'token-%s' % calls['count']

        client = ShopifyClient(
            'test-store',
            token_provider=provider,
            session=Mock(),
        )
        client._sleep = Mock()
        client._session.request = Mock(side_effect=[
            fake_response(401, text='expired'),
            fake_response(json_body={'data': {'shop': {'name': 'Test Store'}}}),
        ])
        info = client.test_connection()
        self.assertEqual(info['name'], 'Test Store')
        self.assertEqual(client._session.request.call_count, 2)
        self.assertGreaterEqual(calls['count'], 2)


class TestStoreClientCredentials(ShopifyConnectorCase):

    def test_dev_dashboard_store_refreshes_and_caches_token(self):
        store = self.env['shopify.store'].create({
            'name': 'Dev Dashboard Store',
            'shop_domain': 'dev-store.myshopify.com',
            'client_id': 'client-id',
            'client_secret': 'client-secret',
            'company_id': self.company.id,
            'currency_id': self.currency.id,
            'warehouse_id': self.warehouse.id,
            'tax_mode': 'none',
        })
        with patch(
            'odoo.addons.zyra_shopify_connector.models.shopify_store.exchange_client_credentials',
            return_value={'access_token': 'fresh-token', 'expires_in': 3600},
        ) as exchange:
            token = store._get_valid_access_token()
        self.assertEqual(token, 'fresh-token')
        self.assertEqual(store.access_token, 'fresh-token')
        self.assertTrue(store.access_token_expires_at)
        exchange.assert_called_once()

        with patch(
            'odoo.addons.zyra_shopify_connector.models.shopify_store.exchange_client_credentials',
        ) as exchange_again:
            cached = store._get_valid_access_token()
        self.assertEqual(cached, 'fresh-token')
        exchange_again.assert_not_called()

    def test_expired_token_is_refreshed(self):
        store = self.env['shopify.store'].create({
            'name': 'Expired Token Store',
            'shop_domain': 'dev-store.myshopify.com',
            'client_id': 'client-id',
            'client_secret': 'client-secret',
            'access_token': 'old-token',
            'access_token_expires_at': fields.Datetime.now() - timedelta(hours=1),
            'company_id': self.company.id,
            'currency_id': self.currency.id,
            'warehouse_id': self.warehouse.id,
            'tax_mode': 'none',
        })
        with patch(
            'odoo.addons.zyra_shopify_connector.models.shopify_store.exchange_client_credentials',
            return_value={'access_token': 'new-token', 'expires_in': 3600},
        ):
            token = store._get_valid_access_token()
        self.assertEqual(token, 'new-token')

    def test_client_credentials_requires_both_fields(self):
        with self.assertRaises(ValidationError):
            self.env['shopify.store'].create({
                'name': 'Incomplete Store',
                'shop_domain': 'dev-store.myshopify.com',
                'client_id': 'only-id',
                'client_secret': '   ',
                'company_id': self.company.id,
                'currency_id': self.currency.id,
                'warehouse_id': self.warehouse.id,
            })

    def test_cached_token_is_reused(self):
        with patch(
            'odoo.addons.zyra_shopify_connector.models.shopify_store.exchange_client_credentials',
        ) as exchange:
            token = self.store._get_valid_access_token()
        self.assertEqual(token, 'shpat_testtoken')
        exchange.assert_not_called()
