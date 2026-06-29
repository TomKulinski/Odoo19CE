import json
import base64
import logging

from odoo import http, _
from odoo.http import request, content_disposition
from odoo.exceptions import AccessError, UserError
from odoo.addons.invoice_layout_designer.models import field_registry

_logger = logging.getLogger(__name__)


class LayoutEditorController(http.Controller):

    @http.route(
        "/layout/editor/load",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def load_template(self, template_id, **kwargs):
        """Load a template for editing in the WYSIWYG editor."""
        template = request.env["document.layout.template"].browse(template_id)
        if not template.exists():
            return {"error": "Template not found"}

        elements = []
        for elem in template.element_ids:
            elements.append(elem.to_editor_dict())

        # Get available fields for this doc type
        available_fields = field_registry.get_available_fields(template.doc_type)

        # Merge custom fields into the picker
        try:
            CustomField = request.env["custom.field.definition"]
            custom_fields = CustomField.get_fields_for_layout(
                field_registry.doc_type_to_model(template.doc_type))
            if custom_fields:
                available_fields = dict(available_fields)  # copy
                available_fields["custom"] = custom_fields
        except Exception:
            pass  # custom fields model may not exist yet during install

        return {
            "template": {
                "id": template.id,
                "name": template.name,
                "doc_type": template.doc_type,
                "paper_format": template.paper_format,
                "paper_width": template.paper_width,
                "paper_height": template.paper_height,
                "margin_top": template.margin_top,
                "margin_bottom": template.margin_bottom,
                "margin_left": template.margin_left,
                "margin_right": template.margin_right,
                "header_height": template.header_height,
                "footer_height": template.footer_height,
                "header_repeat_each_page": template.header_repeat_each_page,
                "layout_json": template.get_layout_data(),
                "background_mode": template.background_mode or "none",
                "has_background": bool(template.background_image),
            },
            "elements": elements,
            "available_fields": available_fields,
            "style": template.style_id.to_css_dict() if template.style_id else {},
        }

    @http.route(
        "/layout/editor/save",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def save_template(self, template_id, template_data, elements_data, **kwargs):
        """Save the complete template from the editor."""
        template = request.env["document.layout.template"].browse(template_id)
        if not template.exists():
            return {"error": "Template not found"}

        # Update template settings
        tpl_vals = {
            "margin_top": template_data.get("margin_top", template.margin_top),
            "margin_bottom": template_data.get("margin_bottom", template.margin_bottom),
            "margin_left": template_data.get("margin_left", template.margin_left),
            "margin_right": template_data.get("margin_right", template.margin_right),
            "header_height": template_data.get("header_height", template.header_height),
            "footer_height": template_data.get("footer_height", template.footer_height),
            "layout_json": json.dumps(template_data.get("layout", {})),
        }
        if "background_mode" in template_data:
            tpl_vals["background_mode"] = template_data["background_mode"]
        if "header_repeat_each_page" in template_data:
            tpl_vals["header_repeat_each_page"] = bool(template_data["header_repeat_each_page"])
        template.write(tpl_vals)

        # Sync elements: delete removed, update existing, create new
        existing_ids = set(template.element_ids.ids)
        incoming_ids = set()

        Element = request.env["document.layout.element"]

        for elem_data in elements_data:
            elem_id = elem_data.get("id")
            vals = self._element_data_to_vals(elem_data, template_id)

            if elem_id and elem_id in existing_ids:
                # Update existing element
                elem = Element.browse(elem_id)
                elem.write(vals)
                incoming_ids.add(elem_id)
            else:
                # Create new element
                vals["template_id"] = template_id
                new_elem = Element.create(vals)
                incoming_ids.add(new_elem.id)

        # Delete removed elements
        to_delete = existing_ids - incoming_ids
        if to_delete:
            Element.browse(list(to_delete)).unlink()

        return {
            "success": True,
            "template_id": template.id,
            "coverage": template.get_coverage_hint(),
        }

    def _element_data_to_vals(self, data, template_id):
        """Convert frontend element data to Odoo field values."""
        vals = {
            "name": data.get("name", "Element"),
            "element_type": data.get("type", "text"),
            "zone": data.get("zone", "body"),
            "sequence": data.get("sequence", 10),
            "pos_x": data.get("pos_x", 0),
            "pos_y": data.get("pos_y", 0),
            "width": data.get("width", 50),
            "height": data.get("height", 10),
            "rotation": data.get("rotation", 0),
            "visible": data.get("visible", True),
            "locked": data.get("locked", False),
            # Text
            "text_content": data.get("text_content", ""),
            "text_align": data.get("text_align", "left"),
            # Field
            "field_model": data.get("field_model", ""),
            "field_name": data.get("field_name", ""),
            "field_format": data.get("field_format", ""),
            "field_default": data.get("field_default", ""),
            # Image
            "image_source": data.get("image_source", "upload"),
            "image_fit": data.get("image_fit", "contain"),
            "image_field_name": data.get("image_field_name", ""),
            # Table
            "table_columns_json": json.dumps(data.get("table_columns", [])),
            "table_show_header": data.get("table_show_header", True),
            "table_show_totals": data.get("table_show_totals", True),
            "table_row_spacing": data.get("table_row_spacing", 0.0),
            "table_zebra": data.get("table_zebra", False),
            "table_border_style": data.get("table_border_style", "horizontal"),
            "table_optional_mode": data.get("table_optional_mode", "separate"),
            "table_optional_label": data.get("table_optional_label", "Optional Items"),
            "table_optional_show_qty": data.get("table_optional_show_qty", True),
            "table_rows_per_page": data.get("table_rows_per_page", 25),
            "table_repeat_header": data.get("table_repeat_header", True),
            "table_show_carryover": data.get("table_show_carryover", True),
            # Sections/Notes/Subtotals
            "table_show_sections": data.get("table_show_sections", True),
            "table_show_notes": data.get("table_show_notes", True),
            "table_show_subtotals": data.get("table_show_subtotals", False),
            "table_section_style": data.get("table_section_style", ""),
            "table_note_style": data.get("table_note_style", ""),
            "table_subtotal_style": data.get("table_subtotal_style", ""),
            "table_subtotal_label": data.get("table_subtotal_label", "Zwischensumme"),
            "table_subtotal_align": data.get("table_subtotal_align", "right"),
            "table_subtotal_show_line": data.get("table_subtotal_show_line", True),
            "totals_show_lines": data.get("totals_show_lines", True),
            # Totals labels
            "totals_label_subtotal": data.get("totals_label_subtotal", "Subtotal:"),
            "totals_label_tax": data.get("totals_label_tax", "Tax:"),
            "totals_label_total": data.get("totals_label_total", "Total:"),
            "totals_style": data.get("totals_style", "default"),
            "totals_offset_x": data.get("totals_offset_x", 0.0),
            "totals_offset_y": data.get("totals_offset_y", 0.0),
            "totals_row_spacing": data.get("totals_row_spacing", 0.0),
            # Line
            "line_color": data.get("line_color", "#000000"),
            "line_width": data.get("line_width", 1.0),
            "line_style": data.get("line_style", "solid"),
            # Shape (structured styling; defaults = status quo / legacy path)
            "shape_use_structured": data.get("shape_use_structured", False),
            "shape_border_style": data.get("shape_border_style", "none"),
            "shape_border_width": data.get("shape_border_width", 1.0),
            "shape_border_color": data.get("shape_border_color", "#000000"),
            "radius_uniform": data.get("radius_uniform", True),
            "radius_tl": data.get("radius_tl", 0.0),
            "radius_tr": data.get("radius_tr", 0.0),
            "radius_br": data.get("radius_br", 0.0),
            "radius_bl": data.get("radius_bl", 0.0),
            "shape_fill_color": data.get("shape_fill_color", ""),
            "shape_opacity": data.get("shape_opacity", 1.0),
            # Container
            "container_layout": data.get("container_layout", "free"),
            "container_padding": data.get("container_padding", 2),
            "container_bg_color": data.get("container_bg_color", "transparent"),
            "container_border": data.get("container_border", "none"),
            "container_border_radius": data.get("container_border_radius", 0),
            "container_shadow": data.get("container_shadow", False),
            "container_opacity": data.get("container_opacity", 1.0),
            # Barcode
            "barcode_type": data.get("barcode_type", "code128"),
            "barcode_field": data.get("barcode_field", ""),
            "barcode_static_value": data.get("barcode_static_value", ""),
            # Conditional visibility
            "condition_field": data.get("condition_field", ""),
            "condition_operator": data.get("condition_operator", "set"),
            "condition_value": data.get("condition_value", ""),
            # Per-page visibility — default "first" so new elements use position:absolute
            # (not position:fixed) and render relative to .ild-page (inside page margins)
            "show_on_page": data.get("show_on_page", "first"),
            # Anchoring: "fixed" (feste Koordinaten) oder "after_table"
            # (fließt im Body unter die Tabelle). Default = altes Verhalten.
            "anchor_mode": data.get("anchor_mode", "fixed"),
            "anchor_gap": data.get("anchor_gap", 3.0),
            "flow_group_key": data.get("flow_group_key", ""),
            "fixed_to": data.get("fixed_to", "none"),
            # JSON fields
            "style_json": json.dumps(data.get("style", {})),
            "config_json": json.dumps(data.get("config", {})),
            # Editor-save ⇒ User-Anpassung. Schützt vor "Reset to Default".
            "is_user_created": True,
        }

        # Handle image upload
        if data.get("image_data_b64"):
            vals["image_data"] = data["image_data_b64"]

        return vals

    @http.route(
        "/layout/editor/preview",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def preview_template(self, template_id, record_id=None, **kwargs):
        """Generate a live preview of the template with real rendered data."""
        template = request.env["document.layout.template"].browse(template_id)
        if not template.exists():
            return {"error": "Template not found"}

        # Get a sample record if none specified
        if record_id:
            record = request.env[field_registry.doc_type_to_model(template.doc_type)].browse(record_id)
        else:
            record = template._get_sample_record()

        if not record:
            return {"error": f"No sample {template.doc_type} record found."}

        # Transpile to QWeb template
        from odoo.addons.invoice_layout_designer.models.report_override import transpile_template
        # ild_preview unterdrückt die separate "Optional Items"-Tabelle: sie hängt
        # am Sample-Record und käme im echten Druck oft nicht → sonst zweite,
        # verwirrende Tabelle im Editor-Preview. Preview == typischer Druck.
        qweb_body = transpile_template(template.with_context(ild_preview=True), record)

        # Wrap in a minimal template for rendering
        import hashlib
        tpl_key = f"invoice_layout_designer.preview_{template.id}"
        full_qweb = f'<t t-name="{tpl_key}"><t t-foreach="docs" t-as="doc">{qweb_body}</t></t>'

        # Register/update the view
        IrView = request.env["ir.ui.view"].sudo()
        existing = IrView.search([("key", "=", tpl_key), ("type", "=", "qweb")], limit=1)
        if existing:
            existing.write({"arch": full_qweb})
        else:
            IrView.create({"name": f"ILD Preview: {template.name}", "type": "qweb", "key": tpl_key, "arch": full_qweb})

        # Render the QWeb against the real record
        try:
            rendered_html = request.env["ir.qweb"]._render(tpl_key, {
                "docs": record,
                "doc": record,
                "company": record.company_id or request.env.company,
                "env": request.env,
            })
            return {
                "html": str(rendered_html),
                "record_id": record.id,
                "record_name": record.display_name,
            }
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Preview render failed: %s", e)
            # Fallback: return raw QWeb (unrendered) so preview at least shows layout
            return {
                "html": qweb_body,
                "record_id": record.id,
                "record_name": record.display_name,
                "warning": "Preview used fallback (unrendered). Error: %s" % str(e),
            }

    @http.route(
        "/layout/editor/preview_pdf",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def preview_pdf(self, template_id, record_id=None, **kwargs):
        """Echte PDF-Vorschau: rendert GENAU dieses Template über die normale
        Report-Pipeline (_render_qweb_pdf). Mehrseitig + exakte Umbrüche +
        Keep-Together = Preview == PDF. Liefert das PDF base64-kodiert."""
        template = request.env["document.layout.template"].browse(template_id)
        if not template.exists():
            return {"error": "Template not found"}

        if record_id:
            record = request.env[field_registry.doc_type_to_model(template.doc_type)].browse(record_id)
        else:
            record = template._get_sample_record()
        if not record:
            return {"error": f"No sample {template.doc_type} record found."}

        # Report-Action je doc_type (analog action_preview_pdf), damit
        # _find_custom_template das richtige report.model bekommt.
        report_ref_map = {
            "account.move": "invoice_layout_designer.action_report_custom_document",
            "purchase.order": "purchase.action_report_purchase_order",
            "sale.order": "sale.action_report_saleorder",
            "auftragsbestaetigung": "sale.action_report_saleorder",
            "stock.picking": "stock.action_report_delivery",
        }
        report_ref = report_ref_map.get(
            template.doc_type,
            "invoice_layout_designer.action_report_custom_document",
        )
        try:
            import base64
            report = request.env.ref(report_ref)
            # ild_force_template_id erzwingt GENAU dieses Template (nicht das
            # is_default-Template des doc_type).
            pdf_content, _ftype = report.with_context(
                ild_force_template_id=template.id
            )._render_qweb_pdf(report_ref, [record.id])
            return {
                "pdf_b64": base64.b64encode(pdf_content).decode("ascii"),
                "record_id": record.id,
                "record_name": record.display_name,
            }
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Preview-PDF render failed: %s", e)
            return {"error": "Preview-PDF fehlgeschlagen: %s" % str(e)}

    @http.route(
        "/layout/editor/fields",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def get_fields(self, doc_type, **kwargs):
        """Get available fields for a document type."""
        registry = field_registry
        return {
            "fields": registry.get_available_fields(doc_type),
            "line_fields": registry.get_available_fields(doc_type).get("line_fields", []),
        }

    @http.route(
        "/layout/editor/upload_image",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def upload_image(self, element_id, image_data, filename="image.png", **kwargs):
        """Upload an image for a layout element."""
        elem = request.env["document.layout.element"].browse(element_id)
        if not elem.exists():
            return {"error": "Element not found"}

        elem.write({
            "image_data": image_data,
            "image_source": "upload",
        })

        return {"success": True}

    @http.route(
        "/layout/editor/upload_background",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def upload_background(self, template_id, image_data, mode="image", **kwargs):
        """Upload a background image / letterhead for the template."""
        template = request.env["document.layout.template"].browse(template_id)
        if not template.exists():
            return {"error": "Template not found"}

        template.write({
            "background_image": image_data,
            "background_mode": mode,
        })
        return {"success": True}

    @http.route(
        "/layout/editor/browse_fields",
        type="jsonrpc",
        auth="user",
        methods=["POST"],
    )
    def browse_fields(self, model_name, doc_type=None, **kwargs):
        """
        Dynamic Field Browser: Return ALL fields for a given model.
        Groups fields by type (char, number, date, relation, etc.)
        and builds the correct field path relative to the document type.
        """
        try:
            Model = request.env[model_name]
        except KeyError:
            return {"error": f"Model '{model_name}' not found"}

        # Build field path prefix based on how this model relates to the doc
        prefix = ""
        if doc_type and model_name != doc_type:
            # Find the relation path from doc to this model
            prefix_map = {
                "res.partner": "partner_id.",
                "res.company": "company_id.",
                "product.template": "product_id.product_tmpl_id.",
                "product.product": "product_id.",
                "account.payment.term": "invoice_payment_term_id.",
                "res.currency": "currency_id.",
                "res.users": "user_id.",
                "account.fiscal.position": "fiscal_position_id.",
            }
            prefix = prefix_map.get(model_name, "")

        fields_desc = Model.fields_get(
            attributes=["string", "type", "relation", "help", "readonly"]
        )

        # Group fields by type
        groups = {
            "text": {"label": "Text Fields", "fields": []},
            "number": {"label": "Numbers", "fields": []},
            "date": {"label": "Dates", "fields": []},
            "boolean": {"label": "Checkboxes", "fields": []},
            "relation": {"label": "Relations", "fields": []},
            "other": {"label": "Other", "fields": []},
        }

        # Field types to skip (internal/technical)
        skip_fields = {
            "id", "__last_update", "create_uid", "create_date",
            "write_uid", "write_date", "display_name",
            "message_ids", "message_follower_ids", "message_partner_ids",
            "activity_ids", "activity_state", "activity_user_id",
            "activity_type_id", "activity_date_deadline", "activity_summary",
            "activity_exception_decoration", "activity_exception_icon",
            "website_message_ids", "message_attachment_count",
            "message_has_error", "message_has_error_counter",
            "message_has_sms_error", "message_is_follower",
            "message_main_attachment_id", "message_needaction",
            "message_needaction_counter", "message_unread",
            "message_unread_counter", "access_url", "access_token",
            "access_warning",
        }

        type_map = {
            "char": "text", "text": "text", "html": "text",
            "integer": "number", "float": "number", "monetary": "number",
            "date": "date", "datetime": "date",
            "boolean": "boolean",
            "many2one": "relation", "one2many": "relation", "many2many": "relation",
            "selection": "text",
        }

        for fname, finfo in sorted(fields_desc.items(), key=lambda x: x[1].get("string", "")):
            if fname in skip_fields or fname.startswith("_"):
                continue

            ftype = finfo.get("type", "")
            group_key = type_map.get(ftype, "other")

            # Skip binary/reference fields (not useful on reports)
            if ftype in ("binary", "reference", "properties"):
                continue

            field_entry = {
                "path": f"{prefix}{fname}",
                "label": finfo.get("string", fname),
                "type": ftype,
                "technical": fname,
            }

            # For many2one, add .name suffix for display
            if ftype == "many2one":
                field_entry["path"] = f"{prefix}{fname}.name"
                field_entry["label"] = f"{finfo.get('string', fname)}"
                field_entry["relation"] = finfo.get("relation", "")

            groups[group_key]["fields"].append(field_entry)

        # Remove empty groups
        result = {k: v for k, v in groups.items() if v["fields"]}

        # Also get available browsable models
        browsable_models = [
            {"model": "res.partner", "label": "Kontakte / Partner"},
            {"model": "res.company", "label": "Unternehmen"},
            {"model": "account.move", "label": "Rechnungen"},
            {"model": "account.move.line", "label": "Rechnungspositionen"},
            {"model": "sale.order", "label": "Angebote / Aufträge"},
            {"model": "sale.order.line", "label": "Auftragspositionen"},
            {"model": "purchase.order", "label": "Bestellungen"},
            {"model": "purchase.order.line", "label": "Bestellpositionen"},
            {"model": "stock.picking", "label": "Lieferscheine"},
            {"model": "stock.move", "label": "Lagerbewegungen"},
            {"model": "product.template", "label": "Produkte"},
            {"model": "product.product", "label": "Produktvarianten"},
            {"model": "res.currency", "label": "Währungen"},
            {"model": "account.payment.term", "label": "Zahlungsbedingungen"},
        ]

        return {
            "fields": result,
            "model": model_name,
            "browsable_models": browsable_models,
        }

    @http.route(
        "/layout/template/export/<int:template_id>",
        type="http",
        auth="user",
    )
    def export_template(self, template_id, **kwargs):
        """Export template as JSON file for download."""
        template = request.env["document.layout.template"].browse(template_id)
        if not template.exists():
            return request.not_found()

        json_data = template.export_template_json()
        filename = f"{template.name.replace(' ', '_')}_layout.json"

        return request.make_response(
            json_data,
            headers=[
                ("Content-Type", "application/json"),
                ("Content-Disposition", content_disposition(filename)),
            ],
        )