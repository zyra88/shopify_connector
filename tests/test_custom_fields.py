from odoo.exceptions import ValidationError
from odoo.tests import Form, tagged

from .common import ShopifyConnectorCase


@tagged('post_install', '-at_install', 'shopify')
class TestCustomFields(ShopifyConnectorCase):
    def setUp(self):
        super().setUp()
        self.custom_field = self.env['ir.model.fields'].create({
            'name': 'x_shopify_test_source',
            'field_description': 'Test Source',
            'model_id': self.env['ir.model']._get_id('sale.order'),
            'ttype': 'char',
            'state': 'manual',
        })
        self.Mapping = self.env['shopify.custom.field']

    def mapping(self, **values):
        return self.Mapping.create(dict({
            'store_id': self.store.id,
            'field_id': self.custom_field.id,
            'value_json': '"Website"',
        }, **values))

    def test_custom_default_reaches_imported_order(self):
        self.mapping()
        order = self.import_payload(self.order_payload())
        self.assertEqual(order.state, 'imported')
        self.assertEqual(order.sale_order_id[self.custom_field.name], 'Website')

    def test_standard_fields_excluded_and_rejected(self):
        standard = self.env['ir.model.fields']._get('sale.order', 'client_order_ref')
        self.assertNotIn(standard, self.Mapping._eligible_fields())
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self.mapping(field_id=standard.id)

    def test_payload_and_missing_value(self):
        mapping = self.mapping(source='payload', payload_path='customer.email')
        self.assertEqual(mapping.get_sale_order_values({'customer': {'email': 'a@example.com'}}),
                         {self.custom_field.name: 'a@example.com'})
        self.assertEqual(mapping.get_sale_order_values({'customer': None}), {})

    def test_invalid_json_and_wrong_type(self):
        for value in ('Website', '12', 'null', '{"a": 1}'):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self.mapping(value_json=value)

    def test_disabled_mapping_is_not_applied(self):
        mapping = self.mapping(active=False)
        self.assertEqual(mapping.get_sale_order_values({}), {})

    def test_false_can_clear_optional_field(self):
        mapping = self.mapping(value_json='false')
        self.assertEqual(mapping.get_sale_order_values({}), {self.custom_field.name: False})

    def test_plain_text_input_and_reopen(self):
        mapping = self.mapping(text_value='Website')
        self.assertEqual(mapping.get_sale_order_values({}), {self.custom_field.name: 'Website'})
        self.assertEqual(mapping.text_value, 'Website')
        mapping.write({'text_value': 'Online shop'})
        self.assertEqual(mapping._fixed_value(), 'Online shop')

    def test_friendly_shopify_choice(self):
        mapping = self.mapping(source='payload', shopify_value='email')
        self.assertEqual(mapping.payload_path, 'email')
        self.assertEqual(mapping.shopify_value, 'email')
        self.assertEqual(mapping.get_sale_order_values({'email': 'a@example.com'}),
                         {self.custom_field.name: 'a@example.com'})

    def test_hidden_controls_do_not_override_text(self):
        mapping = self.mapping(text_value='Website', number_value=0, record_value=0,
                               boolean_value=False, choice_id=False)
        self.assertEqual(mapping._fixed_value(), 'Website')

    def test_legacy_path_is_preserved(self):
        mapping = self.mapping(source='payload', payload_path='customer.note')
        self.assertFalse(mapping.shopify_value)
        self.assertTrue(mapping.legacy_source)
        mapping.write({'active': False})
        self.assertEqual(mapping.payload_path, 'customer.note')

    def test_shopify_amount_to_number(self):
        numeric = self.env['ir.model.fields'].create({
            'name': 'x_shopify_test_amount', 'field_description': 'Test Amount',
            'model_id': self.env['ir.model']._get_id('sale.order'),
            'ttype': 'float', 'state': 'manual',
        })
        mapping = self.mapping(field_id=numeric.id, source='payload', shopify_value='total_price')
        self.assertEqual(mapping.get_sale_order_values({'total_price': '123.45'}),
                         {numeric.name: 123.45})
        with self.assertRaises(ValidationError):
            mapping.get_sale_order_values({'total_price': 'invalid'})

    def test_store_form_mapping_uses_plain_text(self):
        with Form(self.store, view='shopify_connector.shopify_store_view_form') as store_form:
            with store_form.custom_field_ids.new() as mapping_form:
                mapping_form.field_id = self.custom_field
                mapping_form.text_value = 'Website'
        self.assertEqual(self.store.custom_field_ids._fixed_value(), 'Website')

    def test_new_mapping_dropdown_includes_custom_fields(self):
        domain = self.Mapping.fields_get(['field_id'])['field_id']['domain']
        self.assertIn(self.custom_field, self.env['ir.model.fields'].search(domain))
        standard = self.env['ir.model.fields']._get('sale.order', 'client_order_ref')
        self.assertNotIn(standard, self.env['ir.model.fields'].search(domain))

    def test_order_name_and_tracking(self):
        mapping = self.mapping(source='payload', shopify_value='order_name_and_tracking')
        for payload, expected in [
            ({'name': 'TR-1', 'primary_tracking_number': 'TRACK-1'}, 'TR-1-TRACK-1'),
            ({'name': 'TR-1', 'tracking_numbers': ['TRACK-2']}, 'TR-1-TRACK-2'),
            ({'name': 'TR-1'}, 'TR-1'),
            ({'id': '123'}, '123'),
        ]:
            self.assertEqual(mapping.get_sale_order_values(payload),
                             {self.custom_field.name: expected})
        self.assertEqual(mapping.get_sale_order_values({}), {})
