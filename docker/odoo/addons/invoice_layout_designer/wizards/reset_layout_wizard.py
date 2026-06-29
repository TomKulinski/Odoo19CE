"""Wizard for the 'Reset Template to Default Layout' feature.

Shows a preview of which elements would be deleted (or preserved) BEFORE
destructive action. User can:
- See element count + names
- Optionally export current layout as JSON for backup
- Confirm or cancel

Only targets elements where:
    is_user_created = False  AND  xml_id IS NULL

These are "future ghosts" — elements without provenance that the system
didn't know at upgrade time. User-created elements (flagged by the editor)
and XML-seeded defaults (have xml_id) are always preserved.
"""
import json

from odoo import api, fields, models, _


class ResetLayoutWizard(models.TransientModel):
    _name = "document.layout.reset.wizard"
    _description = "Reset Template to Default Layout"

    template_id = fields.Many2one(
        "document.layout.template",
        string="Template",
        required=True,
        ondelete="cascade",
    )

    # Summary fields populated by default_get
    user_element_count = fields.Integer(
        string="User Elements (kept)",
        readonly=True,
        help="Elements created or edited in the layout editor — will NOT be deleted.",
    )
    xml_element_count = fields.Integer(
        string="Default Elements (kept)",
        readonly=True,
        help="Elements from the module's default template — will NOT be deleted.",
    )
    ghost_element_count = fields.Integer(
        string="Ghost Elements (to remove)",
        readonly=True,
        help="Elements with no provenance (no xml_id, not user-created). These are removed on reset.",
    )
    ghost_element_names = fields.Text(
        string="Elements to Remove",
        readonly=True,
        help="Names of ghost elements that will be deleted.",
    )
    user_element_names = fields.Text(
        string="Preserved User Elements",
        readonly=True,
    )

    export_before_reset = fields.Boolean(
        string="Download JSON Backup before Reset",
        default=True,
        help="Recommended. Downloads a JSON snapshot of the current layout "
             "before deletion, so you can re-import it if needed.",
    )

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        tid = self.env.context.get("active_id") or vals.get("template_id")
        if not tid:
            return vals

        template = self.env["document.layout.template"].browse(tid)
        if not template.exists():
            return vals

        user_els = template.element_ids.filtered(lambda e: e.is_user_created)
        xml_els = template.element_ids.filtered(
            lambda e: not e.is_user_created and self._has_xml_id(e)
        )
        ghost_els = template.element_ids.filtered(
            lambda e: not e.is_user_created and not self._has_xml_id(e)
        )

        vals.update({
            "template_id": template.id,
            "user_element_count": len(user_els),
            "xml_element_count": len(xml_els),
            "ghost_element_count": len(ghost_els),
            "ghost_element_names": "\n".join(
                f"• {e.name} ({e.element_type}, zone={e.zone})"
                for e in ghost_els
            ) or _("(none — nothing to remove)"),
            "user_element_names": "\n".join(
                f"• {e.name}" for e in user_els
            ) or _("(none)"),
        })
        return vals

    def _has_xml_id(self, element):
        """True if this element has an ir_model_data / xml_id link."""
        count = self.env["ir.model.data"].sudo().search_count([
            ("model", "=", "document.layout.element"),
            ("res_id", "=", element.id),
        ])
        return count > 0

    def action_confirm_reset(self):
        """Delete ghost elements (and optionally trigger JSON export first)."""
        self.ensure_one()
        template = self.template_id

        # 1. Optional: snapshot layout to JSON attachment
        attachment_id = None
        if self.export_before_reset:
            payload = template.export_template_json()
            filename = f"{(template.name or 'layout').replace(' ', '_')}_backup_before_reset.json"
            att = self.env["ir.attachment"].create({
                "name": filename,
                "type": "binary",
                "datas": __import__("base64").b64encode(payload.encode("utf-8")),
                "res_model": "document.layout.template",
                "res_id": template.id,
                "mimetype": "application/json",
            })
            attachment_id = att.id

        # 2. Delete ghosts
        ghosts = template.element_ids.filtered(
            lambda e: not e.is_user_created and not self._has_xml_id(e)
        )
        removed = len(ghosts)
        if ghosts:
            ghosts.unlink()

        # 3. Report back to user
        msg = _(
            "Reset complete. %(count)d ghost element(s) removed. "
            "User-created and default elements were preserved.",
            count=removed,
        )
        if attachment_id:
            msg += _(" Backup JSON saved as attachment.")

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Template Reset"),
                "message": msg,
                "sticky": False,
                "next": {
                    "type": "ir.actions.act_window",
                    "res_model": "document.layout.template",
                    "res_id": template.id,
                    "views": [(False, "form")],
                    "target": "current",
                },
            },
        }
