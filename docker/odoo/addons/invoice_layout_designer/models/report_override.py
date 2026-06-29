import io
import json
import logging
import os
import re
import tempfile

from odoo import api, fields, models, _
from . import field_registry

_logger = logging.getLogger(__name__)

try:
    import weasyprint
    HAS_WEASYPRINT = True
except (ImportError, OSError):
    HAS_WEASYPRINT = False
    _logger.info("WeasyPrint not available — using wkhtmltopdf as PDF engine.")


# ---------------------------------------------------------------------------
# Helper: replace inline base64 data-URIs with temp file:// references
# WeasyPrint chokes on very large data:image/...;base64 URIs embedded in HTML.
# Writing them to temporary files and using file:// URLs is fully reliable.
# ---------------------------------------------------------------------------
_DATA_URI_RE = re.compile(
    r'(src=["\'])data:(image/[a-zA-Z+]+);base64,([A-Za-z0-9+/=\s]+?)(["\'])',
    re.DOTALL,
)

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


def _replace_data_uris_with_files(html, temp_dir):
    """Replace every data:image/…;base64,… src with a file:// temp path.

    Returns (new_html, list_of_temp_file_paths).
    """
    import base64 as b64mod

    paths = []

    def _repl(m):
        prefix, mime, b64_data, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
        # Strip whitespace that might have been introduced by QWeb rendering
        b64_clean = b64_data.replace("\n", "").replace("\r", "").replace(" ", "")
        ext = _MIME_EXT.get(mime, ".bin")
        try:
            raw = b64mod.b64decode(b64_clean)
        except Exception:
            _logger.warning("ILD: could not decode base64 image (%s, %d chars)", mime, len(b64_clean))
            return m.group(0)  # leave unchanged
        fd, fpath = tempfile.mkstemp(suffix=ext, dir=temp_dir)
        os.write(fd, raw)
        os.close(fd)
        paths.append(fpath)
        file_url = "file://" + fpath
        return f'{prefix}{file_url}{suffix}'

    new_html = _DATA_URI_RE.sub(_repl, html)
    return new_html, paths


class IrActionsReport(models.Model):
    _inherit = "ir.actions.report"

    def _render_qweb_pdf(self, report_ref, res_ids=None, data=None):
        """Override to inject custom layout when available."""
        report = self._get_report(report_ref)
        _logger.warning(
            "ILD DEBUG: _render_qweb_pdf called report_ref=%s report=%s res_ids=%s",
            report_ref, report and report.report_name, res_ids,
        )

        if report and res_ids:
            is_preview = report.report_name == "invoice_layout_designer.report_custom_document"

            # Batch sale.order prints can mix quotations and confirmed orders,
            # which resolve to different templates. Split and merge so each
            # record renders with its correct layout.
            if not is_preview and report.model == "sale.order" and len(res_ids) > 1:
                groups = self._split_sale_orders_by_state(res_ids)
                if len(groups) > 1:
                    try:
                        return self._render_pdf_groups(report_ref, groups, data)
                    except Exception:
                        _logger.warning("Grouped sale.order render failed, falling back", exc_info=True)

            custom_template = self._find_custom_template(report, res_ids)
            if custom_template:
                try:
                    return self._render_custom_layout_pdf(
                        report, custom_template, res_ids, data
                    )
                except Exception:
                    _logger.warning("Custom layout render failed, falling back", exc_info=True)
            if is_preview:
                return super()._render_qweb_pdf(report_ref, res_ids=res_ids, data=data)

        return super()._render_qweb_pdf(report_ref, res_ids=res_ids, data=data)

    def _split_sale_orders_by_state(self, res_ids):
        """Group sale.order ids by state group (quotation vs confirmed).

        Returns a list of id-lists. Preserves original order within each group.
        """
        orders = self.env["sale.order"].browse(res_ids)
        by_state = {}
        for o in orders:
            key = "confirmed" if o.state in ("sale", "done") else "quotation"
            by_state.setdefault(key, []).append(o.id)
        return list(by_state.values())

    def _render_pdf_groups(self, report_ref, groups, data):
        """Render each id-group separately, then merge PDFs into one."""
        from odoo.tools.pdf import OdooPdfFileReader, OdooPdfFileWriter

        writer = OdooPdfFileWriter()
        for group_ids in groups:
            pdf_content, _ftype = self._render_qweb_pdf(report_ref, res_ids=group_ids, data=data)
            reader = OdooPdfFileReader(io.BytesIO(pdf_content), strict=False)
            writer.appendPagesFromReader(reader)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue(), "pdf"

    def _find_custom_template(self, report, res_ids):
        """Find a custom layout template for the given report."""
        model_name = report.model

        # Vorschau erzwingt ein bestimmtes Template (action_preview_pdf). So
        # zeigt die Vorschau genau das geöffnete Template statt des Default-
        # Templates des doc_type.
        forced_id = self.env.context.get("ild_force_template_id")
        if forced_id:
            forced = self.env["document.layout.template"].sudo().browse(
                forced_id
            ).exists()
            if forced:
                _logger.info(
                    "ILD: Erzwungenes Vorschau-Template id=%s ('%s', Stil %s).",
                    forced.id, forced.name, forced.layout_style,
                )
                return forced
            _logger.warning(
                "ILD: ild_force_template_id=%s existiert nicht — normale "
                "Template-Suche.", forced_id,
            )
        # sudo(): Die Template-Auswahl ist Konfiguration, nicht benutzer-
        # spezifisch. Ohne sudo würde z.B. ein Portal-Kunde, der seine Rechnung
        # herunterlädt, beim Zugriff auf document.layout.template einen
        # AccessError auslösen und kein Custom-Layout erhalten. Lesen per sudo
        # ist hier sicher, weil keine vertraulichen Daten exponiert werden.
        Template = self.env["document.layout.template"].sudo()

        # sale.order routes by state to two distinct doc_types:
        #   draft / sent -> "sale.order"           (Angebot / quotation)
        #   sale / done  -> "auftragsbestaetigung"  (Auftragsbestätigung)
        # The model stays sale.order; only the layout template differs.
        sale_state_group = None
        search_doc_type = model_name
        if model_name == "sale.order" and res_ids:
            rec = self.env["sale.order"].sudo().browse(res_ids[:1])
            if rec.exists():
                confirmed = rec.state in ("sale", "done")
                sale_state_group = "confirmed" if confirmed else "quotation"
                search_doc_type = "auftragsbestaetigung" if confirmed else "sale.order"

        # Maßgeblich für die Layout-Wahl ist die Firma des BELEGS, nicht die
        # aktive Firma des Nutzers. Sonst würde z.B. eine Rechnung der DE Company,
        # gedruckt während man in der US-Firma arbeitet, fälschlich das US-Layout
        # verwenden. Fallback auf die aktive Firma, falls der Beleg keine Firma hat.
        company = self.env.company
        if res_ids:
            doc = self.env[model_name].sudo().browse(res_ids[:1])
            if doc.exists() and doc.company_id:
                company = doc.company_id

        _logger.info(
            "ILD: Looking for template: model=%s, search_doc_type=%s, company=%s (id=%s), sale_state=%s",
            model_name, search_doc_type, company.name, company.id, sale_state_group,
        )

        def _search_default(doc_type, with_company=True, state_filters=None):
            domain = [
                ("doc_type", "=", doc_type),
                ("is_default", "=", True),
                ("active", "=", True),
            ]
            if with_company:
                domain.append(("company_id", "=", company.id))
            if state_filters is not None:
                domain.append(("sale_state_filter", "in", state_filters))
            return Template.search(domain, limit=1)

        def _find_sale_template(with_company):
            # sale.order wird nach Verkaufsstatus gewählt. Bisher ignorierte die
            # Suche sale_state_filter komplett -> Angebot UND Auftragsbestätigung
            # landeten beim selben is_default sale.order-Template, Edits am jeweils
            # anderen Template waren unsichtbar (Bug 2). Reihenfolge:
            #   confirmed: dedizierte AB-doc_type -> sale.order+confirmed -> +all -> beliebig
            #   quotation: sale.order+quotation   -> +all -> beliebig
            if sale_state_group == "confirmed":
                return (
                    _search_default("auftragsbestaetigung", with_company)
                    or _search_default("sale.order", with_company, state_filters=["confirmed"])
                    or _search_default("sale.order", with_company, state_filters=["all"])
                    or _search_default("sale.order", with_company)
                )
            return (
                _search_default("sale.order", with_company, state_filters=["quotation"])
                or _search_default("sale.order", with_company, state_filters=["all"])
                or _search_default("sale.order", with_company)
            )

        if model_name == "sale.order":
            template = _find_sale_template(True) or _find_sale_template(False)
        else:
            template = _search_default(search_doc_type)
            if not template:
                template = _search_default(search_doc_type, with_company=False)

        if template:
            _logger.info(
                "ILD: Found template: %s (id=%s, doc_type=%s, state_filter=%s, company=%s)",
                template.name, template.id, template.doc_type,
                template.sale_state_filter, template.company_id.name,
            )
        else:
            _logger.info(
                "ILD: No template found for %s (search_doc_type=%s, sale_state=%s)",
                model_name, search_doc_type, sale_state_group,
            )
            # Log all candidates for debugging
            all_tpl = Template.search([("doc_type", "in", (model_name, search_doc_type, "auftragsbestaetigung"))])
            for t in all_tpl:
                _logger.info("ILD:   available: %s (id=%s, doc_type=%s, state_filter=%s, default=%s, active=%s, company=%s/%s)",
                             t.name, t.id, t.doc_type, t.sale_state_filter, t.is_default, t.active, t.company_id.name, t.company_id.id)

        if not template:
            config_map = {
                "account.move": "default_invoice_template_id",
                "sale.order": "default_sale_template_id",
                "stock.picking": "default_picking_template_id",
                "purchase.order": "default_purchase_template_id",
            }
            config_field = config_map.get(model_name)
            if config_field:
                template_id = self.env["ir.config_parameter"].sudo().get_param(
                    f"invoice_layout_designer.{config_field}", False
                )
                if template_id:
                    template = Template.browse(int(template_id)).exists()

        return template

    def _render_custom_layout_pdf(self, report, template, res_ids, data):
        """Rendert IMMER über die positionierten Elemente (WYSIWYG).

        Alle Stile — auch die Odoo-Stile (Light/Boxed/…) — bestehen aus
        positionierten, im Editor verschiebbaren Elementen. Das gedruckte PDF
        entspricht damit exakt dem, was der Nutzer im Layout-Editor sieht und
        bearbeitet.
        """
        _logger.info(
            "ILD: Template '%s' rendert POSITIONIERT (Stil '%s').",
            template.name, template.layout_style,
        )
        return self._render_positioned_layout_pdf(report, template, res_ids, data)

    def _render_positioned_layout_pdf(self, report, template, res_ids, data):
        """Render PDF using our positioned-element layout.

        Supports two engines:
        - WeasyPrint (recommended): renders HTML→PDF with Chrome-like CSS support
        - wkhtmltopdf (fallback): standard Odoo PDF engine
        """
        # Check which PDF engine to use
        pdf_engine = self.env["ir.config_parameter"].sudo().get_param(
            "invoice_layout_designer.pdf_engine", "weasyprint"
        )

        if pdf_engine == "weasyprint" and HAS_WEASYPRINT:
            return self._render_with_weasyprint(report, template, res_ids, data)
        else:
            return self._render_with_wkhtmltopdf(report, template, res_ids, data)

    def _render_with_weasyprint(self, report, template, res_ids, data):
        """Render PDF using WeasyPrint — identical to browser preview."""
        import shutil

        records = self.env[report.model].browse(res_ids)

        pdf_pages = []
        # Shared temp directory for all image files — cleaned up at the end
        temp_dir = tempfile.mkdtemp(prefix="ild_wp_")
        try:
            for record in records:
                # Generate QWeb body and render against the record.
                # engine="weasyprint": Seitenzahl-Elemente bleiben im Body, da
                # WeasyPrint CSS counter(page)/counter(pages) unterstützt.
                qweb_body = transpile_template(template, None, engine="weasyprint")

                # Build the QWeb template key
                tpl_key = f"invoice_layout_designer.wp_render_{template.id}"
                full_qweb = f'<t t-name="{tpl_key}"><t t-foreach="docs" t-as="doc">{qweb_body}</t></t>'

                # Register/update the view
                IrView = self.env["ir.ui.view"].sudo()
                existing = IrView.search([("key", "=", tpl_key), ("type", "=", "qweb")], limit=1)
                if existing:
                    existing.write({"arch": full_qweb})
                else:
                    IrView.create({"name": f"WP Render: {template.name}", "type": "qweb", "key": tpl_key, "arch": full_qweb})

                # Render QWeb to HTML string
                rendered_html = self.env["ir.qweb"]._render(tpl_key, {
                    "docs": record,
                    "doc": record,
                    "company": record.company_id or self.env.company,
                    "env": self.env,
                })

                # Build complete HTML document with @page CSS for margins.
                # Use template.margin_* (not paperformat_id) so the @page margin,
                # transpile_template's inner .ild-page sizing, and the editor/HTML
                # preview all agree on a single content-box geometry.
                # paperformat_id values are synced into template.margin_* via onchange.
                margin_top = template.margin_top or 0
                margin_bottom = template.margin_bottom or 0
                margin_left = template.margin_left or 0
                margin_right = template.margin_right or 0
                paper_w = template.paper_width or 210
                paper_h = template.paper_height or 297
                # Footer-Band unten reservieren (deckungsgleich mit dem @page in
                # transpile_template) -> langer Body-Inhalt läuft nicht in den
                # per position:fixed unten verankerten Footer.
                footer_reserve = float(template.footer_height or 0)
                margin_bottom = margin_bottom + footer_reserve

                full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<style>
@page {{
    size: {paper_w}mm {paper_h}mm;
    margin: {margin_top}mm {margin_right}mm {margin_bottom}mm {margin_left}mm;
}}
body {{
    margin: 0;
    padding: 0;
    font-family: 'Lato', 'Helvetica Neue', Arial, sans-serif;
}}
</style>
</head>
<body>{str(rendered_html)}</body>
</html>"""

                # WeasyPrint handles data:image/...;base64 URIs natively — pass through.
                wp_doc = weasyprint.HTML(
                    string=full_html,
                    base_url=self.env["ir.config_parameter"].sudo().get_param("web.base.url", "http://localhost:8069"),
                )
                pdf_bytes = wp_doc.write_pdf()
                pdf_pages.append(pdf_bytes)
        finally:
            # Clean up temp image files
            shutil.rmtree(temp_dir, ignore_errors=True)

        # Merge multiple PDFs if needed
        if len(pdf_pages) == 1:
            final_pdf = pdf_pages[0]
        else:
            final_pdf = self._merge_pdfs(pdf_pages)

        _logger.info("ILD: WeasyPrint rendered %d page(s) for %s", len(pdf_pages), report.model)
        return final_pdf, "pdf"

    def _merge_pdfs(self, pdf_list):
        """Merge multiple PDF byte strings into one."""
        try:
            from pypdf import PdfMerger
        except ImportError:
            try:
                from PyPDF2 import PdfMerger
            except ImportError:
                # If no PDF merger available, return first page
                _logger.warning("No PDF merger library available, returning first page only")
                return pdf_list[0]

        import io
        merger = PdfMerger()
        for pdf_bytes in pdf_list:
            merger.append(io.BytesIO(pdf_bytes))
        output = io.BytesIO()
        merger.write(output)
        merger.close()
        return output.getvalue()

    def _render_with_wkhtmltopdf(self, report, template, res_ids, data):
        """Render PDF using wkhtmltopdf — standard Odoo engine."""
        # 1. Generate the QWeb HTML with t-directives
        qweb_body = transpile_template(template, None, engine="wkhtmltopdf")

        # Seitenzahl-Footer für wkhtmltopdf: Im Body unterdrückt der Transpiler
        # die Seitenzahl-Elemente (dort liefe der Counter auf "0"). Hier wird die
        # Seitenzahl als echter wkhtmltopdf-Footer ausgegeben — nur in dieser
        # Region ersetzt wkhtmltopdf die Klassen "page"/"topage" durch die Werte.
        pagenum_elem = template.element_ids.filtered(
            lambda e: e.visible and (
                "[page]" in (e.text_content or "")
                or "[pages]" in (e.text_content or "")
            )
        )
        if pagenum_elem:
            footer_div = (
                '<div class="footer">'
                f'<div style="text-align:right; font-size:7pt; color:#999; '
                f'padding:0 {template.margin_right}mm 0 0;">'
                'Seite <span class="page"></span> von <span class="topage"></span>'
                '</div></div>'
            )
        else:
            footer_div = '<div class="footer" style="display:none;"/>'

        # 2. Build a stable template key per layout template
        template_name = f"invoice_layout_designer.dynamic_layout_{template.id}"

        # 3. Wrap in web.html_container for wkhtmltopdf — Odoo 19 requires
        #    a <main> tag in the HTML structure for _prepare_html to work.
        #    We do NOT use web.external_layout because that adds Odoo's own
        #    header/footer which conflicts with our custom layout elements.
        #    IMPORTANT: Odoo's _prepare_html falls back to rendering
        #    web.external_layout_header/footer when no <div class="header">
        #    and <div class="footer"> are present in the rendered HTML. We
        #    emit EMPTY header/footer divs so wkhtmltopdf uses those (empty)
        #    chrome regions instead of Odoo's default company chrome, which
        #    would otherwise overlap the custom layout.
        full_qweb = f"""
        <t t-name="{template_name}">
            <t t-call="web.html_container">
                <t t-foreach="docs" t-as="doc">
                    <div class="header" style="display:none;"/>
                    {footer_div}
                    <div class="article o_report_layout_custom">
                        <div class="page">
                            {qweb_body}
                        </div>
                    </div>
                </t>
            </t>
        </t>
        """

        # 4. Register or update the dynamic view
        IrView = self.env["ir.ui.view"].sudo()
        existing_view = IrView.search([
            ("key", "=", template_name),
            ("type", "=", "qweb"),
        ], limit=1)

        if existing_view:
            existing_view.write({"arch": full_qweb})
        else:
            IrView.create({
                "name": f"Dynamic Layout: {template.name}",
                "type": "qweb",
                "key": template_name,
                "arch": full_qweb,
            })

        # 5. Use our configured report action directly — it has the correct
        #    paperformat (DPI 96, margins 0). We temporarily set its report_name
        #    to our dynamic template, render, then restore. This is wrapped in
        #    try/finally to be safe. The race window is tiny (< 1 second).
        ild_report = self.env.ref(
            "invoice_layout_designer.action_report_custom_document",
            raise_if_not_found=False,
        )
        if not ild_report:
            return super()._render_qweb_pdf(report_ref, res_ids=res_ids, data=data)

        original_name = ild_report.report_name
        try:
            ild_report.sudo().write({"report_name": template_name})
            result = super()._render_qweb_pdf(
                ild_report,
                res_ids=res_ids,
                data=data,
            )
        finally:
            ild_report.sudo().write({"report_name": original_name})

        return result


# ============================================================================
# TRANSPILER — standalone functions (no Odoo model needed)
# ============================================================================

def transpile_template(template, records=None, engine="wkhtmltopdf"):
    """Convert a layout template to QWeb HTML for PDF rendering."""
    elements = template.element_ids
    style = template.style_id

    base_font = style.font_family if style else "Helvetica, Arial, sans-serif"
    base_size = style.font_size if style else 10
    base_color = style.color if style else "#000000"

    pw = template.paper_width
    ph = template.paper_height
    mt = template.margin_top
    mb = template.margin_bottom
    ml = template.margin_left
    mr = template.margin_right
    fh = float(template.footer_height or 0)
    content_width = pw - ml - mr

    header_elements = elements.filtered(lambda e: e.zone == "header" and e.visible)
    body_elements = elements.filtered(lambda e: e.zone == "body" and e.visible)
    footer_elements = elements.filtered(lambda e: e.zone == "footer" and e.visible)

    # wkhtmltopdf ersetzt Seitenzahl-Platzhalter ([page]/[pages] → Klassen
    # "page"/"topage") NUR in seiner nativen Footer-Region, nicht im Body.
    # Im Body würden CSS-Counter zu "0" rendern ("Seite 0 von 0"). Deshalb
    # werden Seitenzahl-Elemente bei wkhtmltopdf hier aus dem Body entfernt und
    # stattdessen von _render_with_wkhtmltopdf als echter Footer ausgegeben.
    def _is_pagenum(e):
        txt = e.text_content or ""
        return "[page]" in txt or "[pages]" in txt

    if engine == "wkhtmltopdf":
        header_elements = header_elements.filtered(lambda e: not _is_pagenum(e))
        body_elements = body_elements.filtered(lambda e: not _is_pagenum(e))
        footer_elements = footer_elements.filtered(lambda e: not _is_pagenum(e))

    # WeasyPrint: counter(page) in einer position:fixed-Box liefert immer die
    # Gesamtseitenzahl (WeasyPrint-Limitation). Korrekte Seitenzahlen pro Seite
    # gibt es nur in einer @page-Margin-Box. Deshalb das Seitenzahl-Element aus
    # dem fixen Footer-Band herausnehmen und seinen Text in CSS-content für die
    # Margin-Box übersetzen ([page]→counter(page), [pages]→counter(pages)).
    pagenum_margin_content = ""
    if engine == "weasyprint":
        pagenum_elems = footer_elements.filtered(_is_pagenum)
        if pagenum_elems:
            footer_elements = footer_elements.filtered(lambda e: not _is_pagenum(e))
            raw = pagenum_elems[0].text_content or ""
            plain = re.sub(r"<[^>]+>", "", raw)
            tokens = re.split(r"(\[page\]|\[pages\])", plain)
            css_parts = []
            for tok in tokens:
                if tok == "[page]":
                    css_parts.append("counter(page)")
                elif tok == "[pages]":
                    css_parts.append("counter(pages)")
                elif tok:
                    css_parts.append('"' + tok.replace('"', '\\"') + '"')
            pagenum_margin_content = " ".join(css_parts)

    # Spill-Schutz für wkhtmltopdf: Die .ild-page füllt mit min-height die
    # gesamte druckbare Höhe (ph - mt - mb). wkhtmltopdfs altes WebKit rundet
    # mm→px bei 96 DPI und überschreitet die Seite dann um Sekundenbruchteile
    # → eine zweite, fast leere Seite entsteht. Ein kleiner Sicherheitsabzug
    # hält die Box garantiert unter der Seitengrenze. Absolut positionierte
    # Footer-Elemente bleiben sichtbar, da sie über `top` platziert werden und
    # nicht von der min-height der .ild-page abhängen.
    # WeasyPrint rechnet exakt → kein Abzug, sonst bliebe unten Leerraum.
    if engine == "wkhtmltopdf":
        page_safety_mm = 2
    else:
        page_safety_mm = 0
    # Footer-Band wird unten als reservierter Seitenrand abgezogen (siehe
    # @page margin-bottom). Die .ild-page (Body-Box) endet deshalb oberhalb
    # des Footer-Bands, sonst würde eine lange Tabelle unter den Footer laufen
    # und mit ihm verschmelzen.
    page_min_height = max(0, ph - mt - mb - fh - page_safety_mm)

    # WeasyPrint: Seitenzahl in die @bottom-right-Margin-Box. counter(page) in
    # einem position:fixed-Band liefert auf JEDER Seite die Gesamtseitenzahl
    # (WeasyPrint-Limitation); nur in einer Margin-Box zählt counter(page)
    # korrekt pro Seite ("Seite 1 von 2", "Seite 2 von 2"). Der übrige Footer
    # (Firmenzeile etc.) bleibt im position:fixed-Band, das auf jeder Seite
    # wiederholt wird.
    running_footer_css = ""

    page_margin_box = ""
    if engine == "weasyprint" and pagenum_margin_content:
        page_margin_box += (
            f"@bottom-right {{ content: {pagenum_margin_content}; "
            f"font-size: 6.5pt; color: #777; vertical-align: top; "
            f"padding-top: 2mm; }}"
        )

    html_parts = []

    # CSS — fully self-contained, no dependency on Odoo stylesheets
    html_parts.append(f"""
    <style>
        @page {{
            size: {pw}mm {ph}mm;
            /* margin-bottom reserviert zusätzlich das Footer-Band ({fh}mm),
               damit fließender Body-Inhalt (lange Tabellen) nicht in den
               Footer hineinläuft. Footer selbst sitzt per position:fixed in
               diesem reservierten Band; die Seitenzahl kommt aus der Margin-Box. */
            margin: {mt}mm {mr}mm {mb + fh}mm {ml}mm;
            {page_margin_box}
        }}
        {running_footer_css}
        html, body {{
            margin: 0; padding: 0;
            font-family: {base_font};
            font-size: {base_size}pt;
            font-weight: 400;
            color: {base_color};
            -webkit-font-smoothing: antialiased;
            -webkit-print-color-adjust: exact;
        }}
        .ild-page {{
            font-family: {base_font};
            font-size: {base_size}pt;
            font-weight: 400;
            color: {base_color};
            line-height: 1.35;
            position: relative;
            min-height: {page_min_height}mm;
            width: {content_width}mm;
            box-sizing: border-box;
        }}
        .ild-element {{
            position: absolute;
            overflow: hidden;
            font-weight: 400;
            box-sizing: border-box;
        }}
        /* Page counters (WeasyPrint reads CSS counter(page)/counter(pages); wkhtmltopdf
           substitutes the span text itself — both mechanisms coexist safely). */
        .ild-pagenum::before {{ content: counter(page); }}
        .ild-pagetotal::before {{ content: counter(pages); }}
        /* Per-page visibility.
           "all"   : element fixed on every page (page numbers, watermark, logo)
           "first" : element flows naturally; appears on first page if positioned there
           "last"  : element pinned to the last page via position:fixed + @page:last hack
           "middle": element hidden on first page via @page :first override */
        .ild-page-all {{
            position: fixed !important;
        }}
        .ild-page-first {{ /* default absolute flow — shows on page where positioned */ }}
        .ild-page-last {{
            position: fixed !important;
        }}
        .ild-page-middle {{
            /* visible on all pages except first */
        }}
        @page :first {{
            /* Hide "middle" and "last" elements on first page */
        }}
        @page {{
            /* Hide "first" elements on non-first pages is hard in pure CSS;
               handled by keeping "first" in natural flow (appears once). */
        }}
        .ild-element img {{
            max-width: 100%;
            max-height: 100%;
            display: block;
        }}
        /* Odoo-DIN-5008 Positionstabelle: table-borderless, thead schwarz unterstrichen */
        .ild-table {{ width: 100%; border-collapse: collapse; font-size: 9pt; font-weight: 400; }}
        /* table-header-group: Tabellenkopf wird bei mehrseitigen Belegen auf
           JEDER Seite automatisch wiederholt (WeasyPrint + wkhtmltopdf). Damit
           entfällt das manuelle Row-Chunking samt "Continued"-Zeile, das mit
           der physischen Seitenhöhe kollidierte. */
        .ild-table thead {{ display: table-header-group; }}
        .ild-table th {{
            font-weight: 700; text-align: left;
            padding: 1.8mm 1.5mm 1.6mm 1.5mm;
            border-bottom: 0.75pt solid #000;
            font-size: 9pt; color: #000;
            text-transform: none; letter-spacing: normal;
            vertical-align: bottom;
        }}
        .ild-table td {{
            padding: 2mm 1.5mm; vertical-align: top;
            font-size: 9pt; font-weight: 400; color: #000;
        }}
        .ild-table tbody tr {{ page-break-inside: avoid; }}
        .ild-table .ild-zebra:nth-child(even) {{ background-color: #fafafa; }}
        /* Bold-Stil: kräftige Kopf-/Total-Linien wie Odoos "Bold"-Layout. */
        .ild-table-bold th {{ border-bottom: 1.8pt solid #111; text-transform: uppercase; letter-spacing: 0.3pt; }}

        /* Totals-Block: minimal, Subtotal + Total mit 1px schwarz Ober-/Unterstrich */
        /* Keep-Together (Punkt 2): Summenblock wird beim Seitenumbruch NIE
           zerlegt — passt er nicht mehr ganz auf die Seite, rückt er komplett
           auf die nächste (gilt für inline-Totals UND das eigenständige
           Totals-Element, beide tragen .ild-table-totals). */
        .ild-table-totals {{ margin-top: 4mm; text-align: right; font-size: 9pt;
            break-inside: avoid; page-break-inside: avoid; }}
        .ild-table-totals table {{ margin-left: auto; border-collapse: collapse; min-width: 65mm; }}
        .ild-table-totals td {{ padding: 1.4mm 4mm; font-weight: 400; background-color: transparent; }}
        .ild-table-totals td:first-child {{ text-align: left; padding-right: 10mm; color: #333; }}
        .ild-table-totals tr.subtotal-line td {{ border-top: 0.75pt solid #000; }}
        .ild-table-totals tr.total-line td {{
            border-top: 0.75pt solid #000;
            border-bottom: 0.75pt solid #000;
            font-weight: 700; font-size: 10.5pt;
            padding-top: 1.8mm; padding-bottom: 1.8mm;
        }}
    </style>
    """)

    # Background images / letterhead (Task 1) ─────────────────────────────────
    # Drei getrennte Bereiche: Header-Band, Footer-Band, Gesamtseite. Jeder
    # wird als position:fixed-Band emittiert, das die Render-Engine auf JEDER
    # Seite wiederholt (WeasyPrint + wkhtmltopdf) und das im Browser-Preview
    # (iframe) erscheint. WICHTIG gegen die alten Bugs:
    #   * KEIN erneutes base64.b64encode — Odoo-Binary-Felder liefern bereits
    #     base64-kodierte Daten; doppeltes Encoden zerstörte das Bild.
    #   * KEIN z-index:-1 — negatives z-index versteckte das Band hinter einem
    #     deckenden Vorfahren-Hintergrund (im Preview als "kein Bild" sichtbar).
    #   * Bänder stehen AUSSERHALB von .ild-page (Geschwister), exakt wie das
    #     Footer-Band weiter unten. Ein position:fixed-Band INNERHALB der
    #     position:relative .ild-page verankert WeasyPrint an deren Unterkante
    #     (Seite-1-Höhe) statt an der Seitenbox -> es rutscht bei mehrseitigen
    #     Belegen in die Seitenmitte. Als Geschwister bezieht sich `fixed` auf
    #     die Seitenbox und wiederholt sauber auf jeder Seite.
    # Die Bänder werden VOR .ild-page emittiert -> Content liegt im DOM später
    # und überdeckt sie (gleicher Stacking-Context, z-index:0).
    def _bg_data_uri(val):
        if not val:
            return None
        if isinstance(val, bytes):
            val = val.decode("ascii")  # already base64 text — do NOT re-encode
        head = val[:16]
        if head.startswith("/9j/"):
            mime = "image/jpeg"
        elif head.startswith("R0lGOD"):
            mime = "image/gif"
        elif head.startswith("PHN2") or head.startswith("PD94"):
            mime = "image/svg+xml"
        else:
            mime = "image/png"  # iVBOR... and default
        return f"data:{mime};base64,{val}"

    bg_fit = getattr(template, "bg_fit", "contain") or "contain"
    object_fit = {"contain": "contain", "cover": "cover", "fill": "fill"}.get(bg_fit, "contain")

    hh = float(template.header_height or 0)
    page_uri = _bg_data_uri(getattr(template, "bg_page_image", False))
    header_uri = _bg_data_uri(getattr(template, "bg_header_image", False))
    footer_uri = _bg_data_uri(getattr(template, "bg_footer_image", False))

    # Backward compatibility: wenn keines der neuen Felder gesetzt ist, das
    # alte Einzelbild gemäß background_mode abbilden (image=Gesamt, header_only=Header).
    if not (page_uri or header_uri or footer_uri):
        bg_mode = getattr(template, "background_mode", "none") or "none"
        legacy_uri = _bg_data_uri(getattr(template, "background_image", False))
        if legacy_uri and bg_mode == "image":
            page_uri = legacy_uri
        elif legacy_uri and bg_mode == "header_only":
            header_uri = legacy_uri

    # Verankerungspunkt eines position:fixed-Bands ist je Engine anders
    # (identisch zum Footer-Band weiter unten):
    #   * WeasyPrint: relativ zur Seiten-INHALTSFLÄCHE (innerhalb @page-Margins)
    #     -> Ursprung links/oben = 0/0.
    #   * wkhtmltopdf (altes WebKit): relativ zur SEITENBOX (physische Kante)
    #     -> Ursprung über margin-left/top bzw. margin-bottom.
    page_band_h = max(0, ph - mt - mb)           # gesamte druckbare Höhe
    footer_band_top = max(0, ph - mt - mb - fh)  # Oberkante des Footer-Streifens
    if engine == "weasyprint":
        page_pos = "left:0; top:0;"
        header_pos = "left:0; top:0;"
        footer_pos = f"left:0; top:{footer_band_top}mm;"
    else:
        page_pos = f"left:{ml}mm; top:{mt}mm;"
        header_pos = f"left:{ml}mm; top:{mt}mm;"
        footer_pos = f"left:{ml}mm; bottom:{mb}mm;"

    def _bg_band(uri, pos_css, height_mm):
        # Eigene Klasse statt .ild-element, damit die generische
        # ".ild-element img { max-width/height }"-Regel hier nicht greift.
        return (
            f'<div class="ild-bg-band" style="position:fixed; {pos_css} '
            f'width:{content_width}mm; height:{height_mm}mm; z-index:0; '
            f'overflow:hidden; pointer-events:none; margin:0; padding:0;">'
            f'<img src="{uri}" style="width:100%; height:100%; '
            f'object-fit:{object_fit}; display:block;"/>'
            f'</div>'
        )

    # Reihenfolge: Gesamtseite zuunterst, dann Header/Footer darüber, alle VOR Content.
    if page_uri and page_band_h > 0:
        html_parts.append(_bg_band(page_uri, page_pos, page_band_h))
    if header_uri and hh > 0:
        html_parts.append(_bg_band(header_uri, header_pos, hh))
    if footer_uri and fh > 0:
        html_parts.append(_bg_band(footer_uri, footer_pos, fh))

    html_parts.append('<div class="ild-page">')

    # Flat layout: all elements rendered directly inside .ild-page with
    # page-relative Y coordinates. Zone divs are not used as positioning
    # contexts because position:relative on flex children is unreliable in
    # wkhtmltopdf's old WebKit engine. Instead each zone's Y offset is added
    # to elem.pos_y so top: is always relative to the content-area top.
    body_height = max(0, ph - mt - mb - float(template.header_height or 0) - float(template.footer_height or 0))
    header_offset = 0.0
    body_offset = float(template.header_height or 0)

    for elem in header_elements:
        html_parts.append(_render_element(elem, template, y_offset=header_offset))

    # --- Optionaler Odoo-Standard-Belegbarcode (Code128 der Belegnummer) ---
    # Aktivierbar pro Template (show_odoo_barcode). Rechtsbündig in der freien
    # Zone zwischen Adressblock und Belegtitel platziert, analog zu Odoos
    # Standard-Belegen. Wird beim Rendern injiziert (kein Editor-Element).
    if getattr(template, "show_odoo_barcode", False):
        bc_w = 55.0
        bc_h = 7.0
        bc_x = content_width - bc_w
        bc_top = body_offset + 20.0
        html_parts.append(
            f'<div class="ild-element" style="position:absolute; '
            f'left:{bc_x}mm; top:{bc_top}mm; width:{bc_w}mm; height:{bc_h}mm;">'
            f'<img t-att-src="doc.name and '
            f"'/report/barcode/?barcode_type=Code128&amp;value=%s&amp;width=600&amp;height=150' "
            f'% str(doc.name)" style="width:100%; height:100%; object-fit:contain; object-position:right;"/>'
            f'</div>'
        )

    # --- Body: feste vs. unter-Tabelle-verankerte Elemente trennen ---
    # Verankerte Elemente (anchor_mode = "after_table") sollen im
    # Dokumentenfluss direkt unter die Positionstabelle rutschen, statt auf
    # festen Koordinaten zu liegen. Dazu wird ein absolut platzierter
    # Flow-Wrapper an der Tabellenposition aufgebaut, in dem Tabelle und
    # Anker-Elemente statisch untereinander fließen.
    table_elements = body_elements.filtered(
        lambda e: e.element_type == "table"
    )
    anchored_elements = body_elements.filtered(
        lambda e: e.anchor_mode == "after_table" and e.element_type != "table"
    )

    # Strang 1: Elemente, die an den Summenblock fixiert sind (fixed_to="totals"),
    # fließen NICHT eigenständig — sie werden als Overlay GEMEINSAM mit dem
    # Totals-Element gerendert, sodass ihr relativer Offset erhalten bleibt und
    # sie mit dem Totals-Block über Seiten mitwandern. Wirksam nur, wenn das
    # Totals-Element im Fluss liegt (anchor_mode="after_table"); ein fest
    # platziertes Totals braucht keine Sonderbehandlung (Box liegt dann ohnehin
    # absolut darüber). Eine Ebene: fixierte Elemente sind nie selbst Ziel.
    totals_elem_rs = body_elements.filtered(lambda e: e.element_type == "totals")[:1]
    totals_flows = bool(totals_elem_rs) and totals_elem_rs.anchor_mode == "after_table"
    if totals_flows:
        pinned_to_totals = body_elements.filtered(
            lambda e: getattr(e, "fixed_to", "none") == "totals"
            and e.element_type != "totals"
        )
    else:
        pinned_to_totals = body_elements.browse()

    if table_elements:
        # Sobald eine Positionstabelle existiert, MUSS sie im normalen
        # Dokumentenfluss liegen (nicht position:absolute). Nur fließender
        # Inhalt paginiert über mehrere Seiten. Eine absolut platzierte Tabelle
        # bleibt ein einziger Block, läuft bei vielen Zeilen über den
        # Seitenrand hinaus und verschmilzt mit dem (fixierten) Footer. Anker-
        # Elemente sind optional und fließen, falls vorhanden, darunter.
        table_elem = table_elements[0]
        # Fixierte Overlay-Elemente raus aus dem unabhängigen Rendern — sie
        # kommen zusammen mit dem Totals-Block.
        fixed_body = body_elements - table_elements - anchored_elements - pinned_to_totals
        anchored_elements = anchored_elements - pinned_to_totals
        _logger.info(
            "ILD body: Tabelle (id=%s) im Fluss, %d Anker-Element(e) darunter.",
            table_elem.id, len(anchored_elements),
        )
        for elem in fixed_body:
            html_parts.append(
                _render_element(elem, template, y_offset=body_offset)
            )
        # Tabelle + Anker bleiben im NORMALEN Dokumentenfluss (kein absoluter
        # Wrapper) — nur so paginiert wkhtmltopdf lange Tabellen über mehrere
        # Seiten. Die feste Startposition der Tabelle wird über einen
        # In-Flow-Spacer (Höhe, KEIN margin) erzeugt: margin-top des ersten
        # Flusskinds würde sonst mit der Seite kollabieren und die ganze Seite
        # verschieben statt die Tabelle. Die horizontale Position kommt über
        # margin-left (kollabiert nicht).
        table_top = float(table_elem.pos_y) + body_offset
        html_parts.append(
            f'<div style="height: {table_top}mm;"></div>'
        )
        html_parts.append(
            _render_element(
                table_elem, template, flow=True,
                flow_dx=float(table_elem.pos_x), flow_gap=0.0,
            )
        )
        # Anker-Elemente fließen unter die Tabelle. Explizit verbundene
        # Fluss-Gruppen (gleicher flow_group_key, Strang 1 Teil B) fließen als
        # EINE Einheit; ihre interne relative Anordnung bleibt erhalten. Anker
        # ohne Key verhalten sich unverändert (Status quo).
        flow_groups = {}
        flow_singles = []
        for elem in anchored_elements:
            gkey = (elem.flow_group_key or "").strip()
            if gkey:
                flow_groups.setdefault(gkey, anchored_elements.browse())
                flow_groups[gkey] |= elem
            else:
                flow_singles.append(elem)
        # Flussreihenfolge nach oberster Kante (min pos_y) der jeweiligen Einheit.
        flow_units = [(float(e.pos_y), "single", e) for e in flow_singles]
        for gkey, recs in flow_groups.items():
            flow_units.append((min(float(r.pos_y) for r in recs), "group", recs))
        flow_units.sort(key=lambda u: u[0])
        for _top, kind, payload in flow_units:
            if kind == "single":
                if payload.element_type == "totals" and pinned_to_totals:
                    # Totals + fixierte Overlay-Elemente als eine Fluss-Einheit
                    # (relativer Offset bleibt, wandern gemeinsam über Seiten).
                    html_parts.append(
                        _render_anchor_group(
                            payload | pinned_to_totals, template, content_width
                        )
                    )
                else:
                    # Horizontale Position aus der X-Koordinate, vertikaler Abstand
                    # aus anchor_gap (mm).
                    gap = float(payload.anchor_gap) if payload.anchor_gap else 0.0
                    html_parts.append(
                        _render_element(
                            payload, template, flow=True,
                            flow_dx=float(payload.pos_x), flow_gap=gap,
                        )
                    )
            else:
                recs = payload
                if pinned_to_totals and any(r.element_type == "totals" for r in recs):
                    recs = recs | pinned_to_totals
                html_parts.append(
                    _render_anchor_group(recs, template, content_width)
                )
    else:
        # Keine Tabelle im Body → alle Body-Elemente absolut platzieren.
        # Verankerte Elemente ohne Tabelle fallen hierauf zurück und werden
        # wie feste Elemente gerendert.
        if anchored_elements:
            _logger.info(
                "ILD anchor: %d verankerte Element(e), aber keine Tabelle im "
                "Body → Fallback auf feste Position.", len(anchored_elements),
            )
        for elem in body_elements:
            html_parts.append(
                _render_element(elem, template, y_offset=body_offset)
            )

    html_parts.append("</div>")

    # --- Footer als fixes Band (Geschwister von .ild-page, NICHT darin) ---
    # Bisher lagen Footer-Elemente absolut auf einer festen Seite-1-Koordinate
    # (top = footer_offset). Bei mehrseitigen Tabellen lief der fließende Body
    # über diese Koordinate hinweg -> Footer verschmolz mit den Tabellenzeilen.
    # Lösung: ein einziges position:fixed-Band, das auf JEDER Seite unten im
    # reservierten Footer-Rand (siehe @page margin-bottom) erscheint.
    #
    # WICHTIG: Das Band muss AUSSERHALB von .ild-page stehen. .ild-page ist
    # position:relative; liegt das fixe Band darin, verankert WeasyPrint es an
    # der Unterkante von .ild-page (= Seite-1-Höhe) statt am Seitenrand -> es
    # rutscht in die Seitenmitte und kollidiert wieder mit der Tabelle. Als
    # direktes Geschwister von .ild-page bezieht sich `fixed` auf die Seitenbox.
    if footer_elements:
        _logger.info(
            "ILD footer: %d Footer-Element(e) im fixen Band (Höhe %smm, "
            "engine=%s).",
            len(footer_elements), fh, engine,
        )
        # Verankerungspunkt eines position:fixed-Bands ist je Engine anders:
        # - WeasyPrint: relativ zur Seiten-INHALTSFLÄCHE (innerhalb der @page-
        #   Margins). left/top zählen ab der Inhalts-Ecke. Damit das Band im
        #   unten reservierten Margin-Bereich (mb+fh) sitzt, wird es per
        #   top = Inhaltshöhe direkt unter den Body geschoben; left = 0, weil
        #   der linke Rand bereits durch den @page-Margin entsteht.
        # - wkhtmltopdf (altes WebKit): relativ zur SEITENBOX. Hier zählt
        #   bottom/left ab der physischen Seitenkante, deshalb bottom = mb und
        #   left = ml.
        content_height = max(0, ph - mt - mb - fh)
        if engine == "weasyprint":
            band_pos = f"left: 0; top: {content_height}mm;"
        else:
            band_pos = f"left: {ml}mm; bottom: {mb}mm;"
        html_parts.append(
            f'<div class="ild-footer-band" style="position: fixed; '
            f'{band_pos} '
            f'width: {content_width}mm; height: {fh}mm;">'
        )
        for elem in footer_elements:
            raw = elem.text_content or ""
            # Split NUR bei einfachem mehrzeiligem Text (<div>/<span> + <br/>).
            # Komplexe Footer (QWeb-Tabellen mit <t t-set>/<table>/<td>) NICHT
            # splitten — ein <br/> innerhalb einer Tabellenzelle würde sonst die
            # Tabelle zerreißen (kaputtes <td>...</div>) und den Render crashen.
            is_complex = bool(re.search(r'<table|<td|<tr|<t\s', raw))
            if (engine == "weasyprint" and elem.element_type == "text"
                    and not is_complex and re.search(r'<br\s*/?>', raw)):
                # WeasyPrint kürzt mehrzeiligen Inhalt eines position:fixed-
                # Elements auf der LETZTEN Seite auf eine Zeile. Deshalb jede
                # <br/>-getrennte Zeile als eigenes einzeiliges Element rendern
                # (3mm tiefer gestaffelt) — so erscheinen alle Zeilen auf jeder
                # Seite, identisch zur ersten.
                m = re.match(r'^\s*(<(?:div|span)[^>]*>)(.*)(</(?:div|span)>)\s*$', raw, re.DOTALL)
                if m:
                    open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
                else:
                    open_tag, inner, close_tag = "", raw, ""
                lines = []
                for part in re.split(r'<br\s*/?>', inner):
                    if part.strip():
                        lines.append(part.strip())
                for i, line in enumerate(lines):
                    html_parts.append(_render_element(
                        elem, template, y_offset=i * 3.0,
                        content_override=f"{open_tag}{line}{close_tag}"))
            else:
                html_parts.append(_render_element(elem, template, y_offset=0.0))
        html_parts.append("</div>")
    else:
        _logger.info("ILD footer: keine Footer-Elemente vorhanden.")

    return "\n".join(html_parts)


def _render_element(elem, template, y_offset=0.0, flow=False,
                    flow_dx=0.0, flow_gap=0.0, content_override=None):
    """Render a single element to HTML, optionally wrapped in a t-if condition.

    flow=True rendert das Element NICHT auf absoluten Koordinaten, sondern
    als statischen Block im normalen Dokumentenfluss. Wird für unter der
    Tabelle verankerte Elemente genutzt (anchor_mode = "after_table"), die
    mit der Tabellenhöhe nachrutschen sollen.
    """
    # Text-, Field- und Table-Elemente wachsen vertikal mit dem Inhalt:
    # Bei kleiner Schrift (z.B. 7pt Labels) erzwingt der unsichtbare
    # Zeilen-"Strut" der 11pt-Basisschrift eine Zeilenbox, die höher ist
    # als die im Editor gesetzte Box-Höhe. Mit fester `height` + overflow:hidden
    # werden dadurch die Unterlängen (g, p, j) abgeschnitten. Tabellen würden
    # bei vielen Zeilen über die gesetzte Höhe hinaus abgeschnitten. Deshalb
    # hier `min-height` statt `height`: die Box wächst bei Bedarf vertikal mit,
    # horizontal wird weiterhin über `width` + overflow:hidden begrenzt.
    if elem.element_type in ("text", "field", "table", "totals"):
        height_css = f"min-height: {elem.height}mm"
    else:
        height_css = f"height: {elem.height}mm"

    if flow:
        # In-Flow: keine left/top-Koordinaten. `position: static` hebt das
        # absolute Verhalten der .ild-element-Klasse auf, sodass das Element
        # direkt unter dem vorherigen Flussinhalt (= Tabelle) sitzt.
        style_parts = [
            "position: static",
            f"width: {elem.width}mm",
            height_css,
        ]
        if flow_dx:
            style_parts.append(f"margin-left: {flow_dx}mm")
        if flow_gap:
            style_parts.append(f"margin-top: {flow_gap}mm")
        # Keep-Together (Punkt 2): ein fließendes Body-Element wird NIE mittig
        # zerschnitten — passt es nicht mehr auf die Seite, rückt es komplett auf
        # die nächste. Gilt nicht für die Tabelle (die soll zeilenweise brechen).
        if elem.element_type != "table":
            style_parts.append("break-inside: avoid")
            style_parts.append("page-break-inside: avoid")
    else:
        style_parts = [
            f"left: {elem.pos_x}mm",
            f"top: {float(elem.pos_y) + y_offset}mm",
            f"width: {elem.width}mm",
            height_css,
        ]
    if elem.rotation:
        style_parts.append(f"transform: rotate({elem.rotation}deg)")

    elem_style = elem.get_style_data()
    for key, val in elem_style.items():
        style_parts.append(f"{key}: {val}")

    style_str = "; ".join(style_parts)

    renderers = {
        "text": _render_text,
        "field": _render_field,
        "image": _render_image,
        "table": _render_table,
        "line": _render_line,
        "vline": _render_vline,
        "shape": _render_shape,
        "container": _render_container,
        "barcode": _render_barcode,
        "qrcode": _render_barcode,
        "totals": _render_totals,
    }
    renderer = renderers.get(elem.element_type, _render_text)
    if content_override is not None and elem.element_type == "text":
        html = _render_text(elem, style_str, template, content_override=content_override)
    else:
        html = renderer(elem, style_str, template)

    # Inject per-page visibility class on the outer .ild-element wrapper.
    show_on = getattr(elem, "show_on_page", "all") or "all"
    if show_on in ("all", "first", "last", "middle") and show_on != "first":
        # Replace the first occurrence of class="ild-element ..." to add ild-page-<x>.
        # "first" = default natural flow; no extra class needed.
        html = html.replace(
            'class="ild-element',
            f'class="ild-element ild-page-{show_on}',
            1,
        )

    # Conditional visibility: wrap in t-if if condition_field is set
    cond_field = getattr(elem, "condition_field", "") or ""
    cond_op = getattr(elem, "condition_operator", "set") or "set"
    cond_val = getattr(elem, "condition_value", "") or ""
    if cond_field.strip():
        fp = f"doc.{cond_field.strip()}"

        if cond_op == "set":
            expr = fp
        elif cond_op == "unset":
            expr = f"not {fp}"
        elif cond_op == "eq":
            # String comparison: doc.state == 'sale'
            expr = f"str({fp}) == '{cond_val}'"
        elif cond_op == "neq":
            expr = f"str({fp}) != '{cond_val}'"
        elif cond_op == "contains":
            expr = f"'{cond_val}' in str({fp} or '')"
        elif cond_op == "gt":
            expr = f"({fp} or 0) &gt; {cond_val}"
        elif cond_op == "lt":
            expr = f"({fp} or 0) &lt; {cond_val}"
        elif cond_op == "in":
            # Comma-separated list: doc.state in ['draft', 'sent']
            values = [v.strip() for v in cond_val.split(",")]
            values_str = ", ".join(f"'{v}'" for v in values)
            expr = f"str({fp}) in [{values_str}]"
        else:
            expr = fp

        html = f'<t t-if="{expr}">{html}</t>'

    return html


def _render_anchor_group(elems, template, content_width):
    """Render an explicit flow group (Strang 1, Teil B) as ONE in-flow block
    under the table. Members keep their page X and their RELATIVE Y (normalised
    to the group's top edge); the block flows as a unit, so eine wachsende
    Tabelle schiebt die ganze Gruppe gemeinsam nach unten. Horizontale Anordnung
    bleibt 1:1 erhalten. Reines static/relative/absolute-CSS → beide Engines."""
    pys = [float(e.pos_y) for e in elems]
    min_y = min(pys)
    max_y = max(float(e.pos_y) + float(e.height) for e in elems)
    gh = max(0.0, max_y - min_y)
    ordered = elems.sorted(key=lambda e: (e.sequence, e.id))
    leader = ordered[0]
    gap = float(leader.anchor_gap) if leader.anchor_gap else 0.0
    parts = [
        f'<div class="ild-flow-group" style="position: relative; '
        f'width: {content_width}mm; height: {gh}mm; margin-top: {gap}mm; '
        f'break-inside: avoid; page-break-inside: avoid;">'
    ]
    for el in ordered:
        # Non-flow render = position:absolute mit left=pos_x, top=pos_y+y_offset.
        # y_offset = -min_y normiert die Gruppe auf ihre eigene Oberkante; X
        # bleibt seiten-absolut (left=pos_x) und entspricht der Position im
        # full-width-Wrapper → horizontale Anordnung bleibt erhalten.
        parts.append(_render_element(el, template, y_offset=-min_y))
    parts.append("</div>")
    return "".join(parts)


def _render_text(elem, style_str, template=None, content_override=None):
    align = f"text-align: {elem.text_align}" if elem.text_align else ""
    content = content_override if content_override is not None else (elem.text_content or "")
    # Decode HTML entities that Odoo may have escaped in XML data
    content = content.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    # Toolbar text_align must win over inner inline text-align in legacy HTML
    # content (e.g. default_templates `<div style="text-align: center; ...">`).
    # CSS specificity makes inner inline styles override the wrapper's value,
    # so we strip them when the toolbar value is set.
    if elem.text_align:
        content = re.sub(r'text-align\s*:\s*[^;"\']+;?', '', content, flags=re.IGNORECASE)
    # Replace placeholders with QWeb expressions for company/document data
    content = _replace_placeholders(content)
    return f'<div class="ild-element" style="{style_str}; {align}">{content}</div>'


def _replace_placeholders(text):
    """Replace [placeholder] tokens with QWeb t-esc expressions.

    Every company field is wrapped in a t-if guard so that:
    - Empty fields render nothing (not "False")
    - Surrounding labels (e.g. "Tel: [phone]") collapse cleanly when the
      field is empty.  We achieve this by wrapping the ENTIRE line-segment
      that contains the placeholder.  For simple inline use we just guard
      the value itself.
    """
    import re

    # --- Simple guarded replacements (field renders nothing when empty) ---
    replacements = {
        "[company_name]": '<t t-if="doc.company_id.name"><t t-esc="doc.company_id.name"/></t>',
        "[street]": '<t t-if="doc.company_id.street"><t t-esc="doc.company_id.street"/></t>',
        "[zip]": '<t t-if="doc.company_id.zip"><t t-esc="doc.company_id.zip"/></t>',
        "[city]": '<t t-if="doc.company_id.city"><t t-esc="doc.company_id.city"/></t>',
        "[email]": '<t t-if="doc.company_id.email"><t t-esc="doc.company_id.email"/></t>',
        "[website]": '<t t-if="doc.company_id.website"><t t-esc="doc.company_id.website"/></t>',
        "[vat]": '<t t-if="doc.company_id.vat"><t t-esc="doc.company_id.vat"/></t>',
        "[bank_name]": '<t t-if="doc.company_id.bank_ids"><t t-esc="doc.company_id.bank_ids[0].bank_id.name"/></t>',
        "[iban]": '<t t-if="doc.company_id.bank_ids"><t t-esc="doc.company_id.bank_ids[0].acc_number"/></t>',
        "[bic]": '<t t-if="doc.company_id.bank_ids"><t t-esc="doc.company_id.bank_ids[0].bank_id.bic"/></t>',
        "[stnr]": '<t t-if="doc.company_id.company_registry"><t t-esc="doc.company_id.company_registry"/></t>',
        "[court]": '<t t-if="doc.company_id.company_registry"><t t-esc="doc.company_id.company_registry"/></t>',
        "[director]": '',
        "[tax_nr]": '<t t-if="doc.company_id.company_registry"><t t-esc="doc.company_id.company_registry"/></t>',
        "[registry]": '<t t-if="doc.company_id.company_registry"><t t-esc="doc.company_id.company_registry"/></t>',
        # Belegnummer des aktuellen Dokuments (Rechnungs-/Auftrags-/Bestell-/
        # Lieferschein-Nummer). Für Titel wie "Rechnung [number]".
        "[number]": '<t t-if="doc.name"><t t-esc="doc.name"/></t>',
        # Dynamischer Belegtitel je move_type: eine Gutschrift soll im Titel
        # "Gutschrift" statt "Rechnung" tragen. Nur account.move unterscheidet
        # Typen; andere Belege liefern hier nichts (Titel bleibt fest im Element).
        "[doc_title]": (
            '<t t-if="doc._name == \'account.move\'">'
            '<t t-if="doc.move_type == \'out_refund\'">Gutschrift</t>'
            '<t t-elif="doc.move_type == \'in_refund\'">Lieferantengutschrift</t>'
            '<t t-elif="doc.move_type == \'in_invoice\'">Eingangsrechnung</t>'
            '<t t-else="">Rechnung</t>'
            '</t>'
        ),
        # Page numbers — wrapped in a span; actual number filled by CSS counter(page)/counter(pages)
        # which is understood by both wkhtmltopdf (via its own mechanism) and WeasyPrint (via the
        # .ild-pagenum::before / .ild-pagetotal::before rules we emit in transpile_template's CSS).
        "[page]": '<span class="page ild-pagenum"></span>',
        "[pages]": '<span class="topage ild-pagetotal"></span>',
    }

    # --- Label-aware replacements: collapse entire "Label: [field]" when empty ---
    # Match patterns like "Tel: [phone]", "Tel.: [phone]" etc.
    # and wrap the whole thing in a t-if so the label disappears too.
    # NOTE: [fax] is NOT supported — res.company has no fax field in Odoo 19.
    #       We remove [fax] and any preceding label text (e.g. "Fax: [fax]").
    import re as _re
    text = _re.sub(r'[^<>\n]*?\[fax\]', '', text, flags=_re.IGNORECASE)

    label_patterns = {
        "phone": "doc.company_id.phone",
    }
    for tag, field_expr in label_patterns.items():
        # Pattern: optional text before [tag], the placeholder, optional trailing whitespace
        pattern = re.compile(
            r'([^<>\n]*?)\[' + re.escape(tag) + r'\]',
            re.IGNORECASE,
        )
        def _repl(m, fe=field_expr):
            prefix = m.group(1)
            return f'<t t-if="{fe}">{prefix}<t t-esc="{fe}"/></t>'
        text = pattern.sub(_repl, text)

    # --- Apply simple replacements (skip phone/fax as they were handled above) ---
    for placeholder, qweb in replacements.items():
        text = text.replace(placeholder, qweb)

    return text


def _render_field(elem, style_str, template=None):
    field_path = elem.field_name or ""
    if not field_path:
        return f'<div class="ild-element" style="{style_str}">[No field]</div>'

    default_val = elem.field_default or ""
    # Eckige-Klammer-Werte (z.B. "[Name]", "[PLZ Ort]") sind reine
    # Editor-Vorschau-Platzhalter und duerfen NICHT in den echten Beleg
    # gedruckt werden. Ist das Feld leer (z.B. Lieferschein ohne Partner),
    # bleibt die Stelle leer statt einen "[...]"-Platzhalter anzuzeigen.
    if default_val.startswith("[") and default_val.endswith("]"):
        default_val = ""
    align = f"text-align: {elem.text_align}" if elem.text_align else ""
    elem_name = (elem.name or "").lower()

    # ── Special: Return address line (Rücksendezeile) ──
    # Detected by: element named "*rücksende*" or "*return*" or "*absender*",
    # OR a company_id.name field with underline/text-decoration in the style.
    is_return_line = (
        field_path == "company_id.name"
        and (
            "underline" in style_str
            or "text-decoration" in style_str
            or any(kw in elem_name for kw in ("rücksende", "return", "absender", "rückadresse"))
        )
    )
    if is_return_line:
        return (
            f'<div class="ild-element" style="{style_str}; {align}">'
            f'<t t-if="doc.company_id.name"><t t-esc="doc.company_id.name"/></t>'
            f'<t t-if="doc.company_id.street"> · <t t-esc="doc.company_id.street"/></t>'
            f'<t t-if="doc.company_id.zip or doc.company_id.city">'
            f' · <t t-if="doc.company_id.zip"><t t-esc="doc.company_id.zip"/> </t>'
            f'<t t-if="doc.company_id.city"><t t-esc="doc.company_id.city"/></t>'
            f'</t>'
            f'</div>'
        )

    # ── Special: ZIP field shows ZIP + City combined ──
    if field_path == "partner_id.zip":
        return (
            f'<div class="ild-element" style="{style_str}; {align}">'
            f'<t t-if="doc.partner_id.zip or doc.partner_id.city">'
            f'<t t-if="doc.partner_id.zip"><t t-esc="doc.partner_id.zip"/> </t>'
            f'<t t-if="doc.partner_id.city"><t t-esc="doc.partner_id.city"/></t>'
            f'</t>'
            f'<t t-if="not doc.partner_id.zip and not doc.partner_id.city">{default_val}</t>'
            f'</div>'
        )

    # ── Special: Company ZIP shows ZIP + City combined ──
    if field_path == "company_id.zip":
        return (
            f'<div class="ild-element" style="{style_str}; {align}">'
            f'<t t-if="doc.company_id.zip or doc.company_id.city">'
            f'<t t-if="doc.company_id.zip"><t t-esc="doc.company_id.zip"/> </t>'
            f'<t t-if="doc.company_id.city"><t t-esc="doc.company_id.city"/></t>'
            f'</t>'
            f'</div>'
        )

    # ── Date fields: use widget formatting ──
    if field_path in ("invoice_date", "invoice_date_due", "date_order", "validity_date",
                       "date_planned", "scheduled_date", "date_done"):
        return (
            f'<div class="ild-element" style="{style_str}; {align}">'
            f'<t t-if="doc.{field_path}">'
            f'<t t-esc="doc.{field_path}" t-options=\'{{"widget": "date"}}\'/>'
            f'</t>'
            f'<t t-else="">{default_val}</t>'
            f'</div>'
        )

    first_part = field_path.split(".")[0]

    return (
        f'<div class="ild-element" style="{style_str}; {align}">'
        f'<t t-if="doc.{first_part}">'
        f'<t t-esc="doc.{field_path}"/>'
        f'</t>'
        f'<t t-else="">{default_val}</t>'
        f'</div>'
    )


def _render_image(elem, style_str, template=None):
    fit_css = {"contain": "object-fit: contain", "cover": "object-fit: cover", "stretch": "object-fit: fill"}.get(elem.image_fit, "object-fit: contain")

    if elem.image_source == "company_logo":
        return (
            f'<div class="ild-element" style="{style_str}">'
            f'<t t-if="doc.company_id.logo">'
            f'<img t-att-src="image_data_uri(doc.company_id.logo)" '
            f'style="width: 100%; height: 100%; {fit_css};"/>'
            f'</t>'
            f'</div>'
        )
    elif elem.image_source == "upload" and elem.image_data:
        # Uploaded image — embed directly as base64 data URI
        import base64
        try:
            raw = elem.image_data
            if isinstance(raw, bytes):
                b64_str = base64.b64encode(raw).decode("ascii")
            else:
                b64_str = raw  # already base64 string
            return (
                f'<div class="ild-element" style="{style_str}">'
                f'<img src="data:image/png;base64,{b64_str}" '
                f'style="width: 100%; height: 100%; {fit_css};"/>'
                f'</div>'
            )
        except Exception:
            pass  # fall through to placeholder
    elif elem.image_source == "field" and elem.image_field_name:
        return (
            f'<div class="ild-element" style="{style_str}">'
            f'<t t-if="doc.{elem.image_field_name}">'
            f'<img t-att-src="image_data_uri(doc.{elem.image_field_name})" '
            f'style="width: 100%; height: 100%; {fit_css};"/>'
            f'</t>'
            f'</div>'
        )
    return f'<div class="ild-element" style="{style_str}"></div>'


def _ild_col_css(col):
    """Per-column visual overrides (Punkt 1 — Spalten granular).

    All keys are optional: when absent the function returns "" → no extra CSS,
    so existing templates render byte-identical (Defaults = Status quo). Applied
    to BOTH header and data cells of a column so it looks consistent top to
    bottom. Plain font/color/background CSS only → renders in WeasyPrint and
    wkhtmltopdf alike (no object-fit involved).
    """
    css = ""
    if col.get("bold"):
        css += "font-weight:bold;"
    fs = col.get("fsize")
    if fs:
        css += f"font-size:{fs}pt;"
    cc = col.get("color")
    if cc:
        css += f"color:{cc};"
    bgc = col.get("bg")
    if bgc:
        css += f"background-color:{bgc};"
    return css


def _render_table(elem, style_str, template=None):
    """
    Render the line items table with:
    - Page break chunking (configurable rows per page)
    - Repeated headers on continuation pages
    - Carryover subtotal lines
    - Optional items handling (separate section / inline / hidden)
    - Section & note line support (account.move)
    """
    columns = elem.get_table_columns()
    # Strip columns with no field path (they would render empty/placeholder data)
    # and columns the user hid (col.hidden, Punkt 1). Filtering here keeps n_cols
    # / section colspans correct.
    if columns:
        columns = [c for c in columns if c.get("field") and not c.get("hidden")]
    if not columns:
        # Default columns depend on document type
        doc_type_tmp = field_registry.doc_type_to_model(template.doc_type) if template else "account.move"
        if doc_type_tmp == "sale.order":
            columns = [
                {"field": "name", "label": "Description", "width": "40%", "align": "left"},
                {"field": "product_uom_qty", "label": "Qty", "width": "10%", "align": "right"},
                {"field": "price_unit", "label": "Price", "width": "15%", "align": "right", "type": "monetary"},
                {"field": "discount", "label": "Disc%", "width": "10%", "align": "right"},
                {"field": "price_subtotal", "label": "Subtotal", "width": "15%", "align": "right", "type": "monetary"},
            ]
        elif doc_type_tmp == "purchase.order":
            columns = [
                {"field": "name", "label": "Description", "width": "40%", "align": "left"},
                {"field": "product_qty", "label": "Qty", "width": "10%", "align": "right"},
                {"field": "price_unit", "label": "Price", "width": "15%", "align": "right", "type": "monetary"},
                {"field": "price_subtotal", "label": "Subtotal", "width": "15%", "align": "right", "type": "monetary"},
            ]
        elif doc_type_tmp == "stock.picking":
            columns = [
                {"field": "product_id.name", "label": "Product", "width": "35%", "align": "left"},
                {"field": "description_picking", "label": "Description", "width": "25%", "align": "left"},
                {"field": "product_uom_qty", "label": "Demand", "width": "15%", "align": "right"},
                {"field": "quantity", "label": "Done", "width": "15%", "align": "right"},
            ]
        else:
            # account.move default
            columns = [
                {"field": "name", "label": "Description", "width": "40%", "align": "left"},
                {"field": "quantity", "label": "Qty", "width": "10%", "align": "right"},
                {"field": "price_unit", "label": "Price", "width": "15%", "align": "right", "type": "monetary"},
                {"field": "discount", "label": "Disc%", "width": "10%", "align": "right"},
                {"field": "price_subtotal", "label": "Subtotal", "width": "15%", "align": "right", "type": "monetary"},
            ]

    doc_type = field_registry.doc_type_to_model(template.doc_type) if template else "account.move"
    line_field = field_registry.get_line_relation_field(doc_type)

    # Guard: if no line field is known for this doc type, show error
    if not line_field:
        return (
            f'<div class="ild-element" style="{style_str}">'
            f'<p style="color:#999;font-style:italic;">No line items field found for {doc_type}</p>'
            f'</div>'
        )

    # Config
    td_border = {
        "none": "", "full": "border: 0.5pt solid #ccc;",
        "horizontal": "border-bottom: 0.5pt solid #eee;", "outer": "", "bold": "",
    }.get(elem.table_border_style, "")
    outer = 'style="border: 1pt solid #333;"' if elem.table_border_style == "outer" else ""
    # Bold-Stil bekommt eine zusätzliche Tabellen-Klasse für kräftige Kopflinien.
    border_class = " ild-table-bold" if elem.table_border_style == "bold" else ""
    zebra = "ild-zebra" if elem.table_zebra else ""
    # Row spacing: extra vertical padding on data cells. Default base vertical
    # padding (2mm, from .ild-table td) is preserved when spacing == 0 (emit
    # nothing → class controls everything = status quo). When > 0 we override
    # the vertical padding additively (2mm base + spacing) while leaving the
    # horizontal padding to the class. Works in WeasyPrint and wkhtmltopdf.
    row_pad = ""
    if elem.table_row_spacing and elem.table_row_spacing > 0:
        _vp = 2.0 + elem.table_row_spacing
        row_pad = f"padding-top:{_vp:g}mm; padding-bottom:{_vp:g}mm; "
    n_cols = len(columns)
    rows_per_page = elem.table_rows_per_page or 25
    optional_mode = elem.table_optional_mode or "separate"
    optional_label = elem.table_optional_label or "Optional Items"
    show_orig_qty = elem.table_optional_show_qty

    # ===== HELPER: Build table header row =====
    # Discount column + Tax column are wrapped in t-if guards so they disappear
    # when no line uses discount/tax (analog zu Odoos account.report_invoice_document).
    def _thead():
        h = "<thead><tr>"
        for col in columns:
            w = f'width: {col.get("width", "auto")};' if col.get("width") else ""
            a = f'text-align: {col.get("align", "left")};'
            field = col.get("field", "")
            ccss = _ild_col_css(col)
            th = f'<th style="{w} {a}{ccss}">{col.get("label", "")}</th>'
            if field == "discount":
                th = f'<t t-if="ild_display_discount">{th}</t>'
            elif field in ("tax_ids", "tax_id", "taxes_id"):
                th = f'<t t-if="ild_display_taxes">{th}</t>'
            h += th
        h += "</tr></thead>"
        return h

    # ===== HELPER: Build one data cell =====
    def _td(col, line_var="line"):
        a = f'text-align: {col.get("align", "left")};'
        field = col.get("field", "")
        ccss = _ild_col_css(col)
        if field == "sequence":
            return f'<td style="{row_pad}{td_border} {a}{ccss}"><t t-esc="{line_var}_index + 1"/></td>'
        elif field in ("quantity", "product_uom_qty", "product_qty", "qty_done", "quantity_done"):
            # Menge lokalisiert (Dezimalkomma je Sprache) statt rohem Float
            # "15.0". Das float-Widget nutzt die Nachkommastellen des Feldes.
            return (
                f'<td style="{row_pad}{td_border} {a}{ccss}">'
                f'<span t-out="{line_var}.{field}" t-options=\'{{"widget": "float", "precision": 2}}\'/>'
                f'</td>'
            )
        elif col.get("type") == "monetary" or field in ("price_unit", "price_subtotal", "price_total"):
            # Lokalisierte Währungsformatierung über Odoos monetary-Widget
            # (Tausendertrenner + Dezimaltrenner je Sprache, Symbol an korrekter
            # Position) statt rohem '%.2f' (englisch "173.00", kein Trenner).
            return (
                f'<td style="{row_pad}{td_border} {a}{ccss}">'
                f'<span t-out="{line_var}.{field}" '
                f'''t-options='{{"widget": "monetary", "display_currency": doc.currency_id}}'/>'''
                f'</td>'
            )
        elif field in ("tax_ids", "tax_id", "taxes_id"):
            return f'<t t-if="ild_display_taxes"><td style="{row_pad}{td_border} {a}{ccss}"><t t-esc="\', \'.join({line_var}.{field}.mapped(\'name\'))"/></td></t>'
        elif field == "discount":
            return f'<t t-if="ild_display_discount"><td style="{row_pad}{td_border} {a}{ccss}"><t t-if="{line_var}.discount"><t t-esc="\'%.0f\' % {line_var}.discount"/> %</t></td></t>'
        elif "." in field:
            # Related field like product_uom_id.name
            return f'<td style="{row_pad}{td_border} {a}{ccss}"><t t-esc="{line_var}.{field}"/></td>'
        else:
            return f'<td style="{row_pad}{td_border} {a}{ccss}"><t t-esc="{line_var}.{field}"/></td>'

    # ===== START BUILDING =====
    html = f'<div class="ild-element" style="{style_str}; position: relative;">'

    # --- Determine if this doc type supports optional items ---
    # (sale.order has is_optional field on lines in some versions)
    has_optional = doc_type == "sale.order"

    # Editor-Live-Preview: Der separate "Optional Items"-Block hängt am
    # Sample-Record (irgendein bestätigter Beleg), nicht am echten Druckbeleg.
    # Hat das Sample eine qty-0-Zeile, erscheint im Preview eine zweite Tabelle,
    # die im realen Druck nie käme — das verwirrt. Im Preview daher die separate
    # Optional-Tabelle unterdrücken (Layout-Design zeigt die Standard-Tabelle).
    # Der echte PDF-Render setzt diesen Context nicht → Optional bleibt dort.
    is_preview = bool(template and template.env.context.get("ild_preview"))

    # ===== CSS for page break control =====
    html += f"""
    <style>
        .ild-table-chunk {{ page-break-inside: auto; }}
        .ild-table-chunk tr {{ page-break-inside: avoid; page-break-after: auto; }}
        .ild-page-break {{ page-break-before: always; }}
        .ild-optional-row td {{ color: #888; font-style: italic; }}
        .ild-optional-label {{ background-color: #f0f0f0; padding: 1mm 2mm; border-radius: 1mm; font-size: 0.85em; color: #666; }}
        .ild-carryover {{ font-style: italic; color: #999; font-size: 0.85em; border-top: 0.5pt solid #ccc; }}
        .ild-section-header td {{ font-weight: bold; padding-top: 3mm; font-size: 1.05em; }}
        .ild-note-row td {{ font-style: italic; color: #666; }}
    </style>
    """

    # ===================================================================
    # MAIN TABLE — Regular (non-optional) lines
    # ===================================================================
    # Berechne ob Discount/Tax-Spalte überhaupt sichtbar sein soll
    # (Odoo-Standardverhalten aus account.report_invoice_document).
    # Tax-Feldname pro Modell: account.move → tax_ids, sale.order → tax_id, purchase → taxes_id.
    tax_field_map = {
        "account.move": "tax_ids",
        "sale.order": "tax_id",
        "purchase.order": "taxes_id",
        "stock.picking": None,
    }
    tax_field = tax_field_map.get(doc_type)
    html += f'<t t-set="ild_display_discount" t-value="any(l.discount for l in doc.{line_field} if \'discount\' in l._fields)"/>'
    if tax_field:
        html += f'<t t-set="ild_display_taxes" t-value="any(l.{tax_field} for l in doc.{line_field} if \'{tax_field}\' in l._fields)"/>'
    else:
        html += '<t t-set="ild_display_taxes" t-value="False"/>'

    html += f'<table class="ild-table ild-table-chunk{border_class}" {outer}>'

    # Header
    if elem.table_show_header:
        html += _thead()

    html += "<tbody>"

    # Strang 2: Section-Subtotals — Akkumulator initialisieren (nur wenn aktiv
    # und der doc_type Sektionen kennt). Mutable Listen, weil QWeb-t-set in
    # t-foreach pro Iteration zurückgesetzt wird; append/sum/clear umgehen das
    # und sind safe_eval-konform (kein Dunder/__setitem__). ild_sec_amts sammelt
    # die Positionsbeträge der laufenden Sektion, ild_sec_on markiert "mindestens
    # eine Sektion gesehen".
    _do_sub = bool(getattr(elem, "table_show_subtotals", False)) and doc_type in ("account.move", "sale.order")
    if _do_sub:
        html += '<t t-set="ild_sec_amts" t-value="[]"/>'
        html += '<t t-set="ild_sec_on" t-value="[]"/>'

    # --- QWeb line loop with page-break counter ---
    # We use t-set to count rows and inject page breaks
    html += f'<t t-set="ild_row_count" t-value="0"/>'
    html += f'<t t-foreach="doc.{line_field}" t-as="line">'

    # --- FILTER: Skip optional items if separate/hide mode ---
    if has_optional and optional_mode in ("separate", "hide"):
        # In sale.order, optional lines have product_uom_qty == 0 (or is_optional if available).
        # WICHTIG: Section-/Note-Zeilen (display_type gesetzt) haben ebenfalls qty 0,
        # sind aber KEINE optionalen Posten. Sie müssen im Haupt-Fluss bleiben
        # (werden unten über display_type gerendert), sonst verschwinden Sektionen
        # aus der Tabelle und tauchen fälschlich im Optional-Block auf.
        html += '<t t-if="line.product_uom_qty != 0 or line.display_type">'

    # --- account.move: handle sections & notes ---
    show_sections = getattr(elem, "table_show_sections", True)
    show_notes = getattr(elem, "table_show_notes", True)
    show_subtotals = getattr(elem, "table_show_subtotals", False)
    section_css = getattr(elem, "table_section_style", "") or "font-weight:600; font-size:9pt; background:#f5f5f5;"
    note_css = getattr(elem, "table_note_style", "") or "font-style:italic; color:#666;"
    subtotal_css = getattr(elem, "table_subtotal_style", "") or "font-weight:500; border-top:0.5pt solid #ccc;"

    # Section-Subtotal-Zeile (Strang 2): Summe der Positionen der jeweiligen
    # Sektion, rechtsbündig im Stil table_subtotal_style. sum(ild_sec_amts) ist
    # der aktuelle Sektions-Stand. Wird an Sektionsgrenzen (vorherige Sektion)
    # und am Tabellenende (letzte Sektion) emittiert.
    subtotal_tr = (
        f'<tr class="ild-subtotal-row"><td colspan="{n_cols}" '
        f'style="{subtotal_css} text-align:right; padding:1.5mm 1.5mm;">'
        f'<span style="margin-right:4mm;">Zwischensumme</span>'
        f'<span t-out="sum(ild_sec_amts)" '
        f'''t-options='{{"widget": "monetary", "display_currency": doc.currency_id}}'/>'''
        f'</td></tr>'
    )

    # account.move + sale.order teilen dasselbe display_type-System (Sektionen,
    # Notizen). Ein gemeinsamer Block statt zweier fast identischer Zweige.
    if doc_type in ("account.move", "sale.order"):
        # --- Section header (inkl. Subtotal der VORHERIGEN Sektion davor) ---
        sec_inner = ""
        if _do_sub:
            # Subtotal der vorherigen Sektion (nur wenn schon eine Sektion lief),
            # dann Akkumulator leeren und "Sektion aktiv" markieren.
            sec_inner += f'<t t-if="ild_sec_on">{subtotal_tr}</t>'
        if show_sections:
            sec_inner += (
                f'<tr><td colspan="{n_cols}" style="{section_css} padding:2mm 1.5mm;">'
                f'<t t-esc="line.name"/></td></tr>'
            )
        if _do_sub:
            sec_inner += '<t t-set="ild_x" t-value="ild_sec_amts.clear()"/>'
            sec_inner += '<t t-set="ild_x" t-value="ild_sec_on.append(1)"/>'
        html += f'<t t-if="line.display_type == \'line_section\'">{sec_inner}</t>'

        # --- Note row ---
        if show_notes:
            html += (
                f'<t t-elif="line.display_type == \'line_note\'">'
                f'<tr><td colspan="{n_cols}" style="{note_css} padding:1mm 1.5mm;">'
                f'<t t-esc="line.name"/></td></tr></t>'
            )
        else:
            html += '<t t-elif="line.display_type == \'line_note\'"></t>'

        # --- Reguläre Datenzeile: hier den Sektions-Betrag akkumulieren ---
        html += '<t t-else="">'
        if _do_sub:
            html += '<t t-set="ild_x" t-value="ild_sec_amts.append(line.price_subtotal or 0.0)"/>'

    # --- SEITENUMBRUCH ---
    # Kein manuelles Row-Chunking mehr: Der physische Seitenumbruch erfolgt durch
    # die Render-Engine (WeasyPrint/wkhtmltopdf). Der Tabellenkopf wiederholt sich
    # automatisch via thead{display:table-header-group}; einzelne Zeilen brechen
    # nicht um (page-break-inside:avoid). Die frühere "Continued on next page"-
    # Zeile wurde entfernt, weil sie an der logischen Zeilenzahl (rows_per_page)
    # statt am echten Seitenende hing und so mitten auf der Seite erschien.

    # --- Regular data row ---
    if has_optional and optional_mode == "inline":
        # Inline mode: show optional items in same table but styled differently
        html += '<t t-if="line.product_uom_qty == 0">'
        html += f'<tr class="ild-optional-row {zebra}">'
        for col in columns:
            a = f'text-align: {col.get("align", "left")};'
            field = col.get("field", "")
            ccss = _ild_col_css(col)
            if field in ("quantity", "product_uom_qty", "product_qty"):
                if show_orig_qty:
                    # Show original qty from the order line name or a computed field
                    html += f'<td style="{row_pad}{td_border} {a}{ccss}"><span class="ild-optional-label">Optional</span></td>'
                else:
                    html += f'<td style="{row_pad}{td_border} {a}{ccss}">0</td>'
            else:
                html += _td(col)
        html += "</tr>"
        html += "</t>"
        # Non-optional row
        html += '<t t-else="">'
        html += f'<tr class="{zebra}">'
        for col in columns:
            html += _td(col)
        html += "</tr>"
        html += "</t>"
    else:
        # Standard row (no optional inline handling)
        html += f'<tr class="{zebra}">'
        for col in columns:
            html += _td(col)
        html += "</tr>"

    # Close display_type filter (account.move and sale.order)
    if doc_type in ("account.move", "sale.order"):
        html += "</t>"  # close t-else (regular lines)

    # Close optional filter
    if has_optional and optional_mode in ("separate", "hide"):
        html += "</t>"  # close t-if qty != 0

    html += "</t>"  # close t-foreach
    # Strang 2: Subtotal der LETZTEN Sektion (nach der Schleife, noch in tbody).
    if _do_sub:
        html += f'<t t-if="ild_sec_on">{subtotal_tr}</t>'
    html += "</tbody></table>"

    # ===================================================================
    # OPTIONAL ITEMS SECTION (separate mode, sale.order only)
    # ===================================================================
    if has_optional and optional_mode == "separate" and not is_preview:
        html += f"""
        <!-- Check if there are any optional items. Echte optionale Posten haben
             qty 0 UND kein display_type; Section-/Note-Zeilen (auch qty 0) werden
             ausgeschlossen, damit normale Belege mit Sektionen keine leere/spurious
             "Optional Items"-Tabelle erzeugen. -->
        <t t-set="ild_optional_lines" t-value="doc.{line_field}.filtered(lambda l: l.product_uom_qty == 0 and not l.display_type)"/>
        <t t-if="ild_optional_lines">
            <div style="margin-top: 5mm;">
                <div style="font-weight: bold; font-size: 1.05em; color: #555; margin-bottom: 2mm; border-bottom: 0.5pt solid #ccc; padding-bottom: 1mm;">
                    {optional_label}
                </div>
                <table class="ild-table" {outer}>
        """
        if elem.table_show_header:
            html += _thead()

        html += """
                <tbody>
                <t t-foreach="ild_optional_lines" t-as="line">
                    <tr class="ild-optional-row">
        """
        for col in columns:
            a = f'text-align: {col.get("align", "left")};'
            field = col.get("field", "")
            ccss = _ild_col_css(col)
            if field in ("quantity", "product_uom_qty", "product_qty"):
                if show_orig_qty:
                    # For optional items, try to show a meaningful quantity
                    # This uses the description which often contains the original qty
                    html += f'<td style="{row_pad}{td_border} {a}{ccss}"><span class="ild-optional-label">Optional</span></td>'
                else:
                    html += f'<td style="{row_pad}{td_border} {a}{ccss}">—</td>'
            elif col.get("type") == "monetary" or field in ("price_unit", "price_subtotal", "price_total"):
                html += (
                    f'<td style="{row_pad}{td_border} {a}{ccss}">'
                    f'<t t-esc="\'%.2f\' % line.{field}"/>'
                    f' <t t-esc="doc.currency_id.symbol"/>'
                    f'</td>'
                )
            elif field in ("tax_ids", "tax_id", "taxes_id"):
                html += f'<td style="{row_pad}{td_border} {a}{ccss}"><t t-esc="\', \'.join(line.{field}.mapped(\'name\'))"/></td>'
            else:
                html += f'<td style="{row_pad}{td_border} {a}{ccss}"><t t-esc="line.{field}"/></td>'

        html += """
                    </tr>
                </t>
                </tbody></table>
            </div>
        </t>
        """

    # ===================================================================
    # TOTALS SECTION
    # ===================================================================
    if elem.table_show_totals and doc_type in ("account.move", "sale.order", "purchase.order"):
        _logger.info(
            "ILD totals: inline totals under table aktiv (doc_type=%s)", doc_type
        )
        html += _build_totals_html(
            elem, margin_top="4mm",
            offset_x=getattr(elem, "totals_offset_x", 0.0),
            offset_y=getattr(elem, "totals_offset_y", 0.0),
            row_spacing=getattr(elem, "totals_row_spacing", 0.0),
        )
    else:
        _logger.info(
            "ILD totals: inline totals NICHT gerendert (show=%s, doc_type=%s)",
            elem.table_show_totals, doc_type,
        )

    html += "</div>"
    return html


# Style-Presets für den Totals-Block. Bewusst auf Modul-Ebene, damit sowohl
# der Inline-Block in der Tabelle (`_render_table`) als auch das
# eigenständige Totals-Element (`_render_totals`) DASSELBE Markup erzeugen —
# eine einzige Quelle für die Optik (DRY).
TOTALS_STYLE_PRESETS = {
    "default": {
        "wrap": "", "lbl": "color:#555; font-weight:300;", "val": "",
        "tot_lbl": "font-weight:500;", "tot_val": "font-weight:500;",
        "tot_border": "border-top: 0.75pt solid #333;",
        "sub_border": "",
    },
    "bold": {
        "wrap": "", "lbl": "color:#333; font-weight:400;", "val": "font-weight:400;",
        "tot_lbl": "font-weight:600;", "tot_val": "font-weight:600;",
        "tot_border": "border-top: 1.5pt solid #333;",
        "sub_border": "",
    },
    "large": {
        "wrap": "font-size:10pt;", "lbl": "color:#333;", "val": "font-size:10pt;",
        "tot_lbl": "font-weight:600; font-size:11pt;", "tot_val": "font-weight:600; font-size:11pt;",
        "tot_border": "border-top: 2pt solid #222;",
        "sub_border": "",
    },
    "minimal": {
        # Odoo/DIN-5008 Look: 0.75pt schwarz Border-top auf Subtotal + Total, Border-bottom auf Total
        "wrap": "", "lbl": "color:#333;", "val": "",
        "tot_lbl": "font-weight:700; font-size:10.5pt;",
        "tot_val": "font-weight:700; font-size:10.5pt;",
        "tot_border": "border-top: 0.75pt solid #000; border-bottom: 0.75pt solid #000;",
        "sub_border": "border-top: 0.75pt solid #000;",
    },
    "boxed": {
        "wrap": "border:1pt solid #ddd; padding:2mm; border-radius:2mm; background:#fafafa;",
        "lbl": "color:#555;", "val": "",
        "tot_lbl": "font-weight:600;", "tot_val": "font-weight:600;",
        "tot_border": "border-top: 1pt solid #bbb;",
        "sub_border": "",
    },
}


def _build_totals_html(elem, margin_top="0", offset_x=0.0, offset_y=0.0, row_spacing=0.0):
    """Erzeugt den Totals-Block (Zwischensumme/Steuer/Gesamt) als HTML.

    Eine einzige Markup-Quelle, genutzt vom Inline-Table-Totals und vom
    eigenständigen Totals-Element. `margin_top` steuert den Abstand nach
    oben: "4mm" unter der Tabelle, "0" beim frei platzierten Element.

    `offset_x`/`offset_y` (mm) verschieben den GANZEN Block als Einheit relativ
    zu seiner Flussposition (position:relative) — die drei Zeilen bleiben fest
    zusammen. `row_spacing` (mm) erhöht den vertikalen Zeilenabstand additiv.
    Alle drei Defaults 0 = bisheriges Aussehen (Status quo). Reines
    margin/position/padding-CSS → identisch in WeasyPrint und wkhtmltopdf.
    """
    lbl_sub = getattr(elem, "totals_label_subtotal", "") or "Zwischensumme:"
    lbl_tax = getattr(elem, "totals_label_tax", "") or "Steuer:"
    lbl_tot = getattr(elem, "totals_label_total", "") or "Gesamt:"
    tot_style = getattr(elem, "totals_style", "default") or "default"
    s = TOTALS_STYLE_PRESETS.get(tot_style, TOTALS_STYLE_PRESETS["default"])

    # Versatz des gesamten Blocks (nur wenn ungleich 0 → sonst kein CSS = Status quo)
    pos_css = ""
    if (offset_x or 0) or (offset_y or 0):
        pos_css = f"position: relative; left: {offset_x or 0:g}mm; top: {offset_y or 0:g}mm;"

    # Zeilenabstand: additiv auf das vertikale Zell-Padding. Basis 1.4mm (Sub/Tax)
    # bzw. 1.8mm (Total) bleibt erhalten, wenn row_spacing == 0.
    rs = row_spacing or 0
    vp = 1.4 + rs
    vpt = 1.8 + rs

    return f"""
        <div class="ild-table-totals" style="margin-top: {margin_top}; {pos_css} {s['wrap']}">
            <table style="margin-left: auto;">
                <tr class="subtotal-line" style="{s['sub_border']}">
                    <td style="padding: {vp:g}mm 4mm; {s['lbl']}">{lbl_sub}</td>
                    <td style="padding: {vp:g}mm 4mm; text-align: right; {s['val']} {s['sub_border']}">
                        <span t-out="doc.amount_untaxed" t-options='{{"widget": "monetary", "display_currency": doc.currency_id}}'/>
                    </td>
                </tr>
                <tr>
                    <td style="padding: {vp:g}mm 4mm; {s['lbl']}">{lbl_tax}</td>
                    <td style="padding: {vp:g}mm 4mm; text-align: right; {s['val']}">
                        <span t-out="doc.amount_tax" t-options='{{"widget": "monetary", "display_currency": doc.currency_id}}'/>
                    </td>
                </tr>
                <tr class="total-line" style="{s['tot_border']}">
                    <td style="padding: {vpt:g}mm 4mm; {s['tot_lbl']} {s['tot_border']}">{lbl_tot}</td>
                    <td style="padding: {vpt:g}mm 4mm; text-align: right; {s['tot_val']} {s['tot_border']}">
                        <span t-out="doc.amount_total" t-options='{{"widget": "monetary", "display_currency": doc.currency_id}}'/>
                    </td>
                </tr>
            </table>
        </div>
        """


def _render_totals(elem, style_str, template=None):
    """Rendert den Totals-Block als eigenständiges, frei platziertes Element.

    Business: Der Anwender soll Zwischensumme/Steuer/Gesamt unabhängig von
    der Artikeltabelle frei auf dem Beleg positionieren können. Optik bleibt
    identisch zum bisherigen Inline-Block.
    """
    doc_type = field_registry.doc_type_to_model(template.doc_type) if template else "account.move"
    if doc_type not in ("account.move", "sale.order", "purchase.order"):
        _logger.info(
            "ILD totals: Belegtyp %s hat keine Summen — Platzhalter", doc_type
        )
        return (
            f'<div class="ild-element" style="{style_str}; color:#bbb; '
            f'font-style:italic;">[Summen nur für Rechnung/Angebot/Bestellung]</div>'
        )
    _logger.info("ILD totals: eigenständiges Totals-Element (doc_type=%s)", doc_type)
    # Standalone-Element: Position kommt aus pos_x/pos_y (style_str), daher kein
    # zusätzlicher Versatz; nur der Zeilenabstand wird durchgereicht.
    inner = _build_totals_html(
        elem, margin_top="0",
        row_spacing=getattr(elem, "totals_row_spacing", 0.0),
    )
    # Keep-Together (Punkt 2): der eigenständige Summenblock wird nie zerschnitten.
    return f'<div class="ild-element ild-totals-element" style="{style_str}; break-inside: avoid; page-break-inside: avoid;">{inner}</div>'


def _render_line(elem, style_str, template=None):
    border = f"{elem.line_width}pt {elem.line_style} {elem.line_color}"
    return f'<div class="ild-element" style="{style_str}; border-bottom: {border}; height: 0;"></div>'


def _render_vline(elem, style_str, template=None):
    """Render a vertical line."""
    border = f"{elem.line_width}pt {elem.line_style} {elem.line_color}"
    return f'<div class="ild-element" style="{style_str}; border-left: {border}; width: 0;"></div>'


def _render_shape(elem, style_str, template=None):
    # Structured shapes (opt-in via shape_use_structured): build CSS from the
    # discrete border/radius/fill fields. Native CSS border-style dashed/dotted
    # renders in both WeasyPrint and wkhtmltopdf (no object-fit involved).
    if elem.shape_use_structured:
        shape_css = elem.get_shape_css()
        return f'<div class="ild-element" style="{style_str}; {shape_css}"></div>'
    # Legacy fallback: render from the free style JSON exactly as before so
    # existing customer templates stay byte-identical.
    s = elem.get_style_data()
    bg = s.get("background-color", "transparent")
    border = s.get("border", "none")
    radius = s.get("border-radius", "0")
    return f'<div class="ild-element" style="{style_str}; background-color: {bg}; border: {border}; border-radius: {radius};"></div>'


def _render_container(elem, style_str, template=None):
    """Render a container/box element with optional column layout."""
    padding = f"{elem.container_padding}mm" if elem.container_padding else "0"
    bg = elem.container_bg_color or "transparent"
    border = elem.container_border or "none"
    radius = f"{elem.container_border_radius}mm" if elem.container_border_radius else "0"
    shadow = "box-shadow: 0 1mm 3mm rgba(0,0,0,0.12);" if elem.container_shadow else ""
    opacity = f"opacity: {elem.container_opacity};" if elem.container_opacity < 1.0 else ""

    container_style = (
        f"{style_str}; padding: {padding}; background-color: {bg}; "
        f"border: {border}; border-radius: {radius}; {shadow} {opacity}"
    )

    layout = elem.container_layout or "free"

    if layout == "free":
        return f'<div class="ild-element ild-container" style="{container_style}; position: relative;"></div>'
    elif layout == "columns_2":
        return (
            f'<div class="ild-element ild-container" style="{container_style}; display: flex; gap: 2mm;">'
            f'<div style="flex: 1;"></div>'
            f'<div style="flex: 1;"></div>'
            f'</div>'
        )
    elif layout == "columns_3":
        return (
            f'<div class="ild-element ild-container" style="{container_style}; display: flex; gap: 2mm;">'
            f'<div style="flex: 1;"></div>'
            f'<div style="flex: 1;"></div>'
            f'<div style="flex: 1;"></div>'
            f'</div>'
        )
    elif layout == "columns_2_left":
        return (
            f'<div class="ild-element ild-container" style="{container_style}; display: flex; gap: 2mm;">'
            f'<div style="flex: 2;"></div>'
            f'<div style="flex: 1;"></div>'
            f'</div>'
        )
    elif layout == "columns_2_right":
        return (
            f'<div class="ild-element ild-container" style="{container_style}; display: flex; gap: 2mm;">'
            f'<div style="flex: 1;"></div>'
            f'<div style="flex: 2;"></div>'
            f'</div>'
        )
    else:  # stack
        return f'<div class="ild-element ild-container" style="{container_style}; display: flex; flex-direction: column; gap: 1mm;"></div>'


def _render_barcode(elem, style_str, template=None):
    # Odoos /report/barcode/-Controller erwartet die Typ-Namen in exakter
    # Schreibweise (z.B. "QR", "Code128"). Die im Modell gespeicherten Kleinbuch-
    # staben-Werte ("qr", "code128") liefern sonst HTTP 500 -> leeres Bild.
    # swiss_qr (echter Swiss-QR-Bill mit Zahlungsdaten) ist über diesen Controller
    # nicht abbildbar -> vorerst als normaler QR des Feldwerts gerendert.
    barcode_type_map = {
        "code128": "Code128",
        "qr": "QR",
        "swiss_qr": "QR",
    }
    raw_type = elem.barcode_type or "code128"
    bc_type = barcode_type_map.get(raw_type, raw_type)
    field = elem.barcode_field

    if field:
        # KEIN url_encode: Diese Funktion ist im QWeb-Render-Kontext nicht
        # verfügbar (KeyError: 'url_encode') und ließ den GESAMTEN Beleg auf das
        # native Odoo-Layout zurückfallen. Belegnummern (z.B. RE/2026/00010)
        # enthalten keine query-brechenden Zeichen; der Wert wird direkt
        # eingesetzt. Bei Bedarf später serverseitig encodieren.
        return (
            f'<div class="ild-element" style="{style_str}">'
            f'<img t-att-src="doc.{field} and '
            f"'/report/barcode/?barcode_type={bc_type}&amp;value=%s&amp;width=400&amp;height=200' "
            f'% str(doc.{field})" '
            f'style="width: 100%; height: 100%;"/>'
            f'</div>'
        )

    value = elem.barcode_static_value or "PLACEHOLDER"
    return (
        f'<div class="ild-element" style="{style_str}">'
        f'<img src="/report/barcode/?barcode_type={bc_type}&amp;value={value}&amp;width=400&amp;height=200" '
        f'style="width: 100%; height: 100%;"/>'
        f'</div>'
    )