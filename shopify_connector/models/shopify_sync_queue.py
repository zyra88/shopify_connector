import json
import logging
import traceback

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ShopifySyncQueue(models.Model):
    """One unit of work between "fetched from Shopify" and "written to Odoo".

    Fetching and importing are split so that a network problem halfway through
    a large run does not lose the orders already downloaded, and so that a
    single poisonous order can be retried or cancelled in isolation.
    """

    _name = 'shopify.sync.queue'
    _description = 'Shopify Sync Queue'
    _order = 'priority desc, id asc'
    _rec_name = 'display_name'

    store_id = fields.Many2one(
        'shopify.store', string='Store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True, index=True)

    job_type = fields.Selection(
        [('order_import', 'Order Import')], required=True, default='order_import', index=True)
    shopify_order_id = fields.Char(string='Shopify Order ID', index=True)
    shopify_order_name = fields.Char(string='Shopify Order')

    payload = fields.Text(
        required=True,
        help="Normalised Shopify payload, stored so the job can be replayed "
             "without calling Shopify again.")

    state = fields.Selection(
        [
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('done', 'Done'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled'),
        ],
        default='pending', required=True, index=True)

    priority = fields.Integer(default=0)
    attempts = fields.Integer(default=0, readonly=True)
    max_attempts = fields.Integer(default=3)
    next_attempt_at = fields.Datetime(index=True)
    processed_at = fields.Datetime(readonly=True)

    error_message = fields.Text(readonly=True)
    exception_info = fields.Text(string='Technical Details', readonly=True)

    shopify_order_ref_id = fields.Many2one('shopify.order', string='Shopify Order Record', ondelete='set null')
    shopify_order_state = fields.Selection(
        related='shopify_order_ref_id.state', string='Import Result', readonly=True)
    review_reason = fields.Text(
        related='shopify_order_ref_id.review_reason', string='Review Reason', readonly=True)
    sale_order_id = fields.Many2one('sale.order', string='Sale Order', ondelete='set null')
    log_ids = fields.One2many('shopify.sync.log', 'queue_id', string='Logs')

    _sql_constraints = [
        (
            'payload_not_empty',
            'CHECK (payload IS NOT NULL)',
            'A queue job must carry a payload.',
        ),
    ]

    def init(self):
        # A partial index rather than a UNIQUE constraint: an order may legitimately
        # be queued again after a previous job is done, cancelled or failed.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS shopify_sync_queue_open_job_uniq
            ON shopify_sync_queue (store_id, job_type, shopify_order_id)
            WHERE state IN ('pending', 'processing') AND shopify_order_id IS NOT NULL
        """)

    @api.depends('job_type', 'shopify_order_name', 'shopify_order_id')
    def _compute_display_name(self):
        for job in self:
            job.display_name = job.shopify_order_name or job.shopify_order_id or _('Job %s', job.id or '')

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    @api.model
    def enqueue_order(self, store, payload):
        """Queue one normalised Shopify order, replacing any open job for it.

        Returns the queue record. Safe to call repeatedly with the same order:
        a still-open job is refreshed rather than duplicated.
        """
        shopify_order_id = str(payload.get('id') or '')
        if not shopify_order_id:
            raise UserError(_("Shopify returned an order without an id; it cannot be queued."))

        values = {
            'store_id': store.id,
            'job_type': 'order_import',
            'shopify_order_id': shopify_order_id,
            'shopify_order_name': payload.get('name'),
            'payload': json.dumps(payload, default=str),
            'state': 'pending',
            'error_message': False,
            'exception_info': False,
            'next_attempt_at': fields.Datetime.now(),
        }

        existing = self.search([
            ('store_id', '=', store.id),
            ('job_type', '=', 'order_import'),
            ('shopify_order_id', '=', shopify_order_id),
            ('state', 'in', ('pending', 'processing')),
        ], limit=1)
        if existing:
            existing.write(values)
            return existing
        return self.create(values)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def get_payload(self):
        self.ensure_one()
        try:
            return json.loads(self.payload or '{}')
        except ValueError as exc:
            raise UserError(_("Queue job %s carries an unreadable payload: %s", self.display_name, exc))

    def process(self):
        """Run every selected job, isolating failures job by job.

        Returns a summary dict so callers can render a notification.
        """
        summary = {'done': 0, 'needs_review': 0, 'failed': 0}
        importer = self.env['shopify.order.importer']

        for job in self:
            if job.state in ('done', 'cancelled'):
                continue
            job.write({'state': 'processing', 'attempts': job.attempts + 1})

            try:
                # A savepoint keeps a broken order from poisoning the whole run:
                # only this job's writes are rolled back.
                with self.env.cr.savepoint():
                    result = importer.import_order(job.store_id, job.get_payload(), queue=job)
            except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
                job._mark_failed(exc)
                summary['failed'] += 1
                continue

            job.write({
                'state': 'done',
                'processed_at': fields.Datetime.now(),
                'error_message': False,
                'exception_info': False,
                'shopify_order_ref_id': result.get('shopify_order_id'),
                'sale_order_id': result.get('sale_order_id'),
            })
            if result.get('state') == 'needs_review':
                summary['needs_review'] += 1
            else:
                summary['done'] += 1

        return summary

    def _mark_failed(self, exc):
        self.ensure_one()
        message = str(exc) or exc.__class__.__name__
        exhausted = self.attempts >= max(self.max_attempts, 1)
        self.write({
            'state': 'failed' if exhausted else 'pending',
            'error_message': message,
            'exception_info': traceback.format_exc(),
            'next_attempt_at': fields.Datetime.add(
                fields.Datetime.now(), minutes=5 * self.attempts) if not exhausted else False,
        })
        self.env['shopify.sync.log'].log(
            self.store_id, 'error', 'queue',
            _("Order %(order)s failed (attempt %(n)s/%(max)s): %(error)s",
              order=self.shopify_order_name or self.shopify_order_id,
              n=self.attempts, max=self.max_attempts, error=message),
            shopify_reference=self.shopify_order_name or self.shopify_order_id,
            shopify_order_id=self.shopify_order_id,
            queue_id=self.id,
            exception_info=traceback.format_exc(),
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _notify_and_reload(self, message, warning=False):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Shopify'),
                'message': message,
                'type': 'warning' if warning else 'success',
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    def action_process(self):
        jobs = self.filtered(lambda job: job.state not in ('done', 'cancelled'))
        if not jobs:
            return self._notify_and_reload(_("No open jobs to process."), warning=True)
        summary = jobs.process()
        parts = []
        if summary['done']:
            parts.append(_("%s imported", summary['done']))
        if summary['needs_review']:
            parts.append(_("%s need review", summary['needs_review']))
        if summary['failed']:
            parts.append(_("%s failed", summary['failed']))
        message = ', '.join(parts) if parts else _("No jobs were processed.")
        if summary['done'] or summary['needs_review']:
            message += ' ' + _("Switch to the Done filter to review completed jobs.")
        return self._notify_and_reload(
            message,
            warning=bool(summary['failed'] or summary['needs_review']),
        )

    def action_process_pending(self):
        domain = [('state', '=', 'pending')]
        store_ids = self.mapped('store_id').ids
        if store_ids:
            domain.append(('store_id', 'in', store_ids))
        pending = self.search(domain, order='priority desc, id asc')
        if not pending:
            return self._notify_and_reload(_("No pending jobs to process."), warning=True)
        return pending.action_process()

    def action_retry(self):
        self.write({
            'state': 'pending',
            'attempts': 0,
            'error_message': False,
            'exception_info': False,
            'next_attempt_at': fields.Datetime.now(),
        })
        return self._notify_and_reload(_("Reset %(count)s job(s) to Pending.", count=len(self)))

    def action_cancel(self):
        self.filtered(lambda job: job.state != 'done').write({'state': 'cancelled'})
        return True

    def action_open_shopify_order(self):
        self.ensure_one()
        if not self.shopify_order_ref_id:
            raise UserError(_("No Shopify order record is linked to this queue job yet."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Shopify Order'),
            'res_model': 'shopify.order',
            'res_id': self.shopify_order_ref_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_payload(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'shopify.sync.queue',
            'res_id': self.id,
            'view_mode': 'form',
            'views': [(self.env.ref('shopify_connector.shopify_sync_queue_payload_form').id, 'form')],
            'target': 'new',
            'name': _('Shopify Payload'),
        }

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def _cron_process_queue(self, limit=200):
        now = fields.Datetime.now()
        jobs = self.search([
            ('state', '=', 'pending'),
            '|', ('next_attempt_at', '=', False), ('next_attempt_at', '<=', now),
        ], limit=limit)
        if not jobs:
            return True

        in_test = self.env.registry.in_test_mode()
        for job in jobs:
            job.process()
            if not in_test:
                # Commit per job: a worker restart must not replay work already done.
                self.env.cr.commit()
        return True

    @api.model
    def _gc_queue(self, days=30):
        limit_date = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        self.search([
            ('state', 'in', ('done', 'cancelled')),
            ('write_date', '<', limit_date),
        ]).unlink()
        return True
