import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from .field_registry import doc_type_to_model

_logger = logging.getLogger(__name__)

# Marker, mit dem der QWeb-Export die verlustfreie Layout-Serialisierung
# als XML-Kommentar einbettet. Der Import-Wizard sucht exakt diesen Marker,
# um aus einer exportierten QWeb-XML wieder eine Vorlage aufzubauen.
ILD_LAYOUT_DATA_MARKER = "ILD-LAYOUT-DATA:"

DOC_TYPE_SELECTION = [
    ("account.move", "Invoice / Credit Note"),
    ("sale.order", "Quotation (Angebot)"),
    ("auftragsbestaetigung", "Order Confirmation (Auftragsbestätigung)"),
    ("stock.picking", "Delivery Slip"),
    ("purchase.order", "Purchase Order"),
]

PAPER_FORMATS = [
    ("A4", "A4 (210 × 297 mm)"),
    ("letter", "Letter (216 × 279 mm)"),
    ("A5", "A5 (148 × 210 mm)"),
]

PAPER_DIMENSIONS = {
    "A4": {"width": 210, "height": 297},
    "letter": {"width": 216, "height": 279},
    "A5": {"width": 148, "height": 210},
}

# Wählbare Layout-Stile. Die ersten vier sind die historischen Designer-
# Stile, die übrigen sieben bauen die Odoo-Standard-Report-Layouts nach
# (web/data/report_layout.xml), damit Kunden ihre aus Odoo gewohnte
# Optik im Designer wiederfinden und weiterbearbeiten können.
LAYOUT_STYLE_SELECTION = [
    ("din5008", "DIN 5008 (deutscher Geschäftsbrief)"),
    ("modern", "Modern"),
    ("classic", "Classic"),
    ("minimalist", "Minimalist"),
    ("light", "Odoo Light"),
    ("boxed", "Odoo Boxed"),
    ("bold", "Odoo Bold"),
    ("striped", "Odoo Striped"),
    ("bubble", "Odoo Bubble"),
    ("wave", "Odoo Wave"),
    ("folder", "Odoo Folder"),
]

# Stil-Konfiguration für die 7 nachgebauten Odoo-Layouts.
# header_variant steuert den Kopfaufbau, shape_variant die dekorativen
# Formen (Bubble-Kreise, Wellen-Band, Ordner-Lasche). use_company_colors
# brennt company.primary_color/secondary_color in die Formen ein.
ODOO_STYLE_CONFIGS = {
    "light": {
        "table_border_style": "horizontal",
        "table_zebra": False,
        "header_variant": "light",
        "uppercase_table_header": False,
        "shape_variant": None,
        "use_company_colors": False,
    },
    "boxed": {
        "table_border_style": "full",
        "table_zebra": False,
        "header_variant": "split",
        "uppercase_table_header": False,
        "shape_variant": None,
        "use_company_colors": False,
    },
    "bold": {
        "table_border_style": "bold",
        "table_zebra": False,
        "header_variant": "split",
        "uppercase_table_header": True,
        "shape_variant": None,
        "use_company_colors": False,
    },
    "striped": {
        "table_border_style": "horizontal",
        "table_zebra": True,
        "header_variant": "split",
        "uppercase_table_header": False,
        "shape_variant": None,
        "use_company_colors": False,
    },
    "bubble": {
        "table_border_style": "outer",
        "table_zebra": False,
        "header_variant": "split",
        "uppercase_table_header": False,
        "shape_variant": "bubble",
        "use_company_colors": True,
    },
    "wave": {
        "table_border_style": "horizontal",
        "table_zebra": True,
        "header_variant": "split",
        "uppercase_table_header": False,
        "shape_variant": "wave",
        "use_company_colors": True,
    },
    "folder": {
        "table_border_style": "horizontal",
        "table_zebra": True,
        "header_variant": "split",
        "uppercase_table_header": False,
        "shape_variant": "folder",
        "use_company_colors": True,
    },
}


def _hex_to_rgba(hex_color, alpha):
    """Wandelt '#RRGGBB' in 'rgba(r, g, b, a)' um.

    Nötig, weil die PDF-Engines (v.a. wkhtmltopdf) 8-stellige Hex-Farben
    mit Alpha-Kanal nicht zuverlässig rendern — rgba() funktioniert in
    beiden Engines.
    """
    value = (hex_color or "").lstrip("#")
    if len(value) != 6:
        _logger.warning("ILD: ungültige Hex-Farbe '%s', nutze Grau.", hex_color)
        return f"rgba(133, 149, 162, {alpha})"
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    return f"rgba({red}, {green}, {blue}, {alpha})"


class DocTypeConfig:
    """Hält die dokumentart-abhängigen Bausteine eines Layouts.

    Jeder Layout-Stil (Modern, Boxed, ...) soll für alle Dokumentarten
    funktionieren. Was sich je Dokumentart unterscheidet — Titel,
    Info-Felder im Kopf und Tabellenspalten — wird hier gebündelt,
    damit jeder Seeder dieselbe geprüfte Konfiguration verwendet.
    """

    def __init__(self, title, info_fields, table_cols, show_totals):
        self.title = title
        self.info_fields = info_fields
        self.table_cols = table_cols
        self.show_totals = show_totals


class DocumentLayoutTemplate(models.Model):
    _name = "document.layout.template"
    _inherit = ["mail.thread"]
    _description = "Document Layout Template"
    _order = "sequence, name"
    _rec_name = "name"

    name = fields.Char(
        string="Template Name",
        required=True,
        translate=True,
    )
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    doc_type = fields.Selection(
        selection=DOC_TYPE_SELECTION,
        string="Document Type",
        required=True,
        default="account.move",
    )
    # Only relevant when doc_type == "sale.order": distinguishes Angebot vs Auftragsbestätigung
    sale_state_filter = fields.Selection(
        [("all", "Beide (Angebot + Auftragsbestätigung)"),
         ("quotation", "Nur Angebot (draft / sent)"),
         ("confirmed", "Nur Auftragsbestätigung (sale / done)")],
        string="Für Sale-Order-Status",
        default="all",
        help="Bei doc_type = Sale Order: auf welchen Status soll dieses Template angewendet werden?",
    )

    # Dokument-Chain (Task 1): die Sale Order ist die Single Source of Truth für
    # HEADER und FOOTER. Verbundene Ziel-Templates (Rechnung, AB, Lieferschein,
    # Bestellung) verweisen per source_template_id auf das Sale-Order-Template
    # und erhalten dessen Header/Footer per Propagation (Task 2). Der Body bleibt
    # pro Doc-Type souverän — er wird NIE propagiert. Default leer = opt-in.
    source_template_id = fields.Many2one(
        "document.layout.template",
        string="Header/Footer-Quelle (Sale Order)",
        ondelete="set null",
        help="Verknüpft dieses Template mit einem Sale-Order-Basistemplate. "
             "Header und Footer (inkl. bg_header_image/bg_footer_image) werden "
             "von dort übernommen; der Body bleibt unberührt.",
    )
    linked_target_ids = fields.One2many(
        "document.layout.template",
        "source_template_id",
        string="Verknüpfte Ziel-Templates",
        help="Templates, die dieses (Sale-Order-)Template als Header/Footer-"
             "Quelle verwenden.",
    )

    @api.constrains("source_template_id", "doc_type")
    def _check_source_template(self):
        """Chain-Integrität: Quelle muss eine Sale Order sein, eine Sale Order
        darf selbst keine Quelle haben, kein Self-Link."""
        for rec in self:
            src = rec.source_template_id
            if not src:
                continue
            if src.id == rec.id:
                raise ValidationError(_("A template cannot link to itself."))
            if src.doc_type != "sale.order":
                raise ValidationError(_(
                    "The Header/Footer source must be a Sale Order (Angebot) "
                    "template. '%(name)s' has document type '%(dt)s'.",
                    name=src.name, dt=src.doc_type,
                ))
            if rec.doc_type == "sale.order":
                raise ValidationError(_(
                    "A Sale Order template is the chain source and cannot itself "
                    "link to another source template."
                ))

    def action_open_propagate_wizard(self):
        """Open the Header/Footer propagation dialog (source = this Sale Order)."""
        self.ensure_one()
        if self.doc_type != "sale.order":
            raise UserError(_(
                "Only Sale Order templates propagate Header/Footer to linked "
                "documents."))
        if not self.linked_target_ids:
            raise UserError(_(
                "This template has no linked target documents. Link documents "
                "via their 'Header/Footer source' field first."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Propagate Header / Footer"),
            "res_model": "document.layout.propagate.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_source_template_id": self.id,
                "default_target_ids": [(6, 0, self.linked_target_ids.ids)],
            },
        }

    def _propagate_header_footer_to(self, target):
        """Copy HEADER + FOOTER zone elements and header/footer backgrounds from
        this sale.order base template into ``target``.

        HARD CONTRACT (live customer): the target's BODY zone is never read,
        changed or deleted. Only ``zone in ('header','footer')`` is touched.
        One-way only (base -> target); bg_page_image (body) is NOT propagated.
        """
        self.ensure_one()
        if self.doc_type != "sale.order":
            raise UserError(_(
                "Header/Footer propagation can only originate from a Sale Order "
                "template."))
        if target.doc_type == "sale.order":
            raise UserError(_("Cannot propagate into another Sale Order template."))
        # Safety: only ever mutate a genuine linked target of THIS source.
        if target.source_template_id.id != self.id:
            raise UserError(_(
                "Template '%(t)s' is not linked to '%(s)s' as its Header/Footer "
                "source.", t=target.name, s=self.name))

        # Diagnostic only: literal copy-all (per Tom) will replace any field-bound
        # header/footer elements the target may carry (e.g. Modern-style invoice
        # number lives in the header zone). DIN5008 targets keep such fields in
        # the body, so this stays empty there.
        risky = target.element_ids.filtered(
            lambda e: e.zone in ("header", "footer") and e.element_type == "field")
        if risky:
            _logger.warning(
                "ILD propagate: target '%s' has %d field-bound header/footer "
                "element(s) that the copy-all will replace (non-DIN5008 style?).",
                target.name, len(risky))

        body_before = len(target.element_ids.filtered(lambda e: e.zone == "body"))

        # 1. Remove the target's existing header/footer elements (NEVER body).
        target.element_ids.filtered(
            lambda e: e.zone in ("header", "footer")).unlink()

        # 2. Clone the Sale Order's header/footer elements into the target.
        #    is_user_created=True so the propagated chrome is never treated as a
        #    deletable ghost on the target.
        src_hf = self.element_ids.filtered(
            lambda e: e.zone in ("header", "footer"))
        for elem in src_hf:
            elem.copy({"template_id": target.id, "is_user_created": True})

        # 3. Copy header/footer backgrounds (body background stays the target's).
        target.write({
            "bg_header_image": self.bg_header_image,
            "bg_footer_image": self.bg_footer_image,
        })

        # 4. Invariant: body must be byte-for-byte untouched.
        body_after = len(target.element_ids.filtered(lambda e: e.zone == "body"))
        if body_before != body_after:
            raise UserError(_(
                "Internal error: target body element count changed during "
                "Header/Footer propagation (%(b)s -> %(a)s). Aborted.",
                b=body_before, a=body_after))
        _logger.info(
            "ILD propagate: '%s' -> '%s' OK (%d header/footer elements copied, "
            "body %d unchanged).", self.name, target.name, len(src_hf), body_after)
    paper_format = fields.Selection(
        selection=PAPER_FORMATS,
        string="Paper Format",
        default="A4",
        required=True,
    )
    layout_style = fields.Selection(
        selection=LAYOUT_STYLE_SELECTION,
        string="Layout Style",
        required=True,
        default="din5008",
        help="Vorlagen-Optik dieses Templates. Beim Anlegen wird das "
             "Layout automatisch mit Elementen in diesem Stil vorbefüllt. "
             "Bei bestehenden Templates wird ein Stil-Wechsel erst durch "
             "'Reset to Default' angewendet (löscht eigene Anpassungen!).",
    )

    # Layout JSON — the heart of the template
    layout_json = fields.Text(
        string="Layout Definition (JSON)",
        default="{}",
        help="JSON structure containing all element positions, sizes, and styles.",
    )

    # Page margins in mm
    margin_top = fields.Float(string="Top Margin (mm)", default=15.0)
    margin_bottom = fields.Float(string="Bottom Margin (mm)", default=15.0)
    margin_left = fields.Float(string="Left Margin (mm)", default=15.0)
    margin_right = fields.Float(string="Right Margin (mm)", default=15.0)

    # Link to Odoo Paperformat for margin sync
    paperformat_id = fields.Many2one(
        "report.paperformat",
        string="Odoo Paperformat",
        help="Optional: Select an Odoo paperformat to sync margins and paper size. "
             "Leave empty to use the custom margins above.",
    )

    # Header/Footer heights
    header_height = fields.Float(string="Header Height (mm)", default=35.0)
    footer_height = fields.Float(string="Footer Height (mm)", default=25.0)

    # Odoo-Standard-Belegbarcode: optionaler Code128 der Belegnummer oben rechts,
    # analog zu Odoos Standard-Reports (z.B. Lieferschein-Operationen). Per
    # Belegtyp/Template aktivierbar; der Transpiler injiziert den Barcode beim
    # Rendern, damit kein manuelles Element nötig ist.
    show_odoo_barcode = fields.Boolean(
        string="Odoo-Standard-Barcode anzeigen",
        default=False,
        help="Zeigt einen Code128-Barcode der Belegnummer oben rechts, wie in "
             "Odoos Standard-Belegen.",
    )

    # Relations
    element_ids = fields.One2many(
        "document.layout.element",
        "template_id",
        string="Layout Elements",
        # Odoo setzt für One2many standardmäßig copy=False. Beim Duplizieren
        # eines Templates (action_duplicate_template) sollen die Layout-
        # Elemente aber mitkopiert werden — sonst entsteht eine leere Kopie.
        copy=True,
    )
    style_id = fields.Many2one(
        "document.layout.style",
        string="Base Style",
        ondelete="set null",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
        required=True,
    )

    # Metadata
    is_default = fields.Boolean(
        string="Default Template",
        default=False,
        help="If checked, this template will be used as default for its document type.",
    )
    preview_image = fields.Binary(
        string="Preview Image",
        attachment=True,
    )
    description = fields.Text(
        string="Description",
        translate=True,
    )
    tag_ids = fields.Many2many(
        "document.layout.tag",
        string="Tags",
    )

    # Briefpapier / Background
    background_image = fields.Binary(
        string="Background Image / Letterhead",
        attachment=True,
        help="Upload a full-page background image (PNG/JPG) or scanned letterhead. "
             "It will be placed behind all content on every page.",
    )
    background_mode = fields.Selection([
        ("none", "No background"),
        ("image", "Background image (full page)"),
        ("header_only", "Header area only"),
    ], string="Background Mode", default="none",
        help="Legacy single-image background. Superseded by the separate "
             "Header / Footer / Page background fields below; kept for "
             "backward compatibility and migrated automatically.")

    # Getrennte Hintergrundbilder pro Bereich (Task 1). Jedes Feld ist optional;
    # leere Felder => kein Hintergrund in diesem Bereich. Das Rendering bindet
    # sie als fixe, pro Seite wiederholte Bänder ein (siehe report_override).
    bg_header_image = fields.Binary(
        string="Header Background",
        attachment=True,
        help="Background image for the header band (top of every page). "
             "Height follows the template's header height.",
    )
    bg_footer_image = fields.Binary(
        string="Footer Background",
        attachment=True,
        help="Background image for the footer band (bottom of every page). "
             "Height follows the template's footer height.",
    )
    bg_page_image = fields.Binary(
        string="Page Background",
        attachment=True,
        help="Full-page background image, repeated behind the content area "
             "of every page.",
    )
    bg_fit = fields.Selection([
        ("contain", "Contain (fit fully, no crop, may letterbox)"),
        ("cover", "Cover (fill area, may crop edges)"),
        ("fill", "Stretch (fill exactly, may distort)"),
    ], string="Background Scaling", default="contain",
        help="How background images scale into their area. "
             "Contain = no cropping and no distortion (recommended).")

    # Computed
    element_count = fields.Integer(
        compute="_compute_element_count",
        string="Elements",
    )
    paper_width = fields.Float(
        compute="_compute_paper_dimensions",
        string="Paper Width (mm)",
    )
    paper_height = fields.Float(
        compute="_compute_paper_dimensions",
        string="Paper Height (mm)",
    )

    @api.depends("element_ids")
    def _compute_element_count(self):
        for rec in self:
            rec.element_count = len(rec.element_ids)

    @api.depends("paper_format")
    def _compute_paper_dimensions(self):
        for rec in self:
            dims = PAPER_DIMENSIONS.get(rec.paper_format, PAPER_DIMENSIONS["A4"])
            rec.paper_width = dims["width"]
            rec.paper_height = dims["height"]

    @api.onchange("paperformat_id")
    def _onchange_paperformat_id(self):
        """Sync margins from selected Odoo paperformat."""
        if self.paperformat_id:
            pf = self.paperformat_id
            self.margin_top = pf.margin_top or 0
            self.margin_bottom = pf.margin_bottom or 0
            self.margin_left = pf.margin_left or 0
            self.margin_right = pf.margin_right or 0

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._deactivate_other_defaults()
        records._auto_seed_on_create()
        return records

    def _auto_seed_on_create(self):
        """Befüllt frisch angelegte Templates automatisch im gewählten Stil.

        Ein neues Template soll sofort ein fertiges, bearbeitbares Layout
        zeigen — statt einer leeren Fläche. Übersprungen wird:
        - XML-/Modul-Installation (install_mode): dort liefern die
          Daten-Dateien ihre Elemente selbst, der post_init_hook seedet
          den Rest gezielt nach.
        - Aufrufer mit eigenem Element-Aufbau (Wizard, Import) via
          Context-Flag ild_skip_auto_seed.
        - Templates, die bereits Elemente haben (z.B. Duplikate, deren
          element_ids mitkopiert werden).
        """
        if self.env.context.get("install_mode"):
            _logger.info("ILD auto-seed: install_mode aktiv, überspringe %d Template(s).", len(self))
            return
        if self.env.context.get("ild_skip_auto_seed"):
            _logger.info("ILD auto-seed: ild_skip_auto_seed gesetzt, überspringe %d Template(s).", len(self))
            return
        for tpl in self:
            if tpl.element_ids:
                _logger.info(
                    "ILD auto-seed: Template '%s' hat bereits %d Elemente (Kopie/Import), überspringe.",
                    tpl.name, len(tpl.element_ids),
                )
                continue
            _logger.info(
                "ILD auto-seed: befülle Template '%s' (doc_type=%s) im Stil '%s'.",
                tpl.name, tpl.doc_type, tpl.layout_style,
            )
            tpl._apply_layout_style()

    def write(self, vals):
        # layout_style-Wechsel VOR dem super()-write erfassen: Der Stil ist die
        # komplette Optik einer Vorlage. Wählt der Nutzer im Formular einen
        # anderen Stil, erwartet er, dass die PDF danach auch so aussieht. Ohne
        # Neuaufbau blieben die bereits gespeicherten Elemente bestehen und die
        # PDF sähe unabhängig vom gewählten Stil immer gleich aus.
        templates_new_style = self.browse()
        if "layout_style" in vals:
            for tpl in self:
                if tpl.layout_style != vals["layout_style"]:
                    templates_new_style |= tpl

        result = super().write(vals)

        if "is_default" in vals and vals["is_default"]:
            self._deactivate_other_defaults()

        for tpl in templates_new_style:
            _logger.info(
                "ILD: layout_style von Template '%s' geändert auf '%s' -> "
                "Elemente werden im neuen Stil neu aufgebaut.",
                tpl.name, tpl.layout_style,
            )
            tpl._rebuild_layout_elements()

        return result

    @api.onchange("layout_style")
    def _onchange_layout_style_warn_reset(self):
        """Warnt im Formular, bevor ein Stil-Wechsel eigene Anpassungen
        verwirft.

        Hat der Nutzer die Vorlage im Editor bereits angepasst (Elemente mit
        is_user_created=True), würde der automatische Neuaufbau beim Speichern
        diese Anpassungen löschen. Damit das nicht unbemerkt passiert, erscheint
        beim Umschalten des Stils eine Warnung. Der Neuaufbau selbst geschieht
        erst beim Speichern in write().
        """
        self.ensure_one()

        user_element_count = 0
        for element in self.element_ids:
            if element.is_user_created:
                user_element_count += 1

        if not user_element_count:
            _logger.info(
                "ILD: Stil-Wechsel bei '%s' ohne eigene Anpassungen, "
                "keine Warnung nötig.",
                self.name,
            )
            return

        _logger.info(
            "ILD: Stil-Wechsel bei '%s' mit %d eigenen Element(en) -> "
            "Warnung wird angezeigt.",
            self.name, user_element_count,
        )
        return {
            "warning": {
                "title": _("Eigene Anpassungen gehen verloren"),
                "message": _(
                    "Diese Vorlage enthält selbst bearbeitete Elemente. "
                    "Beim Speichern wird das Layout im neuen Stil komplett "
                    "neu aufgebaut — deine Anpassungen werden dabei "
                    "zurückgesetzt."
                ),
            }
        }

    def _deactivate_other_defaults(self):
        """Ensure only one default template per doc_type + company + Status-Scope.

        Bei sale.order gibt es zustandsspezifische Templates (Angebot vs.
        Auftragsbestätigung). Diese sollen NEBENEINANDER Default sein dürfen,
        damit beim Druck das passende Template je Verkaufsstatus gewählt werden
        kann. Deshalb wird nur ein bestehender Default mit DEMSELBEN
        sale_state_filter deaktiviert, nicht alle. Für andere doc_types ist
        sale_state_filter immer 'all' — dort bleibt das Verhalten unverändert
        (genau ein Default pro doc_type + company).
        """
        for rec in self:
            if rec.is_default:
                others = self.search([
                    ("id", "!=", rec.id),
                    ("doc_type", "=", rec.doc_type),
                    ("company_id", "=", rec.company_id.id),
                    ("sale_state_filter", "=", rec.sale_state_filter),
                    ("is_default", "=", True),
                ])
                if others:
                    others.with_context(skip_default_deactivation=True).write(
                        {"is_default": False}
                    )

    @api.constrains("margin_top", "margin_bottom", "margin_left", "margin_right")
    def _check_margins(self):
        for rec in self:
            for field_name in ("margin_top", "margin_bottom", "margin_left", "margin_right"):
                val = getattr(rec, field_name)
                if val < 0 or val > 50:
                    raise ValidationError(
                        _("Margins must be between 0 and 50 mm.")
                    )

    def get_layout_data(self):
        """Return parsed layout JSON."""
        self.ensure_one()
        try:
            return json.loads(self.layout_json or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_layout_data(self, data):
        """Store layout data as JSON."""
        self.ensure_one()
        self.layout_json = json.dumps(data, ensure_ascii=False, indent=2)

    def action_open_editor(self):
        """Open the WYSIWYG layout editor."""
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "invoice_layout_designer.layout_editor",
            "name": _("Layout Editor: %s") % self.name,
            "target": "current",
            "params": {
                "template_id": self.id,
            },
            "views": [[False, "form"]],
        }

    def action_preview_pdf(self):
        """Generate a preview PDF with sample data."""
        self.ensure_one()
        sample = self._get_sample_record()
        if not sample:
            raise UserError(
                _("No sample %s record found for preview.") % self.doc_type
            )
        # Use the native report action per doc_type so _find_custom_template
        # receives the correct report.model and picks the right template.
        report_ref_map = {
            "account.move": "invoice_layout_designer.action_report_custom_document",
            "purchase.order": "purchase.action_report_purchase_order",
            "sale.order": "sale.action_report_saleorder",
            "auftragsbestaetigung": "sale.action_report_saleorder",
            "stock.picking": "stock.action_report_delivery",
        }
        report_ref = report_ref_map.get(
            self.doc_type,
            "invoice_layout_designer.action_report_custom_document",
        )
        action = self.env.ref(report_ref).report_action(sample)
        # Die Vorschau muss GENAU dieses Template zeigen, nicht das Default-
        # Template des doc_type. Ohne diese Erzwingung wählt
        # _find_custom_template anhand is_default und zeigt immer dasselbe
        # Layout, egal welches Template der Nutzer geöffnet hat.
        forced_context = {"ild_force_template_id": self.id}
        existing_context = action.get("context") or {}
        if isinstance(existing_context, dict):
            forced_context = {**existing_context, **forced_context}
        action["context"] = forced_context
        return action

    def _get_sample_record(self):
        """Find a good sample record of the document type for preview.

        Prefers recent, confirmed/posted records with line items for a
        more realistic preview experience.
        """
        self.ensure_one()
        model = doc_type_to_model(self.doc_type)
        Model = self.env[model]

        if model == "account.move":
            # Prefer a posted invoice with lines
            record = Model.search(
                [
                    ("move_type", "in", ("out_invoice", "out_refund")),
                    ("state", "=", "posted"),
                    ("invoice_line_ids", "!=", False),
                ],
                limit=1,
                order="invoice_date desc, id desc",
            )
            if not record:
                # Fallback: any invoice
                record = Model.search(
                    [("move_type", "in", ("out_invoice", "out_refund"))],
                    limit=1,
                    order="id desc",
                )
            return record

        elif model == "sale.order":
            record = Model.search(
                [("state", "in", ("sale", "done")), ("order_line", "!=", False)],
                limit=1, order="date_order desc, id desc",
            )
            if not record:
                record = Model.search([], limit=1, order="id desc")
            return record

        elif model == "purchase.order":
            record = Model.search(
                [("state", "in", ("purchase", "done")), ("order_line", "!=", False)],
                limit=1, order="date_order desc, id desc",
            )
            if not record:
                record = Model.search([], limit=1, order="id desc")
            return record

        # stock.picking and others: most recent
        return Model.search([], limit=1, order="id desc")

    def action_duplicate_template(self):
        """Duplicate this template."""
        self.ensure_one()
        new_template = self.copy(
            default={"name": _("%s (Copy)") % self.name, "is_default": False}
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "document.layout.template",
            "res_id": new_template.id,
            "view_mode": "form",
        }

    def action_open_reset_ghost_wizard(self):
        """Open the 'Clean Ghost Elements' wizard (non-destructive variant).

        Unlike action_reset_to_default (which wipes EVERYTHING), this wizard
        only removes orphaned ghost elements — items without xml_id AND
        without is_user_created=True. User-customized and XML-default
        elements are always preserved.
        """
        self.ensure_one()
        return {
            "name": _("Reset to Default Layout"),
            "type": "ir.actions.act_window",
            "res_model": "document.layout.reset.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"active_id": self.id, "active_model": self._name},
        }

    @api.model
    def _get_style_seeders(self):
        """Registry: layout_style → Seeder-Methode.

        Zentrale Stelle, an der ein Stil-Schlüssel auf den Code trifft,
        der die passenden Layout-Elemente erzeugt. Neue Stile werden hier
        eingetragen — Auswahlfeld (LAYOUT_STYLE_SELECTION) und Registry
        müssen deckungsgleich sein.
        """
        return {
            "din5008": self._seed_style_din5008,
            "modern": self._create_modern_elements,
            "classic": self._create_classic_elements,
            "minimalist": self._create_minimalist_elements,
            "light": self._seed_style_light,
            "boxed": self._seed_style_boxed,
            "bold": self._seed_style_bold,
            "striped": self._seed_style_striped,
            "bubble": self._seed_style_bubble,
            "wave": self._seed_style_wave,
            "folder": self._seed_style_folder,
        }

    def _apply_layout_style(self):
        """Erzeugt die Elemente des aktuell gewählten layout_style.

        Defensive Absicherung: Alt-Datensätze ohne Stil fallen mit
        Warn-Log auf DIN 5008 zurück, damit ein Reset nie ins Leere läuft.
        """
        self.ensure_one()
        style = self.layout_style

        # ALLE Stile (auch die Odoo-Stile) werden als positionierte, im Editor
        # verschiebbare Elemente aufgebaut. So sieht der Nutzer beim Öffnen des
        # Layout-Editors die Standard-Bestandteile als Boxen und kann sie frei
        # bearbeiten — und das gedruckte PDF entspricht genau dem Editor (WYSIWYG).
        seeders = self._get_style_seeders()
        if style not in seeders:
            _logger.warning(
                "ILD: Template '%s' hat unbekannten layout_style '%s', Fallback auf din5008.",
                self.name, style,
            )
            style = "din5008"
        else:
            _logger.info(
                "ILD: Template '%s' (doc_type=%s) wird im Stil '%s' aufgebaut.",
                self.name, self.doc_type, style,
            )
        seeder = seeders[style]
        seeder()

    def _seed_style_din5008(self):
        """DIN 5008 hat historisch zwei Seeder: sale.order braucht eigene
        Feldpfade und Titel (Angebot/Auftragsbestätigung)."""
        if doc_type_to_model(self.doc_type) == "sale.order":
            _logger.info("ILD: DIN5008-Sale-Seeder für '%s'.", self.name)
            self._create_din5008_sale_elements()
        else:
            _logger.info("ILD: DIN5008-Standard-Seeder für '%s' (%s).", self.name, self.doc_type)
            self._create_din5008_elements()

    def _seed_style_light(self):
        self._create_odoo_style_elements("light")

    def _seed_style_boxed(self):
        self._create_odoo_style_elements("boxed")

    def _seed_style_bold(self):
        self._create_odoo_style_elements("bold")

    def _seed_style_striped(self):
        self._create_odoo_style_elements("striped")

    def _seed_style_bubble(self):
        self._create_odoo_style_elements("bubble")

    def _seed_style_wave(self):
        self._create_odoo_style_elements("wave")

    def _seed_style_folder(self):
        self._create_odoo_style_elements("folder")

    def _seed_default_templates_if_empty(self):
        """Idempotent seeder: every default+active template that has no
        elements receives a block set matching its layout_style + doc_type.

        Called from post_init_hook so fresh installs end up with a fully
        prefilled layout per supported document type — invoice, quotation,
        confirmed sale, delivery slip and purchase order — instead of empty
        skeletons. Re-running is safe: templates that already have at least
        one element are skipped.
        """
        templates = self.search([
            ("is_default", "=", True),
            ("active", "=", True),
        ])
        for tpl in templates:
            if tpl.element_ids:
                _logger.info(
                    "ILD seed: Template '%s' (id=%s, %s) hat bereits %d Elemente, überspringe.",
                    tpl.name, tpl.id, tpl.doc_type, len(tpl.element_ids),
                )
                continue
            tpl._apply_layout_style()

    def get_coverage_hint(self):
        """Business: Ein Layout deckt meist mehr Belegarten ab als der Name
        vermuten lässt. Eine Rechnungsvorlage gilt z.B. automatisch auch für
        Lieferantenrechnungen und Gutschriften, eine Angebotsvorlage je nach
        Einstellung auch für Auftragsbestätigungen. Damit der Nutzer nach dem
        Speichern nicht fälschlich glaubt, er müsse pro Belegart ein eigenes
        Layout pflegen, liefert diese Methode einen erklärenden Hinweistext.
        Leerer String = keine zusätzliche Abdeckung (nur eine Belegart)."""
        self.ensure_one()

        if self.doc_type == "account.move":
            _logger.info(
                "ILD coverage: Template '%s' deckt alle Rechnungstypen ab.",
                self.name,
            )
            return _(
                "Dieses Layout wird für alle Rechnungstypen verwendet: "
                "Kunden- und Lieferantenrechnungen sowie Gutschriften."
            )

        if self.doc_type == "auftragsbestaetigung":
            _logger.info(
                "ILD coverage: Template '%s' nur Auftragsbestätigungen.",
                self.name,
            )
            return _(
                "Dieses Layout wird für bestätigte Aufträge "
                "(Auftragsbestätigungen) verwendet."
            )

        if self.doc_type == "sale.order":
            if self.sale_state_filter == "confirmed":
                _logger.info(
                    "ILD coverage: Template '%s' (legacy) nur Auftragsbestätigungen.",
                    self.name,
                )
                return _(
                    "Dieses Layout wird nur für Auftragsbestätigungen verwendet."
                )
            _logger.info(
                "ILD coverage: Template '%s' für Angebote.", self.name,
            )
            return _("Dieses Layout wird für Angebote verwendet.")

        if self.doc_type == "purchase.order":
            _logger.info(
                "ILD coverage: Template '%s' nur Bestellungen.", self.name,
            )
            return _(
                "Dieses Layout wird nur für Bestellungen verwendet "
                "(keine Angebotsanfragen / RFQ)."
            )

        _logger.info(
            "ILD coverage: Template '%s' (doc_type=%s) deckt nur eine "
            "Belegart ab, kein Hinweis nötig.",
            self.name, self.doc_type,
        )
        return ""

    def _is_order_confirmation(self):
        """True if this sale-based template targets confirmed orders (AB).

        Covers both the new dedicated doc_type 'auftragsbestaetigung' and the
        legacy sale.order template with sale_state_filter='confirmed'.
        """
        self.ensure_one()
        return (
            self.doc_type == "auftragsbestaetigung"
            or self.sale_state_filter == "confirmed"
        )

    def action_reset_to_default(self):
        """Reset template elements to their default (original) state.

        Rebuilds the elements in the style selected in layout_style.
        Also cleans up cached dynamic QWeb views.

        WARNING: destructive. Wipes ALL elements including user customizations.
        For a safer variant see action_open_reset_ghost_wizard().
        """
        self.ensure_one()
        self._rebuild_layout_elements()
        return True

    def _rebuild_layout_elements(self):
        """Baut die Elemente einer Vorlage anhand des aktuell gewählten
        layout_style komplett neu auf. Gemeinsame Basis für den manuellen
        Reset (Button) und den automatischen Neuaufbau bei Stil-Wechsel im
        Formular.

        ACHTUNG: destruktiv — vorhandene (auch nutzerangepasste) Elemente
        werden entfernt, da ein Stil-Wechsel die gesamte Optik ersetzt.
        Zusätzlich werden die zwischengespeicherten QWeb-Views (Editor-
        Vorschau und WeasyPrint-Render) verworfen, damit Vorschau und PDF
        sofort den neuen Stand zeigen.
        """
        self.ensure_one()
        self.element_ids.unlink()

        tpl_key = f"invoice_layout_designer.dynamic_layout_{self.id}"
        preview_key = f"invoice_layout_designer.preview_{self.id}"
        wp_key = f"invoice_layout_designer.wp_render_{self.id}"
        old_views = self.env["ir.ui.view"].sudo().search([
            ("key", "in", [tpl_key, preview_key, wp_key]),
            ("type", "=", "qweb"),
        ])
        if old_views:
            _logger.info(
                "ILD: Verwerfe %d zwischengespeicherte View(s) für Template '%s'.",
                len(old_views), self.name,
            )
            old_views.sudo().unlink()

        # Der Stil kommt aus dem layout_style-Feld — der Neuaufbau erzeugt
        # genau die Optik, die im Formular ausgewählt ist.
        self._apply_layout_style()

    def _create_din5008_elements(self):
        """Create a full set of DIN 5008 elements for this template.

        Typography: Base 8.5pt, light weight, professional.
        Spacing: DIN 5008 Form B compliant positions.
        """
        E = self.env["document.layout.element"]
        t = self.id
        cw = self.paper_width - self.margin_left - self.margin_right

        # ── HEADER ──
        E.create({"template_id": t, "name": "Company Logo", "element_type": "image",
                  "zone": "header", "sequence": 10,
                  "pos_x": 0, "pos_y": 0, "width": 45, "height": 18,
                  "image_source": "company_logo", "image_fit": "contain"})
        E.create({"template_id": t, "name": "Company Address", "element_type": "text",
                  "zone": "header", "sequence": 15,
                  "pos_x": cw - 65, "pos_y": 0, "width": 65, "height": 18,
                  "text_align": "right",
                  "text_content": '<div style="font-size:7pt; color:#777; line-height:1.5; font-weight:300;">[company_name]<br/>[street]<br/>[zip] [city]<br/>Tel: [phone]</div>'})
        # Header-Trennlinie
        E.create({"template_id": t, "name": "Header Line", "element_type": "line",
                  "zone": "header", "sequence": 20,
                  "pos_x": 0, "pos_y": 20, "width": cw, "height": 0.3,
                  "line_color": "#cccccc", "line_width": 0.3})

        # ── BODY: Faltmarken + Lochmarke ──
        E.create({"template_id": t, "name": "Faltmarke 1", "element_type": "line",
                  "zone": "body", "sequence": 5,
                  "pos_x": -self.margin_left, "pos_y": 42, "width": 4, "height": 0.5,
                  "line_color": "#bbbbbb", "line_width": 0.25})
        E.create({"template_id": t, "name": "Lochmarke", "element_type": "line",
                  "zone": "body", "sequence": 6,
                  "pos_x": -self.margin_left, "pos_y": 103.5, "width": 6, "height": 0.5,
                  "line_color": "#bbbbbb", "line_width": 0.25})
        E.create({"template_id": t, "name": "Faltmarke 2", "element_type": "line",
                  "zone": "body", "sequence": 7,
                  "pos_x": -self.margin_left, "pos_y": 147, "width": 4, "height": 0.5,
                  "line_color": "#bbbbbb", "line_width": 0.25})

        # ── Rücksendezeile ──
        E.create({"template_id": t, "name": "Rücksendezeile", "element_type": "field",
                  "zone": "body", "sequence": 20,
                  "pos_x": 0, "pos_y": 0, "width": 85, "height": 3.5,
                  "field_name": "company_id.name",
                  "field_default": "",
                  "style_json": '{"font-size": "6pt", "color": "#999", "text-decoration": "underline", "font-weight": "300"}'})

        # ── Kundenadresse ──
        for i, (name, field, default, y, style) in enumerate([
            ("Kundenname", "partner_id.display_name", "", 5, '{"font-size": "8.5pt", "font-weight": "400"}'),
            ("Kundenstrasse", "partner_id.street", "", 9.5, '{"font-size": "8.5pt", "font-weight": "300"}'),
            ("Kunden PLZ/Ort", "partner_id.zip", "", 13.5, '{"font-size": "8.5pt", "font-weight": "300"}'),
            ("Kundenland", "partner_id.country_id.name", "", 17.5, '{"font-size": "8.5pt", "font-weight": "300"}'),
        ]):
            E.create({"template_id": t, "name": name, "element_type": "field",
                      "zone": "body", "sequence": 25 + i,
                      "pos_x": 0, "pos_y": y, "width": 85, "height": 4,
                      "field_name": field, "field_default": default,
                      "style_json": style})

        # ── Informationsblock (rechts) ──
        info_x = cw - 60
        # DIN-5008-Betreffzeile: linksbündig am linken Textrand, unterhalb von
        # Anschrift und Infoblock, oberhalb der Positionstabelle — wie im
        # normalen DIN-5008-Layout (nicht fett, dezente Größe).
        E.create({"template_id": t, "name": "Dokumenttitel", "element_type": "text",
                  "zone": "body", "sequence": 40,
                  "pos_x": 0, "pos_y": 28, "width": 120, "height": 8,
                  "text_content": '<span style="font-size: 14pt; font-weight: 400;">Rechnung</span>'})

        # Labels with real UTF-8 characters (stored correctly in DB)
        labels_and_fields = [
            ("Rechnungsnr.:", "name", "", 8),
            ("Datum:", "invoice_date", "", 12),
            ("Zahlbar bis:", "invoice_date_due", "", 16),
            ("Sachbearbeiter:", "user_id.name", "", 20),
        ]
        seq = 41
        for label, field, default, y_off in labels_and_fields:
            E.create({"template_id": t, "name": label.replace(":", ""), "element_type": "text",
                      "zone": "body", "sequence": seq,
                      "pos_x": info_x, "pos_y": y_off, "width": 28, "height": 3.5,
                      "text_content": f'<span style="font-size:7.5pt; color:#777; font-weight:300;">{label}</span>'})
            seq += 1
            E.create({"template_id": t, "name": label.replace(":", "") + " Value", "element_type": "field",
                      "zone": "body", "sequence": seq,
                      "pos_x": info_x + 30, "pos_y": y_off, "width": 30, "height": 3.5,
                      "field_name": field, "field_default": default,
                      "style_json": '{"font-size": "7.5pt", "font-weight": "300"}'})
            seq += 1

        # ── Trennlinie vor Tabelle ──
        E.create({"template_id": t, "name": "Table Separator", "element_type": "line",
                  "zone": "body", "sequence": 58,
                  "pos_x": 0, "pos_y": 40, "width": cw, "height": 0.3,
                  "line_color": "#dddddd", "line_width": 0.25})

        # ── Positionstabelle ──
        table_cols = json.dumps([
            {"field": "sequence", "label": "Pos.", "width": "5%", "align": "center"},
            {"field": "name", "label": "Bezeichnung", "width": "38%", "align": "left"},
            {"field": "quantity", "label": "Menge", "width": "8%", "align": "right"},
            {"field": "product_uom_id.name", "label": "Einheit", "width": "9%", "align": "center"},
            {"field": "price_unit", "label": "Einzelpreis", "width": "13%", "align": "right", "type": "monetary"},
            {"field": "discount", "label": "Rabatt", "width": "7%", "align": "right"},
            {"field": "tax_ids", "label": "USt.", "width": "8%", "align": "right"},
            {"field": "price_subtotal", "label": "Betrag", "width": "12%", "align": "right", "type": "monetary"},
        ])
        E.create({"template_id": t, "name": "Positionstabelle", "element_type": "table",
                  "zone": "body", "sequence": 60,
                  "pos_x": 0, "pos_y": 42, "width": cw, "height": 80,
                  "table_columns_json": table_cols,
                  "table_show_header": True, "table_show_totals": True,
                  "table_zebra": False, "table_border_style": "horizontal",
                  "table_rows_per_page": 25,
                  # Rechnung (account.move) hat keine optionalen Posten → "hide".
                  "table_optional_mode": "hide",
                  "style_json": '{"font-size": "8pt", "font-weight": "300"}'})

        # ── Dankestext (fließt direkt unter der Tabelle nach, überlappt nie) ──
        E.create({"template_id": t, "name": "Dankestext", "element_type": "text",
                  "zone": "body", "sequence": 70,
                  "pos_x": 0, "pos_y": 120, "width": 100, "height": 12,
                  "anchor_mode": "after_table",
                  "text_content": '<div style="font-size:8pt; line-height:1.5; color:#444; font-weight:300;">Vielen Dank für Ihren Auftrag.<br/><br/>Mit freundlichen Grüßen</div>'})

        # ── FOOTER ──
        E.create({"template_id": t, "name": "Footer Linie", "element_type": "line",
                  "zone": "footer", "sequence": 5,
                  "pos_x": 0, "pos_y": 0, "width": cw, "height": 0.3,
                  "line_color": "#cccccc", "line_width": 0.3})

        col_w = cw / 4
        footer_cols = [
            ("Footer Firma", 0,
             '<div style="font-size:6pt; color:#777; line-height:1.4; font-weight:300;"><span style="font-weight:400; color:#444;">Firma</span><br/>[company_name]<br/>[street]<br/>[zip] [city]</div>'),
            ("Footer Kontakt", col_w,
             '<div style="font-size:6pt; color:#777; line-height:1.4; font-weight:300;"><span style="font-weight:400; color:#444;">Kontakt</span><br/>Tel: [phone]<br/>[email]<br/>[website]</div>'),
            ("Footer Bank", col_w * 2,
             '<div style="font-size:6pt; color:#777; line-height:1.4; font-weight:300;"><span style="font-weight:400; color:#444;">Bankverbindung</span><br/>[bank_name]<br/>IBAN: [iban]<br/>BIC: [bic]</div>'),
            ("Footer Steuern", col_w * 3,
             '<div style="font-size:6pt; color:#777; line-height:1.4; font-weight:300;"><span style="font-weight:400; color:#444;">Steuern</span><br/>USt-ID: [vat]<br/>St.-Nr.: [stnr]</div>'),
        ]
        seq = 10
        for name, x, content in footer_cols:
            E.create({"template_id": t, "name": name, "element_type": "text",
                      "zone": "footer", "sequence": seq,
                      "pos_x": x, "pos_y": 1.5, "width": col_w - 2, "height": 18,
                      "text_content": content})
            seq += 1

        E.create({"template_id": t, "name": "Seitenzahl", "element_type": "text",
                  "zone": "footer", "sequence": 20,
                  "pos_x": cw - 35, "pos_y": 20, "width": 35, "height": 3,
                  "text_align": "right",
                  "text_content": '<span style="font-size:6.5pt; color:#999;">Seite [page] von [pages]</span>'})

    def _create_din5008_sale_elements(self):
        """DIN 5008 variant for sale.order — correct field paths + Angebot/Auftragsbestätigung title.

        Uses sale_state_filter to decide the document title.
        Field mapping:
          account.move.name               -> sale.order.name
          account.move.invoice_date       -> sale.order.date_order
          account.move.invoice_date_due   -> sale.order.validity_date
          account.move.user_id.name       -> sale.order.user_id.name
        """
        E = self.env["document.layout.element"]
        t = self.id
        cw = self.paper_width - self.margin_left - self.margin_right

        # Title: Auftragsbestätigung (confirmed/AB) vs Angebot (quotation).
        # sale.order is now quotation-only; the dedicated 'auftragsbestaetigung'
        # doc_type carries the confirmed layout.
        if self._is_order_confirmation():
            doc_title = "Auftragsbestätigung"
            date_label = "Auftragsdatum:"
            nr_label = "Auftragsnr.:"
        else:
            doc_title = "Angebot"
            date_label = "Angebotsdatum:"
            nr_label = "Angebotsnr.:"

        # ── HEADER (identisch zu Rechnung) ──
        E.create({"template_id": t, "name": "Company Logo", "element_type": "image",
                  "zone": "header", "sequence": 10,
                  "pos_x": 0, "pos_y": 0, "width": 45, "height": 18,
                  "image_source": "company_logo", "image_fit": "contain"})
        E.create({"template_id": t, "name": "Company Address", "element_type": "text",
                  "zone": "header", "sequence": 15,
                  "pos_x": cw - 65, "pos_y": 0, "width": 65, "height": 18,
                  "text_align": "right",
                  "text_content": '<div style="font-size:7pt; color:#777; line-height:1.5; font-weight:300;">[company_name]<br/>[street]<br/>[zip] [city]<br/>Tel: [phone]</div>'})
        E.create({"template_id": t, "name": "Header Line", "element_type": "line",
                  "zone": "header", "sequence": 20,
                  "pos_x": 0, "pos_y": 20, "width": cw, "height": 0.3,
                  "line_color": "#cccccc", "line_width": 0.3})

        # ── Faltmarken ──
        E.create({"template_id": t, "name": "Faltmarke 1", "element_type": "line",
                  "zone": "body", "sequence": 5,
                  "pos_x": -self.margin_left, "pos_y": 42, "width": 4, "height": 0.5,
                  "line_color": "#bbbbbb", "line_width": 0.25})
        E.create({"template_id": t, "name": "Lochmarke", "element_type": "line",
                  "zone": "body", "sequence": 6,
                  "pos_x": -self.margin_left, "pos_y": 103.5, "width": 6, "height": 0.5,
                  "line_color": "#bbbbbb", "line_width": 0.25})
        E.create({"template_id": t, "name": "Faltmarke 2", "element_type": "line",
                  "zone": "body", "sequence": 7,
                  "pos_x": -self.margin_left, "pos_y": 147, "width": 4, "height": 0.5,
                  "line_color": "#bbbbbb", "line_width": 0.25})

        E.create({"template_id": t, "name": "Rücksendezeile", "element_type": "field",
                  "zone": "body", "sequence": 20,
                  "pos_x": 0, "pos_y": 0, "width": 85, "height": 3.5,
                  "field_name": "company_id.name", "field_default": "",
                  "style_json": '{"font-size": "6pt", "color": "#999", "text-decoration": "underline", "font-weight": "300"}'})

        # ── Kundenadresse ──
        for i, (name, field, y, style) in enumerate([
            ("Kundenname", "partner_id.display_name", 5, '{"font-size": "8.5pt", "font-weight": "400"}'),
            ("Kundenstrasse", "partner_id.street", 9.5, '{"font-size": "8.5pt", "font-weight": "300"}'),
            ("Kunden PLZ/Ort", "partner_id.zip", 13.5, '{"font-size": "8.5pt", "font-weight": "300"}'),
            ("Kundenland", "partner_id.country_id.name", 17.5, '{"font-size": "8.5pt", "font-weight": "300"}'),
        ]):
            E.create({"template_id": t, "name": name, "element_type": "field",
                      "zone": "body", "sequence": 25 + i,
                      "pos_x": 0, "pos_y": y, "width": 85, "height": 4,
                      "field_name": field, "field_default": "",
                      "style_json": style})

        # ── Informationsblock (rechts) mit sale.order-Feldern ──
        info_x = cw - 60
        # DIN-5008-Betreff linksbündig, unter Anschrift/Infoblock, über der
        # Tabelle — konsistent zum Rechnungs-DIN5008 (große Headinggröße,
        # nicht fett, großzügiger Abstand).
        E.create({"template_id": t, "name": "Dokumenttitel", "element_type": "text",
                  "zone": "body", "sequence": 40,
                  "pos_x": 0, "pos_y": 28, "width": 120, "height": 8,
                  "text_content": f'<span style="font-size: 14pt; font-weight: 400;">{doc_title}</span>'})

        labels_and_fields = [
            (nr_label, "name", 8),
            (date_label, "date_order", 12),
            ("Gültig bis:", "validity_date", 16),
            ("Sachbearbeiter:", "user_id.name", 20),
        ]
        seq = 41
        for label, field, y_off in labels_and_fields:
            E.create({"template_id": t, "name": label.replace(":", ""), "element_type": "text",
                      "zone": "body", "sequence": seq,
                      "pos_x": info_x, "pos_y": y_off, "width": 28, "height": 3.5,
                      "text_content": f'<span style="font-size:7.5pt; color:#777; font-weight:300;">{label}</span>'})
            seq += 1
            E.create({"template_id": t, "name": label.replace(":", "") + " Value", "element_type": "field",
                      "zone": "body", "sequence": seq,
                      "pos_x": info_x + 30, "pos_y": y_off, "width": 30, "height": 3.5,
                      "field_name": field, "field_default": "",
                      "style_json": '{"font-size": "7.5pt", "font-weight": "300"}'})
            seq += 1

        E.create({"template_id": t, "name": "Table Separator", "element_type": "line",
                  "zone": "body", "sequence": 58,
                  "pos_x": 0, "pos_y": 40, "width": cw, "height": 0.3,
                  "line_color": "#dddddd", "line_width": 0.25})

        # ── Positionstabelle (sale.order.order_line) ──
        table_cols = json.dumps([
            {"field": "sequence", "label": "Pos.", "width": "5%", "align": "center"},
            {"field": "name", "label": "Bezeichnung", "width": "38%", "align": "left"},
            {"field": "product_uom_qty", "label": "Menge", "width": "8%", "align": "right"},
            {"field": "product_uom.name", "label": "Einheit", "width": "9%", "align": "center"},
            {"field": "price_unit", "label": "Einzelpreis", "width": "13%", "align": "right", "type": "monetary"},
            {"field": "discount", "label": "Rabatt", "width": "7%", "align": "right"},
            {"field": "tax_id", "label": "USt.", "width": "8%", "align": "right"},
            {"field": "price_subtotal", "label": "Betrag", "width": "12%", "align": "right", "type": "monetary"},
        ])
        E.create({"template_id": t, "name": "Positionstabelle", "element_type": "table",
                  "zone": "body", "sequence": 60,
                  "pos_x": 0, "pos_y": 42, "width": cw, "height": 80,
                  "table_columns_json": table_cols,
                  "table_show_header": True, "table_show_totals": True,
                  "table_zebra": False, "table_border_style": "horizontal",
                  "table_rows_per_page": 25,
                  "style_json": '{"font-size": "8pt", "font-weight": "300"}'})

        # ── Dankestext ──
        dank_text = (
            "Vielen Dank für Ihre Anfrage.<br/><br/>Wir freuen uns über Ihren Auftrag.<br/><br/>Mit freundlichen Grüßen"
            if not self._is_order_confirmation()
            else "Vielen Dank für Ihren Auftrag.<br/><br/>Mit freundlichen Grüßen"
        )
        E.create({"template_id": t, "name": "Dankestext", "element_type": "text",
                  "zone": "body", "sequence": 70,
                  "pos_x": 0, "pos_y": 120, "width": 100, "height": 20,
                  "anchor_mode": "after_table",
                  "text_content": f'<div style="font-size:8pt; line-height:1.5; color:#444; font-weight:300;">{dank_text}</div>'})

        # ── FOOTER (identisch zu Rechnung) ──
        E.create({"template_id": t, "name": "Footer Linie", "element_type": "line",
                  "zone": "footer", "sequence": 5,
                  "pos_x": 0, "pos_y": 0, "width": cw, "height": 0.3,
                  "line_color": "#cccccc", "line_width": 0.3})

        col_w = cw / 4
        footer_cols = [
            ("Footer Firma", 0,
             '<div style="font-size:6pt; color:#777; line-height:1.4; font-weight:300;"><span style="font-weight:400; color:#444;">Firma</span><br/>[company_name]<br/>[street]<br/>[zip] [city]</div>'),
            ("Footer Kontakt", col_w,
             '<div style="font-size:6pt; color:#777; line-height:1.4; font-weight:300;"><span style="font-weight:400; color:#444;">Kontakt</span><br/>Tel: [phone]<br/>[email]<br/>[website]</div>'),
            ("Footer Bank", col_w * 2,
             '<div style="font-size:6pt; color:#777; line-height:1.4; font-weight:300;"><span style="font-weight:400; color:#444;">Bankverbindung</span><br/>[bank_name]<br/>IBAN: [iban]<br/>BIC: [bic]</div>'),
            ("Footer Steuern", col_w * 3,
             '<div style="font-size:6pt; color:#777; line-height:1.4; font-weight:300;"><span style="font-weight:400; color:#444;">Steuern</span><br/>USt-ID: [vat]<br/>St.-Nr.: [stnr]</div>'),
        ]
        seq = 10
        for name, x, content in footer_cols:
            E.create({"template_id": t, "name": name, "element_type": "text",
                      "zone": "footer", "sequence": seq,
                      "pos_x": x, "pos_y": 1.5, "width": col_w - 2, "height": 18,
                      "text_content": content})
            seq += 1

        E.create({"template_id": t, "name": "Seitenzahl", "element_type": "text",
                  "zone": "footer", "sequence": 20,
                  "pos_x": cw - 35, "pos_y": 20, "width": 35, "height": 3,
                  "text_align": "right",
                  "text_content": '<span style="font-size:6.5pt; color:#999;">Seite [page] von [pages]</span>'})

    def export_template_json(self):
        """Export template as JSON for sharing — includes all element fields."""
        self.ensure_one()
        data = {
            "name": self.name,
            "doc_type": self.doc_type,
            "layout_style": self.layout_style,
            "paper_format": self.paper_format,
            "margins": {
                "top": self.margin_top,
                "bottom": self.margin_bottom,
                "left": self.margin_left,
                "right": self.margin_right,
            },
            "header_height": self.header_height,
            "footer_height": self.footer_height,
            "layout": self.get_layout_data(),
            "elements": [],
        }
        for elem in self.element_ids:
            elem_data = {
                "element_type": elem.element_type,
                "name": elem.name,
                "zone": elem.zone,
                "sequence": elem.sequence,
                "pos_x": elem.pos_x,
                "pos_y": elem.pos_y,
                "width": elem.width,
                "height": elem.height,
                "rotation": elem.rotation,
                "visible": elem.visible,
                "locked": elem.locked,
                # Text
                "text_content": elem.text_content or "",
                "text_align": elem.text_align or "left",
                # Field
                "field_model": elem.field_model or "",
                "field_name": elem.field_name or "",
                "field_format": elem.field_format or "",
                "field_default": elem.field_default or "",
                # Image (binary data excluded for portability — only source type)
                "image_source": elem.image_source or "upload",
                "image_fit": elem.image_fit or "contain",
                "image_field_name": elem.image_field_name or "",
                # Table
                "table_columns": elem.get_table_columns(),
                "table_show_header": elem.table_show_header,
                "table_show_totals": elem.table_show_totals,
                "table_zebra": elem.table_zebra,
                "table_border_style": elem.table_border_style or "horizontal",
                "table_optional_mode": elem.table_optional_mode or "separate",
                "table_optional_label": elem.table_optional_label or "Optional Items",
                "table_optional_show_qty": elem.table_optional_show_qty,
                "table_rows_per_page": elem.table_rows_per_page,
                "table_repeat_header": elem.table_repeat_header,
                "table_show_carryover": elem.table_show_carryover,
                # Line
                "line_color": elem.line_color or "#000000",
                "line_width": elem.line_width,
                "line_style": elem.line_style or "solid",
                # Container
                "container_layout": elem.container_layout or "free",
                "container_padding": elem.container_padding,
                "container_bg_color": elem.container_bg_color or "transparent",
                "container_border": elem.container_border or "none",
                "container_border_radius": elem.container_border_radius,
                "container_shadow": elem.container_shadow,
                "container_opacity": elem.container_opacity,
                # Barcode
                "barcode_type": elem.barcode_type or "code128",
                "barcode_field": elem.barcode_field or "",
                "barcode_static_value": elem.barcode_static_value or "",
                # Conditional visibility
                "condition_field": elem.condition_field or "",
                "condition_operator": elem.condition_operator or "set",
                # Style/Config JSON
                "style_json": elem.get_style_data(),
                "config_json": elem.get_config_data(),
            }
            data["elements"].append(elem_data)
        return json.dumps(data, ensure_ascii=False, indent=2)

    def action_export_qweb(self):
        """Export the template as standalone QWeb XML that works without this module.

        This generates a complete ir.ui.view XML file that can be imported
        into any Odoo instance (including SaaS) via Studio or as a custom module.
        """
        self.ensure_one()
        import base64
        from odoo.addons.invoice_layout_designer.models.report_override import transpile_template

        qweb_body = transpile_template(self, None)
        tpl_name = f"custom_layout.{self.name.lower().replace(' ', '_')}_{self.id}"

        # Verlustfreien Re-Import ermöglichen: die vollständige JSON-
        # Serialisierung base64-kodiert als XML-Kommentar einbetten. Der
        # Block ist für das QWeb-Rendering inert (Kommentar), wird aber vom
        # Import-Wizard wieder ausgelesen. base64 verhindert ungültige
        # Kommentare (keine "--"-Sequenz) und sichert Umlaute/Sonderzeichen.
        layout_json = self.export_template_json()
        layout_b64 = base64.b64encode(layout_json.encode("utf-8")).decode("ascii")
        layout_marker = f"<!-- {ILD_LAYOUT_DATA_MARKER} {layout_b64} -->"
        _logger.info(
            "QWeb-Export mit eingebettetem Layout-Block für Template '%s' (id=%s)",
            self.name, self.id,
        )

        xml_content = f"""<?xml version="1.0" encoding="utf-8"?>
<odoo>
    {layout_marker}
    <!-- Auto-generated by Invoice Layout Designer -->
    <!-- Template: {self.name} | Doc Type: {self.doc_type} -->
    <record id="view_{tpl_name.replace('.', '_')}" model="ir.ui.view">
        <field name="name">{self.name}</field>
        <field name="type">qweb</field>
        <field name="key">{tpl_name}</field>
        <field name="arch" type="xml">
            <t t-name="{tpl_name}">
                <t t-call="web.html_container">
                    <t t-foreach="docs" t-as="doc">
                        <div class="page">
                            {qweb_body}
                        </div>
                    </t>
                </t>
            </t>
        </field>
    </record>
</odoo>"""

        import base64
        self.write({
            "qweb_export_data": base64.b64encode(xml_content.encode("utf-8")),
            "qweb_export_filename": f"{self.name.replace(' ', '_')}_qweb.xml",
        })
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content?model=document.layout.template&id={self.id}"
                   f"&field=qweb_export_data&filename_field=qweb_export_filename"
                   f"&download=true",
            "target": "new",
        }

    qweb_export_data = fields.Binary("QWeb Export", attachment=True)
    qweb_export_filename = fields.Char("QWeb Filename")

    # ═══════════════════════════════════════════════════════════
    # ADDITIONAL TEMPLATE BUILDERS
    # ═══════════════════════════════════════════════════════════

    def _get_doc_type_config(self):
        """Liefert die dokumentart-abhängigen Layout-Bausteine.

        Jeder Layout-Stil soll für alle Dokumentarten (Rechnung, Angebot/
        Auftrag, Lieferschein, Bestellung) korrekt vorbefüllt sein. Hier
        steht zentral, welcher Titel, welche Kopf-Felder und welche
        Tabellenspalten zur jeweiligen Dokumentart gehören — damit nicht
        jeder Seeder eigene (und potenziell falsche) Feldpfade pflegt.

        info_fields: Liste von Tupeln (Label, Feldpfad, Default, y-Position).
        """
        self.ensure_one()
        doc_type = doc_type_to_model(self.doc_type)
        if doc_type == "sale.order":
            if self._is_order_confirmation():
                title = "Auftragsbestätigung"
            else:
                title = "Angebot"
            info_fields = [
                ("Angebotsnr.:", "name", "", 7),
                ("Angebotsdatum:", "date_order", "", 11),
                ("Gültig bis:", "validity_date", "", 15),
            ]
            table_cols = [
                {"field": "name", "label": "Beschreibung", "width": "45%", "align": "left"},
                {"field": "product_uom_qty", "label": "Menge", "width": "10%", "align": "right"},
                {"field": "price_unit", "label": "Einzelpreis", "width": "15%", "align": "right", "type": "monetary"},
                {"field": "price_subtotal", "label": "Betrag", "width": "15%", "align": "right", "type": "monetary"},
            ]
            return DocTypeConfig(title, info_fields, table_cols, True)
        if doc_type == "stock.picking":
            info_fields = [
                ("Beleg-Nr.:", "name", "", 7),
                ("Geplant am:", "scheduled_date", "", 11),
                ("Erledigt am:", "date_done", "", 15),
            ]
            table_cols = [
                {"field": "product_id.display_name", "label": "Produkt", "width": "60%", "align": "left"},
                {"field": "product_uom_qty", "label": "Menge", "width": "20%", "align": "right"},
                {"field": "product_uom.name", "label": "Einheit", "width": "20%", "align": "left"},
            ]
            return DocTypeConfig("Lieferschein", info_fields, table_cols, False)
        if doc_type == "purchase.order":
            info_fields = [
                ("Bestellnr.:", "name", "", 7),
                ("Bestelldatum:", "date_order", "", 11),
                ("Liefertermin:", "date_planned", "", 15),
            ]
            table_cols = [
                {"field": "name", "label": "Beschreibung", "width": "45%", "align": "left"},
                {"field": "product_qty", "label": "Menge", "width": "10%", "align": "right"},
                {"field": "price_unit", "label": "Einzelpreis", "width": "15%", "align": "right", "type": "monetary"},
                {"field": "price_subtotal", "label": "Betrag", "width": "15%", "align": "right", "type": "monetary"},
            ]
            return DocTypeConfig("Bestellung", info_fields, table_cols, True)
        # Fallback: account.move (Rechnung)
        info_fields = [
            ("Rechnungsnr.:", "name", "", 7),
            ("Rechnungsdatum:", "invoice_date", "", 11),
            ("Fällig am:", "invoice_date_due", "", 15),
        ]
        table_cols = [
            {"field": "name", "label": "Beschreibung", "width": "45%", "align": "left"},
            {"field": "quantity", "label": "Menge", "width": "10%", "align": "right"},
            {"field": "price_unit", "label": "Einzelpreis", "width": "15%", "align": "right", "type": "monetary"},
            {"field": "price_subtotal", "label": "Betrag", "width": "15%", "align": "right", "type": "monetary"},
        ]
        return DocTypeConfig("Rechnung", info_fields, table_cols, True)

    def _create_modern_elements(self):
        """Modern template: clean lines, accent color bar, minimal.

        Doc-type-aware: title, info fields and table columns adapt to
        account.move / sale.order / stock.picking / purchase.order so the
        same Modern look works for invoices, quotations, delivery slips
        and purchase orders.
        """
        E = self.env["document.layout.element"]
        t = self.id
        cw = self.paper_width - self.margin_left - self.margin_right

        config = self._get_doc_type_config()
        title = config.title
        info_fields = config.info_fields
        table_cols = config.table_cols
        show_totals = config.show_totals

        # ── Header: logo + company ────────────────────────────────────
        # Kein dunkler Accent-Bar über dem Logo — auf Wunsch entfernt, damit
        # der Kopf wie bei den DIN-5008-Belegen ohne Balken auskommt.
        E.create({"template_id": t, "name": "Logo", "element_type": "image", "zone": "header", "sequence": 10,
                  "pos_x": 0, "pos_y": 4, "width": 40, "height": 16, "image_source": "company_logo", "image_fit": "contain"})
        E.create({"template_id": t, "name": "Company Info", "element_type": "text", "zone": "header", "sequence": 15,
                  "pos_x": cw - 60, "pos_y": 4, "width": 60, "height": 16, "text_align": "right",
                  "text_content": '<div style="font-size:7pt; color:#777; line-height:1.4; font-weight:300;">[company_name]<br/>[street]<br/>[zip] [city]<br/>Tel: [phone]</div>'})

        # ── Body: return line + customer block ────────────────────────
        E.create({"template_id": t, "name": "Rücksendezeile", "element_type": "field", "zone": "body", "sequence": 20,
                  "pos_x": 0, "pos_y": 0, "width": 85, "height": 3,
                  "field_name": "company_id.name", "field_default": "",
                  "style_json": '{"font-size": "6pt", "color": "#aaa", "text-decoration": "underline", "font-weight": "300"}'})
        for i, (name, field, default, y) in enumerate([
            ("Kundenname", "partner_id.display_name", "[Name]", 4),
            ("Strasse", "partner_id.street", "[Strasse]", 8),
            ("PLZ Ort", "partner_id.zip", "[PLZ Ort]", 12),
            ("Land", "partner_id.country_id.name", "[Land]", 16),
        ]):
            E.create({"template_id": t, "name": name, "element_type": "field", "zone": "body", "sequence": 21 + i,
                      "pos_x": 0, "pos_y": y, "width": 85, "height": 3.5,
                      "field_name": field, "field_default": default,
                      "style_json": '{"font-size": "8.5pt", "font-weight": "300"}'})

        # ── Title + info block ────────────────────────────────────────
        info_x = cw - 55
        # Titel linksbündig unter dem Adressblock (wie DIN-5008-Rechnung/AB),
        # damit alle Belegtypen denselben Aufbau zeigen. Der Info-Block
        # (Nr./Datum) bleibt rechts oben; der Titel steht groß darunter.
        E.create({"template_id": t, "name": "Title", "element_type": "text", "zone": "body", "sequence": 40,
                  "pos_x": 0, "pos_y": 24, "width": cw, "height": 8,
                  "text_content": f'<span style="font-size:15pt; font-weight:600; color:#2c3e50;">{title} [number]</span>'})
        for i, (label, field, default, y) in enumerate(info_fields):
            E.create({"template_id": t, "name": f"{label} L", "element_type": "text", "zone": "body", "sequence": 41 + i * 2,
                      "pos_x": info_x, "pos_y": y, "width": 20, "height": 3,
                      "text_content": f'<span style="font-size:7pt; color:#999; font-weight:300;">{label}</span>'})
            E.create({"template_id": t, "name": label, "element_type": "field", "zone": "body", "sequence": 42 + i * 2,
                      "pos_x": info_x + 22, "pos_y": y, "width": 33, "height": 3,
                      "field_name": field, "field_default": default, "style_json": '{"font-size": "7.5pt", "font-weight": "300"}'})

        # ── Table ─────────────────────────────────────────────────────
        table_cols_json = json.dumps(table_cols)
        E.create({"template_id": t, "name": "Tabelle", "element_type": "table", "zone": "body", "sequence": 60,
                  "pos_x": 0, "pos_y": 40, "width": cw, "height": 100, "table_columns_json": table_cols_json,
                  "table_show_header": True, "table_show_totals": show_totals, "table_border_style": "horizontal",
                  "table_optional_mode": ("separate" if doc_type_to_model(self.doc_type) == "sale.order" else "hide")})

        # Footer
        E.create({"template_id": t, "name": "Footer Bar", "element_type": "shape", "zone": "footer", "sequence": 5,
                  "pos_x": 0, "pos_y": 0, "width": cw, "height": 0.5,
                  "style_json": '{"background-color": "#2c3e50", "border": "none"}'})
        fw = cw / 3
        for i, (name, x, content) in enumerate([
            ("F1", 0, '<div style="font-size:6pt; color:#777; font-weight:300;">[company_name]<br/>[street], [zip] [city]</div>'),
            ("F2", fw, '<div style="font-size:6pt; color:#777; font-weight:300;">Tel: [phone]<br/>[email]</div>'),
            ("F3", fw * 2, '<div style="font-size:6pt; color:#777; font-weight:300;">IBAN: [iban]<br/>USt-ID: [vat]</div>'),
        ]):
            E.create({"template_id": t, "name": name, "element_type": "text", "zone": "footer", "sequence": 10 + i,
                      "pos_x": x, "pos_y": 2, "width": fw - 2, "height": 15, "text_content": content})

        # Seitenzahl rechtsbündig — fehlte bisher im Modern-Footer, daher hatte
        # u.a. der Modern-Lieferschein keine Seitennummerierung. [page]/[pages]
        # werden je nach Engine ersetzt (wkhtmltopdf-Footer bzw. CSS-Counter).
        E.create({"template_id": t, "name": "Seitenzahl", "element_type": "text", "zone": "footer", "sequence": 20,
                  "pos_x": cw - 35, "pos_y": 17, "width": 35, "height": 3, "text_align": "right",
                  "text_content": '<span style="font-size:6pt; color:#777; font-weight:300;">Seite [page] von [pages]</span>'})

    def _create_classic_elements(self):
        """Classic template: traditional business letter, serif-leaning, formal.

        Doc-type-aware via _get_doc_type_config(): Titel, Info-Felder und
        Tabellenspalten passen sich der Dokumentart an.
        """
        E = self.env["document.layout.element"]
        t = self.id
        cw = self.paper_width - self.margin_left - self.margin_right
        config = self._get_doc_type_config()

        E.create({"template_id": t, "name": "Logo", "element_type": "image", "zone": "header", "sequence": 10,
                  "pos_x": (cw - 35) / 2, "pos_y": 0, "width": 35, "height": 15,
                  "image_source": "company_logo", "image_fit": "contain"})
        E.create({"template_id": t, "name": "Company", "element_type": "text", "zone": "header", "sequence": 15,
                  "pos_x": 0, "pos_y": 16, "width": cw, "height": 4, "text_align": "center",
                  "text_content": '<div style="font-size:7pt; color:#666; font-weight:300; letter-spacing:1pt;">[company_name] &middot; [street] &middot; [zip] [city]</div>'})
        E.create({"template_id": t, "name": "Line", "element_type": "line", "zone": "header", "sequence": 20,
                  "pos_x": 20, "pos_y": 22, "width": cw - 40, "height": 0.3,
                  "line_color": "#999999", "line_width": 0.3})

        for i, (name, field, default, y) in enumerate([
            ("Name", "partner_id.display_name", "[Name]", 2),
            ("Strasse", "partner_id.street", "", 6),
            ("PLZ", "partner_id.zip", "", 10),
            ("Land", "partner_id.country_id.name", "", 14),
        ]):
            E.create({"template_id": t, "name": name, "element_type": "field", "zone": "body", "sequence": 20 + i,
                      "pos_x": 0, "pos_y": y, "width": 80, "height": 3.5,
                      "field_name": field, "field_default": default,
                      "style_json": '{"font-size": "9pt", "font-weight": "300"}'})

        E.create({"template_id": t, "name": "Title", "element_type": "text", "zone": "body", "sequence": 35,
                  "pos_x": 0, "pos_y": 24, "width": cw, "height": 7,
                  "text_content": f'<div style="font-size:12pt; font-weight:400; border-bottom:0.5pt solid #ccc; padding-bottom:2mm;">{config.title} [number]</div>'})

        info_x = cw - 55
        for i, (label, field, default, _unused_y) in enumerate(config.info_fields):
            y = 24 + i * 4
            E.create({"template_id": t, "name": f"{label} L", "element_type": "text", "zone": "body", "sequence": 40 + i * 2,
                      "pos_x": info_x, "pos_y": y, "width": 25, "height": 3,
                      "text_content": f'<span style="font-size:7.5pt; color:#888; font-weight:300;">{label}</span>'})
            E.create({"template_id": t, "name": label, "element_type": "field", "zone": "body", "sequence": 41 + i * 2,
                      "pos_x": info_x + 27, "pos_y": y, "width": 28, "height": 3,
                      "field_name": field, "field_default": default,
                      "style_json": '{"font-size": "7.5pt", "font-weight": "300"}'})

        table_cols = json.dumps(config.table_cols)
        E.create({"template_id": t, "name": "Tabelle", "element_type": "table", "zone": "body", "sequence": 60,
                  "pos_x": 0, "pos_y": 40, "width": cw, "height": 100, "table_columns_json": table_cols,
                  "table_show_header": True, "table_show_totals": config.show_totals,
                  "table_border_style": "horizontal",
                  "table_optional_mode": ("separate" if doc_type_to_model(self.doc_type) == "sale.order" else "hide")})

        E.create({"template_id": t, "name": "Footer Line", "element_type": "line", "zone": "footer", "sequence": 5,
                  "pos_x": 20, "pos_y": 0, "width": cw - 40, "height": 0.3, "line_color": "#999999", "line_width": 0.3})
        E.create({"template_id": t, "name": "Footer", "element_type": "text", "zone": "footer", "sequence": 10,
                  "pos_x": 0, "pos_y": 2, "width": cw, "height": 15, "text_align": "center",
                  "text_content": '<div style="font-size:6pt; color:#888; font-weight:300; line-height:1.5;">[company_name] | [street] | [zip] [city]<br/>Tel: [phone] | [email] | [website]<br/>IBAN: [iban] | BIC: [bic] | USt-ID: [vat]</div>'})

    def _create_minimalist_elements(self):
        """Minimalist template: ultra-clean, no borders, lots of whitespace.

        Doc-type-aware via _get_doc_type_config(): Titel, Datums-Feld und
        Tabellenspalten passen sich der Dokumentart an.
        """
        E = self.env["document.layout.element"]
        t = self.id
        cw = self.paper_width - self.margin_left - self.margin_right
        config = self._get_doc_type_config()

        E.create({"template_id": t, "name": "Logo", "element_type": "image", "zone": "header", "sequence": 10,
                  "pos_x": 0, "pos_y": 0, "width": 30, "height": 12, "image_source": "company_logo", "image_fit": "contain"})

        for i, (name, field, default, y) in enumerate([
            ("Name", "partner_id.display_name", "", 5),
            ("Strasse", "partner_id.street", "", 9),
            ("PLZ", "partner_id.zip", "", 13),
        ]):
            E.create({"template_id": t, "name": name, "element_type": "field", "zone": "body", "sequence": 20 + i,
                      "pos_x": 0, "pos_y": y, "width": 80, "height": 3.5,
                      "field_name": field, "style_json": '{"font-size": "9pt", "font-weight": "300"}'})

        E.create({"template_id": t, "name": "Title", "element_type": "text", "zone": "body", "sequence": 34,
                  "pos_x": 0, "pos_y": 21, "width": 60, "height": 5,
                  "text_content": f'<span style="font-size:9pt; font-weight:300; color:#999;">{config.title}</span>'})

        E.create({"template_id": t, "name": "Number", "element_type": "field", "zone": "body", "sequence": 35,
                  "pos_x": 0, "pos_y": 26, "width": 60, "height": 6,
                  "field_name": "name", "field_default": "",
                  "style_json": '{"font-size": "14pt", "font-weight": "300", "color": "#333"}'})

        # Zweites Info-Feld der Dokumentart (Datum) rechtsbündig
        date_label, date_field, date_default, _unused_y = config.info_fields[1]
        E.create({"template_id": t, "name": date_label, "element_type": "field", "zone": "body", "sequence": 36,
                  "pos_x": cw - 40, "pos_y": 26, "width": 40, "height": 4, "text_align": "right",
                  "field_name": date_field, "field_default": date_default,
                  "style_json": '{"font-size": "8pt", "font-weight": "300", "color": "#999"}'})

        table_cols = json.dumps(config.table_cols)
        E.create({"template_id": t, "name": "Table", "element_type": "table", "zone": "body", "sequence": 60,
                  "pos_x": 0, "pos_y": 36, "width": cw, "height": 100, "table_columns_json": table_cols,
                  "table_show_header": True, "table_show_totals": config.show_totals,
                  "table_border_style": "none",
                  "table_optional_mode": ("separate" if doc_type_to_model(self.doc_type) == "sale.order" else "hide")})

        E.create({"template_id": t, "name": "Footer", "element_type": "text", "zone": "footer", "sequence": 10,
                  "pos_x": 0, "pos_y": 2, "width": cw, "height": 10,
                  "text_content": '<div style="font-size:6pt; color:#bbb; font-weight:300; line-height:1.4;">[company_name] | [email] | [phone] | IBAN: [iban]</div>'})


    def _get_company_report_colors(self):
        """Liest die Firmenfarben für Bubble/Wave/Folder-Dekoration.

        Hinweis Business: Die Farben werden beim Seeden EINGEBRANNT.
        Ändert die Firma später ihre Farben, muss das Template per
        'Reset to Default' neu aufgebaut werden.
        """
        company = self.company_id or self.env.company
        primary = company.primary_color
        secondary = company.secondary_color
        if not primary:
            _logger.info("ILD: Firma '%s' hat keine Primärfarbe, nutze Odoo-Standard.", company.name)
            primary = "#714B67"
        if not secondary:
            _logger.info("ILD: Firma '%s' hat keine Sekundärfarbe, nutze Odoo-Standard.", company.name)
            secondary = "#8595A2"
        return primary, secondary

    def _create_odoo_style_elements(self, style_key):
        """Baut ein Layout im Stil eines Odoo-Standard-Reports auf.

        Business-Hintergrund: Kunden kennen die Odoo-Layouts (Light,
        Boxed, Bold, Striped, Bubble, Wave, Folder) aus den normalen
        Odoo-Berichten. Dieser Builder stellt dieselbe Optik als
        bearbeitbares Designer-Template bereit — ein Builder für alle
        sieben Stile, gesteuert über ODOO_STYLE_CONFIGS.
        """
        self.ensure_one()
        config = ODOO_STYLE_CONFIGS[style_key]
        dt_config = self._get_doc_type_config()
        E = self.env["document.layout.element"]
        t = self.id
        cw = self.paper_width - self.margin_left - self.margin_right
        primary, secondary = self._get_company_report_colors()
        _logger.info(
            "ILD: Odoo-Stil '%s' für Template '%s' (doc_type=%s).",
            style_key, self.name, self.doc_type,
        )

        self._create_odoo_style_shapes(config, primary, secondary)
        self._create_odoo_style_header(config)

        # ── Body: Rücksendezeile + Kundenanschrift ────────────────────
        E.create({"template_id": t, "name": "Rücksendezeile", "element_type": "field", "zone": "body", "sequence": 20,
                  "pos_x": 0, "pos_y": 0, "width": 85, "height": 3,
                  "field_name": "company_id.name", "field_default": "",
                  "style_json": '{"font-size": "6pt", "color": "#aaa", "text-decoration": "underline"}'})
        address_fields = [
            ("Kundenname", "partner_id.display_name", "[Name]", 4),
            ("Strasse", "partner_id.street", "[Strasse]", 8),
            ("PLZ Ort", "partner_id.zip", "[PLZ Ort]", 12),
            ("Land", "partner_id.country_id.name", "[Land]", 16),
        ]
        for i, (name, field, default, y) in enumerate(address_fields):
            E.create({"template_id": t, "name": name, "element_type": "field", "zone": "body", "sequence": 21 + i,
                      "pos_x": 0, "pos_y": y, "width": 85, "height": 3.5,
                      "field_name": field, "field_default": default,
                      "style_json": '{"font-size": "9pt"}'})

        # ── Titel (links, unter Anschrift, über der Tabelle) + Info-Block ──
        # Der Dokumenttitel gehört als Überschrift direkt über die
        # Positionstabelle — nicht oben rechts auf Höhe von Anschrift/Infoblock
        # (dort wirkte er wie versehentlich darübergelegt).
        info_x = cw - 55
        title_text = dt_config.title
        title_style = "font-size:14pt; font-weight:400; color:#333;"
        if config["uppercase_table_header"]:
            # Bold-Stil: Titel kräftig und in Großbuchstaben
            title_text = title_text.upper()
            title_style = "font-size:14pt; font-weight:700; color:#111; letter-spacing:0.5pt;"
        # Titel inkl. Belegnummer ("Rechnung [number]") — wie im Odoo-Standard.
        # Großzügiger Abstand nach oben (zur Anschrift) und nach unten (zur
        # Tabelle, deren pos_y entsprechend tiefer liegt).
        # Für account.move den dynamischen [doc_title]-Platzhalter nutzen, damit
        # eine Gutschrift "GUTSCHRIFT" statt "RECHNUNG" im Titel trägt. Bei den
        # übrigen Belegen (Angebot, Bestellung, Lieferschein) bleibt der feste
        # Titel, weil [doc_title] dort bewusst nichts liefert.
        if self.doc_type == "account.move":
            title_inner = "[doc_title] [number]"
            if config["uppercase_table_header"]:
                title_style = title_style + " text-transform:uppercase;"
        else:
            title_inner = f"{title_text} [number]"
        E.create({"template_id": t, "name": "Title", "element_type": "text", "zone": "body", "sequence": 40,
                  "pos_x": 0, "pos_y": 28, "width": 150, "height": 8,
                  "text_content": f'<span style="{title_style}">{title_inner}</span>'})
        for i, (label, field, default, y) in enumerate(dt_config.info_fields):
            E.create({"template_id": t, "name": f"{label} L", "element_type": "text", "zone": "body", "sequence": 41 + i * 2,
                      "pos_x": info_x, "pos_y": y, "width": 20, "height": 3,
                      "text_content": f'<span style="font-size:7pt; color:#888;">{label}</span>'})
            E.create({"template_id": t, "name": label, "element_type": "field", "zone": "body", "sequence": 42 + i * 2,
                      "pos_x": info_x + 22, "pos_y": y, "width": 33, "height": 3,
                      "field_name": field, "field_default": default,
                      "style_json": '{"font-size": "7.5pt"}'})

        # ── Tabelle (Stil-Kern: Rahmen + Zebra je Konfig) ─────────────
        table_cols = []
        for col in dt_config.table_cols:
            col_copy = dict(col)
            if config["uppercase_table_header"]:
                col_copy["label"] = col_copy["label"].upper()
            table_cols.append(col_copy)
        E.create({"template_id": t, "name": "Tabelle", "element_type": "table", "zone": "body", "sequence": 60,
                  "pos_x": 0, "pos_y": 42, "width": cw, "height": 100,
                  "table_columns_json": json.dumps(table_cols),
                  "table_show_header": True, "table_show_totals": dt_config.show_totals,
                  "table_border_style": config["table_border_style"],
                  "table_zebra": config["table_zebra"],
                  "table_optional_mode": ("separate" if doc_type_to_model(self.doc_type) == "sale.order" else "hide")})

        self._create_odoo_style_footer(config)

    def _create_odoo_style_shapes(self, config, primary, secondary):
        """Dekorative Formen für Bubble/Wave/Folder.

        Die Odoo-Originale nutzen SVG-Grafiken. Hier werden sie mit
        Shape-Elementen (border-radius) angenähert, damit der User sie
        im Editor verschieben, umfärben oder löschen kann.
        """
        shape_variant = config["shape_variant"]
        if not shape_variant:
            _logger.info("ILD: Stil ohne Deko-Formen, überspringe Shapes für '%s'.", self.name)
            return
        _logger.info("ILD: erzeuge Deko-Formen '%s' für '%s'.", shape_variant, self.name)
        E = self.env["document.layout.element"]
        t = self.id
        cw = self.paper_width - self.margin_left - self.margin_right
        primary_soft = _hex_to_rgba(primary, 0.12)
        secondary_soft = _hex_to_rgba(secondary, 0.12)

        if shape_variant == "bubble":
            E.create({"template_id": t, "name": "Bubble oben rechts", "element_type": "shape", "zone": "header",
                      "sequence": 1, "pos_x": cw - 28, "pos_y": -12, "width": 45, "height": 45,
                      "style_json": f'{{"background-color": "{primary_soft}", "border": "none", "border-radius": "50%"}}'})
            E.create({"template_id": t, "name": "Bubble unten links", "element_type": "shape", "zone": "footer",
                      "sequence": 1, "pos_x": -20, "pos_y": 0, "width": 40, "height": 40,
                      "style_json": f'{{"background-color": "{secondary_soft}", "border": "none", "border-radius": "50%"}}'})
            return
        if shape_variant == "wave":
            E.create({"template_id": t, "name": "Welle oben", "element_type": "shape", "zone": "header",
                      "sequence": 1, "pos_x": -self.margin_left, "pos_y": -self.margin_top,
                      "width": self.paper_width, "height": self.margin_top + 14,
                      "style_json": f'{{"background-color": "{primary_soft}", "border": "none", "border-radius": "0 0 45% 35%"}}'})
            E.create({"template_id": t, "name": "Welle unten", "element_type": "shape", "zone": "footer",
                      "sequence": 1, "pos_x": -self.margin_left, "pos_y": 12,
                      "width": self.paper_width, "height": self.footer_height,
                      "style_json": f'{{"background-color": "{secondary_soft}", "border": "none", "border-radius": "35% 45% 0 0"}}'})
            return
        if shape_variant == "folder":
            E.create({"template_id": t, "name": "Ordner-Lasche", "element_type": "shape", "zone": "header",
                      "sequence": 1, "pos_x": 0, "pos_y": 0, "width": 80, "height": 8,
                      "style_json": f'{{"background-color": "{primary_soft}", "border": "none", "border-radius": "3mm 8mm 0 0"}}'})
            E.create({"template_id": t, "name": "Ordner-Fläche", "element_type": "shape", "zone": "header",
                      "sequence": 2, "pos_x": 0, "pos_y": 8, "width": cw, "height": 22,
                      "style_json": f'{{"background-color": "{primary_soft}", "border": "none", "border-radius": "0 3mm 3mm 3mm"}}'})
            return
        _logger.warning("ILD: unbekannte shape_variant '%s' für '%s'.", shape_variant, self.name)

    def _create_odoo_style_header(self, config):
        """Kopfbereich: Logo + Firmenangaben je Header-Variante."""
        E = self.env["document.layout.element"]
        t = self.id
        cw = self.paper_width - self.margin_left - self.margin_right
        header_variant = config["header_variant"]
        # Bei Folder/Wave sitzt der Inhalt AUF der Deko-Fläche — leicht
        # eingerückt, damit Logo/Adresse nicht am Formenrand kleben.
        inset = 3 if config["shape_variant"] in ("folder", "wave") else 0

        if header_variant == "light":
            _logger.info("ILD: Header-Variante 'light' für '%s'.", self.name)
            E.create({"template_id": t, "name": "Logo", "element_type": "image", "zone": "header", "sequence": 10,
                      "pos_x": 0, "pos_y": 0, "width": 30, "height": 12,
                      "image_source": "company_logo", "image_fit": "contain"})
            E.create({"template_id": t, "name": "Company Slogan", "element_type": "field", "zone": "header", "sequence": 15,
                      "pos_x": cw - 70, "pos_y": 2, "width": 70, "height": 5, "text_align": "right",
                      "field_name": "company_id.report_header", "field_default": "",
                      "style_json": '{"font-size": "8pt", "color": "#777"}'})
            E.create({"template_id": t, "name": "Company Info", "element_type": "text", "zone": "header", "sequence": 20,
                      "pos_x": 0, "pos_y": 14, "width": 90, "height": 14,
                      "text_content": '<div style="font-size:7pt; color:#777; line-height:1.4;">[company_name]<br/>[street], [zip] [city]<br/>Tel: [phone] · [email]</div>'})
            return
        _logger.info("ILD: Header-Variante 'split' für '%s'.", self.name)
        E.create({"template_id": t, "name": "Logo", "element_type": "image", "zone": "header", "sequence": 10,
                  "pos_x": inset, "pos_y": 2 + inset, "width": 40, "height": 16,
                  "image_source": "company_logo", "image_fit": "contain"})
        E.create({"template_id": t, "name": "Company Info", "element_type": "text", "zone": "header", "sequence": 15,
                  "pos_x": cw - 70 - inset, "pos_y": 2 + inset, "width": 70, "height": 18, "text_align": "right",
                  "text_content": '<div style="font-size:7pt; color:#555; line-height:1.5;">[company_name]<br/>[street]<br/>[zip] [city]<br/>Tel: [phone] · [email]</div>'})

    def _create_odoo_style_footer(self, config):
        """Fußbereich: Firmendaten links, Seitenzahl rechts — wie in den
        Odoo-Standard-Layouts (2-spaltiger Footer mit Trennlinie)."""
        E = self.env["document.layout.element"]
        t = self.id
        cw = self.paper_width - self.margin_left - self.margin_right
        E.create({"template_id": t, "name": "Footer Linie", "element_type": "line", "zone": "footer", "sequence": 5,
                  "pos_x": 0, "pos_y": 0, "width": cw, "height": 0.3,
                  "line_color": "#cccccc", "line_width": 0.3})
        E.create({"template_id": t, "name": "Footer Firma", "element_type": "text", "zone": "footer", "sequence": 10,
                  "pos_x": 0, "pos_y": 2, "width": cw - 40, "height": 15,
                  "text_content": '<div style="font-size:6.5pt; color:#777; line-height:1.5;">'
                                  '[company_name] · [street] · [zip] [city]<br/>'
                                  'Tel: [phone] · [email] · [website]<br/>'
                                  'IBAN: [iban] · BIC: [bic] · USt-ID: [vat]</div>'})
        E.create({"template_id": t, "name": "Seitenzahl", "element_type": "text", "zone": "footer", "sequence": 20,
                  "pos_x": cw - 35, "pos_y": 2, "width": 35, "height": 3, "text_align": "right",
                  "text_content": '<span style="font-size:6.5pt; color:#777;">Seite [page] von [pages]</span>'})


class DocumentLayoutTag(models.Model):
    _name = "document.layout.tag"
    _description = "Template Tag"

    name = fields.Char(required=True, translate=True)
    color = fields.Integer(string="Color Index")