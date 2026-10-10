import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_CANCELLABLE_SO_STATES = ('draft', 'sent')


class ShopifySaleOrderConflict(models.Model):
    """A manual sale order and a connector-created duplicate for the same Shopify order."""

    _name = 'shopify.sale.order.conflict'
    _description = 'Shopify Sale Order Duplicate'
    _order = 'store_id, shopify_order_id'
    _rec_name = 'display_name'

    display_name = fields.Char(compute='_compute_display_name', store=True)
    store_id = fields.Many2one('shopify.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True)
    state = fields.Selection(
        [('open', 'Open'), ('resolved', 'Resolved')],
        default='open', required=True, index=True)

    shopify_order_id = fields.Char(string='Shopify Order ID', required=True, index=True)
    shopify_order_name = fields.Char(string='Shopify Order')
    shopify_mirror_id = fields.Many2one('shopify.order', string='Shopify Sync Record', ondelete='set null')

    match_reason = fields.Selection(
        [
            ('studio_order_id', 'Order ID = Shopify order id'),
            ('shopify_order_ref', 'Order ID starts with Shopify ref'),
            ('client_order_ref', 'Customer Reference'),
            ('order_origin', 'Sale Order Origin'),
            ('studio_and_ref', 'Studio Order ID and Reference'),
        ],
        string='Matched On', required=True)

    manual_sale_order_id = fields.Many2one(
        'sale.order', string='Manual Sale Order', required=True, ondelete='cascade')
    imported_sale_order_id = fields.Many2one(
        'sale.order', string='Imported Sale Order', required=True, ondelete='cascade')

    manual_order_name = fields.Char(related='manual_sale_order_id.name', string='Manual Order Reference')
    imported_order_name = fields.Char(related='imported_sale_order_id.name', string='Imported Order Reference')
    manual_partner_id = fields.Many2one(related='manual_sale_order_id.partner_id', string='Manual Customer')
    imported_partner_id = fields.Many2one(related='imported_sale_order_id.partner_id', string='Imported Customer')
    manual_state = fields.Selection(related='manual_sale_order_id.state', string='Manual Status')
    imported_state = fields.Selection(related='imported_sale_order_id.state', string='Imported Status')
    manual_amount_total = fields.Monetary(related='manual_sale_order_id.amount_total', string='Manual Total')
    imported_amount_total = fields.Monetary(related='imported_sale_order_id.amount_total', string='Imported Total')
    currency_id = fields.Many2one(related='manual_sale_order_id.currency_id')

    studio_order_id_display = fields.Char(
        string='Manual Order ID', compute='_compute_studio_order_ids')
    manual_studio_order_id = fields.Char(
        string='Manual Order ID (Studio)', compute='_compute_studio_order_ids')
    imported_studio_order_id = fields.Char(
        string='Imported Order ID (Studio)', compute='_compute_studio_order_ids')
    shopify_mirror_name = fields.Char(
        related='shopify_mirror_id.shopify_order_name', string='Shopify Sync Order')
    show_fill_studio_order_id_button = fields.Boolean(
        compute='_compute_show_fill_studio_order_id_button',
    )

    _sql_constraints = [
        (
            'shopify_conflict_uniq',
            'UNIQUE(store_id, shopify_order_id)',
            'This Shopify order is already listed as a duplicate for this store.',
        ),
    ]

    @api.depends(
        'shopify_order_name', 'shopify_order_id',
        'manual_order_name', 'imported_order_name', 'state',
    )
    def _compute_display_name(self):
        for conflict in self:
            label = conflict.shopify_order_name or conflict.shopify_order_id or _('Duplicate')
            if conflict.manual_order_name and conflict.imported_order_name:
                conflict.display_name = '%s (%s vs %s)' % (
                    label, conflict.manual_order_name, conflict.imported_order_name)
            else:
                conflict.display_name = label

    @api.depends()
    def _compute_show_fill_studio_order_id_button(self):
        show = bool(
            self.env['shopify.store']._count_linked_orders_missing_studio_order_id())
        # List header buttons need the flag even when the list has no rows yet.
        (self or self.env[self._name]).update({
            'show_fill_studio_order_id_button': show,
        })

    @api.depends('manual_sale_order_id', 'imported_sale_order_id')
    def _compute_studio_order_ids(self):
        field_name = self.env['shopify.store']._sale_order_studio_order_id_field_name()
        for conflict in self:
            manual_value = imported_value = ''
            if field_name:
                if conflict.manual_sale_order_id:
                    manual_value = conflict.manual_sale_order_id[field_name] or ''
                if conflict.imported_sale_order_id:
                    imported_value = conflict.imported_sale_order_id[field_name] or ''
            conflict.manual_studio_order_id = manual_value
            conflict.imported_studio_order_id = imported_value
            conflict.studio_order_id_display = manual_value

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def action_scan_duplicates(self):
        """Rebuild the open duplicate list for the current company (or one store from context)."""
        stores = self._scan_stores_from_context()
        self.search([('store_id', 'in', stores.ids), ('state', '=', 'open')]).unlink()
        count = sum(self._scan_store(shopify_store) for shopify_store in stores)
        action = self.env['ir.actions.act_window']._for_xml_id(
            'shopify_connector.action_shopify_sale_order_conflict')
        action['domain'] = [('store_id', 'in', stores.ids)]
        action['name'] = _('Order Duplicates (%s found)', count)
        action['context'] = dict(self.env.context, search_default_open=1)
        return action

    def action_backfill_studio_order_ids(self):
        """Re-fetch from Shopify and rewrite Studio Order ID on linked sale orders."""
        stores = self._scan_stores_from_context()
        updated = sum(
            store._sync_studio_order_ids_on_linked_orders(
                refresh_from_shopify=True,
                force_rewrite=True,
                only_missing_studio_order_id=True,
            )
            for store in stores)
        return stores[:1]._notify(
            _("%(count)s sale order(s) updated with Studio Order ID from Shopify.",
              count=updated),
        )

    @api.model
    def _scan_stores_from_context(self):
        store_id = self.env.context.get('default_store_id') or self.env.context.get('active_id')
        if self.env.context.get('active_model') == 'shopify.store' and store_id:
            return self.env['shopify.store'].browse(store_id)
        return self.env['shopify.store'].search([('company_id', 'in', self.env.companies.ids)])

    @api.model
    def _scan_store(self, store):
        """Match ``shopify.order.shopify_order_id`` to ``sale.order`` Studio Order ID."""
        studio_field = store._sale_order_studio_order_id_field_name()
        if not studio_field:
            _logger.info(
                "Shopify duplicate scan skipped for %s: Studio Order ID field not installed.",
                store.display_name,
            )
            return 0

        created = 0
        seen_shopify_ids = set()
        mirrors = self.env['shopify.order'].search([
            ('store_id', '=', store.id),
            ('shopify_order_id', '!=', False),
        ])
        for mirror in mirrors:
            shopify_id = mirror.shopify_order_id
            if not shopify_id or shopify_id in seen_shopify_ids:
                continue
            if self.search_count([
                ('store_id', '=', store.id),
                ('shopify_order_id', '=', shopify_id),
            ]):
                continue

            imported = self._imported_sale_order(store, mirror, shopify_id)
            if not imported:
                continue

            studio_orders, match_reason = self._sale_orders_matching_mirror(
                store, studio_field, mirror)
            if not studio_orders:
                continue

            manual = self._pick_manual_sale_order(studio_orders, imported)
            if not manual or manual == imported:
                continue

            seen_shopify_ids.add(shopify_id)
            self.create({
                'store_id': store.id,
                'shopify_order_id': shopify_id,
                'shopify_order_name': mirror.shopify_order_name or imported.shopify_order_name,
                'shopify_mirror_id': mirror.id,
                'manual_sale_order_id': manual.id,
                'imported_sale_order_id': imported.id,
                'match_reason': match_reason,
            })
            created += 1
        return created

    @api.model
    def _studio_shopify_id_values(self, shopify_id):
        values = [shopify_id]
        if shopify_id.isdigit():
            values.append(int(shopify_id))
        return values

    @api.model
    def _sale_orders_with_studio_shopify_id(self, store, studio_field, shopify_id):
        return self.env['sale.order'].search([
            ('company_id', '=', store.company_id.id),
            (studio_field, 'in', self._studio_shopify_id_values(shopify_id)),
        ], order='id asc')

    @api.model
    def _sale_orders_matching_mirror(self, store, studio_field, mirror):
        """Match Studio Order ID to the Shopify mirror (numeric id or TR- ref)."""
        SaleOrder = self.env['sale.order']
        company_domain = [('company_id', '=', store.company_id.id)]
        shopify_id = mirror.shopify_order_id

        exact = self._sale_orders_with_studio_shopify_id(store, studio_field, shopify_id)
        if exact:
            manual = self._pick_manual_sale_order(
                exact, self._imported_sale_order(store, mirror, shopify_id))
            imported = self._imported_sale_order(store, mirror, shopify_id)
            if manual and imported and manual != imported:
                return exact, 'studio_order_id'

        shopify_ref = (mirror.shopify_order_name or '').strip()
        if shopify_ref:
            by_prefix = SaleOrder.search(
                company_domain + [(studio_field, '=like', shopify_ref + '-%')],
                order='id asc',
            )
            by_exact_ref = SaleOrder.search(
                company_domain + [(studio_field, '=', shopify_ref)],
                order='id asc',
            )
            combined = (by_prefix | by_exact_ref).sorted('id')
            if combined:
                return combined, 'shopify_order_ref'

        return SaleOrder.browse(), False

    @api.model
    def _imported_sale_order(self, store, mirror, shopify_id):
        if mirror.sale_order_id:
            return mirror.sale_order_id
        return self.env['sale.order'].search([
            ('company_id', '=', store.company_id.id),
            ('shopify_store_id', '=', store.id),
            ('shopify_order_id', '=', shopify_id),
        ], limit=1)

    @api.model
    def _pick_manual_sale_order(self, studio_orders, imported):
        """Prefer the sale order that is not the Shopify-linked import."""
        without_connector = studio_orders.filtered(
            lambda order: not order.shopify_store_id and not order.shopify_order_id)
        if without_connector:
            return without_connector[0]
        if imported:
            others = studio_orders.filtered(lambda order: order != imported)
            if others:
                return others[0]
        if len(studio_orders) > 1:
            return studio_orders[0]
        return self.env['sale.order']

    # ------------------------------------------------------------------
    # Resolve
    # ------------------------------------------------------------------

    def action_keep_manual_order(self):
        """Link Shopify to the manual order, strip/cancel the imported duplicate."""
        for conflict in self:
            conflict._resolve_keep_manual()
        return self._reload_list_action()

    def action_keep_imported_order(self):
        """Keep the connector sale order; clear/cancel the manual duplicate."""
        for conflict in self:
            conflict._resolve_keep_imported()
        return self._reload_list_action()

    def _resolve_keep_manual(self):
        self.ensure_one()
        if self.state != 'open':
            raise UserError(_("This conflict is already resolved."))

        store = self.store_id
        manual = self.manual_sale_order_id
        imported = self.imported_sale_order_id
        mirror = self.shopify_mirror_id

        if manual == imported:
            self.state = 'resolved'
            return

        if mirror:
            mirror.write({'sale_order_id': manual.id})

        self._strip_shopify_from_sale_order(imported)
        self._try_cancel_sale_order(store, imported, _('imported duplicate'))

        if mirror and mirror.payload:
            try:
                payload = json.loads(mirror.payload or '{}')
            except ValueError as exc:
                raise UserError(_(
                    "Cannot refresh the manual order: the Shopify payload on %(mirror)s is invalid.",
                    mirror=mirror.display_name,
                )) from exc
            if payload:
                self.env['shopify.order.importer'].import_order(store, payload)
        else:
            manual.write({
                'shopify_store_id': store.id,
                'shopify_order_id': self.shopify_order_id,
                'shopify_order_name': self.shopify_order_name,
            })
            if mirror:
                manual.shopify_order_ref_id = mirror.id
                mirror.write({'sale_order_id': manual.id})

        self.env['shopify.sync.log'].log(
            store, 'info', 'order_import',
            _("Duplicate resolved: kept manual sale order %(manual)s, removed link from %(imported)s.",
              manual=manual.name, imported=imported.name),
            shopify_reference=self.shopify_order_name,
            shopify_order_id=self.shopify_order_id,
            shopify_order_ref_id=mirror[:1].id if mirror else False,
            sale_order_id=manual.id,
        )
        self.state = 'resolved'

    def _resolve_keep_imported(self):
        self.ensure_one()
        if self.state != 'open':
            raise UserError(_("This conflict is already resolved."))

        store = self.store_id
        manual = self.manual_sale_order_id
        imported = self.imported_sale_order_id
        mirror = self.shopify_mirror_id

        studio_field = store._sale_order_studio_order_id_field_name()
        if studio_field:
            manual.write({studio_field: False})

        self._try_cancel_sale_order(store, manual, _('manual duplicate'))

        if mirror and mirror.sale_order_id != imported:
            mirror.write({'sale_order_id': imported.id})
        imported.shopify_order_ref_id = mirror.id if mirror else False

        self.env['shopify.sync.log'].log(
            store, 'info', 'order_import',
            _("Duplicate resolved: kept imported sale order %(imported)s, cleared manual %(manual)s.",
              imported=imported.name, manual=manual.name),
            shopify_reference=self.shopify_order_name,
            shopify_order_id=self.shopify_order_id,
            shopify_order_ref_id=mirror[:1].id if mirror else False,
            sale_order_id=imported.id,
        )
        self.state = 'resolved'

    @api.model
    def _strip_shopify_from_sale_order(self, sale_order):
        sale_order.write({
            'shopify_store_id': False,
            'shopify_order_id': False,
            'shopify_order_name': False,
            'shopify_order_number': False,
            'shopify_financial_status': False,
            'shopify_fulfillment_status': False,
            'shopify_payment_gateway': False,
            'shopify_tags': False,
            'shopify_currency_code': False,
            'shopify_created_at': False,
            'shopify_updated_at': False,
            'shopify_order_ref_id': False,
        })

    @api.model
    def _try_cancel_sale_order(self, store, sale_order, label):
        """Cancel draft/sent duplicates; leave confirmed orders open for manual handling."""
        if sale_order.state in _CANCELLABLE_SO_STATES:
            sale_order.action_cancel()
            return True
        if sale_order.state == 'cancel':
            return True
        message = _(
            "Duplicate resolution: %(label)s %(order)s was left open (%(state)s). "
            "Only draft or sent quotations are cancelled automatically.",
            label=label, order=sale_order.name, state=sale_order.state,
        )
        _logger.info(message)
        self.env['shopify.sync.log'].log(
            store, 'warning', 'order_import', message,
            shopify_reference=sale_order.shopify_order_name or sale_order.client_order_ref,
            shopify_order_id=sale_order.shopify_order_id or False,
            sale_order_id=sale_order.id,
        )
        return False

    def action_open_manual_sale_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.manual_sale_order_id.id,
            'view_mode': 'form',
        }

    def action_open_imported_sale_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.imported_sale_order_id.id,
            'view_mode': 'form',
        }

    def _reload_list_action(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Order Duplicates'),
            'res_model': 'shopify.sale.order.conflict',
            'view_mode': 'list,form',
            'domain': [('state', '=', 'open')],
            'context': dict(self.env.context),
        }
