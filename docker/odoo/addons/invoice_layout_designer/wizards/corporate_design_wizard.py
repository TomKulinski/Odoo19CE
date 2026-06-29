import base64
import io
import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Layout blueprints per document type
# Each blueprint defines which elements to generate, their positions, and configuration.
# Positions are in mm relative to zone (header/body/footer).

DOC_LABELS = {
    "account.move": {"title": "INVOICE", "title_refund": "CREDIT NOTE", "date_field": "invoice_date", "due_field": "invoice_date_due", "ref_field": "ref", "number_field": "name", "notes_field": "narration"},
    "sale.order": {"title": "QUOTATION", "title_confirmed": "SALES ORDER", "date_field": "date_order", "due_field": "validity_date", "ref_field": "client_order_ref", "number_field": "name", "notes_field": "note"},
    "stock.picking": {"title": "DELIVERY SLIP", "date_field": "scheduled_date", "due_field": "", "ref_field": "origin", "number_field": "name", "notes_field": "note"},
    "purchase.order": {"title": "PURCHASE ORDER", "date_field": "date_order", "due_field": "date_planned", "ref_field": "partner_ref", "number_field": "name", "notes_field": "notes"},
}

TABLE_COLUMNS = {
    "account.move": [
        {"field": "name", "label": "Description", "width": "38%", "align": "left"},
        {"field": "quantity", "label": "Qty", "width": "8%", "align": "right"},
        {"field": "product_uom_id.name", "label": "Unit", "width": "8%", "align": "center"},
        {"field": "price_unit", "label": "Unit Price", "width": "14%", "align": "right", "type": "monetary"},
        {"field": "discount", "label": "Disc%", "width": "8%", "align": "right"},
        {"field": "tax_ids", "label": "Tax", "width": "10%", "align": "right"},
        {"field": "price_subtotal", "label": "Subtotal", "width": "14%", "align": "right", "type": "monetary"},
    ],
    "sale.order": [
        {"field": "name", "label": "Description", "width": "38%", "align": "left"},
        {"field": "product_uom_qty", "label": "Qty", "width": "8%", "align": "right"},
        {"field": "product_uom.name", "label": "Unit", "width": "8%", "align": "center"},
        {"field": "price_unit", "label": "Unit Price", "width": "14%", "align": "right", "type": "monetary"},
        {"field": "discount", "label": "Disc%", "width": "8%", "align": "right"},
        {"field": "tax_id", "label": "Tax", "width": "10%", "align": "right"},
        {"field": "price_subtotal", "label": "Subtotal", "width": "14%", "align": "right", "type": "monetary"},
    ],
    "stock.picking": [
        {"field": "product_id.default_code", "label": "SKU", "width": "15%", "align": "left"},
        {"field": "description_picking", "label": "Description", "width": "35%", "align": "left"},
        {"field": "product_uom_qty", "label": "Demand", "width": "12%", "align": "right"},
        {"field": "quantity", "label": "Done", "width": "12%", "align": "right"},
        {"field": "product_uom.name", "label": "Unit", "width": "10%", "align": "center"},
        {"field": "location_dest_id.name", "label": "Destination", "width": "16%", "align": "left"},
    ],
    "purchase.order": [
        {"field": "name", "label": "Description", "width": "38%", "align": "left"},
        {"field": "product_qty", "label": "Qty", "width": "10%", "align": "right"},
        {"field": "product_uom.name", "label": "Unit", "width": "8%", "align": "center"},
        {"field": "price_unit", "label": "Unit Price", "width": "14%", "align": "right", "type": "monetary"},
        {"field": "taxes_id", "label": "Tax", "width": "12%", "align": "right"},
        {"field": "price_subtotal", "label": "Subtotal", "width": "18%", "align": "right", "type": "monetary"},
    ],
}


class CorporateDesignWizard(models.TransientModel):
    _name = "document.layout.corporate.wizard"
    _description = "Corporate Design Setup Wizard"

    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
        required=True,
    )
    logo = fields.Binary(string="Company Logo", related="company_id.logo", readonly=False)

    # Step 2: Colors
    primary_color = fields.Char(string="Primary Color", default="#2c3e50")
    secondary_color = fields.Char(string="Secondary Color", default="#7f8c8d")
    accent_color = fields.Char(string="Accent Color", default="#3498db")

    # Step 3: Typography
    heading_font = fields.Selection([
        ("Helvetica", "Helvetica (Modern)"),
        ("Arial", "Arial (Clean)"),
        ("Times New Roman", "Times New Roman (Classic)"),
        ("Georgia", "Georgia (Elegant)"),
        ("Verdana", "Verdana (Readable)"),
        ("Courier New", "Courier New (Technical)"),
    ], default="Helvetica", string="Heading Font")

    body_font = fields.Selection([
        ("Helvetica", "Helvetica"),
        ("Arial", "Arial"),
        ("Times New Roman", "Times New Roman"),
        ("Georgia", "Georgia"),
        ("Verdana", "Verdana"),
    ], default="Helvetica", string="Body Font")

    body_font_size = fields.Float(string="Body Font Size (pt)", default=9.5)

    # Step 4: Layout Style
    layout_style = fields.Selection([
        ("modern", "Modern (Logo left, clean lines)"),
        ("classic", "Classic (Centered header, serif)"),
        ("compact", "Compact (Minimal margins, max content)"),
        ("bold", "Bold (Large title, strong colors)"),
        ("din5008", "DIN 5008 (German business letter standard)"),
    ], default="modern", string="Layout Style")

    # Step 5: Generate for
    gen_invoices = fields.Boolean(string="Invoices & Credit Notes", default=True)
    gen_quotations = fields.Boolean(string="Quotations & Sales Orders", default=True)
    gen_delivery = fields.Boolean(string="Delivery Slips", default=True)
    gen_purchase = fields.Boolean(string="Purchase Orders", default=True)

    # Options
    include_payment_info = fields.Boolean(string="Show Payment Info in Footer", default=True)
    include_vat = fields.Boolean(string="Show VAT Numbers", default=True)
    include_page_numbers = fields.Boolean(string="Show Page Numbers", default=True)

    def action_extract_colors(self):
        """Extract dominant colors from the uploaded logo using Pillow."""
        self.ensure_one()
        if not self.logo:
            raise UserError(_("Please upload a logo first."))

        try:
            from PIL import Image
            from collections import Counter

            img_data = base64.b64decode(self.logo)
            img = Image.open(io.BytesIO(img_data)).convert("RGB").resize((80, 80))

            pixels = list(img.getdata())
            color_counts = Counter(pixels)

            # Filter out near-white and near-black
            filtered = [
                (count, color) for color, count in color_counts.most_common(30)
                if 80 < sum(color) < 680
            ]

            if len(filtered) >= 3:
                self.primary_color = "#{:02x}{:02x}{:02x}".format(*filtered[0][1])
                self.secondary_color = "#{:02x}{:02x}{:02x}".format(*filtered[1][1])
                self.accent_color = "#{:02x}{:02x}{:02x}".format(*filtered[2][1])
            elif len(filtered) >= 1:
                self.primary_color = "#{:02x}{:02x}{:02x}".format(*filtered[0][1])

            return {
                "type": "ir.actions.act_window",
                "res_model": self._name,
                "res_id": self.id,
                "view_mode": "form",
                "target": "new",
            }
        except ImportError:
            raise UserError(_("Pillow library required. Install: pip install Pillow"))
        except Exception as e:
            _logger.warning("Color extraction failed: %s", e)
            raise UserError(_("Could not extract colors from logo."))

    def action_generate(self):
        """THE MAIN ACTION: Generate complete layouts for all selected document types."""
        self.ensure_one()

        # 1. Create/update style preset
        style = self._create_style()

        # 2. Determine which doc types to generate
        doc_types = []
        if self.gen_invoices:
            doc_types.append("account.move")
        if self.gen_quotations:
            doc_types.append("sale.order")
        if self.gen_delivery:
            doc_types.append("stock.picking")
        if self.gen_purchase:
            doc_types.append("purchase.order")

        if not doc_types:
            raise UserError(_("Please select at least one document type."))

        # 3. Generate a complete template for each type
        Template = self.env["document.layout.template"]
        created_templates = Template
        for doc_type in doc_types:
            tpl = self._generate_template(doc_type, style)
            created_templates |= tpl

        # 4. Return to the template list
        return {
            "type": "ir.actions.act_window",
            "name": _("Generated Templates"),
            "res_model": "document.layout.template",
            "view_mode": "kanban,list,form",
            "domain": [("id", "in", created_templates.ids)],
            "context": {"search_default_filter_default": 1},
        }

    def _create_style(self):
        """Create or update the corporate style preset."""
        Style = self.env["document.layout.style"]
        style = Style.search([
            ("company_id", "=", self.company_id.id),
            ("name", "=", "Corporate Design"),
        ], limit=1)

        # Differentiate fonts per layout style
        body_font = self.body_font
        heading_font = self.heading_font
        font_size = self.body_font_size

        if self.layout_style == "classic":
            body_font = "Georgia, 'Times New Roman', serif"
            heading_font = "Georgia, 'Times New Roman', serif"
        elif self.layout_style == "compact":
            font_size = max(8, self.body_font_size - 1)
        elif self.layout_style == "bold":
            font_size = self.body_font_size + 1

        vals = {
            "name": "Corporate Design",
            "company_id": self.company_id.id,
            "font_family": body_font,
            "font_size": font_size,
            "color": "#333333",
            "primary_color": self.primary_color,
            "secondary_color": self.secondary_color,
            "accent_color": self.accent_color,
            "heading_font": heading_font,
            "body_font": body_font,
        }

        if style:
            style.write(vals)
        else:
            style = Style.create(vals)
        return style

    def _generate_template(self, doc_type, style):
        """Generate a complete template with all elements for one document type."""
        Template = self.env["document.layout.template"]
        Element = self.env["document.layout.element"]
        info = DOC_LABELS[doc_type]

        # Delete previous auto-generated template for this type if exists
        old = Template.search([
            ("company_id", "=", self.company_id.id),
            ("doc_type", "=", doc_type),
            ("description", "ilike", "Auto-generated by Corporate Design"),
        ])
        old.unlink()

        # Der Wizard baut seine Elemente gleich selbst auf — das Auto-Seeding
        # über layout_style muss deshalb übersprungen werden, sonst entstehen
        # doppelte Elemente. layout_style wird trotzdem gesetzt, damit ein
        # späterer "Reset to Default" den passenden Stil trifft.
        wizard_style_to_layout_style = {
            "modern": "modern",
            "classic": "classic",
            "compact": "minimalist",
            "bold": "bold",
            "din5008": "din5008",
        }
        layout_style = wizard_style_to_layout_style.get(self.layout_style)
        if not layout_style:
            _logger.warning(
                "Corporate Design: kein layout_style-Mapping für '%s', nutze din5008.",
                self.layout_style,
            )
            layout_style = "din5008"
        else:
            _logger.info(
                "Corporate Design: Wizard-Stil '%s' → layout_style '%s'.",
                self.layout_style, layout_style,
            )

        # Create template
        tpl = Template.with_context(ild_skip_auto_seed=True).create({
            "name": f"{info['title'].title()} - {self.company_id.name}",
            "layout_style": layout_style,
            "doc_type": doc_type,
            "paper_format": "A4",
            "margin_top": 10 if self.layout_style == "din5008" else (12 if self.layout_style == "compact" else 15),
            "margin_bottom": 10 if self.layout_style == "din5008" else (12 if self.layout_style == "compact" else 15),
            "margin_left": 25 if self.layout_style == "din5008" else 15,
            "margin_right": 10 if self.layout_style == "din5008" else 15,
            "header_height": 25 if self.layout_style == "din5008" else (38 if self.layout_style == "bold" else 32),
            "footer_height": 22,
            "style_id": style.id,
            "company_id": self.company_id.id,
            "is_default": True,
            "description": "Auto-generated by Corporate Design Wizard",
        })

        # === HEADER ELEMENTS ===
        seq = 10
        cw = 180  # content width (210 - 15 - 15)
        ls = self.layout_style

        # === CLASSIC: Centered header, logo on top center, title below ===
        if ls == "classic":
            # Logo centered
            Element.create({
                "template_id": tpl.id, "name": "Company Logo",
                "element_type": "image", "zone": "header", "sequence": seq,
                "pos_x": 65, "pos_y": 0, "width": 50, "height": 20,
                "image_source": "company_logo", "image_fit": "contain",
            })
            seq += 10
            # Double line separator
            Element.create({
                "template_id": tpl.id, "name": "Header Line",
                "element_type": "line", "zone": "header", "sequence": seq,
                "pos_x": 0, "pos_y": 22, "width": cw, "height": 0.5,
                "line_color": self.primary_color, "line_width": 2.0, "line_style": "solid",
            })
            seq += 10
            Element.create({
                "template_id": tpl.id, "name": "Header Line 2",
                "element_type": "line", "zone": "header", "sequence": seq,
                "pos_x": 0, "pos_y": 23.5, "width": cw, "height": 0.5,
                "line_color": self.primary_color, "line_width": 0.5, "line_style": "solid",
            })
            seq += 10
            # Title centered
            Element.create({
                "template_id": tpl.id, "name": "Document Title",
                "element_type": "text", "zone": "header", "sequence": seq,
                "pos_x": 0, "pos_y": 26, "width": cw, "height": 8,
                "text_content": f'<strong style="font-size: 18pt; color: {self.primary_color}; font-family: Georgia, serif; letter-spacing: 1px;">{info["title"].upper()}</strong>',
                "text_align": "center",
            })
            seq += 10

        # === BOLD: Big logo, colored background bar, huge title ===
        elif ls == "bold":
            # Colored background bar
            Element.create({
                "template_id": tpl.id, "name": "Header Background",
                "element_type": "container", "zone": "header", "sequence": seq,
                "pos_x": -15, "pos_y": -12, "width": 210, "height": 42,
                "container_bg_color": self.primary_color, "container_border": "none",
                "container_layout": "free", "container_opacity": 0.08,
            })
            seq += 10
            # Big logo
            Element.create({
                "template_id": tpl.id, "name": "Company Logo",
                "element_type": "image", "zone": "header", "sequence": seq,
                "pos_x": 0, "pos_y": 0, "width": 55, "height": 22,
                "image_source": "company_logo", "image_fit": "contain",
            })
            seq += 10
            # Huge title
            Element.create({
                "template_id": tpl.id, "name": "Document Title",
                "element_type": "text", "zone": "header", "sequence": seq,
                "pos_x": 60, "pos_y": 0, "width": 120, "height": 14,
                "text_content": f'<strong style="font-size: 28pt; color: {self.primary_color};">{info["title"]}</strong>',
                "text_align": "right",
            })
            seq += 10
            # Thick separator
            Element.create({
                "template_id": tpl.id, "name": "Header Line",
                "element_type": "line", "zone": "header", "sequence": seq,
                "pos_x": 0, "pos_y": 30, "width": cw, "height": 0.5,
                "line_color": self.accent_color, "line_width": 3.0, "line_style": "solid",
            })
            seq += 10

        # === COMPACT: Small logo, tight spacing, no separator ===
        elif ls == "compact":
            # Small logo
            Element.create({
                "template_id": tpl.id, "name": "Company Logo",
                "element_type": "image", "zone": "header", "sequence": seq,
                "pos_x": 0, "pos_y": 0, "width": 30, "height": 12,
                "image_source": "company_logo", "image_fit": "contain",
            })
            seq += 10
            # Small title + number on same line
            Element.create({
                "template_id": tpl.id, "name": "Document Title",
                "element_type": "text", "zone": "header", "sequence": seq,
                "pos_x": 100, "pos_y": 0, "width": 80, "height": 6,
                "text_content": f'<span style="font-size: 12pt; color: {self.primary_color}; font-weight: bold;">{info["title"]}</span>',
                "text_align": "right",
            })
            seq += 10

        # === DIN 5008: German business letter standard ===
        elif ls == "din5008":
            # DIN 5008 measurements (relative to content area, margins are 20mm left, 10mm right)
            # Fold marks at 105mm and 210mm from top
            # Address window: 45mm from left, 33.87mm from top, 85mm wide, 45mm high
            # Info block: right side, starting at 125mm from left

            # Company Logo (top right, DIN 5008 Zusatzinformationen)
            Element.create({
                "template_id": tpl.id, "name": "Company Logo",
                "element_type": "image", "zone": "header", "sequence": seq,
                "pos_x": 120, "pos_y": 0, "width": 60, "height": 20,
                "image_source": "company_logo", "image_fit": "contain",
            })
            seq += 10

            # Rücksendezeile (small company address above address window)
            company = self.company_id
            rueckzeile = f"{company.name} · {company.street or ''} · {company.zip or ''} {company.city or ''}"
            Element.create({
                "template_id": tpl.id, "name": "Rücksendezeile",
                "element_type": "text", "zone": "body", "sequence": seq,
                "pos_x": 25, "pos_y": 0, "width": 85, "height": 4,
                "text_content": f'<span style="font-size: 6pt; color: #666; text-decoration: underline;">{rueckzeile}</span>',
            })
            seq += 10

            # Anschriftfeld (Address window - DIN 5008: 45mm from left, 85mm wide)
            Element.create({
                "template_id": tpl.id, "name": "Customer Name",
                "element_type": "field", "zone": "body", "sequence": seq,
                "pos_x": 25, "pos_y": 5, "width": 85, "height": 5,
                "field_name": "partner_id.name",
                "field_default": "[Customer Name]",
                "style_json": json.dumps({"font-weight": "bold"}),
            })
            seq += 10
            Element.create({
                "template_id": tpl.id, "name": "Customer Street",
                "element_type": "field", "zone": "body", "sequence": seq,
                "pos_x": 25, "pos_y": 10, "width": 85, "height": 4,
                "field_name": "partner_id.street",
                "field_default": "[Street]",
            })
            seq += 10
            Element.create({
                "template_id": tpl.id, "name": "Customer City",
                "element_type": "field", "zone": "body", "sequence": seq,
                "pos_x": 25, "pos_y": 14, "width": 85, "height": 4,
                "field_name": "partner_id.city",
                "field_default": "[ZIP City]",
            })
            seq += 10

            # Informationsblock (right side - DIN 5008)
            Element.create({
                "template_id": tpl.id, "name": "Document Title",
                "element_type": "text", "zone": "body", "sequence": seq,
                "pos_x": 120, "pos_y": 22, "width": 60, "height": 8,
                "text_content": f'<strong style="font-size: 14pt; color: {self.primary_color};">{info["title"]}</strong>',
                "text_align": "left",
            })
            seq += 10

            # Bezugszeichenzeile (DIN 5008 reference line)
            Element.create({
                "template_id": tpl.id, "name": "Bezugszeichenzeile",
                "element_type": "container", "zone": "body", "sequence": seq,
                "pos_x": 0, "pos_y": 35, "width": cw, "height": 12,
                "container_layout": "columns_2", "container_padding": 1,
                "container_bg_color": "#f8f8f8", "container_border": "0.5pt solid #ddd",
            })
            seq += 10

            # Fold marks (Falzmarken)
            Element.create({
                "template_id": tpl.id, "name": "Falzmarke oben",
                "element_type": "line", "zone": "body", "sequence": seq,
                "pos_x": -15, "pos_y": 72, "width": 5, "height": 0.5,
                "line_color": "#ccc", "line_width": 0.5, "line_style": "solid",
            })
            seq += 10
            Element.create({
                "template_id": tpl.id, "name": "Falzmarke unten",
                "element_type": "line", "zone": "body", "sequence": seq,
                "pos_x": -15, "pos_y": 177, "width": 5, "height": 0.5,
                "line_color": "#ccc", "line_width": 0.5, "line_style": "solid",
            })
            seq += 10

        # === MODERN (default): Logo left, title right, clean line ===
        else:
            Element.create({
                "template_id": tpl.id, "name": "Company Logo",
                "element_type": "image", "zone": "header", "sequence": seq,
                "pos_x": 0, "pos_y": 0, "width": 45, "height": 18,
                "image_source": "company_logo", "image_fit": "contain",
            })
            seq += 10
            Element.create({
                "template_id": tpl.id, "name": "Document Title",
                "element_type": "text", "zone": "header", "sequence": seq,
                "pos_x": 100, "pos_y": 0, "width": 80, "height": 10,
                "text_content": f'<strong style="font-size: 16pt; color: {self.primary_color}; font-family: {self.heading_font};">{info["title"]}</strong>',
                "text_align": "right",
            })
            seq += 10

        # === COMMON ELEMENTS (all styles) ===

        # Document Number
        Element.create({
            "template_id": tpl.id, "name": "Document Number",
            "element_type": "field", "zone": "header", "sequence": seq,
            "pos_x": 100, "pos_y": 11, "width": 80, "height": 6,
            "field_name": info["number_field"],
            "field_default": f'[{info["title"]} Number]',
            "text_align": "right",
            "style_json": json.dumps({"font-size": "11pt", "font-weight": "bold", "color": self.primary_color}),
        })
        seq += 10

        # Date
        Element.create({
            "template_id": tpl.id, "name": "Document Date",
            "element_type": "field", "zone": "header", "sequence": seq,
            "pos_x": 100, "pos_y": 18, "width": 80, "height": 5,
            "field_name": info["date_field"],
            "field_default": "[Date]",
            "text_align": "right",
            "style_json": json.dumps({"font-size": "9pt", "color": self.secondary_color}),
        })
        seq += 10

        # Due Date / Validity (if applicable)
        if info.get("due_field"):
            Element.create({
                "template_id": tpl.id, "name": "Due/Valid Date",
                "element_type": "field", "zone": "header", "sequence": seq,
                "pos_x": 100, "pos_y": 23, "width": 80, "height": 5,
                "field_name": info["due_field"],
                "field_default": "[Due Date]",
                "text_align": "right",
                "style_json": json.dumps({"font-size": "9pt", "color": self.secondary_color}),
            })
            seq += 10

        # Reference
        if info.get("ref_field"):
            Element.create({
                "template_id": tpl.id, "name": "Reference",
                "element_type": "field", "zone": "header", "sequence": seq,
                "pos_x": 100, "pos_y": 28, "width": 80, "height": 5,
                "field_name": info["ref_field"],
                "field_default": "",
                "text_align": "right",
                "style_json": json.dumps({"font-size": "8pt", "color": "#999999"}),
            })
            seq += 10

        # === BODY ELEMENTS ===
        seq = 100
        y = 0

        # --- Customer Address Block ---
        Element.create({
            "template_id": tpl.id, "name": "Customer Name",
            "element_type": "field", "zone": "body", "sequence": seq,
            "pos_x": 0, "pos_y": y, "width": 85, "height": 6,
            "field_name": "partner_id.name",
            "field_default": "[Customer Name]",
            "style_json": json.dumps({"font-size": "11pt", "font-weight": "bold"}),
        })
        y += 6
        seq += 10

        Element.create({
            "template_id": tpl.id, "name": "Customer Street",
            "element_type": "field", "zone": "body", "sequence": seq,
            "pos_x": 0, "pos_y": y, "width": 85, "height": 5,
            "field_name": "partner_id.street",
            "field_default": "[Street]",
            "style_json": json.dumps({"font-size": f"{self.body_font_size}pt"}),
        })
        y += 4.5
        seq += 10

        Element.create({
            "template_id": tpl.id, "name": "Customer Street 2",
            "element_type": "field", "zone": "body", "sequence": seq,
            "pos_x": 0, "pos_y": y, "width": 85, "height": 5,
            "field_name": "partner_id.street2", "field_default": "",
            "style_json": json.dumps({"font-size": f"{self.body_font_size}pt"}),
        })
        y += 4.5
        seq += 10

        Element.create({
            "template_id": tpl.id, "name": "Customer ZIP + City",
            "element_type": "field", "zone": "body", "sequence": seq,
            "pos_x": 0, "pos_y": y, "width": 85, "height": 5,
            "field_name": "partner_id.city",
            "field_default": "[ZIP City]",
            "style_json": json.dumps({"font-size": f"{self.body_font_size}pt"}),
        })
        y += 4.5
        seq += 10

        Element.create({
            "template_id": tpl.id, "name": "Customer Country",
            "element_type": "field", "zone": "body", "sequence": seq,
            "pos_x": 0, "pos_y": y, "width": 85, "height": 5,
            "field_name": "partner_id.country_id.name", "field_default": "",
            "style_json": json.dumps({"font-size": f"{self.body_font_size}pt"}),
        })
        y += 5
        seq += 10

        # Customer VAT
        if self.include_vat:
            Element.create({
                "template_id": tpl.id, "name": "Customer VAT",
                "element_type": "field", "zone": "body", "sequence": seq,
                "pos_x": 0, "pos_y": y, "width": 85, "height": 5,
                "field_name": "partner_id.vat", "field_default": "",
                "style_json": json.dumps({"font-size": "8pt", "color": self.secondary_color}),
            })
            y += 5
            seq += 10

        # --- Company Info Block (right side, below header) ---
        # Use dynamic placeholders so the block updates when company data changes
        company_html = (
            f'[street]<br/>'
            f'[zip] [city]<br/>'
            f'Tel: [phone]<br/>'
            f'[email]<br/>'
            f'[website]'
        )
        Element.create({
            "template_id": tpl.id, "name": "Company Info Block",
            "element_type": "text", "zone": "body", "sequence": seq,
            "pos_x": 115, "pos_y": 0, "width": 65, "height": 28,
            "text_content": f'<div style="font-size: 8pt; color: {self.secondary_color}; line-height: 1.5;">{company_html}</div>',
            "text_align": "right",
        })
        y = max(y, 30)
        seq += 10

        # --- Accent Line ---
        Element.create({
            "template_id": tpl.id, "name": "Accent Line",
            "element_type": "line", "zone": "body", "sequence": seq,
            "pos_x": 0, "pos_y": y, "width": cw, "height": 0.5,
            "line_color": self.accent_color,
            "line_width": 2.0 if self.layout_style == "bold" else 1.0,
            "line_style": "solid",
        })
        y += 4
        seq += 10

        # --- Line Items Table ---
        table_cols = TABLE_COLUMNS.get(doc_type, TABLE_COLUMNS["account.move"])
        has_totals = doc_type in ("account.move", "sale.order", "purchase.order")

        Element.create({
            "template_id": tpl.id, "name": "Line Items Table",
            "element_type": "table", "zone": "body", "sequence": seq,
            "pos_x": 0, "pos_y": y, "width": cw, "height": 100,
            "table_columns_json": json.dumps(table_cols),
            "table_show_header": True,
            "table_show_totals": has_totals,
            "table_zebra": self.layout_style in ("modern", "compact"),
            "table_border_style": "horizontal",
            "style_json": json.dumps({"font-size": f"{self.body_font_size}pt"}),
        })
        y += 105
        seq += 10

        # --- Notes / Terms ---
        if info.get("notes_field"):
            Element.create({
                "template_id": tpl.id, "name": "Notes / Terms",
                "element_type": "field", "zone": "body", "sequence": seq,
                "pos_x": 0, "pos_y": y, "width": cw, "height": 20,
                "field_name": info["notes_field"], "field_default": "",
                "style_json": json.dumps({"font-size": "8pt", "color": "#666666"}),
            })
            seq += 10

        # === FOOTER ELEMENTS ===
        seq = 200

        # Footer divider line
        Element.create({
            "template_id": tpl.id, "name": "Footer Line",
            "element_type": "line", "zone": "footer", "sequence": seq,
            "pos_x": 0, "pos_y": 0, "width": cw, "height": 0.5,
            "line_color": self.secondary_color,
            "line_width": 0.5,
            "line_style": "solid",
        })
        seq += 10

        # Footer: Company name + address (left) — dynamic placeholders
        Element.create({
            "template_id": tpl.id, "name": "Footer Company",
            "element_type": "text", "zone": "footer", "sequence": seq,
            "pos_x": 0, "pos_y": 3, "width": 120, "height": 5,
            "text_content": f'<span style="font-size: 7pt; color: {self.secondary_color};"><strong>[company_name]</strong> | [street] | [zip] [city]</span>',
        })
        seq += 10

        # Footer: Contact (center) — dynamic placeholders
        Element.create({
            "template_id": tpl.id, "name": "Footer Contact",
            "element_type": "text", "zone": "footer", "sequence": seq,
            "pos_x": 0, "pos_y": 8, "width": 120, "height": 5,
            "text_content": f'<span style="font-size: 7pt; color: {self.secondary_color};">Tel: [phone] | [email] | [website]</span>',
        })
        seq += 10

        # Footer: VAT (if applicable) — dynamic placeholder
        if self.include_vat:
            Element.create({
                "template_id": tpl.id, "name": "Footer VAT",
                "element_type": "text", "zone": "footer", "sequence": seq,
                "pos_x": 0, "pos_y": 13, "width": 120, "height": 5,
                "text_content": f'<span style="font-size: 7pt; color: {self.secondary_color};">VAT: [vat]</span>',
            })
            seq += 10

        # Footer: Payment info (for invoices) — dynamic placeholders
        if self.include_payment_info and doc_type in ("account.move", "sale.order"):
            Element.create({
                "template_id": tpl.id, "name": "Footer Bank",
                "element_type": "text", "zone": "footer", "sequence": seq,
                "pos_x": 0, "pos_y": 17, "width": cw, "height": 5,
                "text_content": f'<span style="font-size: 7pt; color: {self.secondary_color};">Bank: [bank_name] | IBAN: [iban] | BIC: [bic]</span>',
            })
            seq += 10

        # Page number (right side of footer)
        if self.include_page_numbers:
            Element.create({
                "template_id": tpl.id, "name": "Page Number",
                "element_type": "text", "zone": "footer", "sequence": seq,
                "pos_x": 140, "pos_y": 3, "width": 40, "height": 5,
                "text_content": f'<span style="font-size: 7pt; color: {self.secondary_color};">Page <span class="page"/> / <span class="topage"/></span>',
                "text_align": "right",
            })

        _logger.info(
            "Generated layout template '%s' for %s with %d elements",
            tpl.name, doc_type, len(tpl.element_ids),
        )

        return tpl
