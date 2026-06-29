import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

ELEMENT_TYPES = [
    ("text", "Static Text"),
    ("field", "Dynamic Field"),
    ("image", "Image / Logo"),
    ("table", "Line Items Table"),
    ("line", "Horizontal Line"),
    ("vline", "Vertical Line"),
    ("shape", "Shape / Rectangle"),
    ("container", "Container / Box"),
    ("barcode", "Barcode"),
    ("qrcode", "QR Code"),
    ("totals", "Totals Block"),
]

ZONE_SELECTION = [
    ("header", "Header"),
    ("body", "Body"),
    ("footer", "Footer"),
]

TEXT_ALIGN_SELECTION = [
    ("left", "Left"),
    ("center", "Center"),
    ("right", "Right"),
    ("justify", "Justify"),
]


class DocumentLayoutElement(models.Model):
    _name = "document.layout.element"
    _description = "Layout Element"
    _order = "zone, sequence, id"

    name = fields.Char(string="Element Name", required=True)
    template_id = fields.Many2one(
        "document.layout.template",
        string="Template",
        required=True,
        ondelete="cascade",
    )
    element_type = fields.Selection(
        selection=ELEMENT_TYPES,
        string="Type",
        required=True,
        default="text",
    )
    sequence = fields.Integer(default=10)

    # Zone: header, body, footer
    zone = fields.Selection(
        selection=ZONE_SELECTION,
        string="Zone",
        default="body",
        required=True,
    )

    # Position & Size (in mm, relative to zone top-left)
    pos_x = fields.Float(string="X Position (mm)", default=0)
    pos_y = fields.Float(string="Y Position (mm)", default=0)
    width = fields.Float(string="Width (mm)", default=50)
    height = fields.Float(string="Height (mm)", default=10)

    # Rotation
    rotation = fields.Float(string="Rotation (deg)", default=0)

    # Visibility
    visible = fields.Boolean(default=True)
    locked = fields.Boolean(default=False)

    # Provenance — distinguishes XML-seeded default elements from user-created
    # ones. XML-loader leaves this False; editor-save hooks flip it to True.
    # Reset-to-Default uses this flag to protect user customizations.
    is_user_created = fields.Boolean(
        string="User-Created",
        default=False,
        help="True if this element was created/modified via the layout editor. "
             "False if it was seeded from the module's default XML. Used by the "
             "'Reset to Default Layout' feature to only remove non-user elements.",
    )

    # ----- Type-specific fields -----

    # Text element
    text_content = fields.Text(
        string="Text Content",
        help="Static text or HTML content.",
    )
    text_align = fields.Selection(
        selection=TEXT_ALIGN_SELECTION,
        default="left",
    )

    # Field element
    field_model = fields.Char(
        string="Source Model",
        help="e.g. account.move, sale.order",
    )
    field_name = fields.Char(
        string="Field Path",
        help="Dot-separated field path, e.g. partner_id.name",
    )
    field_format = fields.Char(
        string="Format String",
        help="Python format string, e.g. '{:,.2f}' for numbers",
    )
    field_default = fields.Char(
        string="Default Value",
        help="Shown when field is empty",
    )

    # Image element
    image_data = fields.Binary(string="Image Data", attachment=True)
    image_source = fields.Selection([
        ("upload", "Uploaded Image"),
        ("company_logo", "Company Logo"),
        ("field", "From Record Field"),
    ], string="Image Source", default="upload")
    image_field_name = fields.Char(
        string="Image Field Path",
        help="Field path for dynamic image, e.g. partner_id.image_128",
    )
    image_fit = fields.Selection([
        ("contain", "Fit (Contain)"),
        ("cover", "Fill (Cover)"),
        ("stretch", "Stretch"),
    ], default="contain")

    # Table element
    table_columns_json = fields.Text(
        string="Table Columns (JSON)",
        default="[]",
        help="JSON array defining table columns.",
    )
    table_show_header = fields.Boolean(string="Show Header", default=True)
    table_show_totals = fields.Boolean(string="Show Totals", default=True)
    # Extra vertical spacing between line-item rows (mm). 0 = current default
    # cell padding (status quo). Added on top of the base padding so existing
    # templates render identically until the user raises it.
    table_row_spacing = fields.Float(
        string="Row Spacing (mm)",
        default=0.0,
        help="Additional vertical spacing between line-item rows, in mm. "
             "0 keeps the default compact spacing.",
    )
    table_zebra = fields.Boolean(string="Zebra Stripes", default=False)
    table_border_style = fields.Selection([
        ("none", "No Borders"),
        ("full", "Full Grid"),
        ("horizontal", "Horizontal Lines"),
        ("outer", "Outer Border Only"),
        ("bold", "Bold Header Line"),
    ], default="horizontal")

    # Optional items handling (sale.order)
    table_optional_mode = fields.Selection([
        ("hide", "Hide Optional Items"),
        ("inline", "Show Inline (grayed out, labeled 'Optional')"),
        ("separate", "Show in Separate Section Below"),
    ], default="separate", string="Optional Items Display",
        help="How to display optional items (quantity=0) in quotations.")
    table_optional_label = fields.Char(
        string="Optional Section Title",
        default="Optional Items",
    )
    table_optional_show_qty = fields.Boolean(
        string="Show Original Quantity for Optional",
        default=True,
        help="Show the intended quantity instead of 0 for optional items.",
    )

    # Page break control
    table_rows_per_page = fields.Integer(
        string="Max Rows Per Page",
        default=25,
        help="Maximum line items per page before forcing a page break. 0 = auto.",
    )
    table_repeat_header = fields.Boolean(
        string="Repeat Header on New Page",
        default=True,
    )
    table_show_carryover = fields.Boolean(
        string="Show Carryover Line",
        default=True,
        help="Show 'Carried over from page X' on continuation pages.",
    )

    # Section/Note/Subtotal display in tables
    table_show_sections = fields.Boolean(
        string="Show Section Headers",
        default=True,
        help="Show section header rows (display_type='line_section') in the table.",
    )
    table_show_notes = fields.Boolean(
        string="Show Note Rows",
        default=True,
        help="Show note rows (display_type='line_note') in the table.",
    )
    table_show_subtotals = fields.Boolean(
        string="Show Section Subtotals",
        default=False,
        help="Show a subtotal row after each section.",
    )
    table_section_style = fields.Char(
        string="Section Header CSS",
        default="font-weight:600; font-size:9pt; background:#f5f5f5;",
        help="CSS style for section header rows.",
    )
    table_note_style = fields.Char(
        string="Note Row CSS",
        default="font-style:italic; color:#666;",
        help="CSS style for note rows.",
    )
    table_subtotal_style = fields.Char(
        string="Subtotal CSS",
        default="font-weight:500; border-top:0.5pt solid #ccc;",
        help="CSS style for section subtotal rows.",
    )

    # Totals labels and style
    totals_label_subtotal = fields.Char(
        string="Subtotal Label",
        default="Zwischensumme:",
    )
    totals_label_tax = fields.Char(
        string="Tax Label",
        default="Steuer:",
    )
    totals_label_total = fields.Char(
        string="Total Label",
        default="Gesamt:",
    )
    totals_style = fields.Selection([
        ("default", "Standard"),
        ("bold", "Bold with line"),
        ("large", "Large and prominent"),
        ("minimal", "Minimal (only total bold)"),
        ("boxed", "Boxed"),
    ], string="Totals Style", default="default")
    # Summenblock als EINHEIT verschieben (numerisch, mm). Der Block bleibt ein
    # zusammenhängendes Element — die drei Zeilen (Zwischensumme/Steuer/Gesamt)
    # lösen sich nie. Beim Inline-Block (unter der Tabelle) verschiebt der
    # Versatz den ganzen Block relativ zu seiner Flussposition (position:relative).
    # Beide Defaults 0 = Status quo (kein Versatz).
    totals_offset_x = fields.Float(
        string="Summen X-Versatz (mm)", default=0.0,
        help="Horizontaler Versatz des gesamten Summenblocks in mm. "
             "0 = Standardposition (rechtsbündig unter der Tabelle).")
    totals_offset_y = fields.Float(
        string="Summen Y-Versatz (mm)", default=0.0,
        help="Vertikaler Versatz des gesamten Summenblocks in mm. "
             "0 = Standardposition direkt unter der Tabelle.")
    # Zusätzlicher vertikaler Abstand zwischen den Summenzeilen (mm), analog zur
    # Produkttabelle. 0 = bisheriger kompakter Abstand (Status quo).
    totals_row_spacing = fields.Float(
        string="Summen Zeilenabstand (mm)", default=0.0,
        help="Zusätzlicher vertikaler Abstand zwischen den Summenzeilen "
             "(Zwischensumme/Steuer/Gesamt) in mm. 0 = Standard.")

    # Line/shape element
    line_color = fields.Char(string="Line Color", default="#000000")
    line_width = fields.Float(string="Line Width (pt)", default=1.0)
    line_style = fields.Selection([
        ("solid", "Solid"),
        ("dashed", "Dashed"),
        ("dotted", "Dotted"),
    ], default="solid")

    # ----- Shape element: structured styling (no user HTML/CSS) -----
    # Gate flag: legacy shapes (style_json) render unchanged while False.
    # Editor flips it True as soon as a structured shape field is touched, and
    # the best-effort migration sets it for parseable legacy shapes. This keeps
    # existing customer templates byte-identical until a user opts in.
    shape_use_structured = fields.Boolean(
        string="Shape: Structured Styling",
        default=False,
        help="When True, the shape is rendered from the structured border/"
             "radius/fill fields below. When False, the legacy style JSON is "
             "used (backward compatible).",
    )
    shape_border_style = fields.Selection([
        ("none", "None"),
        ("solid", "Solid"),
        ("dashed", "Dashed"),
        ("dotted", "Dotted"),
    ], string="Shape Border Style", default="none")
    shape_border_width = fields.Float(string="Shape Border Width (px)", default=1.0)
    shape_border_color = fields.Char(string="Shape Border Color", default="#000000")
    # Per-corner radius (px). radius_uniform=True ⇒ radius_tl drives all four.
    radius_uniform = fields.Boolean(string="Uniform Corner Radius", default=True)
    radius_tl = fields.Float(string="Radius Top-Left (px)", default=0.0)
    radius_tr = fields.Float(string="Radius Top-Right (px)", default=0.0)
    radius_br = fields.Float(string="Radius Bottom-Right (px)", default=0.0)
    radius_bl = fields.Float(string="Radius Bottom-Left (px)", default=0.0)
    shape_fill_color = fields.Char(
        string="Shape Fill Color",
        default="",
        help="Hex fill color. Empty = transparent (no fill).",
    )
    shape_opacity = fields.Float(
        string="Shape Fill Opacity (0-1)",
        default=1.0,
        help="Opacity of the fill color only; the border stays opaque.",
    )

    # Container element
    container_layout = fields.Selection([
        ("free", "Free Positioning (absolute)"),
        ("columns_2", "2 Columns (50/50)"),
        ("columns_3", "3 Columns (33/33/33)"),
        ("columns_2_left", "2 Columns (66/33)"),
        ("columns_2_right", "2 Columns (33/66)"),
        ("stack", "Stacked (vertical flow)"),
    ], default="free", string="Container Layout")
    container_padding = fields.Float(string="Inner Padding (mm)", default=2)
    container_bg_color = fields.Char(string="Background Color", default="transparent")
    container_border = fields.Char(string="Border", default="none", help="CSS border, e.g. '1pt solid #ccc'")
    container_border_radius = fields.Float(string="Border Radius (mm)", default=0)
    container_shadow = fields.Boolean(string="Drop Shadow", default=False)
    container_opacity = fields.Float(string="Opacity (0-1)", default=1.0)
    container_child_ids = fields.Text(
        string="Child Element IDs (JSON)",
        default="[]",
        help="JSON array of element IDs contained within this container.",
    )

    # Barcode / QR
    barcode_type = fields.Selection([
        ("code128", "Code 128"),
        ("ean13", "EAN-13"),
        ("qr", "QR Code"),
        ("swiss_qr", "Swiss QR-Bill"),
    ], default="code128")
    barcode_field = fields.Char(
        string="Barcode Data Field",
        help="Field path for barcode data, e.g. name (invoice number)",
    )
    barcode_static_value = fields.Char(
        string="Static Barcode Value",
    )

    # Style JSON — complete CSS-like styling
    style_json = fields.Text(
        string="Style (JSON)",
        default="{}",
    )
    # Config JSON — type-specific additional config
    config_json = fields.Text(
        string="Config (JSON)",
        default="{}",
    )

    # Conditional visibility — element only shown in PDF when condition is met
    condition_field = fields.Char(
        string="Condition Field",
        help="Field path to check, e.g. 'partner_id.vat', 'state', 'move_type'. Leave empty to always show.",
    )
    condition_operator = fields.Selection([
        ("set", "Field is set (not empty)"),
        ("unset", "Field is empty"),
        ("eq", "Equals (=)"),
        ("neq", "Not equals (!=)"),
        ("contains", "Contains"),
        ("gt", "Greater than (>)"),
        ("lt", "Less than (<)"),
        ("in", "Is one of (comma-separated)"),
    ], string="Condition Operator", default="set")
    condition_value = fields.Char(
        string="Condition Value",
        help="Value to compare against. For 'is one of': comma-separated list (e.g. 'draft,sent'). "
             "Examples: state = 'sale' for confirmed orders, move_type = 'out_refund' for credit notes.",
    )

    # Per-page visibility for multi-page documents
    show_on_page = fields.Selection([
        ("all", "Alle Seiten"),
        ("first", "Nur erste Seite"),
        ("last", "Nur letzte Seite"),
        ("middle", "Nur Folgeseiten (nicht erste)"),
    ], string="Anzeigen auf", default="first",
        help="Auf welchen Seiten soll dieses Element erscheinen? "
             "'Erste Seite': z.B. Adresse, Rechnungskopf. "
             "'Letzte Seite': z.B. Totale, Signatur. "
             "'Alle': z.B. Logo, Footer, Seitenzahl.")

    # Geschäftsfall: Eine Positionstabelle wächst mit der Zeilenzahl, alle
    # anderen Elemente nicht. Inhalte, die immer DIREKT unter der letzten
    # Tabellenzeile stehen müssen (Beträge, Zahlungshinweis, Unterschrift,
    # Notizen), können nicht auf festen mm-Koordinaten liegen — bei vielen
    # Positionen würde die Tabelle sie überdecken. "Unter Tabelle" verankert
    # ein Body-Element im Dokumentenfluss direkt nach der Tabelle, sodass es
    # unabhängig von der Zeilenzahl korrekt nachrutscht.
    anchor_mode = fields.Selection([
        ("fixed", "Feste Position"),
        ("after_table", "Unter Tabelle"),
    ], string="Verankerung", default="fixed",
        help="'Feste Position': Element liegt auf festen Koordinaten "
             "(Standard). 'Unter Tabelle': Element fließt im Body direkt "
             "unter die Positionstabelle und rutscht mit ihrer Höhe nach.")

    # Vertikaler Abstand (mm) eines unter der Tabelle verankerten Elements zum
    # vorherigen Flussinhalt (Tabelle bzw. vorheriger Anker-Block). Erlaubt es,
    # z.B. Beträge eng an die Tabelle zu setzen, eine Unterschrift dagegen mit
    # mehr Luft. Nur relevant bei anchor_mode = "after_table".
    anchor_gap = fields.Float(
        string="Abstand zur Tabelle (mm)", default=3.0,
        help="Abstand dieses Blocks zum darüberliegenden Flussinhalt "
             "(Tabelle oder vorheriger Anker-Block) in Millimetern. "
             "Nur wirksam bei Verankerung 'Unter Tabelle'.")

    # Explizite Fluss-Gruppe (Strang 1, Teil B): mehrere Body-Elemente, die der
    # Nutzer bewusst verbindet (z.B. Box + Summenblock), teilen denselben Key
    # und fließen als EINE Einheit unter der Tabelle nach. Innerhalb der Gruppe
    # bleibt die relative Anordnung (X + relatives Y) erhalten. Leer = nicht
    # gruppiert. Rein explizit — keine Automatik über Überlappung/Nähe.
    flow_group_key = fields.Char(
        string="Fluss-Gruppe", default="",
        help="Elemente mit gleichem Key bilden eine explizite Fluss-Gruppe und "
             "fließen als Einheit unter der Tabelle. Leer = nicht gruppiert.")

    # Fixier-Anker (Strang 1): Element wird an ein Strukturelement fixiert und
    # fließt als Overlay mit diesem mit, behält seinen relativen Versatz. Nur EINE
    # Ebene — ein fixiertes Element ist selbst nie Ziel. Ziel aktuell nur der
    # Summenblock ("totals"). "none" = nicht fixiert (Status quo).
    fixed_to = fields.Selection([
        ("none", "Nicht fixiert"),
        ("totals", "An Summenblock fixiert"),
    ], string="Fixiert an", default="none",
        help="Fixiert dieses Element an den Summenblock: es fließt als Overlay "
             "mit dem Summenblock mit und behält seinen relativen Versatz. "
             "'Nicht fixiert' = feste/eigene Position.")

    def get_style_data(self):
        """Return parsed style JSON."""
        self.ensure_one()
        try:
            return json.loads(self.style_json or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_style_data(self, data):
        """Store style as JSON."""
        self.ensure_one()
        self.style_json = json.dumps(data, ensure_ascii=False)

    def get_config_data(self):
        """Return parsed config JSON."""
        self.ensure_one()
        try:
            return json.loads(self.config_json or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_config_data(self, data):
        self.ensure_one()
        self.config_json = json.dumps(data, ensure_ascii=False)

    def get_table_columns(self):
        """Return parsed table columns definition."""
        self.ensure_one()
        try:
            return json.loads(self.table_columns_json or "[]")
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def _hex_to_rgba(hex_color, opacity):
        """Convert #RGB / #RRGGBB to rgba(r,g,b,a). Returns None on bad input
        so callers can fall back to the raw value."""
        if not hex_color:
            return None
        h = hex_color.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            return None
        try:
            r = int(h[0:2], 16)
            g = int(h[2:4], 16)
            b = int(h[4:6], 16)
        except ValueError:
            return None
        try:
            a = float(opacity)
        except (TypeError, ValueError):
            a = 1.0
        a = min(1.0, max(0.0, a))
        # Trim to 3 decimals, keep ints clean
        a_str = ("%.3f" % a).rstrip("0").rstrip(".")
        return "rgba(%d, %d, %d, %s)" % (r, g, b, a_str or "0")

    def get_shape_css(self):
        """Translate the structured shape fields into a CSS declaration string.
        Used by the renderer (PDF) and mirrored client-side in canvas.js for the
        live editor preview. Never interprets user-supplied CSS."""
        self.ensure_one()
        parts = []

        # Fill: empty color => transparent (no fill). Otherwise rgba from hex.
        fill = (self.shape_fill_color or "").strip()
        if fill:
            rgba = self._hex_to_rgba(fill, self.shape_opacity)
            parts.append("background-color: %s" % (rgba or fill))
        else:
            parts.append("background-color: transparent")

        # Border
        style = self.shape_border_style or "none"
        if style != "none" and (self.shape_border_width or 0) > 0:
            color = self.shape_border_color or "#000000"
            # Trim trailing zeros on the width for clean output
            w = ("%g" % self.shape_border_width)
            parts.append("border: %spx %s %s" % (w, style, color))
        else:
            parts.append("border: none")

        # Per-corner radius (px). Uniform => single value drives all four.
        def _r(v):
            return "%gpx" % (v or 0)
        if self.radius_uniform:
            parts.append("border-radius: %s" % _r(self.radius_tl))
        else:
            parts.append("border-radius: %s %s %s %s" % (
                _r(self.radius_tl), _r(self.radius_tr),
                _r(self.radius_br), _r(self.radius_bl),
            ))

        return "; ".join(parts) + ";"

    @api.constrains("pos_x", "pos_y", "width", "height")
    def _check_dimensions(self):
        for rec in self:
            if rec.width < 0 or rec.height < 0:
                raise ValidationError(_("Width and height must be positive."))

    def copy(self, default=None):
        """Ensure binary image data is properly duplicated (not shared reference)."""
        default = dict(default or {})
        # image_data is a Binary(attachment=True) field — Odoo handles
        # attachment duplication automatically when copy() is called, but
        # we ensure the name gets a copy suffix for clarity.
        if "name" not in default:
            default["name"] = _("%s (copy)") % self.name
        return super().copy(default=default)

    def to_editor_dict(self):
        """Serialize element for the frontend editor."""
        self.ensure_one()
        return {
            "id": self.id,
            "name": self.name,
            "type": self.element_type,
            "zone": self.zone,
            "sequence": self.sequence,
            "pos_x": self.pos_x,
            "pos_y": self.pos_y,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "visible": self.visible,
            "locked": self.locked,
            # Text
            "text_content": self.text_content or "",
            "text_align": self.text_align,
            # Field
            "field_model": self.field_model or "",
            "field_name": self.field_name or "",
            "field_format": self.field_format or "",
            "field_default": self.field_default or "",
            # Image
            "image_source": self.image_source,
            "image_fit": self.image_fit,
            "image_field_name": self.image_field_name or "",
            "has_image": bool(self.image_data),
            # Table
            "table_columns": self.get_table_columns(),
            "table_show_header": self.table_show_header,
            "table_show_totals": self.table_show_totals,
            "table_row_spacing": self.table_row_spacing,
            "table_zebra": self.table_zebra,
            "table_border_style": self.table_border_style,
            "table_optional_mode": self.table_optional_mode,
            "table_optional_label": self.table_optional_label,
            "table_optional_show_qty": self.table_optional_show_qty,
            "table_rows_per_page": self.table_rows_per_page,
            "table_repeat_header": self.table_repeat_header,
            "table_show_carryover": self.table_show_carryover,
            # Sections / notes / subtotals: gespeichert vom Controller, mussten
            # aber auch hier serialisiert werden — sonst kommen die Häkchen +
            # Style-Selects beim Laden als undefined zurück (Persistenz-Bug).
            "table_show_sections": self.table_show_sections,
            "table_show_notes": self.table_show_notes,
            "table_show_subtotals": self.table_show_subtotals,
            "table_section_style": self.table_section_style or "",
            "table_note_style": self.table_note_style or "",
            "table_subtotal_style": self.table_subtotal_style or "",
            # Totals (Inline-Table + eigenständiges Totals-Element)
            "totals_label_subtotal": self.totals_label_subtotal or "",
            "totals_label_tax": self.totals_label_tax or "",
            "totals_label_total": self.totals_label_total or "",
            "totals_style": self.totals_style or "default",
            "totals_offset_x": self.totals_offset_x,
            "totals_offset_y": self.totals_offset_y,
            "totals_row_spacing": self.totals_row_spacing,
            # Line
            "line_color": self.line_color,
            "line_width": self.line_width,
            "line_style": self.line_style,
            # Shape (structured styling)
            "shape_use_structured": self.shape_use_structured,
            "shape_border_style": self.shape_border_style or "none",
            "shape_border_width": self.shape_border_width,
            "shape_border_color": self.shape_border_color or "#000000",
            "radius_uniform": self.radius_uniform,
            "radius_tl": self.radius_tl,
            "radius_tr": self.radius_tr,
            "radius_br": self.radius_br,
            "radius_bl": self.radius_bl,
            "shape_fill_color": self.shape_fill_color or "",
            "shape_opacity": self.shape_opacity,
            # Container
            "container_layout": self.container_layout,
            "container_padding": self.container_padding,
            "container_bg_color": self.container_bg_color,
            "container_border": self.container_border,
            "container_border_radius": self.container_border_radius,
            "container_shadow": self.container_shadow,
            "container_opacity": self.container_opacity,
            # Barcode
            "barcode_type": self.barcode_type,
            "barcode_field": self.barcode_field or "",
            "barcode_static_value": self.barcode_static_value or "",
            # Conditional visibility
            "condition_field": self.condition_field or "",
            "condition_operator": self.condition_operator or "set",
            "condition_value": self.condition_value or "",
            # Per-page visibility
            "show_on_page": self.show_on_page or "first",
            # Anchoring (Body-Element fließt nach Tabelle vs. feste Position)
            "anchor_mode": self.anchor_mode or "fixed",
            "anchor_gap": self.anchor_gap,
            "flow_group_key": self.flow_group_key or "",
            "fixed_to": self.fixed_to or "none",
            # Styles
            "style": self.get_style_data(),
            "config": self.get_config_data(),
        }