{
    "name": "Invoice Layout Designer",
    "version": "19.0.2.34.0",
    "post_init_hook": "post_init_seed_defaults",
    "category": "Accounting/Invoicing",
    "summary": "WYSIWYG Layout Designer + Custom Fields Builder for Invoices, Quotations, Delivery Slips & Purchase Orders",
    "description": """
        Professional Invoice Layout Designer for Odoo 19
        =================================================

        Design your business documents visually — no coding required.

        Core Features:
        - WYSIWYG Drag & Drop Editor with A4 Canvas
        - Corporate Design Wizard (Logo → Colors → Font → Generate)
        - Support for Invoices, Quotations, Delivery Slips, Purchase Orders
        - Dynamic field placeholders from any Odoo model
        - Customizable line item tables with page-break handling
        - Optional items display (separate section / inline / hidden)
        - Logo, images, barcodes, QR codes
        - Container/Box elements with column layouts
        - Horizontal + vertical lines, shapes
        - Native Odoo standard layouts (Light/Boxed/Bold/Striped/Bubble/Wave/Folder),
          rendered pixel-perfect via WeasyPrint — identical on Community & Enterprise
        - Optional headless Chrome engine for maximum fidelity (self-hosted)
        - wkhtmltopdf fallback so the module installs and works everywhere
        - Multi-company support

        Custom Fields Builder:
        - Create custom fields on any model without code
        - 7 field types: Text, Number, Date, Dropdown, Checkbox, Link, HTML
        - 6 target models: Contacts, Products, Invoices, Sales, Purchases, Deliveries
        - Automatic form view injection
        - Per-document-type visibility control for print layouts
        - Fields instantly available in layout editor
    """,
    "author": "Your Company",
    "website": "https://www.yourcompany.com",
    "license": "OPL-1",
    # weasyprint NICHT als harte Dependency deklarieren: so installiert das
    # Modul überall (auch ohne WeasyPrint) und fällt zur Laufzeit auf
    # wkhtmltopdf zurück. WeasyPrint ist die empfohlene Default-Engine für die
    # nativen Layouts (im Docker-Image enthalten); Chrome ist optional.
    "external_dependencies": {
        "python": [],
    },
    "depends": [
        "base",
        "mail",
        "account",
        "sale_management",
        "stock",
        "purchase",
        "web",
    ],
    "data": [
        # Security
        "security/layout_security.xml",
        "security/ir.model.access.csv",
        # Views
        # wizard_views zuerst: das Template-Formular referenziert die
        # Import-Wizard-Action (%(action_template_import_wizard)d), die
        # daher zum Parse-Zeitpunkt bereits existieren muss.
        "views/wizard_views.xml",
        "views/document_layout_template_views.xml",
        "views/custom_field_views.xml",
        "views/menuitem.xml",
        "views/res_config_settings_views.xml",
        # Reports
        "report/report_actions.xml",
        "report/report_templates.xml",
        # Data
        "data/default_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "invoice_layout_designer/static/src/scss/layout_editor.scss",
            "invoice_layout_designer/static/src/js/layout_editor/layout_editor.js",
            "invoice_layout_designer/static/src/xml/layout_editor.xml",
            # Strang 4 — document chain arrow overlay on the templates kanban
            "invoice_layout_designer/static/src/js/chain_overlay/chain_kanban.js",
        ],
    },
    "images": [
        "static/description/banner.png",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
    "price": 499.00,
    "currency": "EUR",
}