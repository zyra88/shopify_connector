import json
import logging

from dateutil import parser as date_parser
from pytz import UTC

from odoo import _, api, fields, models
from odoo.tools import float_compare, float_is_zero, float_round

from .shopify_customer_resolver import ShopifyCustomerResolutionError
from .shopify_order import format_address

_logger = logging.getLogger(__name__)

# Sale order states in which the connector still owns the lines. Once a user
# has confirmed the order, re-importing must not rewrite what is being
# delivered or invoiced.
EDITABLE_SO_STATES = ('draft', 'sent')


class ShopifyOrderImportError(Exception):
    """An order could not be imported and should be retried or reviewed."""


class ShopifyOrderImporter(models.AbstractModel):
    """Turn one normalised Shopify order into an Odoo sale order.

    The pipeline is fixed and its order matters:

    ``already imported?`` -> ``resolve customer`` -> ``resolve addresses``
    -> ``resolve products`` -> ``validate mapping`` -> ``sale.order``
    -> ``sale.order.line`` -> ``discounts`` -> ``shipping`` -> ``taxes``
    -> ``reconcile totals`` -> ``log``

    The sale order is never created before the customer exists, and never
    created at all if a line cannot be matched to a product.
    """

    _name = 'shopify.order.importer'
    _description = 'Shopify Order Importer'

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @api.model
    def import_order(self, store, payload, queue=None):
        """Import one order. Returns a result dict; raises only on bugs.

        Business problems (unmapped product, unidentifiable customer, totals
        that do not reconcile) are recorded as ``needs_review`` rather than
        raised, so that the rest of the run continues.
        """
        shopify_order_id = str(payload.get('id') or '')
        if not shopify_order_id:
            raise ShopifyOrderImportError(_("Shopify returned an order without an id."))

        record = self._upsert_shopify_order(store, payload)
        if queue:
            queue.shopify_order_ref_id = record.id

        if not record.sale_order_id:
            existing_order = self._find_existing_sale_order(store, record)
            if existing_order:
                record.write({'sale_order_id': existing_order.id})
                self.env['shopify.sync.log'].log(
                    store, 'info', 'order_import',
                    _("Linked Shopify order %(shopify)s to existing sale order %(sale)s.",
                      shopify=record.shopify_order_name or record.shopify_order_id,
                      sale=existing_order.name),
                    shopify_reference=record.shopify_order_name,
                    shopify_order_id=record.shopify_order_id,
                    shopify_order_ref_id=record.id,
                    sale_order_id=existing_order.id,
                )

        result = {
            'shopify_order_id': record.id,
            'sale_order_id': record.sale_order_id.id or False,
            'state': record.state,
            'message': '',
        }

        # 1. Cancelled upstream: record it, do not build anything new.
        if payload.get('cancelled_at'):
            return self._handle_cancelled(store, record, result)

        # 2. Frozen downstream: an order already confirmed in Odoo only gets
        #    its Shopify status fields refreshed.
        if record.sale_order_id and record.sale_order_id.state not in EDITABLE_SO_STATES:
            return self._refresh_confirmed_order(store, record, payload, result)

        # 3. Customer first. No partner, no sale order.
        try:
            customer = self.env['shopify.customer.resolver'].resolve(store, payload)
        except ShopifyCustomerResolutionError as exc:
            return self._mark_needs_review(store, record, str(exc), result)

        record.write({
            'partner_id': customer['partner'].id,
            'partner_invoice_id': customer['partner_invoice'].id,
            'partner_shipping_id': customer['partner_shipping'].id,
            'customer_mapping_id': customer['mapping'].id if customer['mapping'] else False,
            'is_guest_order': customer['is_guest'],
        })

        # 4. Products. Every line must resolve before anything is written.
        resolved_lines, unmatched = self._resolve_lines(store, record, payload)
        if unmatched:
            reason = _("%(count)s Shopify line(s) could not be matched to an Odoo product:\n%(details)s",
                       count=len(unmatched), details='\n'.join('- %s' % msg for msg in unmatched))
            return self._mark_needs_review(store, record, reason, result)

        # 5. Sale order and its lines.
        sale_order = self._create_or_update_sale_order(store, record, payload, customer, resolved_lines)
        record.write({'sale_order_id': sale_order.id})
        sale_order.shopify_order_ref_id = record.id

        # 6. Reconcile what Odoo computed against what Shopify charged.
        review_reason = self._reconcile_totals(store, record, payload, sale_order)

        if store.auto_confirm_order and not review_reason and sale_order.state == 'draft':
            sale_order.action_confirm()

        record.write({
            'state': 'needs_review' if review_reason else 'imported',
            'review_reason': review_reason or False,
            'error_message': False,
            'last_sync_at': fields.Datetime.now(),
            'import_count': record.import_count + 1,
        })

        message = _("Order %(order)s imported as %(sale)s.",
                    order=record.shopify_order_name, sale=sale_order.name)
        self.env['shopify.sync.log'].log(
            store, 'warning' if review_reason else 'info', 'order_import',
            '%s %s' % (message, review_reason or ''),
            shopify_reference=record.shopify_order_name,
            shopify_order_id=record.shopify_order_id,
            shopify_order_ref_id=record.id,
            sale_order_id=sale_order.id,
            partner_id=customer['partner'].id,
            queue_id=queue.id if queue else False,
        )

        result.update({
            'sale_order_id': sale_order.id,
            'state': record.state,
            'message': message,
        })
        return result

    # ------------------------------------------------------------------
    # Mirror record
    # ------------------------------------------------------------------

    @api.model
    def _upsert_shopify_order(self, store, payload):
        """Create or refresh the ``shopify.order`` mirror. Idempotency anchor."""
        shopify_order_id = str(payload['id'])
        currency = self._find_currency(payload.get('currency'))
        values = {
            'store_id': store.id,
            'shopify_order_id': shopify_order_id,
            'shopify_order_name': payload.get('name'),
            'shopify_order_number': payload.get('order_number') or 0,
            'shopify_created_at': self._parse_datetime(payload.get('created_at')),
            'shopify_updated_at': self._parse_datetime(payload.get('updated_at')),
            'shopify_processed_at': self._parse_datetime(payload.get('processed_at')),
            'shopify_cancelled_at': self._parse_datetime(payload.get('cancelled_at')),
            'shopify_customer_id': (payload.get('customer') or {}).get('id') or False,
            'email': payload.get('email'),
            'phone': payload.get('phone'),
            'billing_address': format_address(payload.get('billing_address')),
            'shipping_address': format_address(payload.get('shipping_address')),
            'currency_code': payload.get('currency'),
            'currency_id': currency.id if currency else False,
            'financial_status': payload.get('financial_status'),
            'fulfillment_status': payload.get('fulfillment_status') or 'unfulfilled',
            'payment_gateway': ', '.join(payload.get('payment_gateway_names') or []) or payload.get('gateway'),
            'note': payload.get('note'),
            'tags': payload.get('tags'),
            'discount_codes': ', '.join(payload.get('discount_codes') or []),
            'taxes_included': payload.get('taxes_included'),
            'shopify_subtotal': float(payload.get('subtotal_price') or 0.0),
            'shopify_total_discounts': float(payload.get('total_discounts') or 0.0),
            'shopify_total_shipping': float(payload.get('total_shipping_price') or 0.0),
            'shopify_total_tax': float(payload.get('total_tax') or 0.0),
            'shopify_total': float(payload.get('total_price') or 0.0),
            'payload': json.dumps(payload, default=str, indent=2),
        }

        record = self.env['shopify.order'].search([
            ('store_id', '=', store.id),
            ('shopify_order_id', '=', shopify_order_id),
        ], limit=1)
        if record:
            record.write(values)
        else:
            record = self.env['shopify.order'].create(values)
        return record

    # ------------------------------------------------------------------
    # Product resolution
    # ------------------------------------------------------------------

    @api.model
    def _resolve_lines(self, store, record, payload):
        """Resolve every product line and refresh the mirror lines.

        Returns ``(resolved, unmatched_messages)``. ``resolved`` keeps the
        Shopify payload alongside the Odoo product so the sale order builder
        does not have to look anything up again.
        """
        resolver = self.env['shopify.product.resolver']
        record.line_ids.unlink()

        resolved, unmatched = [], []
        for sequence, line in enumerate(payload.get('line_items') or [], start=1):
            quantity = float(line.get('current_quantity') if line.get('current_quantity') is not None
                             else line.get('quantity') or 0)
            product, match_source, message = resolver.resolve(store, line)

            mapping = self.env['shopify.variant.mapping']
            if product:
                mapping = resolver.remember_match(store, line, product, match_source)

            price = float(line.get('price') or 0.0)
            discount = float(line.get('total_discount') or 0.0)
            mirror = self.env['shopify.order.line'].create({
                'shopify_order_id': record.id,
                'sequence': sequence * 10,
                'shopify_line_id': line.get('id'),
                'line_type': 'product',
                'shopify_product_id': line.get('product_id'),
                'shopify_variant_id': line.get('variant_id'),
                'sku': line.get('sku'),
                'barcode': line.get('variant_barcode'),
                'name': line.get('name') or line.get('title') or _('Shopify line'),
                'variant_title': line.get('variant_title'),
                'quantity': quantity,
                'price_unit': price,
                'discount_amount': discount,
                'tax_amount': sum(float(t.get('price') or 0.0) for t in line.get('tax_lines') or []),
                'price_subtotal': price * quantity - discount,
                'product_id': product.id if product else False,
                'variant_mapping_id': mapping.id if mapping else False,
                'match_source': match_source or False,
                'resolution_state': 'matched' if product else 'unmatched',
                'resolution_message': message or False,
            })

            if not product:
                unmatched.append(message)
                continue
            if float_is_zero(quantity, precision_rounding=0.000001):
                # Fully refunded/removed line: keep the mirror row for audit,
                # but do not put a zero-quantity line on the sale order.
                continue
            resolved.append({'payload': line, 'product': product, 'mirror': mirror,
                             'quantity': quantity, 'sequence': sequence * 10})

        return resolved, unmatched

    # ------------------------------------------------------------------
    # Sale order
    # ------------------------------------------------------------------

    @api.model
    def _find_existing_sale_order(self, store, record):
        """Reuse a manually created sale order that already represents this Shopify order."""
        SaleOrder = self.env['sale.order']
        company_domain = [('company_id', '=', store.company_id.id)]

        linked = SaleOrder.search(company_domain + [
            ('shopify_store_id', '=', store.id),
            ('shopify_order_id', '=', record.shopify_order_id),
        ], limit=2)
        if len(linked) == 1:
            return linked
        if len(linked) > 1:
            _logger.warning(
                "Shopify: multiple sale orders already linked to order id %s on store %s",
                record.shopify_order_id, store.display_name,
            )
            return linked[0]

        if not store.link_existing_sale_orders:
            return SaleOrder

        blocked_ids = self.env['shopify.order'].search([
            ('store_id', '=', store.id),
            ('shopify_order_id', '!=', record.shopify_order_id),
            ('sale_order_id', '!=', False),
        ]).mapped('sale_order_id').ids

        def _single_match(extra_domain):
            domain = company_domain + extra_domain
            if blocked_ids:
                domain.append(('id', 'not in', blocked_ids))
            candidates = SaleOrder.search(domain, order='id asc')
            if len(candidates) == 1:
                return candidates
            if len(candidates) > 1:
                preferred = candidates.filtered(lambda order: not order.shopify_order_id)
                if preferred:
                    return preferred[:1]
                _logger.warning(
                    "Shopify: multiple sale orders match %s for order %s; skipping auto-link.",
                    extra_domain, record.shopify_order_id,
                )
            return SaleOrder

        studio_field = store._sale_order_studio_order_id_field_name()
        if studio_field:
            studio_value = record.shopify_order_id
            order = _single_match([
                (studio_field, '=', studio_value),
                ('shopify_order_id', '=', False),
            ])
            if order:
                return order
            if studio_value.isdigit():
                order = _single_match([
                    (studio_field, '=', int(studio_value)),
                    ('shopify_order_id', '=', False),
                ])
                if order:
                    return order

        if record.shopify_order_name:
            order = _single_match([
                ('client_order_ref', '=', record.shopify_order_name),
                ('shopify_order_id', '=', False),
            ])
            if order:
                return order

        return SaleOrder

    @api.model
    def _create_or_update_sale_order(self, store, record, payload, customer, resolved_lines):
        sale_order = record.sale_order_id
        values = self._prepare_sale_order_values(store, record, payload, customer)

        if sale_order:
            sale_order.write(values)
            sale_order.order_line.filtered(lambda line: line.shopify_line_type).unlink()
        else:
            sale_order = self.env['sale.order'].create(values)

        line_commands = []
        for resolved in resolved_lines:
            line_commands.append((0, 0, self._prepare_product_line(store, payload, resolved)))
        line_commands += [
            (0, 0, vals) for vals in self._prepare_shipping_lines(store, payload)
        ]
        residual = self._prepare_residual_discount_line(store, payload, resolved_lines)
        if residual:
            line_commands.append((0, 0, residual))

        if line_commands:
            sale_order.write({'order_line': line_commands})

        self._link_mirror_lines(record, sale_order)
        return sale_order

    @api.model
    def _prepare_sale_order_values(self, store, record, payload, customer):
        currency = self._find_currency(payload.get('currency'))
        pricelist = store._get_pricelist_for_currency(currency)
        values = {
            'company_id': store.company_id.id,
            'partner_id': customer['partner'].id,
            'partner_invoice_id': customer['partner_invoice'].id,
            'partner_shipping_id': customer['partner_shipping'].id,
            'date_order': record.shopify_created_at or fields.Datetime.now(),
            'origin': _('Shopify %s', record.shopify_order_name or record.shopify_order_id),
            'client_order_ref': record.shopify_order_name,
            'shopify_store_id': store.id,
            'shopify_order_id': record.shopify_order_id,
            'shopify_order_name': record.shopify_order_name,
            'shopify_order_number': record.shopify_order_number,
            'shopify_financial_status': payload.get('financial_status'),
            'shopify_fulfillment_status': payload.get('fulfillment_status') or 'unfulfilled',
            'shopify_payment_gateway': record.payment_gateway,
            'shopify_tags': payload.get('tags'),
            'shopify_currency_code': payload.get('currency'),
            'shopify_created_at': record.shopify_created_at,
            'shopify_updated_at': record.shopify_updated_at,
        }
        if pricelist:
            values['pricelist_id'] = pricelist.id
        if store.team_id:
            values['team_id'] = store.team_id.id
        if store.user_id:
            values['user_id'] = store.user_id.id
        payment_term = self.env['shopify.payment.term.resolver'].resolve(
            store, record.payment_gateway, payload.get('financial_status'))
        if payment_term:
            values['payment_term_id'] = payment_term.id
        if store.warehouse_on_order and store.warehouse_id:
            values['warehouse_id'] = store.warehouse_id.id
        if store.import_order_note and payload.get('note'):
            values['note'] = payload['note']
        if (not record.sale_order_id and store.use_custom_sale_order_prefix
                and store.sale_order_prefix):
            values['name'] = '%s%s' % (
                store.sale_order_prefix,
                record.shopify_order_name or record.shopify_order_id,
            )
        values.update(store.get_order_from_sale_order_values())
        values.update(store.get_type_of_order_sale_order_values())
        values.update(store.get_studio_order_id_sale_order_values(
            record.shopify_order_id, payload=payload))
        return values

    @api.model
    def _prepare_product_line(self, store, payload, resolved):
        line, product = resolved['payload'], resolved['product']
        quantity = resolved['quantity']
        price_unit, discount, note = self._split_price_and_discount(
            store, float(line.get('price') or 0.0), quantity,
            float(line.get('total_discount') or 0.0),
            float(line.get('discounted_unit_price') or 0.0))

        values = {
            'sequence': resolved['sequence'],
            'product_id': product.id,
            'name': line.get('name') or product.display_name,
            'product_uom_qty': quantity,
            'price_unit': price_unit,
            'discount': discount,
            'shopify_line_id': line.get('id'),
            'shopify_line_type': 'product',
            'shopify_variant_id': line.get('variant_id'),
            'shopify_sku': line.get('sku'),
        }
        values.update(self._prepare_tax_values(store, payload, line.get('tax_lines')))
        if note:
            values['name'] = '%s\n%s' % (values['name'], note)
        return values

    @api.model
    def _prepare_shipping_lines(self, store, payload):
        shipping_lines = payload.get('shipping_lines') or []
        if not shipping_lines:
            return []
        product = store._get_shipping_product()
        rounding = self._currency_rounding(payload)

        values_list = []
        for sequence, ship in enumerate(shipping_lines, start=1):
            price = float(ship.get('price') or 0.0)
            discounted = float(ship.get('discounted_price') if ship.get('discounted_price') is not None else price)
            if float_is_zero(price, precision_rounding=rounding) and float_is_zero(discounted, precision_rounding=rounding):
                continue
            price_unit, discount, _note = self._split_price_and_discount(
                store, price, 1.0, price - discounted, discounted)
            values = {
                'sequence': 900 + sequence,
                'product_id': product.id,
                'name': ship.get('title') or _('Shipping'),
                'product_uom_qty': 1.0,
                'price_unit': price_unit,
                'discount': discount,
                'shopify_line_id': ship.get('id'),
                'shopify_line_type': 'shipping',
            }
            values.update(self._prepare_tax_values(store, payload, ship.get('tax_lines')))
            values_list.append(values)
        return values_list

    @api.model
    def _prepare_residual_discount_line(self, store, payload, resolved_lines):
        """Catch an order-level discount Shopify did not allocate to a line.

        Shopify normally spreads discounts across the line items, so this is
        a safety net rather than the usual path.
        """
        rounding = self._currency_rounding(payload)
        declared = float(payload.get('total_discounts') or 0.0)
        allocated = sum(float(r['payload'].get('total_discount') or 0.0) for r in resolved_lines)
        residual = float_round(declared - allocated, precision_rounding=rounding)
        if float_compare(residual, 0.0, precision_rounding=rounding) <= 0:
            return None
        return {
            'sequence': 990,
            'product_id': store._get_discount_product().id,
            'name': _('Shopify discount %s', payload.get('discount_codes') and
                      ', '.join(payload['discount_codes']) or ''),
            'product_uom_qty': 1.0,
            'price_unit': -residual,
            'discount': 0.0,
            'tax_id': [(5, 0, 0)],
            'shopify_line_type': 'discount',
        }

    @api.model
    def _prepare_tax_values(self, store, payload, tax_lines):
        if store.tax_mode == 'none':
            return {'tax_id': [(5, 0, 0)]}
        if store.tax_mode == 'odoo':
            # Leave tax_id out entirely so Odoo's own computation runs.
            return {}
        taxes = self.env['shopify.tax.resolver'].resolve(
            store, tax_lines, payload.get('taxes_included'))
        return {'tax_id': [(6, 0, taxes.ids)]}

    @api.model
    def _split_price_and_discount(self, store, unit_price, quantity, discount_amount, discounted_unit_price):
        """Turn a Shopify discount amount into Odoo's price/discount pair.

        Shopify states a discount as money; Odoo stores a percentage, and both
        the percentage and the unit price are held at limited precision. The
        two representations therefore do not always reproduce the same line
        total, so both are evaluated under Odoo's real rounding and the closer
        one wins. The percentage is preferred on a tie because it keeps the
        discount visible on the quotation.

        Returns ``(price_unit, discount_percent, note)``.
        """
        rounding = store.currency_id.rounding or 0.01
        if float_is_zero(discount_amount, precision_rounding=rounding) or \
                float_is_zero(unit_price, precision_rounding=rounding) or \
                float_is_zero(quantity, precision_rounding=0.000001):
            return unit_price, 0.0, None

        price_digits = self.env['decimal.precision'].precision_get('Product Price')
        gross = unit_price * quantity
        expected = float_round(gross - discount_amount, precision_rounding=rounding)

        percent = float_round(discount_amount / gross * 100.0, precision_digits=2)
        as_percent = float_round(
            float_round(unit_price, precision_digits=price_digits) * quantity * (1 - percent / 100.0),
            precision_rounding=rounding)

        net_unit = discounted_unit_price or (expected / quantity)
        as_net_price = float_round(
            float_round(net_unit, precision_digits=price_digits) * quantity,
            precision_rounding=rounding)

        if abs(as_percent - expected) <= abs(as_net_price - expected):
            return unit_price, percent, None

        return net_unit, 0.0, _(
            "Shopify discount of %(amount)s applied to the unit price: a "
            "percentage cannot reproduce the Shopify line total at Odoo's "
            "rounding.", amount=discount_amount)

    @api.model
    def _link_mirror_lines(self, record, sale_order):
        by_shopify_id = {
            line.shopify_line_id: line
            for line in sale_order.order_line if line.shopify_line_id
        }
        for mirror in record.line_ids:
            sale_line = by_shopify_id.get(mirror.shopify_line_id)
            if sale_line:
                mirror.sale_order_line_id = sale_line.id

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    @api.model
    def _reconcile_totals(self, store, record, payload, sale_order):
        """Compare the Odoo total with what Shopify charged.

        Returns a review reason, or ``None`` when the two agree.
        """
        shopify_total = float(payload.get('total_price') or 0.0)
        odoo_total = sale_order.amount_total
        tolerance = store.amount_tolerance or 0.01
        difference = odoo_total - shopify_total

        if abs(difference) <= tolerance:
            return None

        reason = _(
            "Totals do not reconcile: Shopify %(shopify).2f vs Odoo %(odoo).2f "
            "(difference %(diff).2f %(currency)s). Check the tax configuration "
            "and the shipping product.",
            shopify=shopify_total, odoo=odoo_total, diff=difference,
            currency=payload.get('currency') or '')
        self.env['shopify.sync.log'].log(
            store, 'warning', 'reconciliation', reason,
            shopify_reference=record.shopify_order_name,
            shopify_order_id=record.shopify_order_id,
            shopify_order_ref_id=record.id,
            sale_order_id=sale_order.id)
        return reason

    # ------------------------------------------------------------------
    # Special cases
    # ------------------------------------------------------------------

    @api.model
    def _handle_cancelled(self, store, record, result):
        message = _("Shopify order %s is cancelled.", record.shopify_order_name)
        if record.sale_order_id and record.sale_order_id.state in EDITABLE_SO_STATES:
            record.sale_order_id._action_cancel()
            message += ' ' + _("The Odoo quotation was cancelled too.")
        elif record.sale_order_id:
            message += ' ' + _("The Odoo sale order %s is already confirmed and was left untouched.",
                               record.sale_order_id.name)
        record.write({
            'state': 'cancelled',
            'review_reason': message,
            'last_sync_at': fields.Datetime.now(),
        })
        self.env['shopify.sync.log'].log(
            store, 'info', 'order_import', message,
            shopify_reference=record.shopify_order_name,
            shopify_order_id=record.shopify_order_id,
            shopify_order_ref_id=record.id)
        result.update({'state': 'cancelled', 'message': message})
        return result

    @api.model
    def _refresh_confirmed_order(self, store, record, payload, result):
        sale_order = record.sale_order_id
        values = {
            'shopify_financial_status': payload.get('financial_status'),
            'shopify_fulfillment_status': payload.get('fulfillment_status') or 'unfulfilled',
            'shopify_updated_at': record.shopify_updated_at,
        }
        field_name = store._sale_order_studio_order_id_field_name()
        if field_name:
            values.update(store.get_studio_order_id_upgrade_values(
                record.shopify_order_id, payload, sale_order[field_name]))
        sale_order.write(values)
        message = _(
            "Sale order %(sale)s is already %(state)s; only the Shopify status "
            "fields were refreshed.", sale=sale_order.name, state=sale_order.state)
        record.write({
            'state': 'imported',
            'last_sync_at': fields.Datetime.now(),
            'import_count': record.import_count + 1,
        })
        self.env['shopify.sync.log'].log(
            store, 'info', 'order_import', message,
            shopify_reference=record.shopify_order_name,
            shopify_order_id=record.shopify_order_id,
            shopify_order_ref_id=record.id,
            sale_order_id=sale_order.id)
        result.update({'state': 'imported', 'sale_order_id': sale_order.id, 'message': message})
        return result

    @api.model
    def _mark_needs_review(self, store, record, reason, result):
        record.write({
            'state': 'needs_review',
            'review_reason': reason,
            'last_sync_at': fields.Datetime.now(),
            'import_count': record.import_count + 1,
        })
        self.env['shopify.sync.log'].log(
            store, 'warning', 'order_import', reason,
            shopify_reference=record.shopify_order_name,
            shopify_order_id=record.shopify_order_id,
            shopify_order_ref_id=record.id)
        result.update({'state': 'needs_review', 'message': reason})
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @api.model
    def _find_currency(self, code):
        if not code:
            return self.env['res.currency']
        return self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', code)], limit=1)

    @api.model
    def _currency_rounding(self, payload):
        currency = self._find_currency(payload.get('currency'))
        return currency.rounding or 0.01

    @staticmethod
    def _parse_datetime(value):
        """Shopify sends ISO-8601 with an offset; Odoo stores naive UTC."""
        if not value:
            return False
        if isinstance(value, str):
            try:
                value = date_parser.parse(value)
            except (ValueError, OverflowError):
                return False
        if value.tzinfo:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value
