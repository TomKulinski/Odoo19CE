/** @odoo-module **/

/**
 * Strang 4 — Schicht A: read-only SVG arrow overlay for the document chain.
 *
 * Draws a discreet Bézier arrow from each Sale-Order source template card to
 * every linked target card (Invoice / Purchase / Delivery / Auftragsbestätigung)
 * in the Layout Templates kanban. Edges come from `source_template_id`, surfaced
 * onto the cards via hidden `.ild-chain-marker` spans (data-tpl-id / data-src-id).
 *
 * The overlay is a fixed, pointer-events:none SVG layered over the viewport;
 * arrow paths re-enable pointer events so a click can open the target template.
 * It re-draws on scroll (capture), resize and any kanban DOM change so the
 * arrows track the cards. Schicht B (drag-to-draw) builds on this later.
 */
import { registry } from "@web/core/registry";
import { kanbanView } from "@web/views/kanban/kanban_view";
import { KanbanRenderer } from "@web/views/kanban/kanban_renderer";
import { useService } from "@web/core/utils/hooks";
import { onMounted, onPatched, onWillUnmount } from "@odoo/owl";

const SVG_NS = "http://www.w3.org/2000/svg";
const OVERLAY_ID = "ild_chain_overlay";
const ARROW_COLOR = "#714B67"; // Odoo primary, matches editor selection color

export class ChainKanbanRenderer extends KanbanRenderer {
    setup() {
        super.setup();
        this.actionService = useService("action");
        this._chainRedraw = this._chainRedraw.bind(this);
        this._chainResizeObserver = null;

        onMounted(() => this._chainSetup());
        onPatched(() => this._chainRedraw());
        onWillUnmount(() => this._chainTeardown());
    }

    _chainSetup() {
        // Redraw on any scroll (capture catches inner scroll containers too)
        // and on viewport resize.
        window.addEventListener("scroll", this._chainRedraw, true);
        window.addEventListener("resize", this._chainRedraw);
        if (typeof ResizeObserver !== "undefined") {
            this._chainResizeObserver = new ResizeObserver(this._chainRedraw);
            const renderer = document.querySelector(".o_kanban_renderer");
            if (renderer) {
                this._chainResizeObserver.observe(renderer);
            }
        }
        this._chainRedraw();
    }

    _chainTeardown() {
        window.removeEventListener("scroll", this._chainRedraw, true);
        window.removeEventListener("resize", this._chainRedraw);
        if (this._chainResizeObserver) {
            this._chainResizeObserver.disconnect();
            this._chainResizeObserver = null;
        }
        const svg = document.getElementById(OVERLAY_ID);
        if (svg) {
            svg.remove();
        }
    }

    _chainGetOverlay() {
        let svg = document.getElementById(OVERLAY_ID);
        if (!svg) {
            svg = document.createElementNS(SVG_NS, "svg");
            svg.id = OVERLAY_ID;
            svg.style.cssText =
                "position:fixed; left:0; top:0; width:100vw; height:100vh; " +
                "pointer-events:none; z-index:900;";
            // Arrowhead marker
            const defs = document.createElementNS(SVG_NS, "defs");
            const marker = document.createElementNS(SVG_NS, "marker");
            marker.setAttribute("id", "ild_chain_arrowhead");
            marker.setAttribute("markerWidth", "8");
            marker.setAttribute("markerHeight", "8");
            marker.setAttribute("refX", "6");
            marker.setAttribute("refY", "3");
            marker.setAttribute("orient", "auto");
            marker.setAttribute("markerUnits", "strokeWidth");
            const tip = document.createElementNS(SVG_NS, "path");
            tip.setAttribute("d", "M0,0 L6,3 L0,6 Z");
            tip.setAttribute("fill", ARROW_COLOR);
            marker.appendChild(tip);
            defs.appendChild(marker);
            svg.appendChild(defs);
            document.body.appendChild(svg);
        }
        return svg;
    }

    /**
     * Recompute and redraw all chain arrows. Cheap and idempotent — safe to call
     * on every scroll/resize/patch frame.
     */
    _chainRedraw() {
        let markers;
        try {
            markers = [...document.querySelectorAll(".ild-chain-marker")];
        } catch (e) {
            return;
        }
        if (!markers.length) {
            const existing = document.getElementById(OVERLAY_ID);
            if (existing) {
                existing.remove();
            }
            return;
        }

        // Map template id -> card element (closest kanban record).
        const cardById = {};
        const edges = [];
        for (const m of markers) {
            const card = m.closest(".o_kanban_record");
            if (!card) {
                continue;
            }
            const tplId = m.dataset.tplId;
            if (tplId) {
                cardById[tplId] = card;
            }
            const srcId = m.dataset.srcId;
            if (srcId && srcId !== "false" && srcId !== "") {
                // edge: source (Sale Order) -> target (this card)
                edges.push({ srcId: String(srcId), tgtId: String(tplId) });
            }
        }

        const svg = this._chainGetOverlay();
        // Clear previous paths but keep <defs>.
        [...svg.querySelectorAll(".ild-chain-edge")].forEach((p) => p.remove());

        for (const edge of edges) {
            const srcCard = cardById[edge.srcId];
            const tgtCard = cardById[edge.tgtId];
            if (!srcCard || !tgtCard) {
                continue; // source card not loaded (other column collapsed/filtered)
            }
            this._chainDrawEdge(svg, srcCard, tgtCard, edge.tgtId);
        }
    }

    _chainDrawEdge(svg, srcCard, tgtCard, tgtId) {
        const a = srcCard.getBoundingClientRect();
        const b = tgtCard.getBoundingClientRect();
        // Anchor from source right-center to target left-center; if the target
        // sits left of the source, flip anchors so the curve stays readable.
        let x1 = a.right;
        let y1 = a.top + a.height / 2;
        let x2 = b.left;
        let y2 = b.top + b.height / 2;
        if (b.left < a.left) {
            x1 = a.left;
            x2 = b.right;
        }
        const dx = Math.max(40, Math.abs(x2 - x1) * 0.5);
        const c1x = x1 + (x2 >= x1 ? dx : -dx);
        const c2x = x2 - (x2 >= x1 ? dx : -dx);
        const d = `M ${x1},${y1} C ${c1x},${y1} ${c2x},${y2} ${x2},${y2}`;

        const path = document.createElementNS(SVG_NS, "path");
        path.setAttribute("class", "ild-chain-edge");
        path.setAttribute("d", d);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", ARROW_COLOR);
        path.setAttribute("stroke-width", "1.5");
        path.setAttribute("stroke-opacity", "0.55");
        path.setAttribute("marker-end", "url(#ild_chain_arrowhead)");
        // Allow clicking the arrow to open the target template.
        path.style.pointerEvents = "stroke";
        path.style.cursor = "pointer";
        path.addEventListener("click", () => this._chainOpenTarget(tgtId));
        svg.appendChild(path);
    }

    _chainOpenTarget(tgtId) {
        const resId = parseInt(tgtId, 10);
        if (!resId) {
            return;
        }
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "document.layout.template",
            res_id: resId,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

export const chainKanbanView = {
    ...kanbanView,
    Renderer: ChainKanbanRenderer,
};

registry.category("views").add("ild_chain_kanban", chainKanbanView);
