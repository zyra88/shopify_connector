import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services.shopify_client import (
    DEFAULT_API_VERSION,
    ShopifyClient,
    ShopifyError,
    exchange_client_credentials,
)

_logger = logging.getLogger(__name__)

# Refresh Dev Dashboard tokens before Shopify's 24-hour expiry.
TOKEN_REFRESH_MARGIN_SECONDS = 300
# Shopify REST ``ids`` filter batch size when re-fetching orders for Studio Order ID.
STUDIO_ORDER_ID_SHOPIFY_FETCH_BATCH = 100
IMPORT_CRON = 'shopify_connector.ir_cron_shopify_import_orders'
SCHEDULED_CRONS = (IMPORT_CRON, 'shopify_connector.ir_cron_shopify_process_queue')
SYNC_INTERVALS = {
    '5m': (5, 'minutes'),
    '15m': (15, 'minutes'),
    '30m': (30, 'minutes'),
    '1h': (1, 'hours'),
    '2h': (2, 'hours'),
    '6h': (6, 'hours'),
    '12h': (12, 'hours'),
    '1d': (1, 'days'),
}


def _chunked(sequence, size):
    sequence = list(sequence)
    for index in range(0, len(sequence), size):
        yield sequence[index:index + size]


class ShopifyStore(models.Model):
    _name = 'shopify.store'
    _description = 'Shopify Store'
    _inherit = ['mail.thread']
    _order = 'sequence, name'

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    color = fields.Integer()

    # -- Connection ----------------------------------------------------
    shop_domain = fields.Char(
        string='Shop Domain', required=True, tracking=True,
        help="Your myshopify domain, for example mystore.myshopify.com. "
             "A bare handle or a full URL is accepted too.")
    client_id = fields.Char(
        string='Client ID', required=True, copy=False,
        groups='shopify_connector.group_shopify_manager',
        help="Client ID from your app's Settings page in the Dev Dashboard.")
    client_secret = fields.Char(
        string='Client Secret', required=True, copy=False,
        groups='shopify_connector.group_shopify_manager',
        help="Client secret (shpss_...) from the Dev Dashboard. The connector "
             "exchanges it for a short-lived access token automatically.")
    access_token = fields.Char(
        string='Cached Access Token', copy=False, readonly=True,
        groups='shopify_connector.group_shopify_manager',
        help="Short-lived token returned by Shopify after exchanging the "
             "Client secret. Refreshed automatically; do not edit.")
    access_token_expires_at = fields.Datetime(
        string='Access Token Expires', readonly=True, copy=False,
        groups='shopify_connector.group_shopify_manager',
        help="When the current Dev Dashboard access token expires. Refreshed "
             "automatically before each API call.")
    api_version = fields.Char(
        required=True, default=DEFAULT_API_VERSION,
        help="Shopify releases a new version every quarter and supports each "
             "for at least 12 months. Bump this before your version expires.")
    api_protocol = fields.Selection(
        [('graphql', 'GraphQL (recommended)'), ('rest', 'REST (legacy)')],
        string='API Transport', default='graphql', required=True,
        help="Shopify marked the REST Admin API as legacy in October 2024. "
             "Use REST only if your app predates the GraphQL migration.")

    connection_state = fields.Selection(
        [('draft', 'Not Tested'), ('connected', 'Connected'), ('error', 'Error')],
        default='draft', readonly=True, copy=False, tracking=True)
    connection_message = fields.Text(readonly=True, copy=False)
    shop_name = fields.Char(readonly=True, copy=False)
    shop_email = fields.Char(readonly=True, copy=False)
    shop_currency_code = fields.Char(string='Shopify Currency', readonly=True, copy=False)
    shop_timezone = fields.Char(readonly=True, copy=False)
    shop_plan = fields.Char(readonly=True, copy=False)

    # -- Odoo bindings -------------------------------------------------
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company, tracking=True)
    currency_id = fields.Many2one(
        'res.currency', string='Store Currency', required=True,
        default=lambda self: self.env.company.currency_id,
        help="Currency your Shopify store settles in. Orders in another "
             "currency need a pricelist in that currency to import cleanly.")
    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Warehouse', required=True,
        domain="[('company_id', '=', company_id)]")
    pricelist_id = fields.Many2one(
        'product.pricelist', string='Pricelist',
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Optional. Sale order line prices always come from Shopify. When "
             "set, this pricelist is preferred for the sale order currency; "
             "otherwise the connector picks any company pricelist that matches "
             "the Shopify order currency, or leaves Odoo to use the customer's "
             "default pricelist.")
    team_id = fields.Many2one(
        'crm.team', string='Sales Team',
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")
    user_id = fields.Many2one(
        'res.users', string='Salesperson',
        domain="[('share', '=', False)]")
    payment_term_id = fields.Many2one(
        'account.payment.term', string='Default Payment Terms',
        help="Fallback when the Shopify payment method cannot be mapped. "
             "COD orders use the COD term; online/Razorpay orders use Prepaid.")

    custom_field_ids = fields.One2many(
        'shopify.custom.field', 'store_id', string='Custom Field Mappings',
        context={'active_test': False}, copy=True)

    # -- Sale order options --------------------------------------------
    use_custom_sale_order_prefix = fields.Boolean(
        string='Custom Sale Order Name',
        help="When enabled, imported sale orders use your prefix plus the "
             "Shopify order name (e.g. SH/#1001). When disabled, Odoo's own "
             "sale order sequence is used and the Shopify reference is kept on "
             "client_order_ref.")
    sale_order_prefix = fields.Char(
        string='Sale Order Prefix',
        help="Only used when Custom Sale Order Name is enabled, "
             "e.g. SH/ turns Shopify #1001 into SH/#1001.")
    order_from_available = fields.Boolean(
        compute='_compute_order_from_available',
        help="Technical: True when sale.order has an Order From selection field.")
    order_from = fields.Selection(
        selection='_get_order_from_selection',
        string='Order From',
        default=lambda self: self._default_studio_selection('Website', 'order_from'),
        help="Written on every imported sale order (e.g. x_studio_order_from_1).")
    type_of_order_available = fields.Boolean(
        compute='_compute_type_of_order_available',
        help="Technical: True when sale.order has a Type of Order Studio field.")
    type_of_order = fields.Selection(
        selection='_get_type_of_order_selection',
        string='Type of Order',
        default=lambda self: self._default_studio_selection('Website', 'type_of_order'),
        help="Written on every imported sale order (e.g. x_studio_type_of_order).")
    auto_confirm_order = fields.Boolean(
        string='Auto-Confirm Sale Orders',
        help="Confirm imported orders automatically. Leave off until product "
             "mapping is proven, otherwise mistakes reserve stock.")
    invoice_mode = fields.Selection(
        [('none', "Don't create"), ('draft', 'Draft'), ('posted', 'Posted')],
        string='Create Invoice', default='none', required=True,
        help="Invoice the sale orders this connector confirms, right after confirmation. "
             "Posted: the invoice is validated too. Needs Auto-Confirm Sale Orders. "
             "Orders confirmed by hand or before this was set are not invoiced.")
    link_existing_sale_orders = fields.Boolean(
        string='Link Existing Sale Orders',
        default=True,
        help="When enabled, import reuses an existing quotation or sale order "
             "that already carries the same Studio Order ID, client reference "
             "(#1001), or Shopify order id instead of creating a duplicate.")
    studio_order_id_mode = fields.Selection(
        [
            ('numeric', 'Numeric Shopify order ID'),
            ('order_name', 'Shopify order name (TR-001270)'),
            (
                'name_and_tracking',
                'Order name and Ekart tracking (TR-001270-LUAC0001601959)',
            ),
        ],
        string='Studio Order ID Format',
        default='name_and_tracking',
        required=True,
        help="Written into the Studio Order ID field on imported sale orders. "
             "Tracking comes from Shopify fulfillments (e.g. Ekart LUAC… numbers).")
    import_order_note = fields.Boolean(string='Import Order Note', default=True)
    warehouse_on_order = fields.Boolean(
        string='Force Warehouse', default=True,
        help="Set the store warehouse on every imported sale order.")

    # -- Import filters -------------------------------------------------
    order_import_from_date = fields.Datetime(
        string='Import Orders From', required=True,
        default=lambda self: fields.Datetime.subtract(fields.Datetime.now(), days=30),
        help="First run only: the oldest order to pull. Later runs continue "
             "from Last Order Sync.")
    last_order_sync_date = fields.Datetime(string='Last Order Sync', readonly=True, copy=False)
    last_product_sync_date = fields.Datetime(string='Last Product Sync', readonly=True, copy=False)
    sync_overlap_minutes = fields.Integer(
        default=10,
        help="Re-scan this many minutes before the last sync. Guards against "
             "clock skew; duplicates are prevented by the order mapping.")
    import_order_status = fields.Selection(
        [('any', 'Any'), ('open', 'Open'), ('closed', 'Closed'), ('cancelled', 'Cancelled')],
        default='any', required=True, string='Order Status')
    import_financial_status = fields.Selection(
        [
            ('any', 'Any'),
            ('paid', 'Paid'),
            ('pending', 'Pending'),
            ('authorized', 'Authorized'),
            ('partially_paid', 'Partially Paid'),
            ('refunded', 'Refunded'),
        ],
        default='any', required=True, string='Financial Status')
    import_fulfillment_status = fields.Selection(
        [
            ('any', 'Any'),
            ('unfulfilled', 'Unfulfilled'),
            ('partial', 'Partially Fulfilled'),
            ('fulfilled', 'Fulfilled'),
        ],
        default='any', required=True, string='Fulfillment Status')
    import_test_orders = fields.Boolean(string='Include Test Orders')
    page_size = fields.Integer(string='Page Size', default=50)
    max_orders_per_run = fields.Integer(
        string='Max Orders / Run', default=0, help="0 means no limit.")
    auto_sync = fields.Boolean(
        string='Scheduled Sync', default=False,
        help="Include this store in the scheduled order import.")
    sync_interval = fields.Selection(
        [('5m', '5 minutes'), ('15m', '15 minutes'), ('30m', '30 minutes'), ('1h', '1 hour'),
         ('2h', '2 hours'), ('6h', '6 hours'), ('12h', '12 hours'), ('1d', '1 day')],
        string='Sync Every', compute='_compute_sync_interval', inverse='_inverse_sync_interval',
        help="How often the scheduled import runs. Shared by every Shopify store.")

    # -- Customer resolution --------------------------------------------
    match_customer_by_email = fields.Boolean(default=True)
    match_customer_by_phone = fields.Boolean(default=True)
    update_existing_customer = fields.Boolean(
        default=True,
        help="Refresh name, email and phone on already-linked customers. "
             "Only fields Shopify owns are touched.")
    customer_tag_ids = fields.Many2many(
        'res.partner.category', string='Tags on New Customers',
        help="Applied to customers this connector creates.")
    receivable_account_id = fields.Many2one(
        'account.account', string='Customer Receivable Account',
        domain="[('account_type', '=', 'asset_receivable'), ('deprecated', '=', False), "
               "('company_ids', 'parent_of', company_id)]",
        help="Account Receivable set on the customers this connector creates or owns "
             "(linked to a Shopify customer). Customers created by hand in Odoo and "
             "matched by email or phone keep their own. Empty: Odoo's default.")
    guest_partner_id = fields.Many2one(
        'res.partner', string='Fallback Guest Customer',
        help="Used only when a guest order has no email, no phone and no "
             "billing name. Leave empty to send such orders to Needs Review.")

    # -- Product resolution ---------------------------------------------
    match_product_by_sku = fields.Boolean(string='Match by SKU', default=True)
    match_product_by_attributes = fields.Boolean(
        string='Match by SKU Attributes', default=True,
        help="Parse Shopify SKUs like TSG-2022-Black-6 into model, colour and "
             "size, then match Odoo variants by those attributes when the "
             "internal reference is empty.")
    match_product_by_barcode = fields.Boolean(string='Match by Barcode', default=True)
    write_sku_on_match = fields.Boolean(
        string='Copy Shopify SKU on Match', default=True,
        help="Fill the Odoo internal reference with the Shopify SKU when a "
             "variant is mapped, so later order imports resolve by SKU too.")
    shipping_product_id = fields.Many2one(
        'product.product', string='Shipping Product',
        domain="[('type', '=', 'service')]",
        help="Service product used for Shopify shipping lines.")
    discount_product_id = fields.Many2one(
        'product.product', string='Discount Product',
        domain="[('type', '=', 'service')]",
        help="Service product used when an order-level discount cannot be "
             "allocated to the order lines.")

    # -- Taxes ------------------------------------------------------------
    tax_mode = fields.Selection(
        [
            ('shopify', 'Use Shopify tax lines'),
            ('odoo', 'Use Odoo product taxes'),
            ('none', 'No taxes'),
        ],
        default='shopify', required=True, string='Tax Handling',
        help="Shopify: reproduce the tax charged at checkout, creating the "
             "matching Odoo tax if needed. Odoo: keep the taxes configured on "
             "the products and let Odoo recompute.")
    tax_account_prefix = fields.Char(
        string='Tax Name Prefix', default='Shopify',
        help="Prefix for taxes this connector creates, so they are easy to find.")

    # -- Reconciliation -----------------------------------------------------
    amount_tolerance = fields.Float(
        string='Total Tolerance', default=0.01, digits=(16, 4),
        help="Maximum accepted gap between the Shopify total and the Odoo "
             "total before the order is flagged Needs Review.")

    # -- Counters -----------------------------------------------------------
    shopify_order_count = fields.Integer(compute='_compute_counts')
    needs_review_count = fields.Integer(compute='_compute_counts')
    sale_order_count = fields.Integer(compute='_compute_counts')
    customer_mapping_count = fields.Integer(compute='_compute_counts')
    variant_mapping_count = fields.Integer(compute='_compute_counts')
    queue_count = fields.Integer(compute='_compute_counts')
    error_log_count = fields.Integer(compute='_compute_counts')

    _sql_constraints = [
        (
            'shop_domain_company_uniq',
            'UNIQUE(shop_domain, company_id)',
            'A Shopify store with this domain already exists for this company.',
        ),
        (
            'page_size_range',
            'CHECK(page_size BETWEEN 1 AND 250)',
            'Page size must be between 1 and 250 (Shopify\'s maximum).',
        ),
    ]

    # ------------------------------------------------------------------
    # Schedule (the shared "Shopify: Import Orders" scheduled action)
    # ------------------------------------------------------------------

    def _import_cron(self):
        return self.env.ref(IMPORT_CRON, raise_if_not_found=False) or self.env['ir.cron']

    def _compute_sync_interval(self):
        cron = self.sudo()._import_cron()
        current = next((key for key, value in SYNC_INTERVALS.items()
                        if value == (cron.interval_number, cron.interval_type)), False)
        for store in self:
            store.sync_interval = current

    def _inverse_sync_interval(self):
        cron = self.sudo()._import_cron()
        interval = SYNC_INTERVALS.get(self[:1].sync_interval)
        if cron and interval:
            cron.write({'interval_number': interval[0], 'interval_type': interval[1]})

    @api.model
    def _update_scheduled_crons(self):
        """Run the scheduled actions exactly when some store has Scheduled Sync on."""
        active = bool(self.sudo().search_count([('auto_sync', '=', True)]))
        for xmlid in SCHEDULED_CRONS:
            cron = self.env.ref(xmlid, raise_if_not_found=False)
            if cron and cron.sudo().active != active:
                cron.sudo().active = active

    @api.model_create_multi
    def create(self, vals_list):
        stores = super().create(vals_list)
        if any(vals.get('auto_sync') for vals in vals_list):
            self._update_scheduled_crons()
        return stores

    def write(self, vals):
        result = super().write(vals)
        if 'auto_sync' in vals or 'active' in vals:
            self._update_scheduled_crons()
        return result

    def unlink(self):
        result = super().unlink()
        self._update_scheduled_crons()
        return result

    # ------------------------------------------------------------------
    # Compute / constraints
    # ------------------------------------------------------------------

    def _compute_counts(self):
        def grouped(model, domain=()):
            if not self.ids:
                return {}
            data = self.env[model]._read_group(
                [('store_id', 'in', self.ids)] + list(domain), ['store_id'], ['__count'])
            return {store.id: count for store, count in data}

        orders = grouped('shopify.order')
        review = grouped('shopify.order', [('state', '=', 'needs_review')])
        customers = grouped('shopify.customer.mapping')
        variants = grouped('shopify.variant.mapping')
        queue = grouped('shopify.sync.queue', [('state', 'in', ('pending', 'processing', 'failed'))])
        errors = grouped('shopify.sync.log', [('level', '=', 'error')])

        sale_orders = {}
        if self.ids:
            sale_orders = {
                store.id: count for store, count in self.env['sale.order']._read_group(
                    [('shopify_store_id', 'in', self.ids)], ['shopify_store_id'], ['__count'])
            }

        for store in self:
            store.shopify_order_count = orders.get(store.id, 0)
            store.needs_review_count = review.get(store.id, 0)
            store.sale_order_count = sale_orders.get(store.id, 0)
            store.customer_mapping_count = customers.get(store.id, 0)
            store.variant_mapping_count = variants.get(store.id, 0)
            store.queue_count = queue.get(store.id, 0)
            store.error_log_count = errors.get(store.id, 0)

    @api.constrains('client_id', 'client_secret')
    def _check_auth_credentials(self):
        for store in self:
            if not (store.client_id or '').strip() or not (store.client_secret or '').strip():
                raise ValidationError(
                    _("Client ID and Client secret are required."))

    @api.constrains('use_custom_sale_order_prefix', 'sale_order_prefix')
    def _check_sale_order_prefix(self):
        for store in self:
            if store.use_custom_sale_order_prefix and not (store.sale_order_prefix or '').strip():
                raise ValidationError(
                    _("Set a sale order prefix or disable Custom Sale Order Name."))

    @api.constrains('company_id', 'warehouse_id', 'pricelist_id')
    def _check_company_consistency(self):
        for store in self:
            if store.warehouse_id.company_id != store.company_id:
                raise ValidationError(
                    _("Warehouse %(wh)s does not belong to company %(company)s.",
                      wh=store.warehouse_id.display_name, company=store.company_id.display_name))
            if not store.pricelist_id:
                continue
            pricelist_company = store.pricelist_id.company_id
            if pricelist_company and pricelist_company != store.company_id:
                raise ValidationError(
                    _("Pricelist %(pl)s does not belong to company %(company)s.",
                      pl=store.pricelist_id.display_name, company=store.company_id.display_name))

    @api.onchange('company_id')
    def _onchange_company_id(self):
        self.currency_id = self.company_id.currency_id
        if self.warehouse_id.company_id != self.company_id:
            self.warehouse_id = self.env['stock.warehouse'].search(
                [('company_id', '=', self.company_id.id)], limit=1)

    # ------------------------------------------------------------------
    # Client
    # ------------------------------------------------------------------

    def _get_valid_access_token(self, force_refresh=False):
        """Return a token that is valid right now, refreshing when needed."""
        self.ensure_one()
        store = self.sudo()
        now = fields.Datetime.now()
        token = (store.access_token or '').strip()
        expires_at = store.access_token_expires_at
        if (
            not force_refresh
            and token
            and expires_at
            and expires_at > fields.Datetime.add(now, seconds=TOKEN_REFRESH_MARGIN_SECONDS)
        ):
            return token
        return store._refresh_client_credentials_token()

    def _refresh_client_credentials_token(self):
        """Exchange Client ID + secret for a new access token and cache it."""
        self.ensure_one()
        store = self.sudo()
        try:
            result = exchange_client_credentials(
                store.shop_domain,
                store.client_id,
                store.client_secret,
            )
        except ShopifyError as exc:
            raise UserError(_("Could not authenticate with Shopify: %s", exc)) from exc

        expires_at = fields.Datetime.add(
            fields.Datetime.now(),
            seconds=max(result['expires_in'] - 60, 60),
        )
        store.write({
            'access_token': result['access_token'],
            'access_token_expires_at': expires_at,
        })
        _logger.info(
            "Refreshed Shopify access token for store %s (expires %s).",
            store.display_name, expires_at,
        )
        return result['access_token']

    def get_client(self):
        """Return a configured :class:`ShopifyClient` for this store."""
        self.ensure_one()
        store = self.sudo()
        try:
            return ShopifyClient(
                shop_domain=store.shop_domain,
                token_provider=store._get_valid_access_token,
                api_version=store.api_version,
                protocol=store.api_protocol,
            )
        except ShopifyError as exc:
            raise UserError(_("Shopify store %(store)s is not configured: %(error)s",
                              store=self.display_name, error=exc)) from exc

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_test_connection(self):
        self.ensure_one()
        log = self.env['shopify.sync.log']
        try:
            info = self.get_client().test_connection()
        except (ShopifyError, UserError) as exc:
            message = str(exc)
            self.sudo().write({'connection_state': 'error', 'connection_message': message})
            log.log(self, 'error', 'test_connection', message)
            return self._notify(_("Connection failed: %s", message), warning=True, sticky=True)

        self.sudo().write({
            'connection_state': 'connected',
            'connection_message': False,
            'shop_name': info.get('name'),
            'shop_email': info.get('email'),
            'shop_currency_code': info.get('currency'),
            'shop_timezone': info.get('timezone'),
            'shop_plan': info.get('plan'),
        })
        message = _("Connected to %(shop)s (%(currency)s).",
                    shop=info.get('name') or self.shop_domain, currency=info.get('currency') or '?')
        log.log(self, 'info', 'test_connection', message)

        if info.get('currency') and self.currency_id.name != info['currency']:
            return self._notify(
                _("%(msg)s Shopify settles in %(shopify)s but the store is configured as "
                  "%(odoo)s; amounts will not reconcile until this matches.",
                  msg=message, shopify=info['currency'], odoo=self.currency_id.name),
                warning=True, sticky=True)
        return self._notify(message)

    def action_sync_products(self):
        """Fetch Shopify products and map variant ids onto Odoo products."""
        totals = {
            'fetched': 0, 'variants': 0, 'mapped': 0, 'updated': 0,
            'unmatched': 0, 'conflicts': 0,
        }
        errors = []
        for store in self:
            try:
                result = store.sync_products(full_sync=True)
            except (ShopifyError, UserError) as exc:
                errors.append('%s: %s' % (store.display_name, exc))
                self.env['shopify.sync.log'].log(store, 'error', 'product_sync', str(exc))
                continue
            for key in totals:
                totals[key] += result.get(key, 0)

        parts = [
            _("%(fetched)s Shopify product(s) fetched", fetched=totals['fetched']),
            _("%(mapped)s variant(s) mapped", mapped=totals['mapped']),
            _("%(updated)s already mapped", updated=totals['updated']),
        ]
        if totals['unmatched']:
            parts.append(_("%(count)s unmatched", count=totals['unmatched']))
        if totals['conflicts']:
            parts.append(_("%(count)s conflict(s)", count=totals['conflicts']))
        message = ', '.join(parts) + '.'
        if not totals['fetched']:
            message += ' ' + _("No Shopify products were returned. Check the connection "
                               "and try again.")
        if errors:
            message += '\n' + '\n'.join(errors)
        return self._notify(
            message,
            warning=bool(errors or totals['unmatched'] or totals['conflicts']),
            sticky=bool(errors),
        )

    def sync_products(self, updated_at_min=None, max_products=None, full_sync=False):
        """Fetch Shopify products and write variant ids onto matching Odoo products."""
        self.ensure_one()
        run_started_at = fields.Datetime.now()
        if not full_sync and updated_at_min is None and self.last_product_sync_date:
            updated_at_min = fields.Datetime.subtract(
                self.last_product_sync_date, minutes=max(self.sync_overlap_minutes, 0))

        stats = self.env['shopify.product.sync'].sync(
            self, updated_at_min=self._to_shopify_datetime(updated_at_min),
            max_products=max_products,
        )
        self.sudo().write({'last_product_sync_date': run_started_at})
        return stats

    def action_scan_order_duplicates(self):
        """Open the duplicate-order screen after scanning this store."""
        self.ensure_one()
        return self.env['shopify.sale.order.conflict'].with_context(
            default_store_id=self.id,
            active_model='shopify.store',
            active_id=self.id,
        ).action_scan_duplicates()

    show_fill_studio_order_id_button = fields.Boolean(
        compute='_compute_show_fill_studio_order_id_button',
        string='Show Fill Order ID Button',
    )
    linked_orders_missing_studio_order_id_count = fields.Integer(
        compute='_compute_show_fill_studio_order_id_button',
        string='Linked Orders Missing Order ID',
    )

    @api.depends()
    def _compute_show_fill_studio_order_id_button(self):
        for store in self:
            count = store._count_imported_orders_missing_studio_order_id()
            store.linked_orders_missing_studio_order_id_count = count
            store.show_fill_studio_order_id_button = bool(count)

    def _count_imported_orders_missing_studio_order_id(self):
        """Imported (Shopify-linked) sale orders with an empty Studio Order ID."""
        self.ensure_one()
        field_name = self._sale_order_studio_order_id_field_name()
        if not field_name:
            return 0
        return self.env['sale.order'].search_count([
            ('company_id', '=', self.company_id.id),
            ('shopify_store_id', '=', self.id),
            ('shopify_order_id', '!=', False),
            (field_name, '=', False),
        ])

    @api.model
    def _count_linked_orders_missing_studio_order_id(self, store_ids=None):
        Store = self.env['shopify.store']
        if store_ids:
            stores = Store.browse(list(store_ids))
        else:
            stores = Store.search([('company_id', 'in', self.env.companies.ids)])
        return sum(store._count_imported_orders_missing_studio_order_id() for store in stores)

    def action_backfill_studio_order_ids(self):
        """Re-fetch from Shopify and fill Studio Order ID on imported orders missing it."""
        self.ensure_one()
        count = self._sync_studio_order_ids_on_linked_orders(
            refresh_from_shopify=True,
            force_rewrite=True,
            only_missing_studio_order_id=True,
        )
        return self._notify(
            _("%(count)s sale order(s) updated with Studio Order ID from Shopify.",
              count=count),
        )

    def _backfill_studio_order_id_field(self):
        """Fill or upgrade Studio Order ID using stored mirror payloads only."""
        self.ensure_one()
        return self._sync_studio_order_ids_on_linked_orders(
            refresh_from_shopify=False, force_rewrite=False)

    def _sync_studio_order_ids_on_linked_orders(
            self, refresh_from_shopify=True, force_rewrite=False,
            only_missing_studio_order_id=False):
        """Return how many ``sale.order`` Studio Order ID values were written."""
        self.ensure_one()
        field_name = self._sale_order_studio_order_id_field_name()
        if not field_name:
            return 0
        domain = [
            ('company_id', '=', self.company_id.id),
            ('shopify_store_id', '=', self.id),
            ('shopify_order_id', '!=', False),
        ]
        if only_missing_studio_order_id:
            domain.append((field_name, '=', False))
        orders = self.env['sale.order'].search(domain)
        if not orders:
            return 0

        shopify_ids = list({order.shopify_order_id for order in orders})
        payloads = self._fetch_shopify_order_payloads(shopify_ids, refresh_from_shopify)

        updated = 0
        for order in orders:
            payload = payloads.get(order.shopify_order_id) or {}
            values = self.get_studio_order_id_refresh_values(
                order.shopify_order_id, payload, order[field_name],
                force=force_rewrite)
            if values:
                order.write(values)
                updated += 1
        return updated

    def _fetch_shopify_order_payloads(self, shopify_order_ids, refresh_from_shopify):
        """Map Shopify order id → normalised payload (mirror updated when refreshing)."""
        self.ensure_one()
        payloads = {}
        if not shopify_order_ids:
            return payloads
        if refresh_from_shopify:
            client = self.get_client()
            importer = self.env['shopify.order.importer']
            for batch in _chunked(shopify_order_ids, STUDIO_ORDER_ID_SHOPIFY_FETCH_BATCH):
                for payload in client.iter_orders(
                        shopify_order_ids=batch, status='any', page_size=len(batch)):
                    shopify_id = str(payload.get('id') or '')
                    if not shopify_id:
                        continue
                    payloads[shopify_id] = payload
                    importer._upsert_shopify_order(self, payload)
            for shopify_id in shopify_order_ids:
                if shopify_id in payloads:
                    continue
                payload = client.get_order(shopify_id)
                if payload:
                    payloads[shopify_id] = payload
                    importer._upsert_shopify_order(self, payload)
        else:
            for shopify_id in shopify_order_ids:
                payloads[shopify_id] = self._order_payload_for_studio_order_id(shopify_id)
        return payloads

    def _order_payload_for_studio_order_id(self, shopify_order_id):
        """Return the normalised payload stored on the Shopify order mirror."""
        self.ensure_one()
        mirror = self.env['shopify.order'].search([
            ('store_id', '=', self.id),
            ('shopify_order_id', '=', shopify_order_id),
        ], limit=1)
        if not mirror or not mirror.payload:
            return {}
        try:
            return json.loads(mirror.payload)
        except ValueError:
            return {}

    def action_import_orders(self):
        """Fetch, queue and process orders for every selected store."""
        totals = {'fetched': 0, 'done': 0, 'needs_review': 0, 'failed': 0}
        errors = []
        for store in self:
            try:
                result = store.sync_orders()
            except (ShopifyError, UserError) as exc:
                errors.append('%s: %s' % (store.display_name, exc))
                self.env['shopify.sync.log'].log(store, 'error', 'order_fetch', str(exc))
                continue
            for key in totals:
                totals[key] += result.get(key, 0)

        parts = [_("%s order(s) fetched", totals['fetched']),
                 _("%s imported", totals['done'])]
        if totals['needs_review']:
            parts.append(_("%s need review", totals['needs_review']))
        if totals['failed']:
            parts.append(_("%s failed", totals['failed']))
        message = ', '.join(parts) + '.'
        if errors:
            message += '\n' + '\n'.join(errors)
        return self._notify(message, warning=bool(errors or totals['failed'] or totals['needs_review']),
                            sticky=bool(errors or totals['failed']))

    def sync_orders(self, updated_at_min=None, shopify_order_ids=None, max_orders=None):
        """Fetch orders into the queue and process them.

        Returns ``{'fetched', 'done', 'needs_review', 'failed'}``.
        """
        self.ensure_one()
        client = self.get_client()
        queue_model = self.env['shopify.sync.queue']
        log = self.env['shopify.sync.log']

        # Captured before fetching so orders modified *during* the run are
        # picked up next time rather than skipped.
        run_started_at = fields.Datetime.now()

        if not shopify_order_ids and updated_at_min is None:
            updated_at_min = self._get_sync_cursor()

        filters = {
            'updated_at_min': self._to_shopify_datetime(updated_at_min),
            'status': self.import_order_status,
            'financial_status': None if self.import_financial_status == 'any' else self.import_financial_status,
            'fulfillment_status': None if self.import_fulfillment_status == 'any' else self.import_fulfillment_status,
            'shopify_order_ids': shopify_order_ids or None,
            'page_size': self.page_size or 50,
            'max_orders': max_orders or self.max_orders_per_run or None,
            'include_test_orders': self.import_test_orders,
        }

        jobs = queue_model.browse()
        fetched = 0
        for payload in client.iter_orders(**filters):
            jobs |= queue_model.enqueue_order(self, payload)
            fetched += 1

        log.log(self, 'info', 'order_fetch',
                _("Fetched %(count)s order(s) from Shopify%(since)s.",
                  count=fetched,
                  since=_(" updated since %s", filters['updated_at_min']) if filters['updated_at_min'] else ''))

        summary = jobs.process() if jobs else {'done': 0, 'needs_review': 0, 'failed': 0}

        if not shopify_order_ids:
            self.sudo().write({'last_order_sync_date': run_started_at})

        summary['fetched'] = fetched
        return summary

    def _get_sync_cursor(self):
        """Datetime to pass as ``updated_at_min``, with the overlap applied."""
        self.ensure_one()
        if not self.last_order_sync_date:
            return self.order_import_from_date
        return fields.Datetime.subtract(
            self.last_order_sync_date, minutes=max(self.sync_overlap_minutes, 0))

    @staticmethod
    def _to_shopify_datetime(value):
        if not value:
            return None
        if isinstance(value, str):
            return value
        return fields.Datetime.to_string(value).replace(' ', 'T') + 'Z'

    def action_reset_sync_cursor(self):
        self.sudo().write({'last_order_sync_date': False})
        return self._notify(_("Sync cursor cleared. The next import starts from the configured date."))

    # ------------------------------------------------------------------
    # Order From (Studio field on sale.order)
    # ------------------------------------------------------------------

    @api.model
    def _sale_order_order_from_field_names(self):
        return ('x_studio_order_from_1', 'x_studio_order_from')

    @api.model
    def _sale_order_order_from_field_name(self):
        """Return the technical name of the Order From field, if installed."""
        SaleOrder = self.env['sale.order']
        for field_name in self._sale_order_order_from_field_names():
            field = SaleOrder._fields.get(field_name)
            if field and field.type == 'selection':
                return field_name
        return False

    @api.model
    def _get_order_from_selection(self):
        """Populate store options from the sale.order Order From field."""
        SaleOrder = self.env['sale.order']
        field_name = self._sale_order_order_from_field_name()
        if not field_name:
            return []
        field = SaleOrder._fields[field_name]
        selection = field.selection
        if callable(selection):
            selection = selection(SaleOrder)
        return list(selection or [])

    @api.depends()
    def _compute_order_from_available(self):
        available = bool(self._sale_order_order_from_field_name())
        for store in self:
            store.order_from_available = available

    def get_order_from_sale_order_values(self):
        """Values to merge into sale.order when importing."""
        self.ensure_one()
        if not self.order_from:
            return {}
        field_name = self._sale_order_order_from_field_name()
        if field_name:
            return {field_name: self.order_from}
        return {}

    @api.model
    def _sale_order_type_of_order_field_names(self):
        return ('x_studio_type_of_order',)

    @api.model
    def _sale_order_type_of_order_field_name(self):
        SaleOrder = self.env['sale.order']
        for field_name in self._sale_order_type_of_order_field_names():
            field = SaleOrder._fields.get(field_name)
            if field and field.type == 'selection':
                return field_name
        return False

    @api.model
    def _get_type_of_order_selection(self):
        SaleOrder = self.env['sale.order']
        field_name = self._sale_order_type_of_order_field_name()
        if not field_name:
            return []
        field = SaleOrder._fields[field_name]
        selection = field.selection
        if callable(selection):
            selection = selection(SaleOrder)
        return list(selection or [])

    @api.depends()
    def _compute_type_of_order_available(self):
        available = bool(self._sale_order_type_of_order_field_name())
        for store in self:
            store.type_of_order_available = available

    def get_type_of_order_sale_order_values(self):
        self.ensure_one()
        if not self.type_of_order:
            return {}
        field_name = self._sale_order_type_of_order_field_name()
        if field_name:
            return {field_name: self.type_of_order}
        return {}

    @api.model
    def _default_studio_selection(self, preferred_key, kind):
        """Pick ``preferred_key`` when it exists on the related Studio selection field."""
        if kind == 'order_from':
            selection = self._get_order_from_selection()
        elif kind == 'type_of_order':
            selection = self._get_type_of_order_selection()
        else:
            return False
        keys = [key for key, _label in selection]
        if preferred_key in keys:
            return preferred_key
        return False

    @api.model
    def _sale_order_studio_order_id_field_names(self):
        return ('x_studio_order_id',)

    @api.model
    def _sale_order_studio_order_id_field_name(self):
        """Return the technical name of the Studio Order ID field, if installed."""
        SaleOrder = self.env['sale.order']
        for field_name in self._sale_order_studio_order_id_field_names():
            field = SaleOrder._fields.get(field_name)
            if field and field.type in ('char', 'text'):
                return field_name
        return False

    def get_studio_order_id_sale_order_values(self, shopify_order_id, payload=None):
        """Values for the Studio Order ID field on ``sale.order``."""
        self.ensure_one()
        field_name = self._sale_order_studio_order_id_field_name()
        if not field_name:
            return {}
        mapping = self.custom_field_ids.filtered(
            lambda item: item.active and item.field_id.name == field_name)
        if mapping:
            if mapping.source == 'payload' and mapping.payload_path == 'order_name_and_tracking':
                return mapping.get_sale_order_values(dict(payload or {}, id=shopify_order_id))
            return {}
        value = self._format_studio_order_id(shopify_order_id, payload or {})
        if value:
            return {field_name: value}
        return {}

    def get_studio_order_id_refresh_values(
            self, shopify_order_id, payload, current_value, force=False):
        """Write values when empty, upgraded, or ``force`` and Shopify id differs."""
        self.ensure_one()
        new_values = self.get_studio_order_id_sale_order_values(
            shopify_order_id, payload=payload)
        if not new_values:
            return {}
        if force:
            field_name = next(iter(new_values))
            if (current_value or '').strip() != new_values[field_name]:
                return new_values
            return {}
        return self.get_studio_order_id_upgrade_values(
            shopify_order_id, payload, current_value)

    def get_studio_order_id_upgrade_values(self, shopify_order_id, payload, current_value):
        """Refresh Studio Order ID when tracking arrives (e.g. ``TR-001270`` → full id)."""
        self.ensure_one()
        field_name = self._sale_order_studio_order_id_field_name()
        if not field_name:
            return {}
        new_values = self.get_studio_order_id_sale_order_values(
            shopify_order_id, payload=payload)
        if not new_values:
            return {}
        new_value = new_values.get(field_name)
        if self._should_upgrade_studio_order_id(current_value, new_value, payload or {}):
            return new_values
        return {}

    def _should_upgrade_studio_order_id(self, current_value, new_value, payload):
        """Allow filling empty ids or appending tracking to an order-name-only id."""
        current = (current_value or '').strip()
        new_value = (new_value or '').strip()
        if not new_value or new_value == current:
            return False
        if not current:
            return True
        if (self.studio_order_id_mode or 'numeric') != 'name_and_tracking':
            return False
        if new_value.startswith('%s-' % current):
            return True
        name = (payload.get('name') or '').strip()
        return bool(name and current == name and new_value.startswith('%s-' % name))

    def _format_studio_order_id(self, shopify_order_id, payload):
        """Build ``TR-001270-LUAC0001601959`` from order name + fulfillment tracking."""
        self.ensure_one()
        mode = self.studio_order_id_mode or 'numeric'
        name = (payload.get('name') or '').strip()
        tracking = (payload.get('primary_tracking_number') or '').strip()
        if not tracking:
            numbers = payload.get('tracking_numbers') or []
            tracking = (numbers[0] if numbers else '') or ''
            tracking = str(tracking).strip()

        if mode == 'order_name' and name:
            return name
        if mode == 'name_and_tracking':
            if name and tracking:
                return '%s-%s' % (name, tracking)
            if name:
                return name
        if shopify_order_id:
            return str(shopify_order_id)
        return False

    # ------------------------------------------------------------------
    # Default products
    # ------------------------------------------------------------------

    def _get_shipping_product(self):
        """Return (creating on first use) the service product for shipping lines."""
        self.ensure_one()
        if self.shipping_product_id:
            return self.shipping_product_id
        product = self.env['product.product'].sudo().create({
            'name': _('Shopify Shipping'),
            'type': 'service',
            'invoice_policy': 'order',
            'list_price': 0.0,
            'taxes_id': [(5, 0, 0)],
            'purchase_ok': False,
            'default_code': 'SHOPIFY-SHIPPING',
        })
        self.sudo().write({'shipping_product_id': product.id})
        return product

    def _get_discount_product(self):
        self.ensure_one()
        if self.discount_product_id:
            return self.discount_product_id
        product = self.env['product.product'].sudo().create({
            'name': _('Shopify Discount'),
            'type': 'service',
            'invoice_policy': 'order',
            'list_price': 0.0,
            'taxes_id': [(5, 0, 0)],
            'purchase_ok': False,
            'default_code': 'SHOPIFY-DISCOUNT',
        })
        self.sudo().write({'discount_product_id': product.id})
        return product

    def _get_pricelist_for_currency(self, currency):
        """Pricelist whose currency matches the Shopify order, if one exists.

        Line prices are taken from Shopify, not the pricelist. Odoo only needs
        a pricelist to set the sale order currency header.
        """
        self.ensure_one()
        Pricelist = self.env['product.pricelist']
        if currency:
            match = Pricelist.search([
                ('currency_id', '=', currency.id),
                ('active', '=', True),
                '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
            ], limit=1)
            if match:
                return match
        if self.pricelist_id and (not currency or self.pricelist_id.currency_id == currency):
            return self.pricelist_id
        return Pricelist.browse()

    # ------------------------------------------------------------------
    # Smart buttons
    # ------------------------------------------------------------------

    def _action_window(self, xml_id, domain, name):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(xml_id)
        action['domain'] = domain
        action['context'] = {'default_store_id': self.id, 'search_default_store_id': self.id}
        action['name'] = name
        return action

    def action_view_shopify_orders(self):
        return self._action_window(
            'shopify_connector.action_shopify_order',
            [('store_id', '=', self.id)], _('Shopify Orders'))

    def action_view_needs_review(self):
        action = self._action_window(
            'shopify_connector.action_shopify_order',
            [('store_id', '=', self.id), ('state', '=', 'needs_review')], _('Needs Review'))
        action['context'] = dict(action['context'], search_default_needs_review=1)
        return action

    def action_view_sale_orders(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('sale.action_orders')
        action['domain'] = [('shopify_store_id', '=', self.id)]
        action['context'] = {'create': False}
        return action

    def action_view_customer_mappings(self):
        return self._action_window(
            'shopify_connector.action_shopify_customer_mapping',
            [('store_id', '=', self.id)], _('Shopify Customers'))

    def action_view_link_variants(self):
        action = self._action_window(
            'shopify_connector.action_shopify_product_catalog',
            [('store_id', '=', self.id)], _('Link Variants'))
        action['context'] = dict(action.get('context') or {}, search_default_unmapped=1)
        return action

    def action_view_queue(self):
        return self._action_window(
            'shopify_connector.action_shopify_sync_queue',
            [('store_id', '=', self.id)], _('Sync Queue'))

    def action_view_logs(self):
        return self._action_window(
            'shopify_connector.action_shopify_sync_log',
            [('store_id', '=', self.id)], _('Sync Logs'))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _notify(self, message, warning=False, sticky=False):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Shopify'),
                'message': message,
                'type': 'warning' if warning else 'success',
                'sticky': sticky,
            },
        }

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def _cron_import_orders(self):
        stores = self.search([('auto_sync', '=', True), ('connection_state', '!=', 'error')])
        in_test = self.env.registry.in_test_mode()
        for store in stores:
            try:
                store.sync_orders()
            except Exception as exc:  # noqa: BLE001 - one store must not stop the others
                _logger.exception("Shopify scheduled sync failed for %s", store.display_name)
                self.env.cr.rollback()
                self.env['shopify.sync.log'].log(store, 'error', 'order_fetch', str(exc))
            if not in_test:
                self.env.cr.commit()
        return True
