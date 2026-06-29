from odoo import api, fields, models, _

try:
    import weasyprint
    HAS_WEASYPRINT = True
except (ImportError, OSError):
    HAS_WEASYPRINT = False


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # Die vier Default-Felder sind NICHT als config_parameter gespeichert,
    # sondern an das is_default-Flag der Templates gekoppelt: get_values liest
    # das aktuell als Standard markierte Template je doc_type, set_values setzt
    # das gewählte Template auf is_default=True (Geschwister werden vom
    # write-Override am Template automatisch zurückgesetzt). So gibt es genau
    # EINE Quelle der Wahrheit (is_default) und das Settings-Panel bleibt mit
    # dem Kanban-"Default"-Badge synchron.
    ild_invoice_template_id = fields.Many2one(
        "document.layout.template",
        string="Default Invoice Layout",
        domain=[("doc_type", "=", "account.move")],
    )
    ild_sale_template_id = fields.Many2one(
        "document.layout.template",
        string="Default Quotation Layout",
        domain=[("doc_type", "=", "sale.order")],
    )
    ild_picking_template_id = fields.Many2one(
        "document.layout.template",
        string="Default Delivery Slip Layout",
        domain=[("doc_type", "=", "stock.picking")],
    )
    ild_purchase_template_id = fields.Many2one(
        "document.layout.template",
        string="Default Purchase Order Layout",
        domain=[("doc_type", "=", "purchase.order")],
    )
    ild_pdf_engine = fields.Selection([
        ("wkhtmltopdf", "wkhtmltopdf (Odoo-Standard / Odoo.sh)"),
        ("weasyprint", "WeasyPrint (empfohlen, pixelgenau)"),
    ], string="PDF Engine", default="weasyprint",
        config_parameter="invoice_layout_designer.pdf_engine",
        help="WeasyPrint erzeugt PDFs die identisch zum Browser-Preview aussehen. "
             "Erfordert: pip install weasyprint")
    ild_weasyprint_available = fields.Boolean(
        string="WeasyPrint verfügbar",
        compute="_compute_weasyprint_available",
    )

    @api.depends("ild_pdf_engine")
    def _compute_weasyprint_available(self):
        for rec in self:
            rec.ild_weasyprint_available = HAS_WEASYPRINT

    def _default_template_for(self, doc_type):
        """Aktuell als Standard markiertes Template für einen doc_type und die
        aktive Firma (oder firmenübergreifend). Basis für die Anzeige im
        Settings-Panel."""
        Template = self.env["document.layout.template"]
        return Template.search([
            ("doc_type", "=", doc_type),
            ("is_default", "=", True),
            ("active", "=", True),
            "|", ("company_id", "=", self.env.company.id),
                 ("company_id", "=", False),
        ], limit=1)

    @api.model
    def get_values(self):
        res = super().get_values()
        res.update(
            ild_invoice_template_id=self._default_template_for("account.move").id,
            ild_sale_template_id=self._default_template_for("sale.order").id,
            ild_picking_template_id=self._default_template_for("stock.picking").id,
            ild_purchase_template_id=self._default_template_for("purchase.order").id,
        )
        return res

    def _apply_default_template(self, template):
        """Markiert das im Settings-Panel gewählte Template als Standard.
        is_default=True am Template löst über dessen write-Override das
        Zurücksetzen der anderen Defaults desselben doc_types aus."""
        if not template:
            return
        if template.is_default:
            return
        template.is_default = True

    def set_values(self):
        super().set_values()
        self._apply_default_template(self.ild_invoice_template_id)
        self._apply_default_template(self.ild_sale_template_id)
        self._apply_default_template(self.ild_picking_template_id)
        self._apply_default_template(self.ild_purchase_template_id)