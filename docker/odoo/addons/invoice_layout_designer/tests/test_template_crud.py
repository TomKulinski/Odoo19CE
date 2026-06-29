from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError


class TestTemplateCrud(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Template = self.env["document.layout.template"]
        self.Element = self.env["document.layout.element"]

        # Auto-Seeding überspringen: die Tests prüfen den leeren
        # Ausgangszustand und fügen Elemente gezielt selbst hinzu. Ohne
        # diesen Context würde der Style-Seeder das Template sofort mit den
        # DIN5008-Default-Elementen befüllen und die Zählungen verfälschen.
        self.template = self.Template.with_context(
            ild_skip_auto_seed=True
        ).create({
            "name": "Test Invoice Layout",
            "doc_type": "account.move",
            "paper_format": "A4",
        })

    def test_create_template(self):
        """Test basic template creation."""
        self.assertTrue(self.template.exists())
        self.assertEqual(self.template.name, "Test Invoice Layout")
        self.assertEqual(self.template.doc_type, "account.move")
        self.assertEqual(self.template.paper_width, 210)
        self.assertEqual(self.template.paper_height, 297)

    def test_create_element(self):
        """Test adding elements to a template."""
        elem = self.Element.create({
            "template_id": self.template.id,
            "name": "Test Text",
            "element_type": "text",
            "zone": "header",
            "pos_x": 10,
            "pos_y": 5,
            "width": 80,
            "height": 10,
            "text_content": "Hello World",
        })
        self.assertTrue(elem.exists())
        self.assertEqual(self.template.element_count, 1)

    def test_unique_default(self):
        """Test that only one default template per doc_type per company."""
        self.template.is_default = True
        template2 = self.Template.create({
            "name": "Another Invoice Layout",
            "doc_type": "account.move",
            "is_default": True,
        })
        # First template should no longer be default
        self.assertFalse(self.template.is_default)
        self.assertTrue(template2.is_default)

    def test_margin_validation(self):
        """Test margin boundaries."""
        with self.assertRaises(ValidationError):
            self.template.margin_top = -5

        with self.assertRaises(ValidationError):
            self.template.margin_left = 60

    def test_layout_json(self):
        """Test JSON storage and retrieval."""
        data = {"version": 1, "elements": []}
        self.template.set_layout_data(data)
        result = self.template.get_layout_data()
        self.assertEqual(result["version"], 1)

    def test_export_json(self):
        """Test template JSON export."""
        import json
        self.Element.create({
            "template_id": self.template.id,
            "name": "Test Element",
            "element_type": "text",
            "zone": "body",
        })
        json_str = self.template.export_template_json()
        data = json.loads(json_str)
        self.assertEqual(data["name"], "Test Invoice Layout")
        self.assertEqual(len(data["elements"]), 1)

    def test_element_serialization(self):
        """Test element to_editor_dict."""
        elem = self.Element.create({
            "template_id": self.template.id,
            "name": "Field Element",
            "element_type": "field",
            "zone": "body",
            "field_name": "partner_id.name",
            "field_default": "[Customer]",
        })
        data = elem.to_editor_dict()
        self.assertEqual(data["type"], "field")
        self.assertEqual(data["field_name"], "partner_id.name")
        self.assertEqual(data["field_default"], "[Customer]")

    def test_duplicate_template(self):
        """Test template duplication."""
        self.Element.create({
            "template_id": self.template.id,
            "name": "Element 1",
            "element_type": "text",
            "zone": "body",
        })
        action = self.template.action_duplicate_template()
        new_id = action["res_id"]
        new_template = self.Template.browse(new_id)
        self.assertTrue(new_template.exists())
        self.assertIn("(Copy)", new_template.name)
        self.assertFalse(new_template.is_default)
