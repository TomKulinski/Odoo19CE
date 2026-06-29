import json

from odoo import api, fields, models, _


class DocumentLayoutStyle(models.Model):
    _name = "document.layout.style"
    _description = "Layout Style Preset"
    _order = "name"

    name = fields.Char(string="Style Name", required=True, translate=True)
    company_id = fields.Many2one(
        "res.company",
        default=lambda self: self.env.company,
    )

    # Typography
    font_family = fields.Char(
        string="Font Family",
        default="Helvetica",
        help="CSS font-family value",
    )
    font_size = fields.Float(string="Font Size (pt)", default=10.0)
    font_weight = fields.Selection([
        ("normal", "Normal"),
        ("bold", "Bold"),
        ("light", "Light"),
    ], default="normal")
    font_style = fields.Selection([
        ("normal", "Normal"),
        ("italic", "Italic"),
    ], default="normal")
    line_height = fields.Float(string="Line Height", default=1.4)

    # Colors
    color = fields.Char(string="Text Color", default="#000000")
    background_color = fields.Char(string="Background Color", default="transparent")

    # Spacing
    padding_top = fields.Float(default=0)
    padding_right = fields.Float(default=0)
    padding_bottom = fields.Float(default=0)
    padding_left = fields.Float(default=0)

    # Border
    border_width = fields.Float(string="Border Width (pt)", default=0)
    border_color = fields.Char(default="#000000")
    border_style = fields.Selection([
        ("none", "None"),
        ("solid", "Solid"),
        ("dashed", "Dashed"),
        ("dotted", "Dotted"),
    ], default="none")
    border_radius = fields.Float(string="Border Radius (mm)", default=0)

    # Corporate design fields
    primary_color = fields.Char(string="Primary Brand Color", default="#1a1a2e")
    secondary_color = fields.Char(string="Secondary Brand Color", default="#16213e")
    accent_color = fields.Char(string="Accent Color", default="#0f3460")
    heading_font = fields.Char(string="Heading Font", default="Helvetica")
    body_font = fields.Char(string="Body Font", default="Helvetica")

    def to_css_dict(self):
        """Convert style to CSS property dict."""
        self.ensure_one()
        css = {}
        if self.font_family:
            css["font-family"] = self.font_family
        if self.font_size:
            css["font-size"] = f"{self.font_size}pt"
        if self.font_weight and self.font_weight != "normal":
            css["font-weight"] = self.font_weight
        if self.font_style and self.font_style != "normal":
            css["font-style"] = self.font_style
        if self.line_height:
            css["line-height"] = str(self.line_height)
        if self.color:
            css["color"] = self.color
        if self.background_color and self.background_color != "transparent":
            css["background-color"] = self.background_color
        # Padding
        padding_parts = [
            f"{self.padding_top}mm",
            f"{self.padding_right}mm",
            f"{self.padding_bottom}mm",
            f"{self.padding_left}mm",
        ]
        if any(getattr(self, f"padding_{d}") > 0 for d in ("top", "right", "bottom", "left")):
            css["padding"] = " ".join(padding_parts)
        # Border
        if self.border_width > 0 and self.border_style != "none":
            css["border"] = f"{self.border_width}pt {self.border_style} {self.border_color}"
        if self.border_radius > 0:
            css["border-radius"] = f"{self.border_radius}mm"
        return css

    def to_css_string(self):
        """Convert to inline CSS string."""
        css_dict = self.to_css_dict()
        return "; ".join(f"{k}: {v}" for k, v in css_dict.items())
