/** @odoo-module **/

export class UndoRedoManager {
    constructor(maxSteps = 50) {
        this.undoStack = [];
        this.redoStack = [];
        this.maxSteps = maxSteps;
        this._lastSaveTime = 0;
        this._debounceMs = 300;
    }

    saveState(snapshot) {
        const now = Date.now();
        // Debounce rapid saves (e.g., during dragging)
        if (now - this._lastSaveTime < this._debounceMs) {
            // Replace last state instead of adding new one
            if (this.undoStack.length > 0) {
                this.undoStack[this.undoStack.length - 1] = snapshot;
            }
            return;
        }

        this._lastSaveTime = now;
        this.undoStack.push(snapshot);
        this.redoStack = []; // Clear redo stack on new action

        // Limit stack size
        if (this.undoStack.length > this.maxSteps) {
            this.undoStack.shift();
        }
    }

    undo() {
        if (this.undoStack.length <= 1) return null; // Keep at least initial state
        const current = this.undoStack.pop();
        this.redoStack.push(current);
        return this.undoStack[this.undoStack.length - 1];
    }

    redo() {
        if (this.redoStack.length === 0) return null;
        const state = this.redoStack.pop();
        this.undoStack.push(state);
        return state;
    }

    get canUndo() {
        return this.undoStack.length > 1;
    }

    get canRedo() {
        return this.redoStack.length > 0;
    }

    clear() {
        this.undoStack = [];
        this.redoStack = [];
    }
}
