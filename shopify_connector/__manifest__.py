{
    'name': 'Shopify Connector',
    'version': '18.0.1.0.25',
    'category': 'Sales/Sales',
    'author': 'M.SHAHIDH',
    'summary': 'Order-first Shopify integration: import orders, resolve customers and products, create sale orders',
    'description': """
Shopify Connector
=================

Order-first Shopify integration for Odoo 18.

The connector treats customer synchronisation as a *step of* order
synchronisation: for every Shopify order the customer is resolved (or created)
first, then the addresses, then the products, and only then is the
``sale.order`` written. There is no standalone customer import.

Features
--------
* Multi-store configuration with connection test (GraphQL or legacy REST).
* Order import with per-order isolation -- one failing order never aborts the run.
* Customer resolution by mapping, Shopify customer id, email then phone.
* Guest checkout support (orders with no Shopify customer record).
* Billing / shipping child contacts under the commercial partner.
* Product matching by mapping, variant id, SKU then barcode. Never by name.
* Discounts, shipping lines, tax lines and payment gateway capture.
* Total reconciliation between Shopify and Odoo with a "Needs Review" state.
* Idempotent: re-running the import creates no duplicates.
* Sync queue, sync logs and actionable error messages.
""",
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'sale_management',
        'stock',
        'account',
    ],
    'external_dependencies': {
        'python': ['requests'],
    },
    'data': [
        'security/shopify_security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'wizards/shopify_import_orders_wizard_views.xml',
        'wizards/shopify_fetch_catalog_wizard_views.xml',
        'views/shopify_sale_order_conflict_views.xml',
        'views/shopify_sync_log_views.xml',
        'views/shopify_sync_queue_views.xml',
        'views/shopify_customer_mapping_views.xml',
        'views/shopify_product_mapping_views.xml',
        'views/shopify_product_catalog_views.xml',
        'views/shopify_order_views.xml',
        'views/shopify_store_views.xml',
        'views/product_views.xml',
        'views/res_partner_views.xml',
        'views/sale_order_views.xml',
        'views/shopify_menus.xml',
        'data/shopify_menu_cleanup.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
