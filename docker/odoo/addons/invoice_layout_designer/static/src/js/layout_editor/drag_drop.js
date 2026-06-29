/** @odoo-module **/

export class DragDropManager {
    constructor() {
        this.snapThreshold = 3; // mm — magnetic snap distance
    }

    /**
     * Calculate snap guides for an element being dragged.
     * Returns { x: number|null, y: number|null, guides: [] }
     */
    calculateSnap(draggedElem, allElements, gridSize, snapToGrid) {
        let snapX = null;
        let snapY = null;
        const guides = [];

        const dragLeft = draggedElem.pos_x;
        const dragRight = draggedElem.pos_x + draggedElem.width;
        const dragTop = draggedElem.pos_y;
        const dragBottom = draggedElem.pos_y + draggedElem.height;
        const dragCenterX = draggedElem.pos_x + draggedElem.width / 2;
        const dragCenterY = draggedElem.pos_y + draggedElem.height / 2;

        for (const other of allElements) {
            if (other.id === draggedElem.id || !other.visible) continue;

            const otherLeft = other.pos_x;
            const otherRight = other.pos_x + other.width;
            const otherTop = other.pos_y;
            const otherBottom = other.pos_y + other.height;
            const otherCenterX = other.pos_x + other.width / 2;
            const otherCenterY = other.pos_y + other.height / 2;

            // Left alignment
            if (Math.abs(dragLeft - otherLeft) < this.snapThreshold) {
                snapX = otherLeft;
                guides.push({ type: "vertical", pos: otherLeft });
            }
            // Right alignment
            if (Math.abs(dragRight - otherRight) < this.snapThreshold) {
                snapX = otherRight - draggedElem.width;
                guides.push({ type: "vertical", pos: otherRight });
            }
            // Center X alignment
            if (Math.abs(dragCenterX - otherCenterX) < this.snapThreshold) {
                snapX = otherCenterX - draggedElem.width / 2;
                guides.push({ type: "vertical", pos: otherCenterX });
            }
            // Top alignment
            if (Math.abs(dragTop - otherTop) < this.snapThreshold) {
                snapY = otherTop;
                guides.push({ type: "horizontal", pos: otherTop });
            }
            // Bottom alignment
            if (Math.abs(dragBottom - otherBottom) < this.snapThreshold) {
                snapY = otherBottom - draggedElem.height;
                guides.push({ type: "horizontal", pos: otherBottom });
            }
            // Center Y alignment
            if (Math.abs(dragCenterY - otherCenterY) < this.snapThreshold) {
                snapY = otherCenterY - draggedElem.height / 2;
                guides.push({ type: "horizontal", pos: otherCenterY });
            }
        }

        // Grid snap (only if no element snap happened)
        if (snapToGrid && snapX === null) {
            snapX = Math.round(dragLeft / gridSize) * gridSize;
        }
        if (snapToGrid && snapY === null) {
            snapY = Math.round(dragTop / gridSize) * gridSize;
        }

        return { x: snapX, y: snapY, guides };
    }
}
