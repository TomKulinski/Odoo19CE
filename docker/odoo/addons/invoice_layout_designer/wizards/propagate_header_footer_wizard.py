import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PropagateHeaderFooterWizard(models.TransientModel):
    """Rückfrage-Dialog (Task 2): wählt aus, auf welche verknüpften Ziele die
    Header/Footer der Sale Order übernommen werden. Die eigentliche, body-sichere
    Propagation liegt in document.layout.template._propagate_header_footer_to().
    """
    _name = "document.layout.propagate.wizard"
    _description = "Propagate Header/Footer to linked templates"

    source_template_id = fields.Many2one(
        "document.layout.template",
        string="Sale Order (Quelle)",
        required=True,
        readonly=True,
    )
    available_target_ids = fields.Many2many(
        "document.layout.template",
        "doc_layout_prop_wiz_avail_rel", "wiz_id", "tpl_id",
        string="Verfügbare Ziele",
        compute="_compute_available_targets",
    )
    target_ids = fields.Many2many(
        "document.layout.template",
        "doc_layout_prop_wiz_target_rel", "wiz_id", "tpl_id",
        string="Ziel-Dokumente",
        domain="[('id', 'in', available_target_ids)]",
        help="Nur diese Ziele erhalten Header/Footer der Sale Order. "
             "Body der Ziele bleibt unberührt.",
    )

    @api.depends("source_template_id")
    def _compute_available_targets(self):
        for wiz in self:
            wiz.available_target_ids = wiz.source_template_id.linked_target_ids

    def action_propagate(self):
        self.ensure_one()
        src = self.source_template_id
        targets = self.target_ids
        if not targets:
            raise UserError(_("Bitte mindestens ein Ziel-Dokument auswählen."))
        for target in targets:
            src._propagate_header_footer_to(target)
        _logger.info(
            "ILD propagate wizard: '%s' -> %d target(s) done.",
            src.name, len(targets),
        )
        return {"type": "ir.actions.act_window_close"}
