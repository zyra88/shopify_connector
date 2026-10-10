from odoo.tests import tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestSchedule(ShopifyConnectorCase):

    def cron(self):
        return self.env.ref('zyra_shopify_connector.ir_cron_shopify_import_orders')

    def test_scheduled_sync_switches_the_scheduled_action(self):
        self.store.auto_sync = False
        self.assertFalse(self.cron().active)
        self.store.auto_sync = True
        self.assertTrue(self.cron().active)
        self.assertTrue(self.env.ref('zyra_shopify_connector.ir_cron_shopify_process_queue').active)
        self.store.auto_sync = False
        self.assertFalse(self.cron().active)

    def test_sync_every_is_written_to_the_scheduled_action(self):
        self.store.sync_interval = '30m'
        cron = self.cron()
        self.assertEqual((cron.interval_number, cron.interval_type), (30, 'minutes'))
        self.store.invalidate_recordset()
        self.assertEqual(self.store.sync_interval, '30m')
        self.store.sync_interval = '1d'
        self.assertEqual((cron.interval_number, cron.interval_type), (1, 'days'))
