# Shopify Connector for Odoo 18

Order-first Shopify integration. The connector imports Shopify orders into
`sale.order`, resolving (and creating) the customer **before** the sale order
exists, then resolving products, then writing the order.

There is no standalone customer import. Customer synchronisation is a step of
order synchronisation.

```
Shopify order
      │
      ▼
already imported? ──► yes, and confirmed in Odoo ──► refresh status only
      │
      ▼
resolve customer  ──► cannot identify ──► Needs Review
      │
      ▼
resolve billing + shipping addresses
      │
      ▼
resolve products  ──► any line unmatched ──► Needs Review (no sale order)
      │
      ▼
create/update sale.order ──► lines ──► discounts ──► shipping ──► taxes
      │
      ▼
reconcile Shopify total vs Odoo total ──► mismatch ──► Needs Review
      │
      ▼
Imported
```

The sale order is never created before the customer is resolved, and never
created at all while a line cannot be matched to a product.

## Requirements

- Odoo 18.0
- Python `requests` (bundled with the official Odoo images)
- A Shopify **Dev Dashboard app** with Client ID and Client secret

### Shopify scopes

| Scope | Why |
| --- | --- |
| `read_orders` | fetch orders |
| `read_customers` | read the customer attached to an order |
| `read_products` | read variant SKU and barcode for matching |
| `read_all_orders` | only needed for orders older than 60 days |

## Setup

Create your app in the [Dev Dashboard](https://dev.shopify.com), grant the
scopes above, install it on your store, then copy the **Client ID** and
**Client secret** from the app's Settings page.

1. In Odoo: **Shopify → Configuration → Stores → New**.
   - *Shop Domain*: `mystore.myshopify.com`
   - *Client ID* and *Client secret*: from the Dev Dashboard
   - Odoo exchanges the secret for a 24-hour token automatically and
     refreshes it before each sync
2. Set the company and warehouse under **Sale Order Defaults**.
3. Click **Test Connection**.
4. Map your catalogue (see below).
5. Click **Import Orders**.

The app and store must belong to the **same Shopify organization** for the
client-credentials grant to work. If you are a partner connecting to a
client store in a different org, the client-credentials grant will not work;
OAuth authorization-code support is not in V1.

## Product matching

A Shopify line item always references a *variant*, so that is what the
importer resolves. Matching is by identifier only, in this order:

1. `shopify.variant.mapping` for this store
2. `product.product.shopify_variant_id`
3. SKU (`default_code`)
4. Barcode
5. `shopify.product.mapping` on a single-variant template — **only** for lines
   that carry no variant ID at all

**Products are never matched by name.** Two Shopify products can share a
title, and a wrong line silently invoices the wrong item. An unresolvable line
sends the whole order to *Needs Review* with a message naming the variant ID,
SKU and barcode that were tried.

Step 5 is deliberately last and deliberately narrow. If a line names a variant
Odoo has never seen, that means Shopify has a variant Odoo does not — falling
back to the template's only variant would quietly sell the wrong item.

Matches found by SKU or barcode are written back as a variant mapping, so the
next import is a single indexed lookup.

### Where the Shopify IDs live

Mirroring how Odoo handles Barcode:

| | Single-variant product | Multi-variant product |
| --- | --- | --- |
| Product template form | Shopify Product ID **and** Shopify Variant ID | Shopify Product ID only |
| Product variant form | — | Shopify Variant ID per variant |

Internally the two identifiers stay distinct:
`product.template.shopify_product_id` and
`product.product.shopify_variant_id`.

### Fixing an unmatched line

Open the order under **Shopify → Orders → Needs Review**, press **Map** on the
red line, pick the Odoo product, then press **Retry Import**. The stored
payload is replayed; Shopify is not called again.

## Customer matching

1. Existing `shopify.customer.mapping` for this store
2. `res.partner.shopify_customer_id`
3. Email (case-insensitive)
4. Phone — compared on a stored, indexed key of the last 9 digits, so
   `+44 (0) 20 7946 0958` matches `02079460958`
5. Create a new customer

Guest checkouts are supported: an order with no Shopify customer record is
resolved from the order's own email, phone and billing address. Guests produce
no mapping row — there is no stable id to key on — so they are re-found by
email or phone on the next import.

An order with no email, no phone and no billing name cannot identify anyone.
Those go to *Needs Review* rather than creating a junk contact. Set a
**Fallback Guest Customer** on the store to import them anyway.

### What gets overwritten

Only `name`, `email` and `phone` are considered Shopify-owned. Empty fields are
always filled in; populated fields are refreshed only for partners this
connector owns, and only when *Update Existing Customer* is on. Everything else
(VAT, tags, payment terms, notes) is left as configured in Odoo.

A guest checkout never overwrites anything, even when it matches an existing
customer. It was matched on a single signal — usually the phone number — and
the contact details it carries belong to that one order, not to the customer
record.

## Addresses

Billing and shipping are resolved independently. When a Shopify address
matches the customer's own address, the customer is used directly; otherwise a
child contact of type `invoice` / `delivery` is found or created and reused on
later imports. They map to `partner_invoice_id` and `partner_shipping_id`.

## Discounts, shipping and taxes

**Discounts.** Shopify states a discount as money, Odoo as a percentage, and
both are stored at limited precision. The connector evaluates both
representations under Odoo's real rounding and uses whichever reproduces the
Shopify line total, preferring the percentage on a tie so the discount stays
visible. An order-level discount Shopify did not allocate to a line becomes a
separate negative line.

**Shipping.** Each Shopify shipping line becomes a sale order line on the
store's shipping service product, carrying its own tax lines. Zero-priced
shipping lines are skipped.

**Taxes.** Three modes:

| Mode | Behaviour |
| --- | --- |
| `Use Shopify tax lines` (default) | Reproduce the rate charged at checkout. An Odoo sale tax with the same rate and tax-inclusiveness is reused; if the chart of accounts has none, one is created and the creation is logged. |
| `Use Odoo product taxes` | Leave `tax_id` alone and let Odoo compute from the products and fiscal position. |
| `No taxes` | Strip taxes from every imported line. |

## Reconciliation

After the lines are written, the Odoo `amount_total` is compared with the
Shopify `total_price`. A gap larger than the store's **Total Tolerance**
(default 0.01) flags the order *Needs Review* — the sale order is still
created, so nothing is lost, but it will not be auto-confirmed.

## Idempotency

Re-running the import never duplicates anything.

| Thing | Key |
| --- | --- |
| Shopify order mirror | `store_id` + `shopify_order_id` |
| Sale order | `shopify_store_id` + `shopify_order_id` (SQL unique) |
| Registered customer | `store_id` + `shopify_customer_id` (SQL unique) |
| Variant mapping | `store_id` + `shopify_variant_id` (SQL unique) |
| Open queue job | partial unique index on `store_id` + `shopify_order_id` |

Re-importing an order that is still a quotation in Odoo rewrites its lines from
Shopify. Once the sale order is confirmed, only the Shopify status fields are
refreshed — the connector never rewrites what is being delivered or invoiced.

## Error handling

Each order is processed inside its own savepoint, so one bad order rolls back
alone and the run continues:

```
#1001 → Success
#1002 → Success
#1003 → Failed: No Odoo product found for Shopify line 'Wool Hat'. Tried: SKU ABC123
#1004 → Success
```

Failures land in three places:

- **Shopify → Orders → Needs Review** — the order, with the exact reason and
  the offending lines highlighted.
- **Shopify → Monitoring → Sync Queue** — the job, with its payload, attempt
  count and next retry time. Failed jobs retry with a growing backoff up to
  *Max Attempts*, then stop.
- **Shopify → Monitoring → Sync Logs** — the full trail.

## Scheduling

Two scheduled actions ship disabled. Turn them on once a manual run looks
right:

- **Shopify: Import Orders** — hourly, for stores with *Scheduled Sync* ticked.
- **Shopify: Process Sync Queue** — every 15 minutes, picks up retries.

A third, **Shopify: Clean Sync History**, is active and weekly: it deletes
info/warning logs older than 90 days and finished queue jobs older than 30 days.
Error logs are kept.

Each import run starts from the last sync time minus **Sync Overlap**
(default 10 minutes), which absorbs clock skew between Shopify and Odoo.
Re-scanning is harmless because of the idempotency keys above.

## Multi-currency

Sale order **line prices always come from Shopify** — the pricelist is not
used to calculate them. An optional store pricelist (or any company pricelist
matching the order currency) is only used to set the sale order currency
header in Odoo.

## Multi-company

Every connector model carries a `company_id` and a global record rule. The
product-level `shopify_product_id` / `shopify_variant_id` fields hold one
identifier at a time; if two stores sell the same catalogue, rely on the
per-store mapping tables, which are the authoritative lookup anyway.

## Security

| Group | Can |
| --- | --- |
| Shopify / User | Run imports, fix mappings, read logs. Implies Sales / User. |
| Shopify / Manager | Everything above, plus store configuration and credentials. |

The access token is only readable by Shopify / Manager.

## Models

| Model | Purpose |
| --- | --- |
| `shopify.store` | Connection and per-store behaviour |
| `shopify.customer.mapping` | Registered Shopify customer ↔ `res.partner` |
| `shopify.product.mapping` | Shopify product ↔ `product.template` |
| `shopify.variant.mapping` | Shopify variant ↔ `product.product` |
| `shopify.order` / `shopify.order.line` | Mirror of the Shopify order and the audit trail of its import |
| `shopify.sync.queue` | Fetched payloads waiting to be imported, with retries |
| `shopify.sync.log` | What happened, and why |

Business logic lives in overridable `AbstractModel` services:
`shopify.customer.resolver`, `shopify.product.resolver`,
`shopify.tax.resolver` and `shopify.order.importer`. The HTTP client
(`services/shopify_client.py`) has no Odoo imports.

## API transport

GraphQL is the default. Shopify marked the REST Admin API legacy on
2024-10-01; it still serves custom apps, so REST remains available for stores
whose app predates the migration. Both transports normalise to the same
payload shape, so the importer is unaware of which one is in use.

Bump **API Version** before your pinned version ages out — Shopify supports
each stable version for at least 12 months.

## Tests

```bash
odoo -d <db> -u shopify_connector --test-enable --test-tags /shopify_connector --stop-after-init
```

77 tests cover client normalisation (GraphQL and REST), Dev Dashboard token
exchange, pagination, throttle
retries, customer and product matching priority, guest checkout, address
splitting, discounts, shipping, taxes, reconciliation, per-order failure
isolation and idempotency. No test touches the network.

## Not in this version

Refunds and returns, fulfilment write-back to Shopify, inventory push, product
and customer catalogue imports, webhooks, and draft orders. The Shopify order
mirror stores the full payload, so these can be added without a re-import.

### Custom sale order fields

The store's **Custom Fields** tab configures database-specific sale order values.
Add a mapping and choose **Your custom field**. Then choose:

- **Use the same value for every order**: enter text or a number, tick a checkbox,
  pick a date, choose an available selection value, or search for an existing
  related record by name. The form shows the control matching your custom field.
- **Copy from Shopify**: choose a named option such as Order number / name,
  Customer email, Payment method, Tracking number, Order number + tracking number,
  Order total, or Shipping city.

Customers do not need JSON, technical keys, record IDs, or Shopify paths.
Shopify numeric amounts are converted for numeric destination fields. Values
must still match the destination field: for example, copy Order total into a
number field, and Customer email into a text field. Selection destinations must
accept the incoming Shopify value; use a fixed dropdown option otherwise.
Previous mappings remain stored, including previously entered Shopify paths.
Choosing a new Shopify option replaces that previous source.

Mappings apply to new and editable sale orders through the normal import flow.
Confirmed orders retain their custom values. Missing or null payload values
are skipped; an explicit `false` clears an optional field. Invalid values fail
the order import with a validation error. Disable a mapping to stop applying it.
Only Shopify Managers can configure mappings; company isolation applies.

Studio/manual fields are discovered directly. Addon fields are included when
all owning XML-ID modules are installed, have a nonempty author without
`Odoo` / `OpenERP`, and are not this connector. This conservative metadata
heuristic excludes standard Odoo fields even when extended by custom addons;
it may omit custom addons with missing or misleading author metadata. Review
addon manifest authors and field XML IDs if a custom field is absent.
Computed, related, readonly, nonstored, group-restricted, one2many, many2many,
and binary fields are excluded. No Python expressions are evaluated.

Existing Order From, Type of Order, and Studio Order ID values are retained
internally for compatibility; their old form section is removed. Configure these
fields through Custom Field Mappings. Explicit mappings override legacy defaults during import. Mapping
the legacy Studio Order ID disables its separate legacy refresh behavior, except
for Order number + tracking number, which retains tracking updates after confirmation.
Upgrade `shopify_connector` to version `18.0.1.0.23` to load the model and view.
