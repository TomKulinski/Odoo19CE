/** @odoo-module **/

import { Component, useState } from "@odoo/owl";

export class PropertiesPanel extends Component {
    static template = "invoice_layout_designer.PropertiesPanel";
    static props = {
        element: { optional: true },
        onUpdate: { type: Function },
        onDelete: { type: Function },
        onDuplicate: { type: Function },
    };

    setup() {
        this.state = useState({
            activeTab: "position", // position | style | type-specific
        });
    }

    get tabs() {
        const tabs = [
            { id: "position", label: "Position" },
            { id: "style", label: "Style" },
        ];
        if (this.props.element) {
            const type = this.props.element.type;
            if (type === "text") tabs.push({ id: "text", label: "Text" });
            if (type === "field") tabs.push({ id: "field", label: "Field" });
            if (type === "image") tabs.push({ id: "image", label: "Image" });
            if (type === "table") tabs.push({ id: "table", label: "Table" });
            if (type === "line") tabs.push({ id: "line", label: "Line" });
            if (type === "barcode" || type === "qrcode") tabs.push({ id: "barcode", label: "Barcode" });
        }
        return tabs;
    }

    // ==================== UPDATE HELPERS ====================

    updateField(fieldName, value) {
        if (!this.props.element) return;
        this.props.onUpdate(this.props.element.id, { [fieldName]: value });
    }

    updateStyle(property, value) {
        if (!this.props.element) return;
        const newStyle = { ...this.props.element.style, [property]: value };
        this.props.onUpdate(this.props.element.id, { style: newStyle });
    }

    updateNumber(fieldName, ev) {
        const val = parseFloat(ev.target.value) || 0;
        this.updateField(fieldName, val);
    }

    updateText(fieldName, ev) {
        this.updateField(fieldName, ev.target.value);
    }

    updateCheckbox(fieldName, ev) {
        this.updateField(fieldName, ev.target.checked);
    }

    updateSelect(fieldName, ev) {
        this.updateField(fieldName, ev.target.value);
    }

    // ==================== TABLE COLUMN EDITING ====================

    addTableColumn() {
        const elem = this.props.element;
        if (!elem) return;
        const cols = [...(elem.table_columns || [])];
        cols.push({
            field: "name",
            label: "New Column",
            width: "auto",
            align: "left",
            type: "char",
        });
        this.updateField("table_columns", cols);
    }

    removeTableColumn(index) {
        const elem = this.props.element;
        if (!elem) return;
        const cols = [...(elem.table_columns || [])];
        cols.splice(index, 1);
        this.updateField("table_columns", cols);
    }

    updateTableColumn(index, field, value) {
        const elem = this.props.element;
        if (!elem) return;
        const cols = JSON.parse(JSON.stringify(elem.table_columns || []));
        if (cols[index]) {
            cols[index][field] = value;
            this.updateField("table_columns", cols);
        }
    }
}
