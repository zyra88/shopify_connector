import logging
import re

from odoo import _, api, models

from .res_partner import phone_key

_logger = logging.getLogger(__name__)

# Enough of a check to avoid searching on obvious junk; real validation is
# Shopify's job, not ours.
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Fields the connector considers Shopify-owned. Everything else on an existing
# partner is left exactly as the Odoo user configured it.
SHOPIFY_OWNED_FIELDS = ('name', 'email', 'phone')


class ShopifyCustomerResolutionError(Exception):
    """The order carries too little information to identify a customer."""


class ShopifyCustomerResolver(models.AbstractModel):
    """Find or create the ``res.partner`` behind a Shopify order.

    Customer resolution is a *step of* order import, never a standalone sync.
    The importer calls :meth:`resolve` before it touches ``sale.order``, and
    refuses to continue if no partner comes back.
    """

    _name = 'shopify.customer.resolver'
    _description = 'Shopify Customer Resolver'

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @api.model
    def resolve(self, store, payload):
        """Resolve the customer and both addresses for one Shopify order.

        Returns a dict with ``partner``, ``partner_invoice``,
        ``partner_shipping``, ``mapping``, ``is_guest`` and ``notes``.
        Raises :class:`ShopifyCustomerResolutionError` when the order carries
        no usable identity at all.
        """
        customer = payload.get('customer') or {}
        billing = payload.get('billing_address') or {}
        shipping = payload.get('shipping_address') or {}
        notes = []

        shopify_customer_id = customer.get('id') and str(customer['id']) or None
        email = self._clean_email(customer.get('email') or payload.get('email'))
        phone = customer.get('phone') or payload.get('phone') or billing.get('phone') or shipping.get('phone')
        is_guest = not shopify_customer_id

        partner, mapping, matched_by = self._find_partner(
            store, shopify_customer_id, email, phone, billing, shipping)

        if partner:
            notes.append(_("Customer matched by %s.", matched_by))
            owned = bool(partner.shopify_customer_id or partner.shopify_mapping_ids)
            self._update_partner(store, partner, customer, billing, shipping, email, phone, is_guest)
        else:
            partner = self._create_partner(store, customer, billing, shipping, email, phone, payload)
            notes.append(_("Customer created from the Shopify order."))
            owned = True
        if owned:
            self._set_receivable_account(store, partner)

        if shopify_customer_id:
            mapping = self.env['shopify.customer.mapping'].upsert(
                store, shopify_customer_id, partner,
                email=email, phone=phone, tags=customer.get('tags'))
            if not partner.shopify_customer_id:
                partner.sudo().shopify_customer_id = shopify_customer_id

        partner_invoice = self._resolve_address(store, partner, billing, 'invoice') or partner
        partner_shipping = self._resolve_address(store, partner, shipping, 'delivery') or partner_invoice

        return {
            'partner': partner,
            'partner_invoice': partner_invoice,
            'partner_shipping': partner_shipping,
            'mapping': mapping,
            'is_guest': is_guest,
            'notes': notes,
        }

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    @api.model
    def _find_partner(self, store, shopify_customer_id, email, phone, billing, shipping):
        """Walk the match priority: mapping, Shopify id, email, phone."""
        Partner = self.env['res.partner']

        if shopify_customer_id:
            mapping = self.env['shopify.customer.mapping'].search([
                ('store_id', '=', store.id),
                ('shopify_customer_id', '=', shopify_customer_id),
            ], limit=1)
            if mapping and mapping.partner_id:
                return mapping.partner_id, mapping, _('existing Shopify mapping')

            # The partner may carry the id from an earlier store or a manual
            # edit even when no mapping row exists yet.
            partner = Partner.search([
                ('shopify_customer_id', '=', shopify_customer_id),
                ('company_id', 'in', (False, store.company_id.id)),
            ], limit=1)
            if partner:
                return partner, None, _('Shopify customer ID')

        if store.match_customer_by_email and email:
            partner = self._search_by_email(store, email)
            if partner:
                return partner, None, _('email')

        if store.match_customer_by_phone and phone:
            partner = self._search_by_phone(store, phone)
            if partner:
                return partner, None, _('phone')

        if store.guest_partner_id and not email and not phone and not self._address_name(billing, shipping):
            return store.guest_partner_id, None, _('fallback guest customer')

        return self.env['res.partner'], None, None

    @api.model
    def _search_by_email(self, store, email):
        partners = self.env['res.partner'].search([
            ('email', '=ilike', email),
            ('active', '=', True),
            ('company_id', 'in', (False, store.company_id.id)),
        ], order='is_company desc, id asc')
        return self._pick_commercial_partner(partners)

    @api.model
    def _search_by_phone(self, store, phone):
        # Matched against the stored, normalised key rather than the raw
        # column: "+44 (0) 20 7946 0958" and "02079460958" are the same number
        # but share no substring Postgres could match.
        key = phone_key(phone)
        if not key:
            return self.env['res.partner']
        partners = self.env['res.partner'].search([
            ('active', '=', True),
            ('company_id', 'in', (False, store.company_id.id)),
            '|', ('shopify_phone_key', '=', key), ('shopify_mobile_key', '=', key),
        ], order='is_company desc, id asc')
        return self._pick_commercial_partner(partners)

    @api.model
    def _pick_commercial_partner(self, partners):
        """Prefer a real customer record over an address child contact."""
        if not partners:
            return self.env['res.partner']
        for partner in partners:
            if not partner.shopify_address_type and partner.type == 'contact':
                return partner
        # Only address children matched: attach to the customer they belong to.
        return partners[0].commercial_partner_id or partners[0]

    # ------------------------------------------------------------------
    # Creation / update
    # ------------------------------------------------------------------

    @api.model
    def _create_partner(self, store, customer, billing, shipping, email, phone, payload):
        name = self._build_name(customer, billing, shipping, email, payload)
        if not name:
            raise ShopifyCustomerResolutionError(_(
                "Shopify order %s has no customer name, email, phone or billing "
                "address, so no Odoo customer can be created. Set a Fallback "
                "Guest Customer on the store to import orders like this.",
                payload.get('name') or payload.get('id')))

        values = {
            'name': name,
            'email': email or False,
            'phone': phone or False,
            'is_company': bool(billing.get('company') or shipping.get('company')),
            'customer_rank': 1,
            'company_id': False,
            'shopify_customer_id': customer.get('id') and str(customer['id']) or False,
        }
        values.update(self._address_values(billing or shipping or customer.get('default_address') or {}))
        if store.customer_tag_ids:
            values['category_id'] = [(6, 0, store.customer_tag_ids.ids)]
        if customer.get('note'):
            values['comment'] = customer['note']

        partner = self.env['res.partner'].sudo().create(values)
        _logger.info("Shopify: created customer %s (%s)", partner.display_name, partner.id)
        return partner

    @api.model
    def _set_receivable_account(self, store, partner):
        account = store.receivable_account_id
        partner = partner.sudo().with_company(store.company_id)
        if account and partner.property_account_receivable_id != account:
            partner.property_account_receivable_id = account

    @api.model
    def _update_partner(self, store, partner, customer, billing, shipping, email, phone, is_guest=False):
        """Refresh Shopify-owned fields without trampling Odoo data.

        Empty fields are always filled in. Populated fields are only refreshed
        for partners this connector owns, and only when the store opts in.

        A guest checkout never overwrites anything: it was matched on a single
        signal (usually the phone number), and the contact details it carries
        are the buyer's for that one order, not a correction to the customer
        record.
        """
        shopify_owned = bool(partner.shopify_customer_id or partner.shopify_mapping_ids)
        may_overwrite = store.update_existing_customer and shopify_owned and not is_guest

        incoming = {
            'name': self._build_name(customer, billing, shipping, email, {}),
            'email': email,
            'phone': phone,
        }
        values = {}
        for field in SHOPIFY_OWNED_FIELDS:
            new_value = incoming.get(field)
            if not new_value:
                continue
            current = partner[field]
            if not current:
                values[field] = new_value
            elif may_overwrite and current != new_value:
                values[field] = new_value

        # Never leave a Shopify customer without the address we know about.
        if not partner.street and not partner.child_ids:
            values.update(self._address_values(billing or shipping))

        if values:
            partner.sudo().write(values)
        return partner

    # ------------------------------------------------------------------
    # Addresses
    # ------------------------------------------------------------------

    @api.model
    def _resolve_address(self, store, partner, address, address_type):
        """Return the partner to use as invoice/delivery address.

        The main partner is reused when the Shopify address is the same one it
        already holds; otherwise a dedicated child contact is found or created.
        Billing and shipping are resolved independently -- Shopify orders
        routinely differ between the two.
        """
        if not address or not any(address.get(key) for key in ('address1', 'city', 'zip', 'country_code')):
            return self.env['res.partner']

        values = self._address_values(address)
        name = self._address_person_name(address) or partner.name

        if self._same_address(partner, values):
            return partner

        existing = partner.child_ids.filtered(
            lambda child: child.type == address_type and self._same_address(child, values))
        if existing:
            return existing[0]

        # Reuse a child the connector previously created for this role before
        # adding yet another contact to the customer.
        reusable = partner.child_ids.filtered(
            lambda child: child.shopify_address_type == (
                'billing' if address_type == 'invoice' else 'shipping'))
        if reusable:
            reusable[0].sudo().write(dict(values, name=name))
            return reusable[0]

        return self.env['res.partner'].sudo().create(dict(
            values,
            name=name,
            type=address_type,
            parent_id=partner.id,
            shopify_address_type='billing' if address_type == 'invoice' else 'shipping',
            email=address.get('email') or partner.email,
            phone=address.get('phone') or partner.phone,
        ))

    @api.model
    def _same_address(self, partner, values):
        compared = ('street', 'street2', 'city', 'zip', 'state_id', 'country_id')
        for field in compared:
            current = partner[field]
            if field in ('state_id', 'country_id'):
                current = current.id or False
            incoming = values.get(field) or False
            if (current or False) != incoming:
                return False
        return True

    @api.model
    def _address_values(self, address):
        if not address:
            return {}
        country = self._find_country(address.get('country_code'), address.get('country'))
        state = self._find_state(country, address.get('province_code'), address.get('province'))
        return {
            'street': address.get('address1') or False,
            'street2': address.get('address2') or False,
            'city': address.get('city') or False,
            'zip': address.get('zip') or False,
            'state_id': state.id if state else False,
            'country_id': country.id if country else False,
        }

    @api.model
    def _find_country(self, code, name):
        Country = self.env['res.country']
        if code:
            country = Country.search([('code', '=ilike', code)], limit=1)
            if country:
                return country
        if name:
            country = Country.search([('name', '=ilike', name)], limit=1)
            if country:
                return country
        return Country

    @api.model
    def _find_state(self, country, code, name):
        State = self.env['res.country.state']
        if not country:
            return State
        if code:
            state = State.search([('country_id', '=', country.id), ('code', '=ilike', code)], limit=1)
            if state:
                return state
        if name:
            state = State.search([('country_id', '=', country.id), ('name', '=ilike', name)], limit=1)
            if state:
                return state
        return State

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @api.model
    def _build_name(self, customer, billing, shipping, email, payload):
        name = ' '.join(filter(None, [customer.get('first_name'), customer.get('last_name')])).strip()
        if name:
            return name
        for address in (billing, shipping):
            name = self._address_person_name(address)
            if name:
                return name
        if customer.get('company'):
            return customer['company']
        # An email is a poor display name but a real identifier. A bare order
        # number is neither, so it is not used as a fallback: an order with no
        # identity at all goes to Needs Review instead of creating a junk
        # contact that nobody can ever match again.
        return email or ''

    @api.model
    def _address_person_name(self, address):
        if not address:
            return ''
        name = ' '.join(filter(None, [address.get('first_name'), address.get('last_name')])).strip()
        return name or (address.get('name') or '').strip() or (address.get('company') or '').strip()

    @api.model
    def _address_name(self, billing, shipping):
        return self._address_person_name(billing) or self._address_person_name(shipping)

    @staticmethod
    def _clean_email(email):
        email = (email or '').strip().lower()
        return email if _EMAIL_RE.match(email) else ''
