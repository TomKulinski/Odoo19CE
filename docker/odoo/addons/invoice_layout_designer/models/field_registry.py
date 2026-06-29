"""
Field Registry — pure Python, no Odoo model.
Provides available fields per document type for the editor sidebar.
Called directly from controllers and other models.
"""

import logging

_logger = logging.getLogger(__name__)

# doc_type values normally equal Odoo model names. 'auftragsbestaetigung'
# (Auftragsbestätigung / order confirmation) is a VIRTUAL doc_type that shares
# the sale.order model — it only differs in routing & title. Map it back to its
# backing model wherever a real model name or field-registry key is needed.
DOC_TYPE_MODEL = {
    "auftragsbestaetigung": "sale.order",
}


def doc_type_to_model(doc_type):
    """Resolve a doc_type to its backing Odoo model name."""
    return DOC_TYPE_MODEL.get(doc_type, doc_type)


DOC_LINE_MODELS = {
    "account.move": "account.move.line",
    "sale.order": "sale.order.line",
    "stock.picking": "stock.move",
    "purchase.order": "purchase.order.line",
}

DOC_LINE_FIELDS = {
    "account.move": "invoice_line_ids",
    "sale.order": "order_line",
    "stock.picking": "move_ids",
    "purchase.order": "order_line",
}

COMMON_FIELDS = {
    "account.move": {
        "header": [
            {"path": "name", "label": "Invoice Number", "type": "char"},
            {"path": "invoice_date", "label": "Invoice Date", "type": "date"},
            {"path": "invoice_date_due", "label": "Due Date", "type": "date"},
            {"path": "ref", "label": "Reference", "type": "char"},
            {"path": "payment_reference", "label": "Payment Reference", "type": "char"},
            {"path": "move_type", "label": "Document Type", "type": "selection"},
            {"path": "state", "label": "Status", "type": "selection"},
            {"path": "currency_id.name", "label": "Currency", "type": "char"},
        ],
        "partner": [
            {"path": "partner_id.name", "label": "Customer Name", "type": "char"},
            {"path": "partner_id.street", "label": "Customer Street", "type": "char"},
            {"path": "partner_id.street2", "label": "Customer Street 2", "type": "char"},
            {"path": "partner_id.zip", "label": "Customer ZIP", "type": "char"},
            {"path": "partner_id.city", "label": "Customer City", "type": "char"},
            {"path": "partner_id.country_id.name", "label": "Customer Country", "type": "char"},
            {"path": "partner_id.vat", "label": "Customer VAT", "type": "char"},
            {"path": "partner_id.phone", "label": "Customer Phone", "type": "char"},
            {"path": "partner_id.email", "label": "Customer Email", "type": "char"},
        ],
        "company": [
            {"path": "company_id.name", "label": "Company Name", "type": "char"},
            {"path": "company_id.street", "label": "Company Street", "type": "char"},
            {"path": "company_id.zip", "label": "Company ZIP", "type": "char"},
            {"path": "company_id.city", "label": "Company City", "type": "char"},
            {"path": "company_id.country_id.name", "label": "Company Country", "type": "char"},
            {"path": "company_id.vat", "label": "Company VAT", "type": "char"},
            {"path": "company_id.phone", "label": "Company Phone", "type": "char"},
            {"path": "company_id.email", "label": "Company Email", "type": "char"},
            {"path": "company_id.website", "label": "Company Website", "type": "char"},
        ],
        "amounts": [
            {"path": "amount_untaxed", "label": "Subtotal (Untaxed)", "type": "monetary"},
            {"path": "amount_tax", "label": "Tax Amount", "type": "monetary"},
            {"path": "amount_total", "label": "Total", "type": "monetary"},
            {"path": "amount_residual", "label": "Amount Due", "type": "monetary"},
        ],
        "line_fields": [
            {"path": "product_id.name", "label": "Product", "type": "char"},
            {"path": "product_id.default_code", "label": "SKU / Reference", "type": "char"},
            {"path": "name", "label": "Description", "type": "text"},
            {"path": "quantity", "label": "Quantity", "type": "float"},
            {"path": "product_uom_id.name", "label": "Unit of Measure", "type": "char"},
            {"path": "price_unit", "label": "Unit Price", "type": "monetary"},
            {"path": "discount", "label": "Discount (%)", "type": "float"},
            {"path": "tax_ids.name", "label": "Taxes", "type": "char"},
            {"path": "price_subtotal", "label": "Subtotal", "type": "monetary"},
        ],
        "other": [
            {"path": "invoice_payment_term_id.name", "label": "Payment Terms", "type": "char"},
            {"path": "narration", "label": "Terms & Notes", "type": "html"},
            {"path": "fiscal_position_id.name", "label": "Fiscal Position", "type": "char"},
            {"path": "invoice_user_id.name", "label": "Salesperson", "type": "char"},
        ],
    },
    "sale.order": {
        "header": [
            {"path": "name", "label": "Order Number", "type": "char"},
            {"path": "date_order", "label": "Order Date", "type": "datetime"},
            {"path": "validity_date", "label": "Expiration Date", "type": "date"},
            {"path": "client_order_ref", "label": "Customer Reference", "type": "char"},
            {"path": "state", "label": "Status", "type": "selection"},
        ],
        "partner": [
            {"path": "partner_id.name", "label": "Customer Name", "type": "char"},
            {"path": "partner_id.street", "label": "Customer Street", "type": "char"},
            {"path": "partner_id.street2", "label": "Customer Street 2", "type": "char"},
            {"path": "partner_id.zip", "label": "Customer ZIP", "type": "char"},
            {"path": "partner_id.city", "label": "Customer City", "type": "char"},
            {"path": "partner_id.country_id.name", "label": "Customer Country", "type": "char"},
            {"path": "partner_id.vat", "label": "Customer VAT", "type": "char"},
        ],
        "company": [
            {"path": "company_id.name", "label": "Company Name", "type": "char"},
            {"path": "company_id.street", "label": "Company Street", "type": "char"},
            {"path": "company_id.city", "label": "Company City", "type": "char"},
            {"path": "company_id.vat", "label": "Company VAT", "type": "char"},
        ],
        "amounts": [
            {"path": "amount_untaxed", "label": "Subtotal", "type": "monetary"},
            {"path": "amount_tax", "label": "Tax Amount", "type": "monetary"},
            {"path": "amount_total", "label": "Total", "type": "monetary"},
        ],
        "line_fields": [
            {"path": "product_id.name", "label": "Product", "type": "char"},
            {"path": "name", "label": "Description", "type": "text"},
            {"path": "product_uom_qty", "label": "Quantity", "type": "float"},
            {"path": "price_unit", "label": "Unit Price", "type": "monetary"},
            {"path": "discount", "label": "Discount (%)", "type": "float"},
            {"path": "tax_id.name", "label": "Taxes", "type": "char"},
            {"path": "price_subtotal", "label": "Subtotal", "type": "monetary"},
        ],
        "other": [
            {"path": "note", "label": "Terms & Conditions", "type": "html"},
            {"path": "user_id.name", "label": "Salesperson", "type": "char"},
            {"path": "payment_term_id.name", "label": "Payment Terms", "type": "char"},
        ],
    },
    "stock.picking": {
        "header": [
            {"path": "name", "label": "Picking Number", "type": "char"},
            {"path": "origin", "label": "Source Document", "type": "char"},
            {"path": "scheduled_date", "label": "Scheduled Date", "type": "datetime"},
            {"path": "date_done", "label": "Done Date", "type": "datetime"},
            {"path": "state", "label": "Status", "type": "selection"},
        ],
        "partner": [
            {"path": "partner_id.name", "label": "Delivery Partner", "type": "char"},
            {"path": "partner_id.street", "label": "Delivery Street", "type": "char"},
            {"path": "partner_id.zip", "label": "Delivery ZIP", "type": "char"},
            {"path": "partner_id.city", "label": "Delivery City", "type": "char"},
            {"path": "partner_id.country_id.name", "label": "Delivery Country", "type": "char"},
        ],
        "company": [
            {"path": "company_id.name", "label": "Company Name", "type": "char"},
            {"path": "company_id.street", "label": "Company Street", "type": "char"},
            {"path": "company_id.city", "label": "Company City", "type": "char"},
        ],
        "amounts": [],
        "line_fields": [
            {"path": "product_id.name", "label": "Product", "type": "char"},
            {"path": "product_id.default_code", "label": "SKU", "type": "char"},
            {"path": "description_picking", "label": "Description", "type": "text"},
            {"path": "product_uom_qty", "label": "Demand", "type": "float"},
            {"path": "quantity", "label": "Done Qty", "type": "float"},
            {"path": "product_uom.name", "label": "UoM", "type": "char"},
        ],
        "other": [
            {"path": "note", "label": "Notes", "type": "text"},
            {"path": "carrier_id.name", "label": "Carrier", "type": "char"},
        ],
    },
    "purchase.order": {
        "header": [
            {"path": "name", "label": "PO Number", "type": "char"},
            {"path": "date_order", "label": "Order Date", "type": "datetime"},
            {"path": "date_planned", "label": "Planned Date", "type": "datetime"},
            {"path": "partner_ref", "label": "Vendor Reference", "type": "char"},
            {"path": "state", "label": "Status", "type": "selection"},
        ],
        "partner": [
            {"path": "partner_id.name", "label": "Vendor Name", "type": "char"},
            {"path": "partner_id.street", "label": "Vendor Street", "type": "char"},
            {"path": "partner_id.zip", "label": "Vendor ZIP", "type": "char"},
            {"path": "partner_id.city", "label": "Vendor City", "type": "char"},
            {"path": "partner_id.country_id.name", "label": "Vendor Country", "type": "char"},
            {"path": "partner_id.vat", "label": "Vendor VAT", "type": "char"},
        ],
        "company": [
            {"path": "company_id.name", "label": "Company Name", "type": "char"},
            {"path": "company_id.street", "label": "Company Street", "type": "char"},
            {"path": "company_id.city", "label": "Company City", "type": "char"},
            {"path": "company_id.vat", "label": "Company VAT", "type": "char"},
        ],
        "amounts": [
            {"path": "amount_untaxed", "label": "Subtotal", "type": "monetary"},
            {"path": "amount_tax", "label": "Tax Amount", "type": "monetary"},
            {"path": "amount_total", "label": "Total", "type": "monetary"},
        ],
        "line_fields": [
            {"path": "product_id.name", "label": "Product", "type": "char"},
            {"path": "name", "label": "Description", "type": "text"},
            {"path": "product_qty", "label": "Quantity", "type": "float"},
            {"path": "price_unit", "label": "Unit Price", "type": "monetary"},
            {"path": "taxes_id.name", "label": "Taxes", "type": "char"},
            {"path": "price_subtotal", "label": "Subtotal", "type": "monetary"},
        ],
        "other": [
            {"path": "notes", "label": "Terms & Conditions", "type": "text"},
            {"path": "user_id.name", "label": "Purchase Rep", "type": "char"},
            {"path": "payment_term_id.name", "label": "Payment Terms", "type": "char"},
        ],
    },
}


def get_available_fields(doc_type):
    """Return all available fields for a document type."""
    return COMMON_FIELDS.get(doc_type_to_model(doc_type), {})


def get_line_model(doc_type):
    """Return the line model name for a document type."""
    return DOC_LINE_MODELS.get(doc_type_to_model(doc_type), "")


def get_line_relation_field(doc_type):
    """Return the One2many field name that links doc to lines."""
    return DOC_LINE_FIELDS.get(doc_type_to_model(doc_type), "")


def resolve_field_value(record, field_path, field_format=None, default=""):
    """Resolve a dot-separated field path on a record."""
    if not record or not field_path:
        return default
    try:
        value = record
        for part in field_path.split("."):
            if hasattr(value, part):
                value = getattr(value, part)
            else:
                return default
        if hasattr(value, "_name") and not value:
            return default
        if value is False or value is None:
            return default
        if field_format:
            try:
                return field_format.format(value)
            except (ValueError, KeyError):
                pass
        if isinstance(value, float):
            return f"{value:,.2f}"
        return str(value)
    except Exception:
        return default
