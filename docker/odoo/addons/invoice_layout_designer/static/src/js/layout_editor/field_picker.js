/** @odoo-module **/

import { Component, useState } from "@odoo/owl";

export class FieldPicker extends Component {
    static template = "invoice_layout_designer.FieldPicker";
    static props = {
        availableFields: { type: Object },
        onAddField: { type: Function },
        onAddElement: { type: Function },
    };

    setup() {
        this.state = useState({
            searchQuery: "",
            expandedCategories: { header: true, partner: false, company: false, amounts: false, line_fields: false, other: false },
        });
    }

    get elementTypes() {
        return [
            { type: "text", icon: "📝", label: "Text Block" },
            { type: "field", icon: "🏷️", label: "Dynamic Field" },
            { type: "image", icon: "🖼️", label: "Image / Logo" },
            { type: "table", icon: "📊", label: "Line Items Table" },
            { type: "totals", icon: "Σ", label: "Summen-Block" },
            { type: "line", icon: "➖", label: "Separator Line" },
            { type: "shape", icon: "⬜", label: "Rectangle" },
            { type: "barcode", icon: "📊", label: "Barcode" },
            { type: "qrcode", icon: "📱", label: "QR Code" },
        ];
    }

    get categoryLabels() {
        return {
            header: "Document Fields",
            partner: "Customer / Partner",
            company: "Company Info",
            amounts: "Amounts & Totals",
            line_fields: "Line Item Fields",
            other: "Other Fields",
        };
    }

    get filteredFields() {
        const query = this.state.searchQuery.toLowerCase().trim();
        if (!query) return this.props.availableFields;

        const result = {};
        for (const [cat, fields] of Object.entries(this.props.availableFields)) {
            const filtered = fields.filter(f =>
                f.label.toLowerCase().includes(query) ||
                f.path.toLowerCase().includes(query)
            );
            if (filtered.length > 0) {
                result[cat] = filtered;
            }
        }
        return result;
    }

    toggleCategory(cat) {
        this.state.expandedCategories[cat] = !this.state.expandedCategories[cat];
    }

    onDragStartElement(ev, type) {
        ev.dataTransfer.setData("application/json", JSON.stringify({
            action: "add_element",
            type: type,
        }));
        ev.dataTransfer.effectAllowed = "copy";
    }

    onDragStartField(ev, field) {
        ev.dataTransfer.setData("application/json", JSON.stringify({
            action: "add_field",
            field: field,
        }));
        ev.dataTransfer.effectAllowed = "copy";
    }

    onClickElement(type) {
        this.props.onAddElement(type);
    }

    onClickField(field) {
        this.props.onAddField(field);
    }
}
