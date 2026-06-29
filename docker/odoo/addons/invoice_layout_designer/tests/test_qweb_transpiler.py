from odoo.tests.common import TransactionCase
from odoo.addons.invoice_layout_designer.models.report_override import transpile_template


class TestQwebTranspiler(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Template = self.env["document.layout.template"]
        self.Element = self.env["document.layout.element"]

        # Auto-Seeding überspringen: jeder Transpiler-Test rendert genau das
        # eine selbst angelegte Element. Ohne Skip würde der Style-Seeder das
        # Template mit DIN5008-Defaults füllen und die isolierten Assertions
        # (besonders die "darf-nicht-enthalten"-Prüfungen) verfälschen.
        self.template = self.Template.with_context(
            ild_skip_auto_seed=True
        ).create({
            "name": "Test Template",
            "doc_type": "account.move",
            "paper_format": "A4",
            "header_height": 35,
            "footer_height": 25,
        })

    def test_transpile_empty_template(self):
        html = transpile_template(self.template)
        self.assertIn("ild-page", html)
        self.assertIn("<style>", html)

    def test_transpile_text_element(self):
        self.Element.create({
            "template_id": self.template.id, "name": "Title",
            "element_type": "text", "zone": "header",
            "text_content": "INVOICE",
            "pos_x": 10, "pos_y": 5, "width": 80, "height": 10,
        })
        html = transpile_template(self.template)
        self.assertIn("INVOICE", html)
        # Header-Zonen-Elemente werden als absolut positioniertes
        # .ild-element innerhalb der .ild-page gerendert (kein eigener
        # ild-header-Wrapper mehr).
        self.assertIn("ild-element", html)

    def test_transpile_field_element(self):
        self.Element.create({
            "template_id": self.template.id, "name": "Customer",
            "element_type": "field", "zone": "body",
            "field_name": "partner_id.name", "field_default": "[Customer]",
            "pos_x": 0, "pos_y": 0, "width": 60, "height": 8,
        })
        html = transpile_template(self.template)
        self.assertIn("doc.partner_id.name", html)
        self.assertIn("t-esc", html)

    def test_transpile_table_element(self):
        self.Element.create({
            "template_id": self.template.id, "name": "Lines",
            "element_type": "table", "zone": "body",
            "pos_x": 0, "pos_y": 30, "width": 180, "height": 80,
            "table_columns_json": '[{"field": "name", "label": "Description", "align": "left"}]',
            "table_show_header": True, "table_show_totals": True,
        })
        html = transpile_template(self.template)
        self.assertIn("ild-table", html)
        self.assertIn("t-foreach", html)
        self.assertIn("invoice_line_ids", html)

    def test_transpile_hidden_elements(self):
        self.Element.create({
            "template_id": self.template.id, "name": "Hidden",
            "element_type": "text", "zone": "body",
            "text_content": "SECRET", "visible": False,
            "pos_x": 0, "pos_y": 0, "width": 50, "height": 10,
        })
        html = transpile_template(self.template)
        self.assertNotIn("SECRET", html)

    def test_transpile_line_element(self):
        self.Element.create({
            "template_id": self.template.id, "name": "Sep",
            "element_type": "line", "zone": "body",
            "pos_x": 0, "pos_y": 20, "width": 180, "height": 1,
            "line_color": "#2c3e50", "line_width": 1.5,
        })
        html = transpile_template(self.template)
        self.assertIn("border-bottom", html)
        self.assertIn("#2c3e50", html)

    def test_transpile_image_logo(self):
        self.Element.create({
            "template_id": self.template.id, "name": "Logo",
            "element_type": "image", "zone": "header",
            "image_source": "company_logo",
            "pos_x": 0, "pos_y": 0, "width": 40, "height": 20,
        })
        html = transpile_template(self.template)
        self.assertIn("company_id.logo", html)

    # ================================================================
    # NEW TESTS: PDF Fixes
    # ================================================================

    def test_transpile_logo_has_tif_guard(self):
        """Fix #4: Company logo must be wrapped in t-if to avoid blank image errors."""
        self.Element.create({
            "template_id": self.template.id, "name": "Logo",
            "element_type": "image", "zone": "header",
            "image_source": "company_logo",
            "pos_x": 0, "pos_y": 0, "width": 40, "height": 20,
        })
        html = transpile_template(self.template)
        self.assertIn('t-if="doc.company_id.logo"', html)

    def test_transpile_return_address_line(self):
        """Fix #1: Rücksendezeile should show Firma · Straße · PLZ Ort."""
        self.Element.create({
            "template_id": self.template.id, "name": "Rücksendezeile",
            "element_type": "field", "zone": "body",
            "field_name": "company_id.name",
            "pos_x": 0, "pos_y": 0, "width": 85, "height": 5,
            "style_json": '{"text-decoration": "underline", "font-size": "7pt"}',
        })
        html = transpile_template(self.template)
        # Must contain company street and zip/city with · separator
        self.assertIn("doc.company_id.street", html)
        self.assertIn("doc.company_id.zip", html)
        self.assertIn("doc.company_id.city", html)
        self.assertIn(" · ", html)

    def test_transpile_return_address_by_name(self):
        """Fix #1: Rücksendezeile detected by element name 'return'."""
        self.Element.create({
            "template_id": self.template.id, "name": "Return Address",
            "element_type": "field", "zone": "body",
            "field_name": "company_id.name",
            "pos_x": 0, "pos_y": 0, "width": 85, "height": 5,
        })
        html = transpile_template(self.template)
        self.assertIn("doc.company_id.street", html)

    def test_transpile_partner_zip_includes_city(self):
        """Fix #2: partner_id.zip field must render ZIP + City combined."""
        self.Element.create({
            "template_id": self.template.id, "name": "PLZ Ort",
            "element_type": "field", "zone": "body",
            "field_name": "partner_id.zip",
            "pos_x": 0, "pos_y": 10, "width": 80, "height": 5,
        })
        html = transpile_template(self.template)
        self.assertIn("doc.partner_id.zip", html)
        self.assertIn("doc.partner_id.city", html)

    def test_transpile_placeholder_guards(self):
        """Fix #5: Placeholders like [phone] must be wrapped in t-if guards."""
        self.Element.create({
            "template_id": self.template.id, "name": "Footer",
            "element_type": "text", "zone": "footer",
            "text_content": "Tel: [phone] | [email]",
            "pos_x": 0, "pos_y": 0, "width": 120, "height": 5,
        })
        html = transpile_template(self.template)
        # Phone should be guarded (label-aware: whole "Tel: [phone]" in t-if)
        self.assertIn('t-if="doc.company_id.phone"', html)
        # Email should be guarded
        self.assertIn('t-if="doc.company_id.email"', html)
        # Should NOT contain raw "[phone]" or "[email]"
        self.assertNotIn("[phone]", html)
        self.assertNotIn("[email]", html)

    def test_transpile_table_monetary_widget(self):
        """Fix #7: Table monetary cells must use Odoo's monetary widget."""
        self.Element.create({
            "template_id": self.template.id, "name": "Lines",
            "element_type": "table", "zone": "body",
            "pos_x": 0, "pos_y": 30, "width": 180, "height": 80,
            "table_columns_json": '[{"field": "price_subtotal", "label": "Subtotal", "align": "right", "type": "monetary"}]',
            "table_show_header": True, "table_show_totals": True,
        })
        html = transpile_template(self.template)
        self.assertIn('"widget": "monetary"', html)
        self.assertIn("display_currency", html)
        # Should NOT use raw %.2f formatting
        self.assertNotIn("%.2f", html)

    def test_transpile_footer_uses_fixed_band(self):
        """Footer wird als eigenes position:fixed-Band gerendert.

        Architektur-Update (siehe LESSONS 2026-06-01): mehrseitige Belege
        brauchen einen pro Seite wiederholten Footer. Das löst ein
        `ild-footer-band`-Element ausserhalb der `.ild-page`, das per
        `position: fixed` im reservierten Bottom-Margin sitzt.
        """
        self.Element.create({
            "template_id": self.template.id, "name": "Footer Text",
            "element_type": "text", "zone": "footer",
            "text_content": "Company Footer",
            "pos_x": 0, "pos_y": 0, "width": 120, "height": 5,
        })
        html = transpile_template(self.template)
        self.assertIn("ild-footer-band", html)
        self.assertIn("position: fixed", html)
        self.assertIn("Company Footer", html)

    def test_transpile_table_empty_columns_uses_defaults(self):
        """Fix #3: Table with empty columns_json should use doc-type-specific defaults."""
        self.Element.create({
            "template_id": self.template.id, "name": "Lines",
            "element_type": "table", "zone": "body",
            "pos_x": 0, "pos_y": 30, "width": 180, "height": 80,
            "table_columns_json": "[]",
            "table_show_header": True,
        })
        html = transpile_template(self.template)
        # Should have real column headers, not empty
        self.assertIn("Description", html)
        self.assertIn("Qty", html)

    def test_transpile_date_field_uses_widget(self):
        """Bonus: Date fields should use Odoo's date widget for locale formatting."""
        self.Element.create({
            "template_id": self.template.id, "name": "Date",
            "element_type": "field", "zone": "body",
            "field_name": "invoice_date",
            "pos_x": 0, "pos_y": 0, "width": 40, "height": 6,
        })
        html = transpile_template(self.template)
        self.assertIn('"widget": "date"', html)
