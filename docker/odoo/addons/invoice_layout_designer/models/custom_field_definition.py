import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

FIELD_TYPE_SELECTION = [
    ("char", "Text (Single Line)"),
    ("text", "Text (Multi Line)"),
    ("html", "Rich Text (HTML)"),
    ("integer", "Whole Number"),
    ("float", "Decimal Number"),
    ("date", "Date"),
    ("datetime", "Date & Time"),
    ("boolean", "Checkbox (Yes/No)"),
    ("selection", "Dropdown (Selection)"),
    ("many2one", "Link to Another Record"),
]

TARGET_MODEL_SELECTION = [
    ("res.partner", "Contacts / Partners"),
    ("product.template", "Products"),
    ("account.move", "Invoices / Credit Notes"),
    ("sale.order", "Quotations / Sales Orders"),
    ("purchase.order", "Purchase Orders"),
    ("stock.picking", "Delivery Slips"),
]

DOC_TYPE_SELECTION = [
    ("account.move", "Invoice / Credit Note"),
    ("sale.order", "Quotation / Sales Order"),
    ("stock.picking", "Delivery Slip"),
    ("purchase.order", "Purchase Order"),
]

# Where to inject the field in the form view
VIEW_PLACEMENT_SELECTION = [
    ("auto", "Automatic (best fit)"),
    ("after_name", "After Name/Title"),
    ("group_extra", "In 'Other Info' / Extra Group"),
    ("new_tab", "New Custom Tab"),
]


class CustomFieldDefinition(models.Model):
    _name = "custom.field.definition"
    _description = "Custom Field Definition"
    _order = "target_model, sequence, name"
    _inherit = ["mail.thread"]

    name = fields.Char(
        string="Field Label",
        required=True,
        tracking=True,
        help="The label shown to users, e.g. 'Shipment Incoterm'",
    )
    technical_name = fields.Char(
        string="Technical Name",
        compute="_compute_technical_name",
        store=True,
        readonly=False,
        help="Auto-generated from label. Prefix x_ is added automatically.",
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    # Target
    target_model = fields.Selection(
        selection=TARGET_MODEL_SELECTION,
        string="Target Model",
        required=True,
        tracking=True,
    )
    target_model_name = fields.Char(
        compute="_compute_target_model_name",
    )

    # Field type
    field_type = fields.Selection(
        selection=FIELD_TYPE_SELECTION,
        string="Field Type",
        required=True,
        default="char",
        tracking=True,
    )

    # Type-specific config
    selection_options = fields.Text(
        string="Dropdown Options (one per line)",
        help="Enter one option per line, e.g.:\nEXW\nFOB\nCIF\nDDP",
    )
    many2one_model = fields.Char(
        string="Related Model",
        help="Technical name of the target model, e.g. 'account.incoterms' or 'res.country'",
    )
    many2one_model_id = fields.Many2one(
        "ir.model",
        string="Related Model (Select)",
        ondelete="set null",
        domain=[("transient", "=", False)],
    )

    # Default value
    default_value = fields.Char(
        string="Default Value",
        help="Default value for the field (as string)",
    )

    # Constraints
    is_required = fields.Boolean(string="Required", default=False)
    is_readonly = fields.Boolean(string="Read Only", default=False)

    # View placement
    view_placement = fields.Selection(
        selection=VIEW_PLACEMENT_SELECTION,
        default="group_extra",
        string="Show in Form View",
    )
    custom_tab_name = fields.Char(
        string="Custom Tab Name",
        default="Custom Fields",
        help="Tab name when placement is 'New Custom Tab'",
    )

    # Document type visibility for PRINT layouts
    show_on_invoice = fields.Boolean(string="Show on Invoice Layout", default=True)
    show_on_sale = fields.Boolean(string="Show on Quotation Layout", default=True)
    show_on_picking = fields.Boolean(string="Show on Delivery Slip Layout", default=False)
    show_on_purchase = fields.Boolean(string="Show on Purchase Order Layout", default=False)

    # Technical: link to actual ir.model.fields record
    ir_model_field_id = fields.Many2one(
        "ir.model.fields",
        string="Created Field",
        readonly=True,
        ondelete="set null",
    )
    state = fields.Selection([
        ("draft", "Draft"),
        ("active", "Active (Field Created)"),
        ("error", "Error"),
    ], default="draft", tracking=True)
    error_message = fields.Text(string="Error Details", readonly=True)

    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
    )

    @api.depends("name")
    def _compute_technical_name(self):
        for rec in self:
            if rec.name and not rec.technical_name:
                # Generate technical name from label
                clean = rec.name.lower().strip()
                clean = clean.replace(" ", "_").replace("-", "_")
                clean = "".join(c for c in clean if c.isalnum() or c == "_")
                # Ensure x_ prefix (Odoo requirement for custom fields)
                if not clean.startswith("x_"):
                    clean = f"x_{clean}"
                rec.technical_name = clean
            elif not rec.name:
                rec.technical_name = ""

    @api.depends("target_model")
    def _compute_target_model_name(self):
        for rec in self:
            match = dict(TARGET_MODEL_SELECTION).get(rec.target_model, "")
            rec.target_model_name = match

    @api.onchange("many2one_model_id")
    def _onchange_many2one_model_id(self):
        if self.many2one_model_id:
            self.many2one_model = self.many2one_model_id.model

    @api.constrains("technical_name", "target_model")
    def _check_unique_field(self):
        for rec in self:
            if rec.technical_name and rec.target_model:
                existing = self.search([
                    ("id", "!=", rec.id),
                    ("technical_name", "=", rec.technical_name),
                    ("target_model", "=", rec.target_model),
                ])
                if existing:
                    raise ValidationError(
                        _("A custom field with technical name '%s' already exists on %s.")
                        % (rec.technical_name, rec.target_model)
                    )

    @api.constrains("field_type", "selection_options")
    def _check_selection_options(self):
        for rec in self:
            if rec.field_type == "selection" and not rec.selection_options:
                raise ValidationError(_("Dropdown fields require at least one option."))

    @api.constrains("field_type", "many2one_model")
    def _check_many2one_model(self):
        for rec in self:
            if rec.field_type == "many2one" and not rec.many2one_model:
                raise ValidationError(_("Link fields require a target model."))

    # ==================== ACTIONS ====================

    def action_create_field(self):
        """Create the actual field on the target model via ir.model.fields."""
        self.ensure_one()
        if self.state == "active" and self.ir_model_field_id:
            raise UserError(_("Field already exists. Delete and recreate if you need changes."))

        try:
            vals = self._prepare_ir_model_fields_vals()

            # Create the field
            ir_field = self.env["ir.model.fields"].sudo().create(vals)

            # Create view injection
            self._inject_into_form_view()

            self.write({
                "ir_model_field_id": ir_field.id,
                "state": "active",
                "error_message": False,
            })

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Success"),
                    "message": _("Field '%s' created on %s!") % (self.name, self.target_model_name),
                    "type": "success",
                    "sticky": False,
                },
            }

        except Exception as e:
            self.write({
                "state": "error",
                "error_message": str(e),
            })
            raise UserError(_("Failed to create field: %s") % str(e))

    def action_delete_field(self):
        """Remove the created field from the target model."""
        self.ensure_one()
        if self.ir_model_field_id:
            try:
                # Remove view injection first
                self._remove_from_form_view()
                # Delete the field
                self.ir_model_field_id.sudo().unlink()
            except Exception as e:
                _logger.warning("Could not delete ir.model.fields: %s", e)

        self.write({
            "ir_model_field_id": False,
            "state": "draft",
            "error_message": False,
        })

    def _prepare_ir_model_fields_vals(self):
        """Build the vals dict for ir.model.fields.create()."""
        self.ensure_one()
        ir_model = self.env["ir.model"].search([
            ("model", "=", self.target_model)
        ], limit=1)
        if not ir_model:
            raise UserError(_("Model '%s' not found.") % self.target_model)

        vals = {
            "name": self.technical_name,
            "field_description": self.name,
            "model_id": ir_model.id,
            "ttype": self.field_type,
            "required": self.is_required,
            "readonly": self.is_readonly,
            "copied": True,
            "store": True,
        }

        # Selection options
        if self.field_type == "selection":
            options = [
                line.strip() for line in (self.selection_options or "").split("\n")
                if line.strip()
            ]
            selection_vals = [(opt, opt) for opt in options]
            vals["selection_ids"] = [
                (0, 0, {"value": opt, "name": opt, "sequence": i * 10})
                for i, opt in enumerate(options)
            ]

        # Many2one
        if self.field_type == "many2one":
            comodel = self.many2one_model
            if not comodel:
                raise UserError(_("Please select a related model for the link field."))
            comodel_record = self.env["ir.model"].search([
                ("model", "=", comodel)
            ], limit=1)
            if not comodel_record:
                raise UserError(_("Related model '%s' not found.") % comodel)
            vals["relation"] = comodel

        return vals

    def _inject_into_form_view(self):
        """Add the field to the target model's form view via an inherited view."""
        self.ensure_one()
        # Find the primary form view
        primary_view = self.env["ir.ui.view"].search([
            ("model", "=", self.target_model),
            ("type", "=", "form"),
            ("inherit_id", "=", False),
        ], limit=1, order="priority")

        if not primary_view:
            _logger.warning("No primary form view found for %s", self.target_model)
            return

        # Build the arch XML for the inherited view
        field_xml = f'<field name="{self.technical_name}"/>'

        if self.view_placement == "after_name":
            arch = f"""
            <data>
                <field name="name" position="after">
                    {field_xml}
                </field>
            </data>
            """
        elif self.view_placement == "new_tab":
            tab_name = self.custom_tab_name or "Custom Fields"
            arch = f"""
            <data>
                <xpath expr="//notebook" position="inside">
                    <page string="{tab_name}" name="custom_fields_{self.id}">
                        <group>
                            {field_xml}
                        </group>
                    </page>
                </xpath>
            </data>
            """
        else:
            # Default: try to add to a group, or at the end of the sheet
            arch = f"""
            <data>
                <xpath expr="//sheet" position="inside">
                    <group string="Custom Fields" name="ild_custom_fields">
                        {field_xml}
                    </group>
                </xpath>
            </data>
            """

        # Create the inherited view
        self.env["ir.ui.view"].sudo().create({
            "name": f"ild.custom.field.{self.technical_name}",
            "model": self.target_model,
            "inherit_id": primary_view.id,
            "arch": arch,
            "priority": 99,
        })

    def _remove_from_form_view(self):
        """Remove the inherited view that injects this field."""
        self.ensure_one()
        views = self.env["ir.ui.view"].sudo().search([
            ("name", "=", f"ild.custom.field.{self.technical_name}"),
            ("model", "=", self.target_model),
        ])
        views.sudo().unlink()

    # ==================== LAYOUT INTEGRATION ====================

    def get_fields_for_layout(self, doc_type):
        """
        Return custom fields that should appear in the layout editor
        for a specific document type.
        Called by the field registry to enrich the picker sidebar.
        """
        domain = [("state", "=", "active")]

        # Filter by document type visibility
        if doc_type == "account.move":
            domain.append(("show_on_invoice", "=", True))
        elif doc_type == "sale.order":
            domain.append(("show_on_sale", "=", True))
        elif doc_type == "stock.picking":
            domain.append(("show_on_picking", "=", True))
        elif doc_type == "purchase.order":
            domain.append(("show_on_purchase", "=", True))

        fields_list = self.search(domain)
        result = []
        for f in fields_list:
            # Determine the field path based on target model
            if f.target_model == doc_type:
                path = f.technical_name
            elif f.target_model == "res.partner":
                path = f"partner_id.{f.technical_name}"
            elif f.target_model == "product.template":
                # Only useful in line items context
                continue
            else:
                continue

            result.append({
                "path": path,
                "label": f"✦ {f.name}",
                "type": f.field_type,
                "custom": True,
                "custom_field_id": f.id,
            })

        return result
