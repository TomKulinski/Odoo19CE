/** @odoo-module **/

import {Component, useState, useRef, onMounted, onWillUnmount} from "@odoo/owl";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";
import {rpc} from "@web/core/network/rpc";
import {_t} from "@web/core/l10n/translation";

const MM = 3.7795; // 1mm at 96dpi

// ═══════════════════════════════════════════════════════════
// UNDO STACK
// ═══════════════════════════════════════════════════════════
class UndoStack {
    constructor() {
        this.u = [];
        this.r = [];
        this._t = 0;
    }

    push(s) {
        const n = Date.now();
        if (n - this._t < 300 && this.u.length) this.u[this.u.length - 1] = s;
        else this.u.push(s);
        if (this.u.length > 60) this.u.shift();
        this.r = [];
        this._t = n;
    }

    undo() {
        if (this.u.length <= 1) return null;
        this.r.push(this.u.pop());
        return this.u[this.u.length - 1];
    }

    redo() {
        if (!this.r.length) return null;
        const s = this.r.pop();
        this.u.push(s);
        return s;
    }

    get canUndo() {
        return this.u.length > 1;
    }

    get canRedo() {
        return this.r.length > 0;
    }
}

// ═══════════════════════════════════════════════════════════
// DIN 5008 GUIDE POSITIONS (Form B, relative to top-left of content area)
// ═══════════════════════════════════════════════════════════
const DIN5008_GUIDES = {
    // Faltmarken & Lochmarke (y-positions from page top, adjusted for margins)
    fold1_y: 87,       // Faltmarke 1: 87mm from page top
    punch_y: 148.5,    // Lochmarke: 148.5mm
    fold2_y: 192,      // Faltmarke 2: 192mm
    // Anschriftfeld
    addr_top: 27,      // 27mm from top margin = 45mm from page top (with 10mm margin + header)
    addr_left: 0,      // Left aligned (after 25mm margin)
    addr_width: 85,    // 85mm wide
    addr_height: 27.3, // ~27mm high
    // Informationsblock (rechts)
    info_left: 100,
    info_top: 0,
    info_width: 75,
};

// ═══════════════════════════════════════════════════════════
// MAIN EDITOR COMPONENT
// ═══════════════════════════════════════════════════════════
export class LayoutEditor extends Component {
    static template = "invoice_layout_designer.LayoutEditorFull";
    static props = {action: {type: Object, optional: true}, actionId: {optional: true}, "*": true};

    setup() {
        this.notification = useService("notification");
        this.actionService = useService("action");
        this.hist = new UndoStack();
        this._nid = 1;

        this.state = useState({
            loading: true, saving: false, previewing: false, dirty: false,
            tpl: {}, els: [], fields: {},
            // Selection: supports multi-select
            selId: null, selIds: [],
            zoom: 100, grid: true, snap: true, gridSz: 2,
            leftPanel: "elements", fSearch: "",
            fieldBrowser: {open: false, model: "", fields: {}, models: [], loading: false, search: ""},
            expCat: {
                header: true,
                partner: true,
                company: false,
                amounts: true,
                line_fields: false,
                other: false,
                custom: true
            },
            propTab: "pos",
            guides: {x: [], y: []},
            editingTextId: null,
            // New features
            showPreview: false, previewSrcdoc: "", previewFullscreen: false,
            showShortcuts: false,
            showDinGuides: false,
            showRulers: true,
            showCtxMenu: false, ctxMenuX: 0, ctxMenuY: 0, ctxMenuElId: null,
            // Field Browser modal
            showFieldBrowser: false, fieldBrowserTarget: "", fieldBrowserColIndex: -1,
            fieldBrowserModel: "", fieldBrowserFields: {}, fieldBrowserModels: [],
            fieldBrowserSearch: "",
            showConditionHelper: false,
        });

        this._onKey = this._handleKey.bind(this);
        this._onCtxClose = () => {
            this.state.showCtxMenu = false;
        };
        onMounted(async () => {
            document.addEventListener("keydown", this._onKey);
            document.addEventListener("click", this._onCtxClose);
            await this._load();
        });
        onWillUnmount(() => {
            document.removeEventListener("keydown", this._onKey);
            document.removeEventListener("click", this._onCtxClose);
        });
    }

    // ═══════════════════════════════════════════════════════
    // GETTERS
    // ═══════════════════════════════════════════════════════
    get tid() {
        // Normalfall: ueber den Button geoeffnet -> params.template_id.
        // Deeplink/Reload (direkte URL) verliert die params; dort traegt
        // der Breadcrumb die Datensatz-ID als active_id. Fallback darauf,
        // sonst laedt der Editor mit leerem Canvas.
        return (
            this.props.action?.params?.template_id
            || this.props.action?.context?.active_id
            || this.props.action?.context?.params?.template_id
        );
    }

    get sel() {
        return this.state.els.find(e => e.id === this.state.selId) || null;
    }

    get multiSel() {
        return this.state.els.filter(e => this.state.selIds.includes(e.id));
    }

    get hasMultiSel() {
        return this.state.selIds.length > 1;
    }

    get cw() {
        const t = this.state.tpl;
        return (t.paper_width || 210) - (t.margin_left || 15) - (t.margin_right || 15);
    }

    get ch() {
        const t = this.state.tpl;
        return (t.paper_height || 297) - (t.margin_top || 15) - (t.margin_bottom || 15);
    }

    get sc() {
        return this.state.zoom / 100;
    }

    // Bounding box (mm) of a zone — used to clamp element positions/sizes.
    zoneBox(zone) {
        const t = this.state.tpl || {};
        const w = this.cw;
        const hh = t.header_height || 35, fh = t.footer_height || 25;
        if (zone === "header") return {w, h: hh};
        if (zone === "footer") return {w, h: fh};
        return {w, h: Math.max(0, this.ch - hh - fh)};
    }

    // Clamp an element's geometry so it stays inside its zone.
    // Returns a patch object (only the fields that changed).
    clampToZone(el) {
        const {w: zw, h: zh} = this.zoneBox(el.zone);
        const patch = {};
        let width = Math.max(2, Math.min(el.width || 0, zw));
        let height = Math.max(1, Math.min(el.height || 0, zh));
        let x = Math.max(0, Math.min(el.pos_x || 0, zw - width));
        let y = Math.max(0, Math.min(el.pos_y || 0, zh - height));
        if (width !== el.width) patch.width = width;
        if (height !== el.height) patch.height = height;
        if (x !== el.pos_x) patch.pos_x = x;
        if (y !== el.pos_y) patch.pos_y = y;
        return patch;
    }

    // Grow header_height/footer_height so every element in those zones fits
    // vertically. Prevents Y-coordinate collisions between header/body elements
    // in the rendered PDF when users have placed header content beyond the
    // configured header_height. Returns true if any zone was enlarged.
    _growZonesToContent() {
        const t = this.state.tpl;
        if (!t) return false;
        let maxHeader = 0, maxFooter = 0;
        for (const el of this.state.els) {
            const bottom = (Number(el.pos_y) || 0) + (Number(el.height) || 0);
            if (el.zone === "header" && bottom > maxHeader) maxHeader = bottom;
            else if (el.zone === "footer" && bottom > maxFooter) maxFooter = bottom;
        }
        let changed = false;
        if (maxHeader > (Number(t.header_height) || 0)) {
            t.header_height = Math.ceil(maxHeader);
            changed = true;
        }
        if (maxFooter > (Number(t.footer_height) || 0)) {
            t.footer_height = Math.ceil(maxFooter);
            changed = true;
        }
        return changed;
    }

    // One-shot: grow zones to contain content, then clamp every element to its zone.
    // `quiet=true` suppresses notifications (used by save()).
    autoFitAll(quiet = false) {
        const zonesGrew = this._growZonesToContent();
        let n = 0;
        for (const el of this.state.els) {
            const patch = this.clampToZone(el);
            if (Object.keys(patch).length) {
                Object.assign(el, patch);
                n++;
            }
        }
        if (n || zonesGrew) {
            this.state.dirty = true;
            this._commit();
            if (!quiet) {
                this.notification.add(_t(`Auto-Fit: ${n} Element(e) angepasst${zonesGrew ? ", Zonen vergrößert" : ""}.`), {type: "success"});
            }
        } else if (!quiet) {
            this.notification.add(_t("Auto-Fit: alles passt bereits."), {type: "info"});
        }
        return {clamped: n, zonesGrew};
    }

    mm(v) {
        return v * MM * this.sc;
    }

    px2mm(v) {
        return v / (MM * this.sc);
    }

    // ═══════════════════════════════════════════════════════
    // DATA LOAD/SAVE
    // ═══════════════════════════════════════════════════════
    async _load() {
        try {
            const r = await rpc("/layout/editor/load", {template_id: this.tid});
            if (r.error) {
                this.notification.add(r.error, {type: "danger"});
                return;
            }
            this.state.tpl = r.template;
            this.state.els = r.elements;
            this.state.fields = r.available_fields || {};
            this.state.loading = false;
            this.hist.push(this._snap());
            this._autoFitInitialZoom();
        } catch {
            this.notification.add("Load failed", {type: "danger"});
            this.state.loading = false;
        }
    }

    _autoFitInitialZoom() {
        // Großen Bildschirmen mehr Canvas-Fläche geben, damit das A4-Papier
        // nicht winzig im Drag-Drop-Bereich liegt. Berechnet wird nach der
        // tatsächlichen Breite des Canvas-Containers; Fallback auf
        // window.innerWidth wenn der Container noch nicht im DOM ist.
        const paperWidthMm = (this.state.tpl && this.state.tpl.paper_width) || 210;
        // 3.78 px pro mm bei 100% Zoom (entspricht 96 DPI).
        const paperPx = paperWidthMm * 3.78;
        const targetFillPct = 0.85;
        setTimeout(() => {
            const scroll = document.querySelector(".ild-canvas-scroll");
            const availPx = scroll ? scroll.clientWidth : (window.innerWidth - 600);
            if (!availPx || !paperPx) return;
            const desired = Math.floor((availPx * targetFillPct / paperPx) * 100);
            const z = Math.max(80, Math.min(160, desired));
            this.state.zoom = z;
        }, 30);
    }

    async save() {
        if (this.state.saving) return;
        // Auto-fit before save: grow zones to contain content, then clamp stray
        // elements. Guarantees editor-saved geometry matches what the renderer
        // draws (no header/body Y-overlap in PDF).
        this.autoFitAll(true);
        this.state.saving = true;
        try {
            const r = await rpc("/layout/editor/save", {
                template_id: this.tid,
                template_data: this.state.tpl,
                elements_data: this.state.els,
            });
            if (r.success) {
                this.state.dirty = false;
                this.notification.add(_t("Saved!"), {type: "success"});
                // Hinweis, dass dieses Layout meist mehrere Belegarten abdeckt
                // (z.B. Kunden- + Lieferantenrechnungen). Text kommt bereits
                // übersetzt aus dem Backend, daher kein _t() nötig.
                if (r.coverage) {
                    this.notification.add(r.coverage, {
                        type: "info",
                        sticky: true,
                    });
                }
            }
        } catch {
            this.notification.add(_t("Save failed"), {type: "danger"});
        }
        this.state.saving = false;
    }

    _snap() {
        return JSON.parse(JSON.stringify({t: this.state.tpl, e: this.state.els}));
    }

    _restore(s) {
        this.state.tpl = s.t;
        this.state.els = s.e;
        this.state.dirty = true;
    }

    _commit() {
        this.state.dirty = true;
        this.hist.push(this._snap());
        // Auto-refresh preview if panel is open (debounced)
        if (this.state.showPreview) {
            clearTimeout(this._previewTimer);
            this._previewTimer = setTimeout(() => this._refreshPreview(), 1500);
        }
    }

    // Build iframe srcdoc for preview — paper rendered at real mm size,
    // then scaled down with CSS transform to fit the iframe width.
    _buildPreviewSrcdoc(html, pw, ph, mt, mr, mb, ml) {
        return `<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<style>
html,body { margin:0; padding:0; background:#f0f0f0; font-family:Arial,Helvetica,sans-serif; }
.preview-wrap { padding:20px 10px; display:flex; justify-content:center; }
.preview-scale { transform-origin: top center; transform: scale(var(--s,1)); }
.preview-page { background:white; width:${pw}mm; padding:${mt}mm ${mr}mm ${mb}mm ${ml}mm; box-shadow:0 2px 12px rgba(0,0,0,.25); min-height:${ph}mm; font-size:9pt; box-sizing:border-box; }
</style></head>
<body>
<div class="preview-wrap"><div class="preview-scale"><div class="preview-page">${html}</div></div></div>
<style>
/* Override position:fixed for preview — fixed is for PDF multi-page repeating.
   In the single-page preview iframe, fixed elements must use absolute so they
   render relative to .ild-page (inside margin padding), not the iframe viewport. */
.ild-page-all, .ild-page-last, .ild-page-middle { position: absolute !important; }
</style>
<script>
(function(){
  const paperW = ${pw} * 3.7795; // mm -> px at 96dpi
  function fit(){
    const avail = document.documentElement.clientWidth - 40; // padding
    const scale = Math.min(1, avail / paperW);
    document.querySelector('.preview-scale').style.setProperty('--s', scale);
  }
  fit();
  window.addEventListener('resize', fit);
})();
</script>
</body></html>`;
    }

    async _refreshPreview() {
        // Silent save + preview refresh without notifications
        if (this.state.previewing) return;
        this.state.previewing = true;
        try {
            await rpc("/layout/editor/save", {
                template_id: this.tid,
                template_data: this.state.tpl,
                elements_data: this.state.els,
            });
            this.state.dirty = false;
            const r = await rpc("/layout/editor/preview", {template_id: this.tid});
            if (r.html) {
                const t = this.state.tpl || {};
                const pw = t.paper_width || 210, ph = t.paper_height || 297;
                const mt = t.margin_top ?? 15, mr = t.margin_right ?? 15, mb = t.margin_bottom ?? 15, ml = t.margin_left ?? 15;
                this.state.previewSrcdoc = this._buildPreviewSrcdoc(r.html, pw, ph, mt, mr, mb, ml);
            }
        } catch (e) { /* silent fail */
        }
        this.state.previewing = false;
    }

    undo() {
        const s = this.hist.undo();
        if (s) this._restore(s);
    }

    redo() {
        const s = this.hist.redo();
        if (s) this._restore(s);
    }

    // ═══════════════════════════════════════════════════════
    // ELEMENT CRUD
    // ═══════════════════════════════════════════════════════
    _defs(type) {
        return {
            text: {name: "Text", w: 80, h: 10, tc: "Your text here", st: {"font-size": "10pt"}},
            field: {name: "Field", w: 60, h: 7, st: {"font-size": "10pt"}},
            image: {name: "Logo", w: 40, h: 22, is: "company_logo", st: {}},
            table: {
                name: "Line Items", w: 180, h: 80, st: {},
                cols: [
                    {field: "name", label: "Description", width: "40%", align: "left"},
                    {field: "quantity", label: "Qty", width: "10%", align: "right"},
                    {field: "price_unit", label: "Price", width: "15%", align: "right", type: "monetary"},
                    {field: "discount", label: "Disc%", width: "10%", align: "right"},
                    {field: "price_subtotal", label: "Subtotal", width: "15%", align: "right", type: "monetary"},
                ]
            },
            line: {name: "H-Line", w: 180, h: 0.5, st: {}},
            vline: {name: "V-Line", w: 0.5, h: 40, st: {}},
            shape: {name: "Rectangle", w: 40, h: 20, st: {"background-color": "#f0f0f0", border: "0.5pt solid #ccc"}},
            container: {
                name: "Container",
                w: 90,
                h: 40,
                st: {border: "0.5pt solid #ddd", "background-color": "#fafafa"}
            },
            barcode: {name: "Barcode", w: 50, h: 15, st: {}},
            qrcode: {name: "QR Code", w: 25, h: 25, st: {}},
            totals: {name: "Summen", w: 70, h: 28, st: {}},
        }[type] || {name: "Element", w: 50, h: 10, st: {}};
    }

    _hasOverlap(x, y, w, h, zone) {
        for (const el of this.state.els) {
            if (el.zone !== zone) continue;
            const ex = el.pos_x || 0, ey = el.pos_y || 0, ew = el.width || 0, eh = el.height || 0;
            if (x < ex + ew && x + w > ex && y < ey + eh && y + h > ey) {
                return true;
            }
        }
        return false;
    }

    _findFreePos(type, zone) {
        const d = this._defs(type);
        const w = d.w, h = d.h;
        const {h: zh} = this.zoneBox(zone);
        const zoneEls = this.state.els.filter(e => e.zone === zone);

        if (zoneEls.length === 0) {
            return {x: 10, y: 10};
        }

        // Place below the lowest existing element with 5mm gap
        let maxBottom = 0;
        for (const el of zoneEls) {
            const bottom = (el.pos_y || 0) + (el.height || 0);
            if (bottom > maxBottom) maxBottom = bottom;
        }
        const yBelow = maxBottom + 5;
        if (yBelow + h <= zh) {
            return {x: 10, y: yBelow};
        }

        // No vertical space — cascade from top-left with 5mm offsets
        let ox = 10, oy = 10;
        for (let i = 0; i < 20; i++) {
            if (!this._hasOverlap(ox, oy, w, h, zone)) {
                return {x: ox, y: oy};
            }
            ox += 5;
            oy += 5;
            if (ox + w > this.cw) ox = 5;
            if (oy + h > zh) oy = 5;
        }
        return {x: 10, y: 10};
    }

    addEl(type, zone = "body", x = undefined, y = undefined) {
        if (x === undefined || y === undefined) {
            const pos = this._findFreePos(type, zone);
            x = pos.x;
            y = pos.y;
        }
        const d = this._defs(type);
        const el = {
            id: `new_${Date.now()}_${this._nid++}`,
            name: d.name,
            type,
            zone,
            sequence: this.state.els.length * 10 + 10,
            pos_x: x,
            pos_y: y,
            width: d.w,
            height: d.h,
            rotation: 0,
            visible: true,
            locked: false,
            text_content: d.tc || "",
            text_align: "left",
            field_model: this.state.tpl.doc_type || "",
            field_name: "",
            field_format: "",
            field_default: "",
            image_source: d.is || "upload",
            image_fit: "contain",
            image_field_name: "",
            image_data_b64: "",
            table_columns: d.cols || [],
            table_show_header: true,
            table_show_totals: true,
            table_zebra: false,
            table_border_style: "horizontal",
            table_optional_mode: "separate",
            table_optional_label: "Optional Items",
            table_optional_show_qty: true,
            table_rows_per_page: 25,
            table_repeat_header: true,
            table_show_carryover: true,
            table_show_sections: true,
            table_show_notes: true,
            table_show_subtotals: false,
            table_section_style: "",
            table_note_style: "",
            table_subtotal_style: "",
            totals_label_subtotal: "Zwischensumme:",
            totals_label_tax: "Steuer:",
            totals_label_total: "Gesamt:",
            totals_style: "default",
            line_color: "#000000",
            line_width: 1,
            line_style: "solid",
            container_layout: "free",
            container_padding: 2,
            container_bg_color: "transparent",
            container_border: "0.5pt solid #ddd",
            container_border_radius: 0,
            container_shadow: false,
            container_opacity: 1,
            barcode_type: type === "qrcode" ? "qr" : "code128",
            barcode_field: "",
            barcode_static_value: "",
            style: {...(d.st || {})},
            config: {},
            // Feature #8: Conditional visibility
            condition_field: "",
            condition_operator: "set",
            condition_value: "",
            // "first" = position:absolute (relative to .ild-page, inside margins)
            // "all"   = position:fixed — only for repeating elements like page numbers/logo
            show_on_page: "first",
            // "fixed" = feste Koordinaten (Standard); "after_table" = Block
            // fließt im Body unter die Positionstabelle und rutscht mit nach.
            anchor_mode: "fixed",
            // Vertikaler Abstand (mm) zum vorherigen Flussinhalt bei "after_table".
            anchor_gap: 3.0,
            // Explizite Fluss-Gruppe (leer = nicht gruppiert).
            flow_group_key: "",
            // Fixier-Anker an Strukturelement ("none"/"totals").
            fixed_to: "none",
        };
        this.state.els.push(el);
        this._selectEl(el.id);
        this._commit();
    }

    // ═══════════════════════════════════════════════════════
    // FLUSS-GRUPPEN (Strang 1, Teil B) — explizit, nicht automatisch
    // ═══════════════════════════════════════════════════════
    groupSelected() {
        // Verbindet die aktuell mehrfach selektierten Body-Elemente zu EINER
        // Fluss-Gruppe: gleicher flow_group_key + anchor_mode="after_table",
        // damit sie als Einheit unter der Tabelle nachfließen.
        const ids = this.state.selIds || [];
        if (ids.length < 2) {
            this.notification.add(_t("Mindestens 2 Elemente auswählen, um zu gruppieren."), {type: "warning"});
            return;
        }
        const key = `g_${Date.now()}_${this._nid++}`;
        for (const id of ids) {
            this.upEl(id, {flow_group_key: key, anchor_mode: "after_table"});
        }
        this.notification.add(_t("Elemente als Fluss-Gruppe verbunden."), {type: "success"});
    }

    ungroupSelected() {
        // Löst die Gruppenzugehörigkeit (Key leeren). Ist nur EIN Mitglied
        // selektiert, wird die GANZE Gruppe gelöst (alle mit gleichem Key).
        // anchor_mode bleibt unangetastet — der Nutzer entscheidet separat.
        let ids = (this.state.selIds && this.state.selIds.length)
            ? [...this.state.selIds]
            : (this.sel ? [this.sel.id] : []);
        if (!ids.length) return;
        const keys = new Set(
            this.state.els
                .filter(e => ids.includes(e.id) && e.flow_group_key)
                .map(e => e.flow_group_key)
        );
        if (keys.size) {
            for (const e of this.state.els) {
                if (e.flow_group_key && keys.has(e.flow_group_key)) ids.push(e.id);
            }
        }
        ids = [...new Set(ids)];
        for (const id of ids) {
            this.upEl(id, {flow_group_key: ""});
        }
        this.notification.add(_t("Gruppierung gelöst."), {type: "success"});
    }

    addFieldEl(f) {
        this.addEl("field", "body");
        const el = this.state.els[this.state.els.length - 1];
        Object.assign(el, {
            name: f.label,
            field_name: f.path,
            field_default: `[${f.label}]`,
            field_model: this.state.tpl.doc_type
        });
        this._commit();
    }

    addTotalsBlock() {
        // Umgebogen: erzeugt KEINE losen Einzelfelder mehr (die doppelte,
        // widersprüchliche Summen verursachten). Es gibt nur noch den EINEN
        // zusammenhängenden type=totals-Block. Methode bleibt als Alias auf
        // addEl("totals") erhalten, falls noch irgendwo referenziert.
        this.addEl("totals");
    }

    delSel() {
        if (this.state.selIds.length > 1) {
            // Multi-delete
            this.state.els = this.state.els.filter(e => !this.state.selIds.includes(e.id));
            this.state.selIds = [];
            this.state.selId = null;
            this._commit();
        } else if (this.state.selId) {
            const i = this.state.els.findIndex(e => e.id === this.state.selId);
            if (i >= 0) {
                this.state.els.splice(i, 1);
                this.state.selId = null;
                this.state.selIds = [];
                this._commit();
            }
        }
    }

    dupSel() {
        if (!this.sel) return;
        const c = JSON.parse(JSON.stringify(this.sel));
        c.id = `new_${Date.now()}_${this._nid++}`;
        c.name += " (copy)";
        c.pos_x += 5;
        c.pos_y += 5;
        this.state.els.push(c);
        this._selectEl(c.id);
        this._commit();
    }

    upEl(id, v) {
        const el = this.state.els.find(e => e.id === id);
        if (el) {
            Object.assign(el, v);
            this._commit();
        }
    }

    // ═══════════════════════════════════════════════════════
    // SELECTION (single + multi)
    // ═══════════════════════════════════════════════════════
    _selectEl(id, additive = false) {
        if (additive) {
            // Toggle in multi-selection
            const idx = this.state.selIds.indexOf(id);
            if (idx >= 0) {
                this.state.selIds.splice(idx, 1);
                this.state.selId = this.state.selIds[this.state.selIds.length - 1] || null;
            } else {
                this.state.selIds.push(id);
                this.state.selId = id;
            }
        } else {
            this.state.selId = id;
            this.state.selIds = [id];
        }
    }

    clearSelection() {
        this.state.selId = null;
        this.state.selIds = [];
    }

    // ═══════════════════════════════════════════════════════
    // KEYBOARD
    // ═══════════════════════════════════════════════════════
    _handleKey(ev) {
        if (["INPUT", "TEXTAREA", "SELECT"].includes(ev.target.tagName)) return;
        const c = ev.ctrlKey || ev.metaKey;
        if (c && ev.key === "s") {
            ev.preventDefault();
            this.save();
        } else if (c && ev.key === "z" && !ev.shiftKey) {
            ev.preventDefault();
            this.undo();
        } else if (c && (ev.key === "y" || (ev.key === "z" && ev.shiftKey))) {
            ev.preventDefault();
            this.redo();
        } else if (c && ev.key === "d") {
            ev.preventDefault();
            this.dupSel();
        } else if (c && ev.key === "a") {
            ev.preventDefault();
            this.state.selIds = this.state.els.map(e => e.id);
            this.state.selId = this.state.els[0]?.id || null;
        } else if (ev.key === "Delete" && this.state.selId) {
            ev.preventDefault();
            this.delSel();
        } else if (ev.key === "Escape") {
            this.clearSelection();
            this.state.showCtxMenu = false;
            this.state.showShortcuts = false;
        } else if (ev.key === "?" && !c) {
            ev.preventDefault();
            this.state.showShortcuts = !this.state.showShortcuts;
        } else if (this.sel && !this.sel.locked) {
            const g = ev.shiftKey ? 1 : this.state.gridSz;
            if (ev.key === "ArrowLeft") {
                ev.preventDefault();
                this._moveSelected(-g, 0);
            }
            if (ev.key === "ArrowRight") {
                ev.preventDefault();
                this._moveSelected(g, 0);
            }
            if (ev.key === "ArrowUp") {
                ev.preventDefault();
                this._moveSelected(0, -g);
            }
            if (ev.key === "ArrowDown") {
                ev.preventDefault();
                this._moveSelected(0, g);
            }
        }
    }

    // Move all selected elements (multi-select aware)
    _moveSelected(dx, dy) {
        const ids = this.state.selIds.length > 1 ? this.state.selIds : (this.state.selId ? [this.state.selId] : []);
        for (const id of ids) {
            const el = this.state.els.find(e => e.id === id);
            if (el && !el.locked) {
                el.pos_x = Math.max(0, el.pos_x + dx);
                el.pos_y = Math.max(0, el.pos_y + dy);
            }
        }
        this.state.dirty = true;
        this._commit();
    }

    // ═══════════════════════════════════════════════════════
    // MOUSE: Drag, Resize, Canvas Click
    // ═══════════════════════════════════════════════════════
    onElDown(ev, el) {
        ev.stopPropagation();
        if (ev.button !== 0) return;

        // Multi-select with Shift/Ctrl
        const additive = ev.shiftKey || ev.ctrlKey || ev.metaKey;
        this._selectEl(el.id, additive);

        if (el.locked) return;
        const sx = ev.clientX, sy = ev.clientY;

        // Capture starting positions of ALL selected elements (for multi-move)
        const startPositions = {};
        const moveIds = this.state.selIds.length > 1 ? this.state.selIds : [el.id];
        for (const mid of moveIds) {
            const me = this.state.els.find(e => e.id === mid);
            if (me) startPositions[mid] = {x: me.pos_x, y: me.pos_y};
        }

        const SNAP_DIST = 3;
        const mv = (e) => {
            const rawDx = this.px2mm(e.clientX - sx);
            const rawDy = this.px2mm(e.clientY - sy);

            for (const mid of moveIds) {
                const me = this.state.els.find(el2 => el2.id === mid);
                if (!me || me.locked) continue;
                let nx = startPositions[mid].x + rawDx;
                let ny = startPositions[mid].y + rawDy;
                // Shift während des Ziehens hebt das Raster-Einrasten auf
                // (freies, pixelgenaues Verschieben). So lässt sich ein Block
                // wieder exakt auf seine ursprüngliche Position bringen, auch
                // wenn diese nicht auf dem Raster liegt (z.B. 1.5mm).
                if (this.state.snap && !e.shiftKey) {
                    const g = this.state.gridSz;
                    nx = Math.round(nx / g) * g;
                    ny = Math.round(ny / g) * g;
                }
                const {w: zw, h: zh} = this.zoneBox(me.zone);
                me.pos_x = Math.max(0, Math.min(nx, zw - (me.width || 0)));
                me.pos_y = Math.max(0, Math.min(ny, zh - (me.height || 0)));
            }

            // Magnetic guides (only for primary element)
            const guides = {x: [], y: []};
            const others = this.state.els.filter(o => !moveIds.includes(o.id) && o.zone === el.zone && o.visible !== false);
            for (const o of others) {
                for (const ex of [o.pos_x, o.pos_x + o.width, o.pos_x + o.width / 2]) {
                    for (const mx of [el.pos_x, el.pos_x + el.width, el.pos_x + el.width / 2]) {
                        if (Math.abs(mx - ex) < SNAP_DIST) guides.x.push(ex);
                    }
                }
                for (const ey of [o.pos_y, o.pos_y + o.height, o.pos_y + o.height / 2]) {
                    for (const my of [el.pos_y, el.pos_y + el.height, el.pos_y + el.height / 2]) {
                        if (Math.abs(my - ey) < SNAP_DIST) guides.y.push(ey);
                    }
                }
            }
            this.state.guides = guides;
            this.state.dirty = true;
        };
        const up = () => {
            this.state.guides = {x: [], y: []};
            this._commit();
            document.removeEventListener("mousemove", mv);
            document.removeEventListener("mouseup", up);
        };
        document.addEventListener("mousemove", mv);
        document.addEventListener("mouseup", up);
    }

    onResDown(ev, el, h) {
        ev.stopPropagation();
        ev.preventDefault();
        if (el.locked) return;
        const sx = ev.clientX, sy = ev.clientY, ox = el.pos_x, oy = el.pos_y, ow = el.width, oh = el.height;
        const mv = (e) => {
            const dx = this.px2mm(e.clientX - sx), dy = this.px2mm(e.clientY - sy);
            const {w: zw, h: zh} = this.zoneBox(el.zone);
            if (h.includes("e")) el.width = Math.max(5, Math.min(ow + dx, zw - ox));
            if (h.includes("w")) {
                el.width = Math.max(5, Math.min(ow - dx, ow + ox));
                el.pos_x = Math.max(0, ox + (ow - el.width));
            }
            if (h.includes("s")) el.height = Math.max(2, Math.min(oh + dy, zh - oy));
            if (h.includes("n")) {
                el.height = Math.max(2, Math.min(oh - dy, oh + oy));
                el.pos_y = Math.max(0, oy + (oh - el.height));
            }
            this.state.dirty = true;
        };
        const up = () => {
            this._commit();
            document.removeEventListener("mousemove", mv);
            document.removeEventListener("mouseup", up);
        };
        document.addEventListener("mousemove", mv);
        document.addEventListener("mouseup", up);
    }

    onCanvasClick(ev) {
        if (!ev.target.closest(".ild-el")) this.clearSelection();
    }

    // ═══════════════════════════════════════════════════════
    // Feature #5: CONTEXT MENU (Right-click)
    // ═══════════════════════════════════════════════════════
    onElContextMenu(ev, el) {
        ev.preventDefault();
        ev.stopPropagation();
        this._selectEl(el.id);
        this.state.showCtxMenu = true;
        this.state.ctxMenuX = ev.clientX;
        this.state.ctxMenuY = ev.clientY;
        this.state.ctxMenuElId = el.id;
    }

    ctxAction(action) {
        this.state.showCtxMenu = false;
        const el = this.state.els.find(e => e.id === this.state.ctxMenuElId);
        if (!el) return;
        switch (action) {
            case "duplicate":
                this._selectEl(el.id);
                this.dupSel();
                break;
            case "delete":
                this._selectEl(el.id);
                this.delSel();
                break;
            case "lock":
                this.upEl(el.id, {locked: !el.locked});
                break;
            case "visible":
                this.upEl(el.id, {visible: !el.visible});
                break;
            case "toHeader":
                this.upEl(el.id, {zone: "header"});
                break;
            case "toBody":
                this.upEl(el.id, {zone: "body"});
                break;
            case "toFooter":
                this.upEl(el.id, {zone: "footer"});
                break;
            case "bringFront":
                this.upEl(el.id, {sequence: Math.max(...this.state.els.map(e => e.sequence)) + 10});
                break;
            case "sendBack":
                this.upEl(el.id, {sequence: Math.min(...this.state.els.map(e => e.sequence)) - 10});
                break;
            case "alignLeft":
                this.upEl(el.id, {pos_x: 0});
                break;
            case "alignCenter":
                this.upEl(el.id, {pos_x: (this.cw - el.width) / 2});
                break;
            case "alignRight":
                this.upEl(el.id, {pos_x: this.cw - el.width});
                break;
        }
    }

    // ═══════════════════════════════════════════════════════
    // DROP
    // ═══════════════════════════════════════════════════════
    onDragOver(ev) {
        ev.preventDefault();
    }

    onDrop(ev, zone) {
        ev.preventDefault();
        try {
            const d = JSON.parse(ev.dataTransfer.getData("text/plain"));
            const r = ev.currentTarget.getBoundingClientRect();
            const {w: zw, h: zh} = this.zoneBox(zone);
            const x = Math.max(0, Math.min(this.px2mm(ev.clientX - r.left), Math.max(0, zw - 10)));
            const y = Math.max(0, Math.min(this.px2mm(ev.clientY - r.top), Math.max(0, zh - 5)));
            if (d.a === "e") this.addEl(d.t, zone, x, y);
            else if (d.a === "f") this.addFieldEl(d.f);
        } catch {
        }
    }

    dragElem(ev, t) {
        ev.dataTransfer.setData("text/plain", JSON.stringify({a: "e", t}));
    }

    dragField(ev, f) {
        ev.dataTransfer.setData("text/plain", JSON.stringify({a: "f", f}));
    }

    // ═══════════════════════════════════════════════════════
    // Feature #6: LAYER DRAG REORDER
    // ═══════════════════════════════════════════════════════
    onLayerDragStart(ev, el) {
        ev.dataTransfer.setData("text/plain", JSON.stringify({a: "layer", id: el.id}));
        ev.dataTransfer.effectAllowed = "move";
    }

    onLayerDrop(ev, targetEl) {
        ev.preventDefault();
        try {
            const d = JSON.parse(ev.dataTransfer.getData("text/plain"));
            if (d.a !== "layer") return;
            const srcIdx = this.state.els.findIndex(e => e.id === d.id);
            const tgtIdx = this.state.els.findIndex(e => e.id === targetEl.id);
            if (srcIdx < 0 || tgtIdx < 0 || srcIdx === tgtIdx) return;
            const [moved] = this.state.els.splice(srcIdx, 1);
            this.state.els.splice(tgtIdx, 0, moved);
            // Re-sequence
            this.state.els.forEach((e, i) => e.sequence = (i + 1) * 10);
            this._commit();
        } catch {
        }
    }

    onLayerDragOver(ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
    }

    // ═══════════════════════════════════════════════════════
    // VIEW HELPERS
    // ═══════════════════════════════════════════════════════
    get etypes() {
        return [
            {t: "text", i: "fa-font", l: "Text"}, {t: "field", i: "fa-tag", l: "Field"},
            {t: "image", i: "fa-image", l: "Image"}, {t: "table", i: "fa-table", l: "Table"},
            {t: "line", i: "fa-minus", l: "H-Line"}, {t: "vline", i: "fa-ellipsis-v", l: "V-Line"},
            {t: "shape", i: "fa-square-o", l: "Shape"}, {t: "container", i: "fa-object-group", l: "Container"},
            {t: "barcode", i: "fa-barcode", l: "Barcode"}, {t: "qrcode", i: "fa-qrcode", l: "QR"},
            {t: "totals", i: "fa-calculator", l: "Summen"},
        ];
    }

    get catNames() {
        return {
            header: "Document",
            partner: "Customer",
            company: "Company",
            amounts: "Totals",
            line_fields: "Line Items",
            other: "Other",
            custom: "✦ Custom Fields"
        };
    }

    // Feature #11: Element type icon mapping
    typeIcon(type) {
        return {
            text: "fa-font",
            field: "fa-tag",
            image: "fa-image",
            table: "fa-table",
            line: "fa-minus",
            vline: "fa-ellipsis-v",
            shape: "fa-square-o",
            container: "fa-object-group",
            barcode: "fa-barcode",
            qrcode: "fa-qrcode",
            totals: "fa-calculator"
        }[type] || "fa-cube";
    }

    get fFields() {
        const q = (this.state.fSearch || "").toLowerCase();
        if (!q) return this.state.fields;
        const r = {};
        for (const [c, fs] of Object.entries(this.state.fields || {})) {
            const m = fs.filter(f => f.label.toLowerCase().includes(q) || f.path.toLowerCase().includes(q));
            if (m.length) r[c] = m;
        }
        return r;
    }

    toggleCat(c) {
        this.state.expCat[c] = !this.state.expCat[c];
    }

    elsByZone(z) {
        return this.state.els.filter(e => e.zone === z && e.visible !== false);
    }

    eStyle(el) {
        const x = Number(el.pos_x) || 0, y = Number(el.pos_y) || 0;
        const w = Number(el.width) > 0 ? Number(el.width) : 40;
        const h = Number(el.height) > 0 ? Number(el.height) : 10;
        const pw = this.mm(w), ph = this.mm(h);
        const dynFont = Math.max(8, Math.min(13, Math.floor(Math.min(pw / 8, ph / 1.5))));
        let s = `position:absolute;left:${this.mm(x)}px;top:${this.mm(y)}px;width:${pw}px;height:${ph}px;cursor:${el.locked ? "default" : "move"};box-sizing:border-box;word-wrap:break-word;padding:2px 4px;font-size:${dynFont}px;line-height:1.3;overflow:hidden;text-overflow:ellipsis;`;
        if (el.rotation) s += `transform:rotate(${el.rotation}deg);`;
        if (el.text_align) s += `text-align:${el.text_align};`;

        if (el.type === "line") {
            s += `border-bottom:${el.line_width || 1}pt ${el.line_style || "solid"} ${el.line_color || "#000"};height:0;padding:0;background:transparent;`;
        } else if (el.type === "vline") {
            s += `border-left:${el.line_width || 1}pt ${el.line_style || "solid"} ${el.line_color || "#000"};width:0;padding:0;background:transparent;`;
        } else if (el.type === "container") {
            s += `border:${el.container_border || "1px solid #aaa"};background:rgba(180,180,200,0.12);`;
            if (el.container_border_radius) s += `border-radius:${el.container_border_radius}mm;`;
            if (el.container_shadow) s += `box-shadow:0 1px 3px rgba(0,0,0,0.12);`;
        } else if (el.type === "image") {
            s += "background:rgba(113,75,103,0.15);display:flex;align-items:center;justify-content:center;color:#714B67;font-weight:600;";
        } else if (el.type === "table") {
            s += "background:rgba(113,75,103,0.08);border:1.5px solid rgba(113,75,103,0.35);color:#333;font-weight:500;";
        } else if (el.type === "field") {
            s += "background:rgba(230,220,235,0.9);color:#000;font-weight:600;";
        } else if (el.type === "text") {
            s += "background:rgba(240,237,245,0.85);color:#1a1a2e;font-weight:500;";
        } else if (el.type === "barcode" || el.type === "qrcode") {
            s += "background:rgba(200,200,210,0.3);display:flex;align-items:center;justify-content:center;color:#333;";
        } else if (el.type === "totals") {
            s += "background:rgba(113,75,103,0.08);border:1.5px solid rgba(113,75,103,0.35);color:#333;font-weight:500;display:flex;align-items:center;justify-content:flex-end;text-align:right;";
        } else {
            s += "background:rgba(200,200,210,0.2);color:#333;";
        }

        // Selection: support multi-select highlighting
        const isSelected = this.state.selIds.includes(el.id);
        if (el.id === this.state.selId) {
            s += "outline:2px solid #714B67;outline-offset:1px;z-index:100;";
        } else if (isSelected) {
            s += "outline:1.5px dashed #714B67;outline-offset:1px;z-index:99;";
        } else if (el.type !== "line" && el.type !== "vline") {
            s += "border:1px dashed rgba(113,75,103,0.4);";
        }
        return s;
    }

    eLabel(el) {
        const name = el.name || "";
        if (el.type === "text") {
            let txt = (el.text_content || "").replace(/<[^>]*>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/<[^>]*>/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
            if (txt.length > 40) txt = txt.substring(0, 37) + "...";
            if (name && name !== "Text" && txt) return name;
            return txt || name || "[Text]";
        }
        if (el.type === "field") return el.field_default || el.field_name || name || "...";
        if (el.type === "image") return "🏢 " + (name || "Logo");
        if (el.type === "table") {
            const c = el.table_columns || [];
            if (!c.length) return "📊 " + (name || "Table");
            return "📊 " + c.slice(0, 4).map(col => col.label || col.field).join(" | ");
        }
        if (el.type === "line" || el.type === "vline") return "";
        if (el.type === "shape") return name || "Shape";
        if (el.type === "container") return "📦 " + (name || el.container_layout || "Container");
        if (el.type === "barcode") return "▐▐▐ " + (name || "Barcode");
        if (el.type === "qrcode") return "▣ " + (name || "QR");
        if (el.type === "totals") {
            const lt = el.totals_label_total || "Gesamt:";
            return "Σ " + (el.totals_label_subtotal || "Zwischensumme:") + " / " + (el.totals_label_tax || "Steuer:") + " / " + lt;
        }
        return name || "[Element]";
    }

    _tblPrev(el) {
        return "";
    }

    zStyle(z) {
        const t = this.state.tpl, w = this.mm(this.cw);
        if (z === "header") return `position:relative;width:${w}px;height:${this.mm(t.header_height || 35)}px;border-bottom:2px solid rgba(229,115,115,0.6);background:rgba(229,115,115,.07);`;
        if (z === "footer") return `position:relative;width:${w}px;height:${this.mm(t.footer_height || 25)}px;border-top:2px solid rgba(100,181,246,0.6);margin-top:auto;background:rgba(100,181,246,.07);`;
        return `position:relative;width:${w}px;min-height:${this.mm(this.ch - (t.header_height || 35) - (t.footer_height || 25))}px;flex:1;background:rgba(129,199,132,.05);`;
    }

    // Feature #12: RULERS (mm markings)
    get rulerMarksX() {
        const marks = [];
        const cw = this.cw;
        for (let i = 0; i <= cw; i += 10) marks.push({pos: this.mm(i), label: i});
        return marks;
    }

    get rulerMarksY() {
        const marks = [];
        const ch = this.ch;
        for (let i = 0; i <= ch; i += 10) marks.push({pos: this.mm(i), label: i});
        return marks;
    }

    // ═══════════════════════════════════════════════════════
    // PROPS PANEL
    // ═══════════════════════════════════════════════════════
    sp(f, ev) {
        if (!this.sel) return;
        const v = ev.target.type === "checkbox" ? ev.target.checked : ev.target.type === "number" ? (parseFloat(ev.target.value) || 0) : ev.target.value;
        this.upEl(this.sel.id, {[f]: v});
    }

    // Shape props: any structured edit opts the shape into structured rendering
    // (flips shape_use_structured True) so the legacy style_json path is left
    // behind only on explicit user action.
    spShape(f, ev) {
        if (!this.sel) return;
        const v = ev.target.type === "checkbox" ? ev.target.checked : ev.target.type === "number" ? (parseFloat(ev.target.value) || 0) : ev.target.value;
        this.upEl(this.sel.id, {[f]: v, shape_use_structured: true});
    }

    ss(p, ev) {
        if (!this.sel) return;
        this.upEl(this.sel.id, {style: {...(this.sel.style || {}), [p]: ev.target.value}});
    }

    sa(v) {
        if (this.sel) this.upEl(this.sel.id, {text_align: v});
    }

    addCol() {
        if (!this.sel) return;
        this.upEl(this.sel.id, {
            table_columns: [...(this.sel.table_columns || []), {
                field: "name",
                label: "New",
                width: "auto",
                align: "left"
            }]
        });
    }

    rmCol(i) {
        if (!this.sel) return;
        const c = [...(this.sel.table_columns || [])];
        c.splice(i, 1);
        this.upEl(this.sel.id, {table_columns: c});
    }

    upCol(i, k, v) {
        if (!this.sel) return;
        const c = JSON.parse(JSON.stringify(this.sel.table_columns || []));
        if (c[i]) {
            c[i][k] = v;
            this.upEl(this.sel.id, {table_columns: c});
        }
    }

    // Reorder columns via up/down buttons (Punkt 1). Buttons are simpler and
    // more robust than HTML5 drag inside the cramped property panel.
    moveCol(i, dir) {
        if (!this.sel) return;
        const c = JSON.parse(JSON.stringify(this.sel.table_columns || []));
        const j = i + dir;
        if (j < 0 || j >= c.length) return;
        [c[i], c[j]] = [c[j], c[i]];
        this.upEl(this.sel.id, {table_columns: c});
    }

    zoomIn() {
        this.state.zoom = Math.min(200, this.state.zoom + 10);
    }

    zoomOut() {
        this.state.zoom = Math.max(30, this.state.zoom - 10);
    }

    zoomFit() {
        this.state.zoom = 80;
    }

    // Feature #13: Zoom to selection (double-click in layer list)
    zoomToEl(el) {
        this._selectEl(el.id);
        // Scroll the canvas to center on this element
        const canvasEl = document.querySelector(".ild-canvas-scroll");
        if (canvasEl) {
            const elX = this.mm(el.pos_x + el.width / 2);
            const elY = this.mm(el.pos_y + el.height / 2);
            canvasEl.scrollTo({
                left: Math.max(0, elX - canvasEl.clientWidth / 2),
                top: Math.max(0, elY - canvasEl.clientHeight / 2),
                behavior: "smooth",
            });
        }
    }

    // ═══════════════════════════════════════════════════════
    // IMAGE UPLOAD
    // ═══════════════════════════════════════════════════════
    onImageUpload(ev) {
        if (!this.sel || this.sel.type !== "image") return;
        const file = ev.target.files?.[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = (e) => {
            const b64 = e.target.result.split(",")[1];
            this.upEl(this.sel.id, {image_data_b64: b64, image_source: "upload"});
            this.notification.add(_t("Image uploaded!"), {type: "success"});
        };
        reader.readAsDataURL(file);
    }

    // ═══════════════════════════════════════════════════════
    // LOCK/UNLOCK
    // ═══════════════════════════════════════════════════════
    lockAll() {
        this.state.els.forEach(e => e.locked = true);
        this._commit();
    }

    unlockAll() {
        this.state.els.forEach(e => e.locked = false);
        this._commit();
    }

    // FIELD BROWSER (unified — handles left panel, conditions, columns, field elements)
    // ═══════════════════════════════════════════════════════
    async openFieldBrowser(target = "add", colIndex = -1) {
        this.state.fieldBrowserTarget = target || "add";
        this.state.fieldBrowserColIndex = colIndex;
        this.state.fieldBrowserSearch = "";

        // Determine which model to browse based on context
        let modelName;
        if (target === "column") {
            const lineModels = {
                "account.move": "account.move.line",
                "sale.order": "sale.order.line",
                "purchase.order": "purchase.order.line",
                "stock.picking": "stock.move",
            };
            modelName = lineModels[this.state.tpl.doc_type] || "account.move.line";
        } else {
            modelName = this.state.tpl.doc_type || "account.move";
        }

        // Always use the new modal browser
        this.state.fieldBrowserModel = modelName;
        this.state.showFieldBrowser = true;
        try {
            const r = await rpc("/layout/editor/browse_fields", {
                model_name: modelName,
                doc_type: this.state.tpl.doc_type
            });
            this.state.fieldBrowserFields = r.fields || {};
            this.state.fieldBrowserModels = r.browsable_models || [];
        } catch {
            this.notification.add(_t("Failed to load fields"), {type: "warning"});
        }
    }

    async browseModel(modelName) {
        this.state.fieldBrowserModel = modelName;
        try {
            const r = await rpc("/layout/editor/browse_fields", {
                model_name: modelName,
                doc_type: this.state.tpl.doc_type
            });
            this.state.fieldBrowserFields = r.fields || {};
            if (r.browsable_models) this.state.fieldBrowserModels = r.browsable_models;
        } catch {
            this.notification.add(_t("Failed to load fields"), {type: "warning"});
        }
    }

    selectBrowserField(fieldPath) {
        const target = this.state.fieldBrowserTarget;
        if (target === "condition") {
            this.upEl(this.state.selId, {condition_field: fieldPath});
        } else if (target === "column" && this.state.fieldBrowserColIndex >= 0) {
            this.upCol(this.state.fieldBrowserColIndex, "field", fieldPath);
        } else if (target === "field") {
            this.upEl(this.state.selId, {field_name: fieldPath});
        } else {
            // "add" mode: create a new field element
            this.addEl("field", "body");
            const el = this.state.els[this.state.els.length - 1];
            // Find label from fieldBrowserFields
            let label = fieldPath;
            for (const [, group] of Object.entries(this.state.fieldBrowserFields || {})) {
                const found = (group.fields || []).find(f => f.path === fieldPath);
                if (found) {
                    label = found.label;
                    break;
                }
            }
            Object.assign(el, {
                name: label,
                field_name: fieldPath,
                field_default: `[${label}]`,
                field_model: this.state.tpl.doc_type
            });
            this._commit();
            this.notification.add(`Feld "${label}" hinzugefuegt!`, {type: "success"});
        }
        this.closeFieldBrowser();
    }

    addBrowserField(field) {
        // Legacy: redirect to selectBrowserField
        this.state.fieldBrowserTarget = "add";
        this.selectBrowserField(field.path);
    }

    closeFieldBrowser() {
        this.state.showFieldBrowser = false;
        this.state.fieldBrowserFields = {};
        this.state.fieldBrowser = {open: false};
    }

    get filteredBrowserFields() {
        const fb = this.state.fieldBrowser;
        if (!fb || !fb.fields) return {};
        const q = (fb.search || "").toLowerCase();
        if (!q) return fb.fields;
        const result = {};
        for (const [group, data] of Object.entries(fb.fields)) {
            const filtered = data.fields.filter(f => f.label.toLowerCase().includes(q) || f.path.toLowerCase().includes(q) || (f.technical || "").toLowerCase().includes(q));
            if (filtered.length) result[group] = {...data, fields: filtered};
        }
        return result;
    }

    applyConditionPreset(field, operator, value) {
        if (!this.state.selId) return;
        this.upEl(this.state.selId, {
            condition_field: field,
            condition_operator: operator,
            condition_value: value,
        });
        this.state.showConditionHelper = false;
    }

    clearCondition() {
        if (!this.state.selId) return;
        this.upEl(this.state.selId, {
            condition_field: "",
            condition_operator: "set",
            condition_value: "",
        });
    }

    // ═══════════════════════════════════════════════════════
    // ALIGN
    // ═══════════════════════════════════════════════════════
    alignEl(dir) {
        if (!this.sel) return;
        const cw = this.cw;
        if (dir === "left") this.upEl(this.sel.id, {pos_x: 0});
        else if (dir === "center") this.upEl(this.sel.id, {pos_x: (cw - this.sel.width) / 2});
        else if (dir === "right") this.upEl(this.sel.id, {pos_x: cw - this.sel.width});
    }


    // ═══════════════════════════════════════════════════════
    // INLINE TEXT EDITING
    // ═══════════════════════════════════════════════════════
    onElDblClick(ev, el) {
        ev.stopPropagation();
        if (el.locked) return;
        if (el.type === "text") this.state.editingTextId = el.id;
    }

    onInlineTextBlur(ev, el) {
        el.text_content = ev.target.innerText || ev.target.innerHTML || "";
        this.state.editingTextId = null;
        this._commit();
    }

    onInlineTextKey(ev, el) {
        if (ev.key === "Escape") this.state.editingTextId = null;
    }

    // ═══════════════════════════════════════════════════════
    // Feature #1: SIDE-BY-SIDE PREVIEW
    // ═══════════════════════════════════════════════════════
    async livePreview() {
        await this.save();
        this.state.previewing = true;
        try {
            const r = await rpc("/layout/editor/preview", {template_id: this.tid});
            if (r.error) {
                this.notification.add(r.error, {type: "warning"});
            } else if (r.html) {
                const t = this.state.tpl || {};
                const pw = t.paper_width || 210, ph = t.paper_height || 297;
                const mt = t.margin_top ?? 15, mr = t.margin_right ?? 15, mb = t.margin_bottom ?? 15, ml = t.margin_left ?? 15;
                this.state.previewSrcdoc = this._buildPreviewSrcdoc(r.html, pw, ph, mt, mr, mb, ml);
                this.state.showPreview = true;
                this.state.previewFullscreen = false;
            }
        } catch {
            this.notification.add(_t("Preview failed"), {type: "danger"});
        }
        this.state.previewing = false;
    }

    closePreview() {
        this.state.showPreview = false;
        this.state.previewSrcdoc = "";
        this.state.previewFullscreen = false;
    }

    togglePreviewFullscreen() {
        this.state.previewFullscreen = !this.state.previewFullscreen;
    }

    onPreviewResizeDown(ev) {
        ev.preventDefault();
        const panel = ev.target.closest(".ild-preview-panel");
        if (!panel) return;
        const startX = ev.clientX;
        const startW = panel.offsetWidth;
        // Während des Ziehens die Maus-Events des Vorschau-iframes
        // deaktivieren. Sonst verschluckt das iframe mousemove/mouseup,
        // sobald der Cursor darüber gerät — die Vorschau ließ sich dadurch
        // nur vergrößern (nach rechts ziehen führt über das iframe), und der
        // hängende Listener blockierte sogar den Schließen-Button.
        const iframe = panel.querySelector(".ild-preview-iframe");
        if (iframe) {
            iframe.style.pointerEvents = "none";
        }
        document.body.style.userSelect = "none";
        const mv = (e) => {
            const dx = startX - e.clientX; // dragging left = wider
            const newW = Math.max(250, Math.min(900, startW + dx));
            panel.style.width = newW + "px";
            panel.style.minWidth = newW + "px";
            panel.style.maxWidth = newW + "px";
        };
        const up = () => {
            document.removeEventListener("mousemove", mv);
            document.removeEventListener("mouseup", up);
            if (iframe) {
                iframe.style.pointerEvents = "";
            }
            document.body.style.userSelect = "";
        };
        document.addEventListener("mousemove", mv);
        document.addEventListener("mouseup", up);
    }

    // ═══════════════════════════════════════════════════════
    // LINE ROTATION
    // ═══════════════════════════════════════════════════════
    rotateSel(deg) {
        if (!this.sel) return;
        this.upEl(this.sel.id, {rotation: ((this.sel.rotation || 0) + deg) % 360});
    }

    // ═══════════════════════════════════════════════════════
    // RESET TEMPLATE
    // ═══════════════════════════════════════════════════════
    async resetToDefault() {
        if (!confirm(_t("Reset to default? All current changes will be lost."))) return;
        try {
            await rpc("/web/dataset/call_kw/document.layout.template/action_reset_to_default", {
                model: "document.layout.template",
                method: "action_reset_to_default",
                args: [[this.tid]],
                kwargs: {},
            });
            await this._load();
            this.notification.add(_t("Template reset!"), {type: "success"});
        } catch (e) {
            console.error("Reset failed:", e);
            // Even if the action return causes issues, reload anyway
            await this._load();
            this.notification.add(_t("Template reset!"), {type: "success"});
        }
    }

    // ═══════════════════════════════════════════════════════
    // CONTAINER CHILDREN
    // ═══════════════════════════════════════════════════════
    addToContainer(containerId) {
        if (!this.sel || this.sel.id === containerId) return;
        const container = this.state.els.find(e => e.id === containerId);
        if (!container || container.type !== "container") return;
        const el = this.sel;
        el.pos_x = Math.max(0, el.pos_x - container.pos_x);
        el.pos_y = Math.max(0, el.pos_y - container.pos_y);
        try {
            const children = JSON.parse(container.container_child_ids || "[]");
            if (!children.includes(el.id)) {
                children.push(el.id);
                container.container_child_ids = JSON.stringify(children);
            }
        } catch {
        }
        this._commit();
    }

    removeFromContainer() {
        if (!this.sel) return;
        for (const el of this.state.els) {
            if (el.type !== "container") continue;
            try {
                const children = JSON.parse(el.container_child_ids || "[]");
                const idx = children.indexOf(this.sel.id);
                if (idx >= 0) {
                    children.splice(idx, 1);
                    el.container_child_ids = JSON.stringify(children);
                    this.sel.pos_x += el.pos_x;
                    this.sel.pos_y += el.pos_y;
                    this._commit();
                    return;
                }
            } catch {
            }
        }
    }

    // ═══════════════════════════════════════════════════════
    // Feature #4: DIN 5008 GUIDELINES
    // ═══════════════════════════════════════════════════════
    get dinGuides() {
        if (!this.state.showDinGuides) return [];
        const g = DIN5008_GUIDES;
        const mt = this.state.tpl.margin_top || 10;
        const hh = this.state.tpl.header_height || 45;
        return [
            {type: "h", y: g.addr_top, label: "Anschriftfeld oben", color: "#e57373"},
            {type: "h", y: g.addr_top + g.addr_height, label: "Anschriftfeld unten", color: "#e57373"},
            {type: "v", x: g.addr_width, label: "Anschriftfeld rechts", color: "#e57373"},
            {type: "v", x: g.info_left, label: "Infoblock links", color: "#64b5f6"},
            {type: "h", y: g.fold1_y - mt - hh, label: "Faltmarke 1", color: "#aaa"},
            {type: "h", y: g.punch_y - mt - hh, label: "Lochmarke", color: "#aaa"},
            {type: "h", y: g.fold2_y - mt - hh, label: "Faltmarke 2", color: "#aaa"},
        ];
    }

    // ═══════════════════════════════════════════════════════
    // Feature #10: KEYBOARD SHORTCUTS LIST
    // ═══════════════════════════════════════════════════════
    get shortcuts() {
        return [
            {key: "Ctrl+S", desc: "Save"},
            {key: "Ctrl+Z", desc: "Undo"},
            {key: "Ctrl+Y", desc: "Redo"},
            {key: "Ctrl+D", desc: "Duplicate"},
            {key: "Ctrl+A", desc: "Select All"},
            {key: "Delete", desc: "Delete selected"},
            {key: "Escape", desc: "Deselect / Close"},
            {key: "Arrows", desc: "Move element (grid)"},
            {key: "Shift+Arrows", desc: "Move element (1mm)"},
            {key: "Shift+Drag", desc: "Move freely (ignore grid)"},
            {key: "Shift+Click", desc: "Multi-select"},
            {key: "Right-click", desc: "Context menu"},
            {key: "Double-click", desc: "Edit text inline"},
            {key: "?", desc: "Show shortcuts"},
        ];
    }

    goBack() {
        this.actionService.doAction({
            type: "ir.actions.act_window", res_model: "document.layout.template",
            res_id: this.tid, view_mode: "form", views: [[false, "form"]],
        });
    }
}

registry.category("actions").add("invoice_layout_designer.layout_editor", LayoutEditor);