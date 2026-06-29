from odoo.tests.common import TransactionCase
from odoo.addons.invoice_layout_designer.models import field_registry


class TestFieldRegistry(TransactionCase):

    def test_get_invoice_fields(self):
        fields = field_registry.get_available_fields("account.move")
        self.assertIn("header", fields)
        self.assertIn("partner", fields)
        self.assertIn("amounts", fields)
        self.assertIn("line_fields", fields)
        header_paths = [f["path"] for f in fields["header"]]
        self.assertIn("name", header_paths)
        self.assertIn("invoice_date", header_paths)

    def test_get_sale_order_fields(self):
        fields = field_registry.get_available_fields("sale.order")
        self.assertIn("header", fields)

    def test_get_unknown_doc_type(self):
        fields = field_registry.get_available_fields("unknown.model")
        self.assertEqual(fields, {})

    def test_get_line_model(self):
        self.assertEqual(field_registry.get_line_model("account.move"), "account.move.line")
        self.assertEqual(field_registry.get_line_model("sale.order"), "sale.order.line")

    def test_get_line_relation_field(self):
        self.assertEqual(field_registry.get_line_relation_field("account.move"), "invoice_line_ids")
        self.assertEqual(field_registry.get_line_relation_field("sale.order"), "order_line")

    def test_resolve_field_value(self):
        partner = self.env["res.partner"].create({"name": "Test Partner", "email": "test@example.com"})
        self.assertEqual(field_registry.resolve_field_value(partner, "name"), "Test Partner")
        self.assertEqual(field_registry.resolve_field_value(partner, "email"), "test@example.com")

    def test_resolve_empty_field(self):
        partner = self.env["res.partner"].create({"name": "Test"})
        self.assertEqual(field_registry.resolve_field_value(partner, "website", default="N/A"), "N/A")
