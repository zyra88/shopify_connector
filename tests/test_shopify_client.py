"""Client tests: normalisation, pagination and error mapping, no network."""

import json
from unittest.mock import Mock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..services.shopify_client import (
    ShopifyAPIError,
    ShopifyAuthError,
    ShopifyClient,
    parse_gid,
)


def fake_response(status_code=200, json_body=None, headers=None, text=''):
    response = Mock()
    response.status_code = status_code
    response.headers = headers or {}
    response.text = text or json.dumps(json_body or {})
    response.url = 'https://test-store.myshopify.com/admin/api/2026-07/graphql.json'
    response.json = Mock(return_value=json_body if json_body is not None else {})
    return response


GRAPHQL_ORDER = {
    'id': 'gid://shopify/Order/55000000000001',
    'legacyResourceId': '55000000000001',
    'name': '#1001',
    'number': 1001,
    'createdAt': '2026-01-05T10:00:00Z',
    'updatedAt': '2026-01-05T10:05:00Z',
    'processedAt': '2026-01-05T10:00:00Z',
    'cancelledAt': None,
    'closedAt': None,
    'currencyCode': 'GBP',
    'presentmentCurrencyCode': 'GBP',
    'displayFinancialStatus': 'PAID',
    'displayFulfillmentStatus': 'UNFULFILLED',
    'taxesIncluded': True,
    'taxExempt': False,
    'test': False,
    'note': 'Leave at the door',
    'tags': ['web', 'priority'],
    'email': 'ada@example.com',
    'phone': None,
    'paymentGatewayNames': ['shopify_payments'],
    'discountCodes': ['WELCOME10'],
    'subtotalPriceSet': {'shopMoney': {'amount': '180.00', 'currencyCode': 'GBP'}},
    'totalDiscountsSet': {'shopMoney': {'amount': '20.00', 'currencyCode': 'GBP'}},
    'totalShippingPriceSet': {'shopMoney': {'amount': '5.00', 'currencyCode': 'GBP'}},
    'totalTaxSet': {'shopMoney': {'amount': '30.83', 'currencyCode': 'GBP'}},
    'totalPriceSet': {'shopMoney': {'amount': '185.00', 'currencyCode': 'GBP'}},
    'billingAddress': {
        'firstName': 'Ada', 'lastName': 'Lovelace', 'name': 'Ada Lovelace',
        'company': None, 'address1': '12 Analytical Street', 'address2': None,
        'city': 'London', 'province': None, 'provinceCode': None,
        'zip': 'EC1A 1BB', 'country': 'United Kingdom', 'countryCodeV2': 'GB',
        'phone': '+44 20 7946 0958',
    },
    'shippingAddress': None,
    'customer': {
        'id': 'gid://shopify/Customer/66000000000001',
        'legacyResourceId': '66000000000001',
        'firstName': 'Ada', 'lastName': 'Lovelace',
        'email': 'ada@example.com', 'phone': None,
        'tags': ['vip'], 'note': None, 'defaultAddress': None,
    },
    'taxLines': [{'title': 'VAT', 'rate': 0.2,
                  'priceSet': {'shopMoney': {'amount': '30.83', 'currencyCode': 'GBP'}}}],
    'shippingLines': {'nodes': [{
        'id': 'gid://shopify/ShippingLine/99000000000001',
        'title': 'Standard', 'code': 'standard', 'source': 'shopify',
        'originalPriceSet': {'shopMoney': {'amount': '5.00', 'currencyCode': 'GBP'}},
        'discountedPriceSet': {'shopMoney': {'amount': '5.00', 'currencyCode': 'GBP'}},
        'taxLines': [],
    }]},
    'lineItems': {'nodes': [{
        'id': 'gid://shopify/LineItem/11000000000001',
        'name': 'Trail Runner Shoe', 'title': 'Trail Runner Shoe',
        'variantTitle': None, 'sku': 'SKU-RUN-01', 'vendor': 'Trekiva',
        'quantity': 2, 'currentQuantity': 2,
        'requiresShipping': True, 'taxable': True,
        'originalUnitPriceSet': {'shopMoney': {'amount': '100.00', 'currencyCode': 'GBP'}},
        'discountedUnitPriceSet': {'shopMoney': {'amount': '90.00', 'currencyCode': 'GBP'}},
        'totalDiscountSet': {'shopMoney': {'amount': '20.00', 'currencyCode': 'GBP'}},
        'taxLines': [{'title': 'VAT', 'rate': 0.2,
                      'priceSet': {'shopMoney': {'amount': '30.00', 'currencyCode': 'GBP'}}}],
        'product': {'id': 'gid://shopify/Product/77000000000001', 'legacyResourceId': '77000000000001'},
        'variant': {'id': 'gid://shopify/ProductVariant/44000000000001',
                    'legacyResourceId': '44000000000001',
                    'sku': 'SKU-RUN-01', 'barcode': '5901234123457', 'title': 'Default'},
    }]},
    'transactions': [{
        'gateway': 'shopify_payments', 'kind': 'SALE', 'status': 'SUCCESS',
        'amountSet': {'shopMoney': {'amount': '185.00', 'currencyCode': 'GBP'}},
    }],
    'fulfillments': [{
        'status': 'SUCCESS',
        'trackingInfo': [{
            'company': 'Ekart',
            'number': 'LUAC0001601959',
            'url': 'https://example.com/track',
        }],
    }],
}


@tagged('post_install', '-at_install', 'shopify')
class TestShopifyClient(TransactionCase):

    def _client(self, protocol='graphql'):
        client = ShopifyClient(
            'test-store', 'shpat_token', '2026-07', protocol=protocol, session=Mock())
        client._sleep = lambda _seconds: None
        return client

    # -- domain handling ------------------------------------------------

    def test_domain_is_normalised(self):
        cases = {
            'mystore': 'mystore.myshopify.com',
            'mystore.myshopify.com': 'mystore.myshopify.com',
            'https://mystore.myshopify.com/': 'mystore.myshopify.com',
            'https://mystore.myshopify.com/admin': 'mystore.myshopify.com',
        }
        for given, expected in cases.items():
            self.assertEqual(ShopifyClient._normalise_domain(given), expected, given)

    def test_parse_gid(self):
        self.assertEqual(parse_gid('gid://shopify/Order/12345'), '12345')
        self.assertEqual(parse_gid('12345'), '12345')
        self.assertIsNone(parse_gid(None))

    # -- GraphQL normalisation -------------------------------------------

    def test_graphql_order_is_normalised_to_rest_shape(self):
        order = self._client()._normalise_graphql_order(GRAPHQL_ORDER)

        self.assertEqual(order['id'], '55000000000001')
        self.assertEqual(order['name'], '#1001')
        self.assertEqual(order['financial_status'], 'paid')
        # Shopify's REST vocabulary uses null, not "unfulfilled".
        self.assertIsNone(order['fulfillment_status'])
        self.assertEqual(order['tags'], 'web, priority')
        self.assertEqual(order['discount_codes'], ['WELCOME10'])
        self.assertTrue(order['taxes_included'])
        self.assertEqual(order['total_price'], '185.00')
        self.assertEqual(order['total_shipping_price'], '5.00')
        self.assertEqual(order['customer']['id'], '66000000000001')
        self.assertEqual(order['customer']['tags'], 'vip')
        self.assertEqual(order['billing_address']['country_code'], 'GB')
        self.assertEqual(order['shipping_address'], {})
        self.assertEqual(order['primary_tracking_number'], 'LUAC0001601959')

        line = order['line_items'][0]
        self.assertEqual(line['id'], '11000000000001')
        self.assertEqual(line['variant_id'], '44000000000001')
        self.assertEqual(line['product_id'], '77000000000001')
        self.assertEqual(line['variant_barcode'], '5901234123457')
        self.assertEqual(line['price'], '100.00')
        self.assertEqual(line['total_discount'], '20.00')
        self.assertEqual(line['tax_lines'][0]['rate'], 0.2)

        self.assertEqual(order['shipping_lines'][0]['title'], 'Standard')
        self.assertEqual(order['transactions'][0]['kind'], 'sale')

    def test_rest_order_is_normalised_to_the_same_shape(self):
        rest_payload = {
            'id': 55000000000001,
            'name': '#1001',
            'order_number': 1001,
            'currency': 'GBP',
            'financial_status': 'paid',
            'fulfillment_status': None,
            'taxes_included': True,
            'total_price': '185.00',
            'total_shipping_price_set': {'shop_money': {'amount': '5.00'}},
            'customer': {'id': 66000000000001, 'first_name': 'Ada', 'email': 'ada@example.com'},
            'billing_address': {'address1': '12 Analytical Street', 'country_code': 'GB'},
            'line_items': [{
                'id': 11000000000001, 'name': 'Trail Runner Shoe', 'sku': 'SKU-RUN-01',
                'quantity': 2, 'price': '100.00', 'total_discount': '20.00',
                'product_id': 77000000000001, 'variant_id': 44000000000001,
                'tax_lines': [{'title': 'VAT', 'rate': 0.2, 'price': '30.00'}],
            }],
            'shipping_lines': [{'id': 99000000000001, 'title': 'Standard', 'price': '5.00'}],
        }
        order = self._client('rest')._normalise_rest_order(rest_payload)

        self.assertEqual(order['id'], '55000000000001')
        self.assertEqual(order['customer']['id'], '66000000000001')
        self.assertEqual(order['line_items'][0]['variant_id'], '44000000000001')
        # 100.00 - (20.00 / 2)
        self.assertEqual(float(order['line_items'][0]['discounted_unit_price']), 90.0)
        self.assertEqual(order['total_shipping_price'], '5.00')

    # -- search query building --------------------------------------------

    def test_search_query_combines_filters(self):
        query = ShopifyClient._build_search_query(
            '2026-01-01T00:00:00Z', None, 'open', 'paid', None, None)
        self.assertEqual(
            query,
            "updated_at:>='2026-01-01T00:00:00Z' AND status:open AND financial_status:paid")

    def test_search_query_for_specific_ids_ignores_other_filters(self):
        query = ShopifyClient._build_search_query(
            '2026-01-01T00:00:00Z', None, 'any', None, None, ['1', 'gid://shopify/Order/2'])
        self.assertEqual(query, '(id:1 OR id:2)')

    # -- pagination and errors ---------------------------------------------

    def test_graphql_pagination_follows_the_cursor(self):
        client = self._client()
        pages = [
            {'data': {'orders': {
                'nodes': [dict(GRAPHQL_ORDER, id='gid://shopify/Order/1')],
                'pageInfo': {'hasNextPage': True, 'endCursor': 'cursor-1'}}}},
            {'data': {'orders': {
                'nodes': [dict(GRAPHQL_ORDER, id='gid://shopify/Order/2')],
                'pageInfo': {'hasNextPage': False, 'endCursor': None}}}},
        ]
        client._session.request = Mock(side_effect=[fake_response(json_body=page) for page in pages])

        orders = list(client.iter_orders())
        self.assertEqual([order['id'] for order in orders], ['1', '2'])

        second_call_body = json.loads(client._session.request.call_args_list[1].kwargs['data'])
        self.assertEqual(second_call_body['variables']['after'], 'cursor-1')

    def test_test_orders_are_skipped_unless_requested(self):
        client = self._client()
        body = {'data': {'orders': {
            'nodes': [dict(GRAPHQL_ORDER, test=True)],
            'pageInfo': {'hasNextPage': False}}}}
        client._session.request = Mock(return_value=fake_response(json_body=body))
        self.assertEqual(list(client.iter_orders()), [])

        client._session.request = Mock(return_value=fake_response(json_body=body))
        self.assertEqual(len(list(client.iter_orders(include_test_orders=True))), 1)

    def test_401_becomes_an_auth_error(self):
        client = self._client()
        client._session.request = Mock(return_value=fake_response(401, text='Invalid API key'))
        with self.assertRaises(ShopifyAuthError):
            client.test_connection()

    def test_graphql_errors_are_surfaced(self):
        client = self._client()
        client._session.request = Mock(return_value=fake_response(
            json_body={'errors': [{'message': 'Field does not exist'}]}))
        with self.assertRaises(ShopifyAPIError):
            client.test_connection()

    def test_429_is_retried_then_succeeds(self):
        client = self._client()
        client._session.request = Mock(side_effect=[
            fake_response(429, headers={'Retry-After': '0.01'}),
            fake_response(json_body={'data': {'shop': {'name': 'Test Store'}}}),
        ])
        self.assertEqual(client.test_connection()['name'], 'Test Store')
        self.assertEqual(client._session.request.call_count, 2)

    def test_rest_product_normalisation(self):
        raw = {
            'id': 99000000000001,
            'title': 'Trail Runner Shoe',
            'variants': [{
                'id': 88000000000001,
                'title': 'Default Title',
                'sku': 'SKU-RUN-01',
                'barcode': '5901234123457',
            }],
        }
        product = ShopifyClient._normalise_rest_product(raw)
        self.assertEqual(product['id'], '99000000000001')
        self.assertEqual(product['variants'][0]['id'], '88000000000001')
        self.assertEqual(product['variants'][0]['sku'], 'SKU-RUN-01')

    def test_graphql_product_pagination(self):
        client = self._client()
        pages = [
            {'data': {'products': {
                'nodes': [{
                    'id': 'gid://shopify/Product/1',
                    'legacyResourceId': '1',
                    'title': 'Shoe',
                    'variants': {'nodes': [{
                        'id': 'gid://shopify/ProductVariant/11',
                        'legacyResourceId': '11',
                        'title': 'Default',
                        'sku': 'SKU-1',
                        'barcode': None,
                    }]},
                }],
                'pageInfo': {'hasNextPage': True, 'endCursor': 'cursor-1'}}}},
            {'data': {'products': {
                'nodes': [{
                    'id': 'gid://shopify/Product/2',
                    'legacyResourceId': '2',
                    'title': 'Boot',
                    'variants': {'nodes': []},
                }],
                'pageInfo': {'hasNextPage': False, 'endCursor': None}}}},
        ]
        client._session.request = Mock(side_effect=[fake_response(json_body=page) for page in pages])
        products = list(client.iter_products())
        self.assertEqual([product['id'] for product in products], ['1', '2'])
        self.assertEqual(products[0]['variants'][0]['id'], '11')

    def test_rest_link_header_pagination(self):
        client = self._client('rest')
        next_url = 'https://test-store.myshopify.com/admin/api/2026-07/orders.json?page_info=abc'
        client._session.request = Mock(side_effect=[
            fake_response(
                json_body={'orders': [{'id': 1, 'name': '#1'}]},
                headers={'Link': '<%s>; rel="next"' % next_url}),
            fake_response(json_body={'orders': [{'id': 2, 'name': '#2'}]}),
        ])
        orders = list(client.iter_orders())
        self.assertEqual([order['id'] for order in orders], ['1', '2'])
        self.assertEqual(client._session.request.call_args_list[1].args[1], next_url)
