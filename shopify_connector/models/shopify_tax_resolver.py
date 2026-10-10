from odoo import _, api, models
from odoo.tools import float_compare, float_is_zero


class ShopifyTaxResolver(models.AbstractModel):
    """Translate Shopify tax lines into Odoo taxes.

    Shopify tells us the rate it actually charged at checkout. Rather than
    letting Odoo recompute (which would silently disagree with the money the
    customer paid), we look for an Odoo tax with the same rate and
    tax-inclusiveness, and create one if the chart of accounts has none.
    """

    _name = 'shopify.tax.resolver'
    _description = 'Shopify Tax Resolver'

    @api.model
    def resolve(self, store, tax_lines, taxes_included):
        """Return the ``account.tax`` recordset for a list of Shopify tax lines."""
        if store.tax_mode != 'shopify':
            return self.env['account.tax']

        taxes = self.env['account.tax']
        for tax_line in tax_lines or []:
            rate = float(tax_line.get('rate') or 0.0)
            price = float(tax_line.get('price') or 0.0)
            if float_is_zero(rate, precision_digits=6) and float_is_zero(price, precision_digits=6):
                continue
            tax = self._find_or_create_tax(store, tax_line.get('title'), rate, taxes_included)
            if tax:
                taxes |= tax
        return taxes

    @api.model
    def _find_or_create_tax(self, store, title, rate, taxes_included):
        amount = round(rate * 100.0, 4)
        company = store.company_id
        domain = [
            ('type_tax_use', '=', 'sale'),
            ('amount_type', '=', 'percent'),
            ('company_id', '=', company.id),
            ('price_include', '=', bool(taxes_included)),
            ('active', '=', True),
        ]
        candidates = self.env['account.tax'].sudo().search(domain)
        candidates = candidates.filtered(
            lambda tax: float_compare(tax.amount, amount, precision_digits=4) == 0)
        if candidates:
            # A tax whose name echoes Shopify's is the better match when the
            # chart of accounts happens to hold several taxes at the same rate.
            if title:
                named = candidates.filtered(lambda tax: title.lower() in (tax.name or '').lower())
                if named:
                    return named[0]
            return candidates[0]

        name = self._build_tax_name(store, title, amount, taxes_included)
        tax = self.env['account.tax'].sudo().create({
            'name': name,
            'amount': amount,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': company.id,
            'country_id': (company.account_fiscal_country_id or company.country_id).id,
            'price_include_override': 'tax_included' if taxes_included else 'tax_excluded',
        })
        self.env['shopify.sync.log'].log(
            store, 'warning', 'tax_resolve',
            _("Created Odoo tax %(name)s because no sale tax at %(rate)s%% "
              "(%(mode)s) existed in %(company)s.",
              name=name, rate=amount,
              mode=_('tax included') if taxes_included else _('tax excluded'),
              company=company.display_name))
        return tax

    @api.model
    def _build_tax_name(self, store, title, amount, taxes_included):
        parts = [store.tax_account_prefix or 'Shopify']
        if title:
            parts.append(title)
        parts.append('%g%%' % amount)
        if taxes_included:
            parts.append(_('(incl.)'))
        return ' '.join(parts)
