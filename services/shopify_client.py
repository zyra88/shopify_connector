"""HTTP client for the Shopify Admin API.

Two transports are supported. Both return the *same* normalised order
dictionary, so nothing downstream of this module needs to know which one is in
use:

``graphql``
    The API Shopify actively develops. Default for new stores.
``rest``
    Legacy since 2024-10 but still served for custom apps. Kept for stores
    whose app was provisioned before the GraphQL migration.

The normalised shape follows the REST order payload because that is the
vocabulary the rest of the connector (and Shopify's own documentation) uses:
``line_items``, ``financial_status``, ``total_discounts`` and so on. The
GraphQL normaliser flattens ``MoneyBag`` values to plain decimal strings and
resolves ``gid://shopify/<Type>/<id>`` handles to their numeric legacy ids.

This module is deliberately free of Odoo imports so it can be exercised in
isolation.
"""

import json
import logging
import random
import re
import time
from urllib.parse import urlparse

import requests

_logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = '2026-07'
DEFAULT_TIMEOUT = 60
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 250

# Shopify replenishes the REST leaky bucket at 2 calls/second. Backing off once
# we have consumed this share of the bucket keeps us out of 429 territory
# without serialising the whole import.
REST_BUCKET_BACKOFF_RATIO = 0.8

# GraphQL is metered in cost points rather than calls. Pause when the remaining
# budget drops below what the next page is likely to need.
GRAPHQL_COST_FLOOR = 200

_GID_RE = re.compile(r'gid://shopify/[A-Za-z]+/(\d+)')
_LINK_RE = re.compile(r'<(?P<url>[^>]+)>;\s*rel="(?P<rel>[^"]+)"')


class ShopifyError(Exception):
    """Base class for every failure raised by this client."""


class ShopifyConfigError(ShopifyError):
    """The store credentials are missing or malformed."""


class ShopifyAuthError(ShopifyError):
    """Shopify rejected the access token or the app is missing a scope."""


class ShopifyRateLimitError(ShopifyError):
    """Throttled by Shopify for longer than we are willing to wait."""


class ShopifyAPIError(ShopifyError):
    """Shopify returned an error response or an unusable payload."""


def parse_gid(value):
    """Return the numeric id embedded in a Shopify global id.

    Accepts a raw gid, an already-numeric id, or ``None``.
    """
    if value in (None, '', False):
        return None
    value = str(value)
    match = _GID_RE.match(value)
    if match:
        return match.group(1)
    return value


def _money(money_bag, key='shopMoney'):
    """Flatten a GraphQL ``MoneyBag`` to a decimal string."""
    if not money_bag:
        return '0.0'
    money = money_bag.get(key) or money_bag.get('presentmentMoney') or {}
    return money.get('amount') or '0.0'


def _currency(money_bag, key='shopMoney'):
    if not money_bag:
        return None
    money = money_bag.get(key) or {}
    return money.get('currencyCode')


def exchange_client_credentials(shop_domain, client_id, client_secret,
                                timeout=DEFAULT_TIMEOUT, session=None):
    """Exchange Dev Dashboard credentials for a short-lived access token.

    Shopify no longer issues permanent ``shpat_`` tokens for new apps. Apps
    created in the Dev Dashboard use the client credentials grant instead; the
    returned token is valid for 24 hours and must be refreshed before expiry.
    """
    domain = ShopifyClient._normalise_domain(shop_domain)
    client_id = (client_id or '').strip()
    client_secret = (client_secret or '').strip()
    if not domain:
        raise ShopifyConfigError("Shopify shop domain is not set.")
    if not client_id or not client_secret:
        raise ShopifyConfigError("Shopify Client ID and Client secret are not set.")

    http = session or requests.Session()
    url = 'https://%s/admin/oauth/access_token' % domain
    try:
        response = http.post(
            url,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            data={
                'grant_type': 'client_credentials',
                'client_id': client_id,
                'client_secret': client_secret,
            },
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise ShopifyAPIError("Could not reach Shopify token endpoint: %s" % exc) from exc

    if response.status_code in (401, 403):
        raise ShopifyAuthError(
            "Shopify rejected the Client ID or Client secret for %s (HTTP %s). "
            "Check the credentials in the Dev Dashboard and that the app is "
            "installed on this store with read_orders, read_customers and "
            "read_products scopes.\n%s"
            % (domain, response.status_code, response.text[:500])
        )
    if not response.ok:
        body = response.text[:500]
        if 'shop_not_permitted' in body:
            raise ShopifyAuthError(
                "Shopify refused a client-credentials token for %s: the app and "
                "store must belong to the same Shopify organization. If this is "
                "a partner app on a client store, install it via custom "
                "distribution and use the authorization code grant instead.\n%s"
                % (domain, body)
            )
        raise ShopifyAPIError(
            "Shopify token request failed for %s (HTTP %s): %s"
            % (domain, response.status_code, body)
        )

    data = response.json()
    access_token = (data.get('access_token') or '').strip()
    if not access_token:
        raise ShopifyAPIError(
            "Shopify token response for %s did not include an access_token."
            % domain
        )
    try:
        expires_in = int(data.get('expires_in') or 86399)
    except (TypeError, ValueError):
        expires_in = 86399
    return {
        'access_token': access_token,
        'expires_in': expires_in,
        'scope': data.get('scope'),
    }


class ShopifyClient:
    """Stateless-per-call wrapper around one Shopify store's Admin API."""

    def __init__(self, shop_domain, access_token=None, api_version=None,
                 protocol='graphql', timeout=DEFAULT_TIMEOUT, max_retries=5,
                 session=None, token_provider=None):
        self.shop_domain = self._normalise_domain(shop_domain)
        self.access_token = (access_token or '').strip()
        self.api_version = (api_version or DEFAULT_API_VERSION).strip()
        self.protocol = protocol if protocol in ('graphql', 'rest') else 'graphql'
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.max_retries = max_retries
        self._token_provider = token_provider
        if not self.shop_domain:
            raise ShopifyConfigError("Shopify shop domain is not set.")
        if not self.access_token and not self._token_provider:
            raise ShopifyConfigError("Shopify Admin API access token is not set.")
        self._session = session or requests.Session()
        self._session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'Odoo-Shopify-Connector/18.0',
        })
        self._apply_access_token()

    # ------------------------------------------------------------------
    # Low level transport
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_domain(shop_domain):
        """Accept ``shop``, ``shop.myshopify.com`` or a full URL."""
        if not shop_domain:
            return ''
        domain = shop_domain.strip().rstrip('/')
        if '://' in domain:
            domain = urlparse(domain).netloc
        domain = domain.split('/')[0]
        if domain and '.' not in domain:
            domain = '%s.myshopify.com' % domain
        return domain

    def _base_url(self):
        return 'https://%s/admin/api/%s' % (self.shop_domain, self.api_version)

    def _sleep(self, seconds):
        # Extracted so tests can patch the wait away.
        time.sleep(seconds)

    def _backoff(self, attempt, retry_after=None):
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except (TypeError, ValueError):
                pass
        # Full jitter: avoids a thundering herd when several stores sync at once.
        return min(2 ** attempt, 32) * (0.5 + random.random() / 2)

    def _apply_access_token(self, force_refresh=False):
        """Set the session header from a static token or the token provider."""
        if self._token_provider:
            token = self._token_provider(force_refresh=force_refresh)
            self.access_token = (token or '').strip()
        if not self.access_token:
            raise ShopifyConfigError("Shopify Admin API access token is not set.")
        self._session.headers.update({'X-Shopify-Access-Token': self.access_token})

    def _raise_for_auth(self, response):
        if response.status_code in (401, 403):
            raise ShopifyAuthError(
                "Shopify rejected the credentials for %s (HTTP %s). Check the "
                "Admin API access token and that the app has the read_orders, "
                "read_customers and read_products scopes.\n%s"
                % (self.shop_domain, response.status_code, response.text[:500])
            )
        if response.status_code == 404:
            raise ShopifyAPIError(
                "Shopify returned 404 for %s. Check the shop domain and that "
                "API version %s is still supported."
                % (response.url, self.api_version)
            )

    def _request(self, method, url, **kwargs):
        """Perform one HTTP call, retrying throttles and transient failures."""
        auth_retry = kwargs.pop('_auth_retry', False)
        kwargs.setdefault('timeout', self.timeout)
        self._apply_access_token()
        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = self._session.request(method, url, **kwargs)
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt == self.max_retries - 1:
                    break
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code in (401, 403) and self._token_provider and not auth_retry:
                self._apply_access_token(force_refresh=True)
                return self._request(method, url, _auth_retry=True, **kwargs)

            if response.status_code == 429:
                if attempt == self.max_retries - 1:
                    raise ShopifyRateLimitError(
                        "Shopify kept throttling %s after %s attempts."
                        % (url, self.max_retries)
                    )
                self._sleep(self._backoff(attempt, response.headers.get('Retry-After')))
                continue

            if response.status_code >= 500:
                last_error = ShopifyAPIError(
                    "Shopify returned HTTP %s for %s: %s"
                    % (response.status_code, url, response.text[:500])
                )
                if attempt == self.max_retries - 1:
                    break
                self._sleep(self._backoff(attempt))
                continue

            self._raise_for_auth(response)
            if response.status_code >= 400:
                raise ShopifyAPIError(
                    "Shopify returned HTTP %s for %s: %s"
                    % (response.status_code, url, response.text[:500])
                )
            return response

        if isinstance(last_error, ShopifyError):
            raise last_error
        raise ShopifyAPIError(
            "Could not reach Shopify at %s after %s attempts: %s"
            % (url, self.max_retries, last_error)
        )

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError as exc:
            raise ShopifyAPIError(
                "Shopify returned a non-JSON response for %s: %s"
                % (response.url, response.text[:500])
            ) from exc

    # ------------------------------------------------------------------
    # REST transport
    # ------------------------------------------------------------------

    def _rest_get(self, path, params=None):
        url = path if path.startswith('http') else '%s/%s' % (self._base_url(), path.lstrip('/'))
        response = self._request('GET', url, params=params)
        self._respect_rest_bucket(response)
        return response

    def _respect_rest_bucket(self, response):
        header = response.headers.get('X-Shopify-Shop-Api-Call-Limit')
        if not header or '/' not in header:
            return
        used, _, limit = header.partition('/')
        try:
            used, limit = int(used), int(limit)
        except ValueError:
            return
        if limit and used / limit >= REST_BUCKET_BACKOFF_RATIO:
            _logger.debug("Shopify REST bucket at %s, pausing before next call", header)
            self._sleep(1.0)

    @staticmethod
    def _next_page_url(response):
        link = response.headers.get('Link') or response.headers.get('link')
        if not link:
            return None
        for match in _LINK_RE.finditer(link):
            if match.group('rel') == 'next':
                return match.group('url')
        return None

    # ------------------------------------------------------------------
    # GraphQL transport
    # ------------------------------------------------------------------

    def _graphql(self, query, variables=None):
        url = '%s/graphql.json' % self._base_url()
        payload = json.dumps({'query': query, 'variables': variables or {}})
        response = self._request('POST', url, data=payload)
        body = self._json(response)

        errors = body.get('errors')
        if errors:
            throttled = any(
                (err.get('extensions') or {}).get('code') == 'THROTTLED'
                for err in errors if isinstance(err, dict)
            )
            messages = '; '.join(
                err.get('message', str(err)) if isinstance(err, dict) else str(err)
                for err in errors
            )
            if throttled:
                raise ShopifyRateLimitError("Shopify throttled the GraphQL query: %s" % messages)
            if 'access denied' in messages.lower() or 'not approved' in messages.lower():
                raise ShopifyAuthError(
                    "Shopify denied the GraphQL query for %s: %s. The app is "
                    "probably missing a scope." % (self.shop_domain, messages)
                )
            raise ShopifyAPIError("Shopify GraphQL error: %s" % messages)

        if body.get('data') is None:
            raise ShopifyAPIError("Shopify GraphQL returned no data: %s" % json.dumps(body)[:500])

        self._respect_graphql_cost(body)
        return body['data']

    def _respect_graphql_cost(self, body):
        throttle = (((body.get('extensions') or {}).get('cost') or {}).get('throttleStatus') or {})
        available = throttle.get('currentlyAvailable')
        restore_rate = throttle.get('restoreRate') or 50
        if available is None or available >= GRAPHQL_COST_FLOOR:
            return
        wait = (GRAPHQL_COST_FLOOR - available) / max(restore_rate, 1)
        _logger.debug("Shopify GraphQL budget at %s points, pausing %.1fs", available, wait)
        self._sleep(min(wait, 10.0))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def test_connection(self):
        """Return shop metadata, proving the credentials and scopes work."""
        if self.protocol == 'rest':
            data = self._json(self._rest_get('shop.json')).get('shop') or {}
            return {
                'name': data.get('name'),
                'domain': data.get('myshopify_domain') or data.get('domain'),
                'email': data.get('email'),
                'currency': data.get('currency'),
                'timezone': data.get('iana_timezone'),
                'plan': data.get('plan_display_name'),
            }
        data = self._graphql(SHOP_QUERY)
        shop = data.get('shop') or {}
        return {
            'name': shop.get('name'),
            'domain': shop.get('myshopifyDomain'),
            'email': shop.get('email'),
            'currency': (shop.get('currencyCode')),
            'timezone': shop.get('ianaTimezone'),
            'plan': (shop.get('plan') or {}).get('displayName'),
        }

    def iter_orders(self, updated_at_min=None, created_at_min=None, status='any',
                    financial_status=None, fulfillment_status=None,
                    shopify_order_ids=None, page_size=DEFAULT_PAGE_SIZE,
                    max_orders=None, include_test_orders=False):
        """Yield normalised order dicts, transparently paginating.

        ``updated_at_min`` / ``created_at_min`` are ISO-8601 strings.
        ``shopify_order_ids`` restricts the run to specific numeric order ids.
        """
        page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        if self.protocol == 'rest':
            generator = self._iter_orders_rest(
                updated_at_min, created_at_min, status, financial_status,
                fulfillment_status, shopify_order_ids, page_size,
            )
        else:
            generator = self._iter_orders_graphql(
                updated_at_min, created_at_min, status, financial_status,
                fulfillment_status, shopify_order_ids, page_size,
            )

        yielded = 0
        for order in generator:
            if order.get('test') and not include_test_orders:
                continue
            yield order
            yielded += 1
            if max_orders and yielded >= max_orders:
                return

    def get_order(self, shopify_order_id):
        """Fetch a single order by numeric id, or ``None`` if it is gone."""
        shopify_order_id = parse_gid(shopify_order_id)
        if self.protocol == 'rest':
            try:
                body = self._json(self._rest_get('orders/%s.json' % shopify_order_id))
            except ShopifyAPIError:
                return None
            raw = body.get('order')
            return self._normalise_rest_order(raw) if raw else None
        data = self._graphql(ORDER_QUERY, {'id': 'gid://shopify/Order/%s' % shopify_order_id})
        node = data.get('order')
        return self._normalise_graphql_order(node) if node else None

    def iter_products(self, updated_at_min=None, page_size=DEFAULT_PAGE_SIZE,
                      max_products=None):
        """Yield normalised product dicts with their variants, paginating."""
        page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        if self.protocol == 'rest':
            generator = self._iter_products_rest(updated_at_min, page_size)
        else:
            generator = self._iter_products_graphql(updated_at_min, page_size)

        yielded = 0
        for product in generator:
            yield product
            yielded += 1
            if max_products and yielded >= max_products:
                return

    # -- REST order iteration ------------------------------------------

    def _iter_orders_rest(self, updated_at_min, created_at_min, status,
                          financial_status, fulfillment_status,
                          shopify_order_ids, page_size):
        params = {'limit': page_size, 'status': status or 'any'}
        if updated_at_min:
            params['updated_at_min'] = updated_at_min
        if created_at_min:
            params['created_at_min'] = created_at_min
        if financial_status:
            params['financial_status'] = financial_status
        if fulfillment_status:
            params['fulfillment_status'] = fulfillment_status
        if shopify_order_ids:
            params['ids'] = ','.join(str(parse_gid(i)) for i in shopify_order_ids)
            params.pop('updated_at_min', None)
            params.pop('created_at_min', None)

        url, first = 'orders.json', True
        while url:
            response = self._rest_get(url, params=params if first else None)
            first = False
            for raw in self._json(response).get('orders') or []:
                yield self._normalise_rest_order(raw)
            url = self._next_page_url(response)

    # -- GraphQL order iteration ---------------------------------------

    @staticmethod
    def _build_search_query(updated_at_min, created_at_min, status,
                            financial_status, fulfillment_status, shopify_order_ids):
        clauses = []
        if shopify_order_ids:
            ids = ' OR '.join('id:%s' % parse_gid(i) for i in shopify_order_ids)
            return '(%s)' % ids
        if updated_at_min:
            clauses.append("updated_at:>='%s'" % updated_at_min)
        if created_at_min:
            clauses.append("created_at:>='%s'" % created_at_min)
        if status and status != 'any':
            clauses.append('status:%s' % status)
        if financial_status:
            clauses.append('financial_status:%s' % financial_status)
        if fulfillment_status:
            clauses.append('fulfillment_status:%s' % fulfillment_status)
        return ' AND '.join(clauses)

    def _iter_orders_graphql(self, updated_at_min, created_at_min, status,
                             financial_status, fulfillment_status,
                             shopify_order_ids, page_size):
        search = self._build_search_query(
            updated_at_min, created_at_min, status, financial_status,
            fulfillment_status, shopify_order_ids,
        )
        cursor = None
        while True:
            variables = {'first': page_size, 'after': cursor, 'query': search or None}
            connection = (self._graphql(ORDERS_QUERY, variables).get('orders') or {})
            for node in connection.get('nodes') or []:
                yield self._normalise_graphql_order(node)
            page_info = connection.get('pageInfo') or {}
            if not page_info.get('hasNextPage'):
                return
            cursor = page_info.get('endCursor')
            if not cursor:
                return

    # -- REST product iteration ----------------------------------------

    def _iter_products_rest(self, updated_at_min, page_size):
        params = {'limit': page_size}
        if updated_at_min:
            params['updated_at_min'] = updated_at_min

        url, first = 'products.json', True
        while url:
            response = self._rest_get(url, params=params if first else None)
            first = False
            for raw in self._json(response).get('products') or []:
                yield self._normalise_rest_product(raw)
            url = self._next_page_url(response)

    # -- GraphQL product iteration -------------------------------------

    def _iter_products_graphql(self, updated_at_min, page_size):
        search = "updated_at:>='%s'" % updated_at_min if updated_at_min else None
        cursor = None
        while True:
            variables = {'first': page_size, 'after': cursor, 'query': search}
            connection = (self._graphql(PRODUCTS_QUERY, variables).get('products') or {})
            for node in connection.get('nodes') or []:
                yield self._normalise_graphql_product(node)
            page_info = connection.get('pageInfo') or {}
            if not page_info.get('hasNextPage'):
                return
            cursor = page_info.get('endCursor')
            if not cursor:
                return

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_rest_product(raw):
        product_id = str(raw.get('id') or '')
        variants = []
        for variant in raw.get('variants') or []:
            variants.append({
                'id': str(variant.get('id') or ''),
                'product_id': product_id,
                'title': variant.get('title'),
                'option1': variant.get('option1'),
                'option2': variant.get('option2'),
                'option3': variant.get('option3'),
                'sku': (variant.get('sku') or '').strip() or None,
                'barcode': (variant.get('barcode') or '').strip() or None,
            })
        return {
            'id': product_id,
            'title': raw.get('title'),
            'variants': variants,
        }

    @staticmethod
    def _normalise_graphql_product(node):
        product_id = parse_gid(node.get('id')) or parse_gid(node.get('legacyResourceId'))
        variants = []
        for variant in (node.get('variants') or {}).get('nodes') or []:
            options = variant.get('selectedOptions') or []
            option_values = [opt.get('value') for opt in options[:3]]
            variants.append({
                'id': parse_gid(variant.get('id')) or parse_gid(variant.get('legacyResourceId')),
                'product_id': product_id,
                'title': variant.get('title'),
                'option1': option_values[0] if len(option_values) > 0 else None,
                'option2': option_values[1] if len(option_values) > 1 else None,
                'option3': option_values[2] if len(option_values) > 2 else None,
                'sku': (variant.get('sku') or '').strip() or None,
                'barcode': (variant.get('barcode') or '').strip() or None,
            })
        return {
            'id': product_id,
            'title': node.get('title'),
            'variants': variants,
        }

    @staticmethod
    def _normalise_rest_address(raw):
        if not raw:
            return {}
        return {
            'first_name': raw.get('first_name'),
            'last_name': raw.get('last_name'),
            'name': raw.get('name'),
            'company': raw.get('company'),
            'address1': raw.get('address1'),
            'address2': raw.get('address2'),
            'city': raw.get('city'),
            'province': raw.get('province'),
            'province_code': raw.get('province_code'),
            'zip': raw.get('zip'),
            'country': raw.get('country'),
            'country_code': raw.get('country_code'),
            'phone': raw.get('phone'),
        }

    @staticmethod
    def _normalise_tax_lines(raw_lines, graphql=False):
        lines = []
        for raw in raw_lines or []:
            if graphql:
                lines.append({
                    'title': raw.get('title'),
                    'rate': raw.get('rate') or 0.0,
                    'price': _money(raw.get('priceSet')),
                })
            else:
                lines.append({
                    'title': raw.get('title'),
                    'rate': raw.get('rate') or 0.0,
                    'price': raw.get('price') or '0.0',
                })
        return lines

    def _normalise_rest_order(self, raw):
        line_items = []
        for item in raw.get('line_items') or []:
            quantity = item.get('quantity') or 0
            price = float(item.get('price') or 0.0)
            total_discount = float(item.get('total_discount') or 0.0)
            discounted_unit = price - (total_discount / quantity if quantity else 0.0)
            line_items.append({
                'id': str(item.get('id') or ''),
                'name': item.get('name'),
                'title': item.get('title'),
                'variant_title': item.get('variant_title'),
                'sku': item.get('sku'),
                'vendor': item.get('vendor'),
                'quantity': quantity,
                'current_quantity': item.get('current_quantity', quantity),
                'requires_shipping': item.get('requires_shipping', True),
                'taxable': item.get('taxable', True),
                'price': item.get('price') or '0.0',
                'discounted_unit_price': '%s' % discounted_unit,
                'total_discount': item.get('total_discount') or '0.0',
                'product_id': str(item['product_id']) if item.get('product_id') else None,
                'variant_id': str(item['variant_id']) if item.get('variant_id') else None,
                'variant_barcode': None,
                'tax_lines': self._normalise_tax_lines(item.get('tax_lines')),
            })

        shipping_lines = []
        for ship in raw.get('shipping_lines') or []:
            shipping_lines.append({
                'id': str(ship.get('id') or ''),
                'title': ship.get('title'),
                'code': ship.get('code'),
                'source': ship.get('source'),
                'price': ship.get('price') or '0.0',
                'discounted_price': ship.get('discounted_price') or ship.get('price') or '0.0',
                'tax_lines': self._normalise_tax_lines(ship.get('tax_lines')),
            })

        customer = raw.get('customer') or {}
        gateways = raw.get('payment_gateway_names') or []
        if raw.get('gateway') and raw['gateway'] not in gateways:
            gateways = [raw['gateway']] + list(gateways)

        tracking_numbers = self._tracking_numbers_from_rest(raw)
        return {
            'id': str(raw.get('id') or ''),
            'admin_graphql_api_id': raw.get('admin_graphql_api_id'),
            'name': raw.get('name'),
            'order_number': raw.get('order_number') or raw.get('number'),
            'created_at': raw.get('created_at'),
            'updated_at': raw.get('updated_at'),
            'processed_at': raw.get('processed_at'),
            'cancelled_at': raw.get('cancelled_at'),
            'closed_at': raw.get('closed_at'),
            'currency': raw.get('currency'),
            'presentment_currency': raw.get('presentment_currency'),
            'financial_status': raw.get('financial_status'),
            'fulfillment_status': raw.get('fulfillment_status'),
            'taxes_included': bool(raw.get('taxes_included')),
            'tax_exempt': bool(raw.get('tax_exempt')),
            'test': bool(raw.get('test')),
            'note': raw.get('note'),
            'tags': raw.get('tags'),
            'email': raw.get('email') or raw.get('contact_email'),
            'phone': raw.get('phone'),
            'gateway': gateways[0] if gateways else None,
            'payment_gateway_names': gateways,
            'discount_codes': [c.get('code') for c in raw.get('discount_codes') or [] if c.get('code')],
            'subtotal_price': raw.get('subtotal_price') or '0.0',
            'total_discounts': raw.get('total_discounts') or '0.0',
            'total_shipping_price': (
                ((raw.get('total_shipping_price_set') or {}).get('shop_money') or {}).get('amount')
                or sum(float(s['price']) for s in shipping_lines) or '0.0'
            ),
            'total_tax': raw.get('total_tax') or '0.0',
            'total_price': raw.get('total_price') or '0.0',
            'customer': {
                'id': str(customer['id']) if customer.get('id') else None,
                'first_name': customer.get('first_name'),
                'last_name': customer.get('last_name'),
                'email': customer.get('email'),
                'phone': customer.get('phone'),
                'tags': customer.get('tags'),
                'note': customer.get('note'),
                'default_address': self._normalise_rest_address(customer.get('default_address')),
            } if customer else {},
            'billing_address': self._normalise_rest_address(raw.get('billing_address')),
            'shipping_address': self._normalise_rest_address(raw.get('shipping_address')),
            'tax_lines': self._normalise_tax_lines(raw.get('tax_lines')),
            'line_items': line_items,
            'shipping_lines': shipping_lines,
            'transactions': [
                {
                    'gateway': t.get('gateway'),
                    'kind': t.get('kind'),
                    'status': t.get('status'),
                    'amount': t.get('amount'),
                }
                for t in raw.get('transactions') or []
            ],
            'tracking_numbers': tracking_numbers,
            'primary_tracking_number': tracking_numbers[0] if tracking_numbers else False,
            'raw': raw,
        }

    @staticmethod
    def _tracking_numbers_from_rest(raw):
        numbers = []
        for fulfillment in raw.get('fulfillments') or []:
            number = (fulfillment.get('tracking_number') or '').strip()
            if number:
                numbers.append(number)
        return numbers

    @staticmethod
    def _tracking_numbers_from_graphql(node):
        numbers = []
        for fulfillment in node.get('fulfillments') or []:
            tracking_info = fulfillment.get('trackingInfo') or []
            if isinstance(tracking_info, dict):
                tracking_info = [tracking_info]
            for info in tracking_info:
                number = (info.get('number') or '').strip()
                if number:
                    numbers.append(number)
        return numbers

    @staticmethod
    def _normalise_graphql_address(raw):
        if not raw:
            return {}
        return {
            'first_name': raw.get('firstName'),
            'last_name': raw.get('lastName'),
            'name': raw.get('name'),
            'company': raw.get('company'),
            'address1': raw.get('address1'),
            'address2': raw.get('address2'),
            'city': raw.get('city'),
            'province': raw.get('province'),
            'province_code': raw.get('provinceCode'),
            'zip': raw.get('zip'),
            'country': raw.get('country'),
            'country_code': raw.get('countryCodeV2'),
            'phone': raw.get('phone'),
        }

    def _normalise_graphql_order(self, node):
        line_items = []
        for item in ((node.get('lineItems') or {}).get('nodes') or []):
            variant = item.get('variant') or {}
            product = item.get('product') or {}
            line_items.append({
                'id': parse_gid(item.get('id')),
                'name': item.get('name'),
                'title': item.get('title'),
                'variant_title': item.get('variantTitle'),
                'sku': item.get('sku') or variant.get('sku'),
                'vendor': item.get('vendor'),
                'quantity': item.get('quantity') or 0,
                'current_quantity': item.get('currentQuantity', item.get('quantity') or 0),
                'requires_shipping': item.get('requiresShipping', True),
                'taxable': item.get('taxable', True),
                'price': _money(item.get('originalUnitPriceSet')),
                'discounted_unit_price': _money(item.get('discountedUnitPriceSet')),
                'total_discount': _money(item.get('totalDiscountSet')),
                'product_id': parse_gid(product.get('id')) or parse_gid(product.get('legacyResourceId')),
                'variant_id': parse_gid(variant.get('id')) or parse_gid(variant.get('legacyResourceId')),
                'variant_barcode': variant.get('barcode'),
                'tax_lines': self._normalise_tax_lines(item.get('taxLines'), graphql=True),
            })

        shipping_lines = []
        for ship in ((node.get('shippingLines') or {}).get('nodes') or []):
            shipping_lines.append({
                'id': parse_gid(ship.get('id')),
                'title': ship.get('title'),
                'code': ship.get('code'),
                'source': ship.get('source'),
                'price': _money(ship.get('originalPriceSet')),
                'discounted_price': _money(ship.get('discountedPriceSet')),
                'tax_lines': self._normalise_tax_lines(ship.get('taxLines'), graphql=True),
            })

        customer = node.get('customer') or {}
        gateways = node.get('paymentGatewayNames') or []
        financial = (node.get('displayFinancialStatus') or '').lower() or None
        fulfillment = (node.get('displayFulfillmentStatus') or '').lower() or None
        # Shopify's REST vocabulary uses a null fulfillment_status for
        # unfulfilled orders; GraphQL spells it out. Keep the REST convention so
        # both transports store the same value.
        if fulfillment == 'unfulfilled':
            fulfillment = None

        tracking_numbers = self._tracking_numbers_from_graphql(node)
        return {
            'id': parse_gid(node.get('id')) or parse_gid(node.get('legacyResourceId')),
            'admin_graphql_api_id': node.get('id'),
            'name': node.get('name'),
            'order_number': node.get('number'),
            'created_at': node.get('createdAt'),
            'updated_at': node.get('updatedAt'),
            'processed_at': node.get('processedAt'),
            'cancelled_at': node.get('cancelledAt'),
            'closed_at': node.get('closedAt'),
            'currency': node.get('currencyCode'),
            'presentment_currency': node.get('presentmentCurrencyCode'),
            'financial_status': financial,
            'fulfillment_status': fulfillment,
            'taxes_included': bool(node.get('taxesIncluded')),
            'tax_exempt': bool(node.get('taxExempt')),
            'test': bool(node.get('test')),
            'note': node.get('note'),
            'tags': ', '.join(node.get('tags') or []) if isinstance(node.get('tags'), list) else node.get('tags'),
            'email': node.get('email'),
            'phone': node.get('phone'),
            'gateway': gateways[0] if gateways else None,
            'payment_gateway_names': list(gateways),
            'discount_codes': list(node.get('discountCodes') or []),
            'subtotal_price': _money(node.get('subtotalPriceSet')),
            'total_discounts': _money(node.get('totalDiscountsSet')),
            'total_shipping_price': _money(node.get('totalShippingPriceSet')),
            'total_tax': _money(node.get('totalTaxSet')),
            'total_price': _money(node.get('totalPriceSet')),
            'customer': {
                'id': parse_gid(customer.get('id')) or parse_gid(customer.get('legacyResourceId')),
                'first_name': customer.get('firstName'),
                'last_name': customer.get('lastName'),
                'email': customer.get('email'),
                'phone': customer.get('phone'),
                'tags': ', '.join(customer.get('tags') or []) if isinstance(customer.get('tags'), list) else customer.get('tags'),
                'note': customer.get('note'),
                'default_address': self._normalise_graphql_address(customer.get('defaultAddress')),
            } if customer else {},
            'billing_address': self._normalise_graphql_address(node.get('billingAddress')),
            'shipping_address': self._normalise_graphql_address(node.get('shippingAddress')),
            'tax_lines': self._normalise_tax_lines(node.get('taxLines'), graphql=True),
            'line_items': line_items,
            'shipping_lines': shipping_lines,
            'transactions': [
                {
                    'gateway': t.get('gateway'),
                    'kind': (t.get('kind') or '').lower(),
                    'status': (t.get('status') or '').lower(),
                    'amount': _money(t.get('amountSet')),
                }
                for t in node.get('transactions') or []
            ],
            'tracking_numbers': tracking_numbers,
            'primary_tracking_number': tracking_numbers[0] if tracking_numbers else False,
            'raw': node,
        }


# ----------------------------------------------------------------------
# GraphQL documents
# ----------------------------------------------------------------------

SHOP_QUERY = """
query ShopifyConnectorShop {
  shop {
    name
    myshopifyDomain
    email
    ianaTimezone
    currencyCode
    plan { displayName }
  }
}
"""

_ORDER_FRAGMENT = """
fragment MoneyBagFields on MoneyBag {
  shopMoney { amount currencyCode }
  presentmentMoney { amount currencyCode }
}

fragment AddressFields on MailingAddress {
  firstName lastName name company
  address1 address2 city province provinceCode
  zip country countryCodeV2 phone
}

fragment TaxLineFields on TaxLine {
  title
  rate
  priceSet { ...MoneyBagFields }
}

fragment OrderFields on Order {
  id
  legacyResourceId
  name
  number
  createdAt
  updatedAt
  processedAt
  cancelledAt
  closedAt
  currencyCode
  presentmentCurrencyCode
  displayFinancialStatus
  displayFulfillmentStatus
  taxesIncluded
  taxExempt
  test
  note
  tags
  email
  phone
  paymentGatewayNames
  discountCodes
  subtotalPriceSet { ...MoneyBagFields }
  totalDiscountsSet { ...MoneyBagFields }
  totalShippingPriceSet { ...MoneyBagFields }
  totalTaxSet { ...MoneyBagFields }
  totalPriceSet { ...MoneyBagFields }
  billingAddress { ...AddressFields }
  shippingAddress { ...AddressFields }
  customer {
    id
    legacyResourceId
    firstName
    lastName
    email
    phone
    tags
    note
    defaultAddress { ...AddressFields }
  }
  taxLines { ...TaxLineFields }
  shippingLines(first: 20) {
    nodes {
      id
      title
      code
      source
      originalPriceSet { ...MoneyBagFields }
      discountedPriceSet { ...MoneyBagFields }
      taxLines { ...TaxLineFields }
    }
  }
  lineItems(first: 250) {
    nodes {
      id
      name
      title
      variantTitle
      sku
      vendor
      quantity
      currentQuantity
      requiresShipping
      taxable
      originalUnitPriceSet { ...MoneyBagFields }
      discountedUnitPriceSet { ...MoneyBagFields }
      totalDiscountSet { ...MoneyBagFields }
      taxLines { ...TaxLineFields }
      product { id legacyResourceId }
      variant { id legacyResourceId sku barcode title }
    }
  }
  transactions(first: 20) {
    gateway
    kind
    status
    amountSet { ...MoneyBagFields }
  }
  fulfillments(first: 20) {
    status
    trackingInfo {
      company
      number
      url
    }
  }
}
"""

ORDERS_QUERY = _ORDER_FRAGMENT + """
query ShopifyConnectorOrders($first: Int!, $after: String, $query: String) {
  orders(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes { ...OrderFields }
  }
}
"""

ORDER_QUERY = _ORDER_FRAGMENT + """
query ShopifyConnectorOrder($id: ID!) {
  order(id: $id) { ...OrderFields }
}
"""

PRODUCTS_QUERY = """
query ShopifyConnectorProducts($first: Int!, $after: String, $query: String) {
  products(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      legacyResourceId
      title
      variants(first: 250) {
        nodes {
          id
          legacyResourceId
          title
          sku
          barcode
          selectedOptions {
            name
            value
          }
        }
      }
    }
  }
}
"""
