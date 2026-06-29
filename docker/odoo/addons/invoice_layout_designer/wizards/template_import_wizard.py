import json
import re
import base64
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.invoice_layout_designer.models.document_layout_template import (
    ILD_LAYOUT_DATA_MARKER,
)

_logger = logging.getLogger(__name__)


class TemplateImportWizard(models.TransientModel):
    _name = "document.layout.import.wizard"
    _description = "Import Layout Template"

    json_file = fields.Binary(string="Template File (JSON or QWeb XML)", required=True)
    json_filename = fields.Char(string="Filename")
    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
        required=True,
    )

    def _extract_layout_json(self, raw_bytes):
        """Liefert das Layout-Dict aus einer hochgeladenen Datei.

        Akzeptiert zwei Formate:
        - reines JSON (klassischer Export ``export_template_json``)
        - QWeb-XML-Export, der die Serialisierung als base64-Kommentar
          (Marker ``ILD-LAYOUT-DATA:``) eingebettet trägt.
        """
        if not raw_bytes:
            _logger.info("Import: leere Datei hochgeladen.")
            raise UserError(_("Please upload a JSON or QWeb XML file."))

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            _logger.info("Import: Datei nicht UTF-8-dekodierbar: %s", e)
            raise ValidationError(_("Invalid file: %s") % str(e))

        stripped = text.lstrip()
        is_xml = stripped.startswith("<?xml") or stripped.startswith("<odoo")
        if is_xml:
            _logger.info("Import: QWeb-XML erkannt, suche eingebetteten Layout-Block.")
            pattern = re.escape(ILD_LAYOUT_DATA_MARKER) + r"\s*([A-Za-z0-9+/=]+)\s*-->"
            match = re.search(pattern, text)
            if not match:
                _logger.info("Import: QWeb-XML ohne Layout-Block — Re-Import nicht möglich.")
                raise ValidationError(_(
                    "This QWeb XML does not contain embedded layout data. "
                    "Please re-export the template with the current module version."
                ))
            try:
                decoded = base64.b64decode(match.group(1)).decode("utf-8")
                data = json.loads(decoded)
            except (ValueError, UnicodeDecodeError) as e:
                _logger.info("Import: Layout-Block defekt: %s", e)
                raise ValidationError(_("Embedded layout data is corrupted: %s") % str(e))
            _logger.info("Import: Layout-Block aus QWeb-XML erfolgreich gelesen.")
            return data

        _logger.info("Import: Datei als reines JSON behandelt.")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            _logger.info("Import: JSON ungültig: %s", e)
            raise ValidationError(_("Invalid JSON file: %s") % str(e))

    def action_import(self):
        """Import a template from a JSON or QWeb XML file."""
        self.ensure_one()
        if not self.json_file:
            raise UserError(_("Please upload a JSON or QWeb XML file."))

        raw = base64.b64decode(self.json_file)
        data = self._extract_layout_json(raw)

        # Validate required fields
        if "name" not in data or "doc_type" not in data:
            raise ValidationError(_("JSON must contain 'name' and 'doc_type' fields."))

        valid_types = [
            "account.move", "sale.order", "stock.picking", "purchase.order"
        ]
        if data["doc_type"] not in valid_types:
            raise ValidationError(
                _("Invalid document type: %s. Must be one of: %s")
                % (data["doc_type"], ", ".join(valid_types))
            )

        # Create template
        Template = self.env["document.layout.template"]
        margins = data.get("margins", {})

        # Import liefert seine Elemente selbst mit — Auto-Seeding über
        # layout_style überspringen, sonst mischen sich Default-Elemente
        # unter die importierten.
        template = Template.with_context(ild_skip_auto_seed=True).create({
            "name": data["name"],
            "doc_type": data["doc_type"],
            "layout_style": data.get("layout_style", "din5008"),
            "paper_format": data.get("paper_format", "A4"),
            "margin_top": margins.get("top", 15),
            "margin_bottom": margins.get("bottom", 15),
            "margin_left": margins.get("left", 15),
            "margin_right": margins.get("right", 15),
            "header_height": data.get("header_height", 35),
            "footer_height": data.get("footer_height", 25),
            "layout_json": json.dumps(data.get("layout", {})),
            "company_id": self.company_id.id,
        })

        # Create elements
        Element = self.env["document.layout.element"]
        for elem_data in data.get("elements", []):
            vals = {
                "template_id": template.id,
                "name": elem_data.get("name", "Element"),
                "element_type": elem_data.get("element_type", "text"),
                "zone": elem_data.get("zone", "body"),
                "sequence": elem_data.get("sequence", 10),
                "pos_x": elem_data.get("pos_x", 0),
                "pos_y": elem_data.get("pos_y", 0),
                "width": elem_data.get("width", 50),
                "height": elem_data.get("height", 10),
                "rotation": elem_data.get("rotation", 0),
                "visible": elem_data.get("visible", True),
                "locked": elem_data.get("locked", False),
                # Text
                "text_content": elem_data.get("text_content", ""),
                "text_align": elem_data.get("text_align", "left"),
                # Field
                "field_model": elem_data.get("field_model", ""),
                "field_name": elem_data.get("field_name", ""),
                "field_format": elem_data.get("field_format", ""),
                "field_default": elem_data.get("field_default", ""),
                # Image
                "image_source": elem_data.get("image_source", "upload"),
                "image_fit": elem_data.get("image_fit", "contain"),
                "image_field_name": elem_data.get("image_field_name", ""),
                # Table
                "table_columns_json": json.dumps(elem_data.get("table_columns", [])),
                "table_show_header": elem_data.get("table_show_header", True),
                "table_show_totals": elem_data.get("table_show_totals", True),
                "table_zebra": elem_data.get("table_zebra", False),
                "table_border_style": elem_data.get("table_border_style", "horizontal"),
                "table_optional_mode": elem_data.get("table_optional_mode", "separate"),
                "table_optional_label": elem_data.get("table_optional_label", "Optional Items"),
                "table_optional_show_qty": elem_data.get("table_optional_show_qty", True),
                "table_rows_per_page": elem_data.get("table_rows_per_page", 25),
                "table_repeat_header": elem_data.get("table_repeat_header", True),
                "table_show_carryover": elem_data.get("table_show_carryover", True),
                # Line
                "line_color": elem_data.get("line_color", "#000000"),
                "line_width": elem_data.get("line_width", 1.0),
                "line_style": elem_data.get("line_style", "solid"),
                # Container
                "container_layout": elem_data.get("container_layout", "free"),
                "container_padding": elem_data.get("container_padding", 2),
                "container_bg_color": elem_data.get("container_bg_color", "transparent"),
                "container_border": elem_data.get("container_border", "none"),
                "container_border_radius": elem_data.get("container_border_radius", 0),
                "container_shadow": elem_data.get("container_shadow", False),
                "container_opacity": elem_data.get("container_opacity", 1.0),
                # Barcode
                "barcode_type": elem_data.get("barcode_type", "code128"),
                "barcode_field": elem_data.get("barcode_field", ""),
                "barcode_static_value": elem_data.get("barcode_static_value", ""),
                # Conditional visibility
                "condition_field": elem_data.get("condition_field", ""),
                "condition_operator": elem_data.get("condition_operator", "set"),
                # Style/Config
                "style_json": json.dumps(elem_data.get("style_json", {})),
                "config_json": json.dumps(elem_data.get("config_json", {})),
            }
            Element.create(vals)

        return {
            "type": "ir.actions.act_window",
            "res_model": "document.layout.template",
            "res_id": template.id,
            "view_mode": "form",
        }
