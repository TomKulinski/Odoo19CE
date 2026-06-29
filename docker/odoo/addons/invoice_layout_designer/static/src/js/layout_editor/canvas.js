/** @odoo-module **/

import { Component, useRef, onMounted, useState, xml } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";

// localStorage key for the "don't warn me again" decision (Punkt 2). Persisted
// per user/browser — the simplest durable scope; a template-scoped flag would
// need a server round-trip and a model field for a pure-UX guard.
const TOTALS_WARN_KEY = "ild_totals_move_warned";

// Warning shown before the totals block is moved for the first time. The block
// is never split — this only guards the free positioning. Checkbox persists the
// decision so it never asks again.
class TotalsMoveWarning extends Component {
    static template = xml`
        <Dialog title="title" size="'md'">
            <p>Achtung: Wenn du den Summenblock frei positionierst, kannst du das Layout beschädigen. Wirklich fortfahren?</p>
            <label class="d-flex align-items-center gap-2 mt-2" style="cursor:pointer;">
                <input type="checkbox" t-on-change="(ev) => this.onToggle(ev)"/>
                <span>Für die Zukunft nicht mehr fragen</span>
            </label>
            <t t-set-slot="footer">
                <button class="btn btn-primary" t-on-click="() => this.confirm()">Fortfahren</button>
                <button class="btn btn-secondary" t-on-click="() => this.props.close()">Abbrechen</button>
            </t>
        </Dialog>`;
    static components = { Dialog };
    static props = { close: Function, onConfirm: Function };
    setup() {
        this.title = "Summenblock verschieben";
        this.dontAsk = false;
    }
    onToggle(ev) {
        this.dontAsk = ev.target.checked;
    }
    confirm() {
        this.props.onConfirm(this.dontAsk);
        this.props.close();
    }
}

export class LayoutCanvas extends Component {
    static template = "invoice_layout_designer.LayoutCanvas";
    static props = {
        template: { type: Object, optional: true },
        elements: { type: Array },
        selectedElementId: { optional: true },
        zoom: { type: Number },
        showGrid: { type: Boolean },
        gridSize: { type: Number },
        contentWidth: { type: Number },
        contentHeight: { type: Number },
        onSelectElement: { type: Function },
        onMoveElement: { type: Function },
        onResizeElement: { type: Function },
        onUpdateElement: { type: Function },
    };

    setup() {
        this.canvasRef = useRef("canvas");
        this.dialog = useService("dialog");
        this.dragState = useState({
            isDragging: false,
            isResizing: false,
            resizeHandle: null,
            startX: 0,
            startY: 0,
            startElemX: 0,
            startElemY: 0,
            startElemW: 0,
            startElemH: 0,
            elementId: null,
        });
    }

    get scaleFactor() {
        return this.props.zoom / 100;
    }

    // Convert pixel coordinates to mm (at 96 DPI, 1mm = 3.7795px)
    pxToMm(px) {
        return px / (3.7795 * this.scaleFactor);
    }

    mmToPx(mm) {
        return mm * 3.7795 * this.scaleFactor;
    }

    // Convert #RGB / #RRGGBB + opacity to rgba(). Returns null on bad input.
    _hexToRgba(hex, opacity) {
        if (!hex) return null;
        let h = hex.trim().replace(/^#/, "");
        if (h.length === 3) h = h.split("").map((c) => c + c).join("");
        if (h.length !== 6) return null;
        const r = parseInt(h.slice(0, 2), 16);
        const g = parseInt(h.slice(2, 4), 16);
        const b = parseInt(h.slice(4, 6), 16);
        if ([r, g, b].some(Number.isNaN)) return null;
        let a = parseFloat(opacity);
        if (Number.isNaN(a)) a = 1;
        a = Math.min(1, Math.max(0, a));
        return `rgba(${r}, ${g}, ${b}, ${a})`;
    }

    // ==================== ELEMENT RENDERING HELPERS ====================

    getElementStyle(elem) {
        const style = {
            position: "absolute",
            left: `${this.mmToPx(elem.pos_x)}px`,
            top: `${this.mmToPx(elem.pos_y)}px`,
            width: `${this.mmToPx(elem.width)}px`,
            height: `${this.mmToPx(elem.height)}px`,
            overflow: "hidden",
            cursor: elem.locked ? "default" : "move",
        };

        // Apply custom styles
        if (elem.style) {
            if (elem.style["font-size"]) style.fontSize = elem.style["font-size"];
            if (elem.style["font-family"]) style.fontFamily = elem.style["font-family"];
            if (elem.style["color"]) style.color = elem.style["color"];
            if (elem.style["background-color"]) style.backgroundColor = elem.style["background-color"];
            if (elem.style["border"]) style.border = elem.style["border"];
            if (elem.style["border-radius"]) style.borderRadius = elem.style["border-radius"];
            if (elem.style["font-weight"]) style.fontWeight = elem.style["font-weight"];
            if (elem.style["text-align"]) style.textAlign = elem.style["text-align"];
        }

        // Structured shape preview — mirrors element.get_shape_css() (Python).
        // Overrides any legacy style_json bg/border/radius for this element.
        if (elem.type === "shape" && elem.shape_use_structured) {
            const fill = (elem.shape_fill_color || "").trim();
            if (fill) {
                const op = elem.shape_opacity ?? 1;
                style.backgroundColor = this._hexToRgba(fill, op) || fill;
            } else {
                style.backgroundColor = "transparent";
            }
            const bs = elem.shape_border_style || "none";
            if (bs !== "none" && (elem.shape_border_width || 0) > 0) {
                style.border = `${elem.shape_border_width}px ${bs} ${elem.shape_border_color || "#000000"}`;
            } else {
                style.border = "none";
            }
            if (elem.radius_uniform ?? true) {
                style.borderRadius = `${elem.radius_tl || 0}px`;
            } else {
                style.borderRadius = `${elem.radius_tl || 0}px ${elem.radius_tr || 0}px ${elem.radius_br || 0}px ${elem.radius_bl || 0}px`;
            }
        }

        if (elem.text_align) {
            style.textAlign = elem.text_align;
        }

        if (elem.rotation) {
            style.transform = `rotate(${elem.rotation}deg)`;
        }

        if (!elem.visible) {
            style.opacity = "0.3";
        }

        // Selection highlight
        if (elem.id === this.props.selectedElementId) {
            style.outline = "2px solid #714B67";
            style.outlineOffset = "1px";
            style.zIndex = "100";
        }

        return Object.entries(style).map(([k, v]) => {
            // Convert camelCase to kebab-case
            const key = k.replace(/([A-Z])/g, "-$1").toLowerCase();
            return `${key}: ${v}`;
        }).join("; ");
    }

    getElementContent(elem) {
        switch (elem.type) {
            case "text":
                return elem.text_content || "[Text]";
            case "field":
                return elem.field_default || `{{ ${elem.field_name || "field"} }}`;
            case "image":
                if (elem.image_source === "company_logo") return "🏢 Company Logo";
                if (elem.has_image) return "📷 Image";
                return "🖼️ [Image placeholder]";
            case "table":
                return this._renderTablePreview(elem);
            case "totals":
                return '<div style="text-align:right;font-size:0.72em;color:#999;line-height:1.5;">'
                    + (elem.totals_label_subtotal || "Zwischensumme:") + " ---<br/>"
                    + (elem.totals_label_tax || "Steuer:") + " ---<br/><b>"
                    + (elem.totals_label_total || "Gesamt:") + " ---</b></div>";
            case "line":
                return "";
            case "shape":
                return "";
            case "barcode":
                return `📊 Barcode (${elem.barcode_type})`;
            case "qrcode":
                return "📱 QR Code";
            default:
                return `[${elem.type}]`;
        }
    }

    // Per-column visual overrides, mirroring _ild_col_css() on the server so the
    // in-editor mock matches the PDF. All keys optional → empty string default.
    _colCss(col) {
        let css = "";
        if (col.bold) css += "font-weight:bold;";
        if (col.fsize) css += `font-size:${col.fsize}pt;`;
        if (col.color) css += `color:${col.color};`;
        if (col.bg) css += `background-color:${col.bg};`;
        return css;
    }

    _renderTablePreview(elem) {
        // Drop hidden columns (Punkt 1), matching the renderer's filter.
        const cols = (elem.table_columns || []).filter(c => !c.hidden);
        if (cols.length === 0) return "[Line Items Table]";

        let html = '<table style="width: 100%; border-collapse: collapse; font-size: inherit;">';
        if (elem.table_show_header) {
            html += "<thead><tr>";
            for (const col of cols) {
                html += `<th style="text-align: ${col.align || "left"}; border-bottom: 1px solid #333; padding: 1px 3px; font-size: 0.8em; ${this._colCss(col)}">${col.label || ""}</th>`;
            }
            html += "</tr></thead>";
        }
        // Show 2-3 sample rows
        html += "<tbody>";
        for (let i = 0; i < 3; i++) {
            const bgColor = elem.table_zebra && i % 2 === 1 ? "#f5f5f5" : "transparent";
            html += `<tr style="background: ${bgColor};">`;
            for (const col of cols) {
                const align = col.align || "left";
                html += `<td style="text-align: ${align}; padding: 1px 3px; border-bottom: 0.5px solid #eee; font-size: 0.7em; color: #999; ${this._colCss(col)}">---</td>`;
            }
            html += "</tr>";
        }
        html += "</tbody></table>";

        if (elem.table_show_totals) {
            html += '<div style="text-align: right; font-size: 0.7em; color: #999; margin-top: 2px;">Subtotal: --- | Tax: --- | <b>Total: ---</b></div>';
        }

        return html;
    }

    getLineStyle(elem) {
        return `position: absolute; left: ${this.mmToPx(elem.pos_x)}px; top: ${this.mmToPx(elem.pos_y)}px; ` +
            `width: ${this.mmToPx(elem.width)}px; height: 0; ` +
            `border-bottom: ${elem.line_width}pt ${elem.line_style} ${elem.line_color}; ` +
            `cursor: ${elem.locked ? "default" : "move"};` +
            (elem.id === this.props.selectedElementId ? " outline: 2px solid #714B67; outline-offset: 2px; z-index: 100;" : "");
    }

    // ==================== ZONE HELPERS ====================

    getHeaderElements() {
        return this.props.elements.filter(e => e.zone === "header");
    }

    getBodyElements() {
        return this.props.elements.filter(e => e.zone === "body");
    }

    getFooterElements() {
        return this.props.elements.filter(e => e.zone === "footer");
    }

    getZoneStyle(zone) {
        const t = this.props.template;
        if (!t) return "";

        const width = this.mmToPx(this.props.contentWidth);

        if (zone === "header") {
            return `position: relative; width: ${width}px; height: ${this.mmToPx(t.header_height)}px; ` +
                `border-bottom: 1px dashed #ccc; background: rgba(113, 75, 103, 0.03);`;
        }
        if (zone === "footer") {
            return `position: relative; width: ${width}px; height: ${this.mmToPx(t.footer_height)}px; ` +
                `border-top: 1px dashed #ccc; background: rgba(113, 75, 103, 0.03); margin-top: auto;`;
        }
        // Body fills remaining space
        const bodyHeight = this.props.contentHeight - (t.header_height || 0) - (t.footer_height || 0);
        return `position: relative; width: ${width}px; min-height: ${this.mmToPx(bodyHeight)}px; flex: 1;`;
    }

    // ==================== MOUSE HANDLERS ====================

    // True when the totals block may move without warning: either the user
    // persisted "don't ask again", or they just confirmed the dialog (one-shot).
    _totalsWarnPassed() {
        try {
            if (window.localStorage.getItem(TOTALS_WARN_KEY) === "1") return true;
        } catch (e) { /* ignore */ }
        if (this._totalsProceedOnce) {
            this._totalsProceedOnce = false; // consume one-shot
            return true;
        }
        return false;
    }

    onMouseDownElement(ev, elementId) {
        ev.stopPropagation();
        ev.preventDefault();
        this.props.onSelectElement(elementId);

        const elem = this.props.elements.find(e => e.id === elementId);
        if (!elem || elem.locked) return;

        // Totals block: warn before the first free move (Punkt 2). We only guard
        // the position change — the three rows always stay one unit. The gesture
        // is aborted while the dialog is open; the user re-drags after confirming.
        if (elem.type === "totals" && !this._totalsWarnPassed()) {
            this.dialog.add(TotalsMoveWarning, {
                onConfirm: (dontAsk) => {
                    if (dontAsk) {
                        try { window.localStorage.setItem(TOTALS_WARN_KEY, "1"); } catch (e) { /* ignore */ }
                    }
                    // Allow exactly the next mousedown to perform the move.
                    this._totalsProceedOnce = true;
                },
            });
            return;
        }

        this.dragState.isDragging = true;
        this.dragState.elementId = elementId;
        this.dragState.startX = ev.clientX;
        this.dragState.startY = ev.clientY;
        this.dragState.startElemX = elem.pos_x;
        this.dragState.startElemY = elem.pos_y;

        const onMouseMove = (moveEv) => {
            if (!this.dragState.isDragging) return;
            const dx = this.pxToMm(moveEv.clientX - this.dragState.startX);
            const dy = this.pxToMm(moveEv.clientY - this.dragState.startY);
            this.props.onUpdateElement(elementId, {
                pos_x: Math.max(0, this.dragState.startElemX + dx),
                pos_y: Math.max(0, this.dragState.startElemY + dy),
            });
        };

        const onMouseUp = () => {
            this.dragState.isDragging = false;
            document.removeEventListener("mousemove", onMouseMove);
            document.removeEventListener("mouseup", onMouseUp);
        };

        document.addEventListener("mousemove", onMouseMove);
        document.addEventListener("mouseup", onMouseUp);
    }

    onResizeHandleMouseDown(ev, elementId, handle) {
        ev.stopPropagation();
        ev.preventDefault();

        const elem = this.props.elements.find(e => e.id === elementId);
        if (!elem || elem.locked) return;

        this.dragState.isResizing = true;
        this.dragState.resizeHandle = handle;
        this.dragState.elementId = elementId;
        this.dragState.startX = ev.clientX;
        this.dragState.startY = ev.clientY;
        this.dragState.startElemW = elem.width;
        this.dragState.startElemH = elem.height;
        this.dragState.startElemX = elem.pos_x;
        this.dragState.startElemY = elem.pos_y;

        const onMouseMove = (moveEv) => {
            if (!this.dragState.isResizing) return;
            const dx = this.pxToMm(moveEv.clientX - this.dragState.startX);
            const dy = this.pxToMm(moveEv.clientY - this.dragState.startY);

            const updates = {};

            if (handle.includes("e")) {
                updates.width = Math.max(5, this.dragState.startElemW + dx);
            }
            if (handle.includes("w")) {
                updates.width = Math.max(5, this.dragState.startElemW - dx);
                updates.pos_x = this.dragState.startElemX + dx;
            }
            if (handle.includes("s")) {
                updates.height = Math.max(3, this.dragState.startElemH + dy);
            }
            if (handle.includes("n")) {
                updates.height = Math.max(3, this.dragState.startElemH - dy);
                updates.pos_y = this.dragState.startElemY + dy;
            }

            this.props.onUpdateElement(elementId, updates);
        };

        const onMouseUp = () => {
            this.dragState.isResizing = false;
            document.removeEventListener("mousemove", onMouseMove);
            document.removeEventListener("mouseup", onMouseUp);
        };

        document.addEventListener("mousemove", onMouseMove);
        document.addEventListener("mouseup", onMouseUp);
    }

    onCanvasClick(ev) {
        // Deselect when clicking on empty canvas area
        if (ev.target === this.canvasRef.el || ev.target.classList.contains("ild-zone")) {
            this.props.onSelectElement(null);
        }
    }

    // ==================== DROP HANDLER ====================

    onDragOver(ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "copy";
    }

    onDrop(ev, zone) {
        ev.preventDefault();
        const data = ev.dataTransfer.getData("application/json");
        if (!data) return;

        try {
            const payload = JSON.parse(data);
            // Calculate drop position relative to zone
            const rect = ev.currentTarget.getBoundingClientRect();
            const x = this.pxToMm(ev.clientX - rect.left);
            const y = this.pxToMm(ev.clientY - rect.top);

            if (payload.action === "add_element") {
                // Will be handled by parent
                this.props.onDropElement?.(payload.type, zone, x, y);
            } else if (payload.action === "add_field") {
                this.props.onDropField?.(payload.field, zone, x, y);
            }
        } catch (e) {
            console.error("Drop parse error:", e);
        }
    }
}
