# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Module Commands

```bash
# Modul upgraden (Odoo muss gestoppt sein)
cd docker
docker compose stop odoo
docker compose run --rm odoo odoo -d demo -u invoice_layout_designer --stop-after-init
docker compose up -d

# Tests ausführen
docker compose run --rm odoo odoo -d demo --test-enable --stop-after-init -i invoice_layout_designer
```

## Architecture Overview

Odoo 19 Addon. WYSIWYG-Layoutdesigner für Rechnungen, Angebote, Lieferscheine und Bestellungen. Kein Coding nötig für Endnutzer.

**PDF-Rendering-Pipeline:**
`ir.actions.report._render_qweb_pdf()` (Override) → `_find_custom_template()` → `transpile_template()` → QWeb HTML → wkhtmltopdf oder WeasyPrint → PDF

**Editor-Pipeline:**
Client Action → JavaScript Editor → `/layout/editor/load` (load) → User drags/edits → `/layout/editor/save` (save, setzt `is_user_created=True`)

## Key Models

| Modell | Zweck |
|--------|-------|
| `document.layout.template` | Haupttemplate (doc_type, Margins, JSON-Layout). `transpile_template()` in `report_override.py` konvertiert zu QWeb-HTML |
| `document.layout.element` | Einzelnes Element (text/field/image/table/line/shape/container/barcode). Zones: header/body/footer |
| `document.layout.style` | Wiederverwendbare CSS-Presets (Schrift, Farben, Abstände) |
| `custom.field.definition` | No-Code Custom Fields. Erstellt `ir.model.fields` + View-Injektion via `action_create_field()` |

## Critical Files

- **`models/report_override.py`** (~1300 Zeilen): Transpiler + PDF-Override. Enthält `transpile_template()`, alle Element-Renderer, WeasyPrint/wkhtmltopdf-Routing.
- **`models/document_layout_template.py`** (~1000 Zeilen): Template-CRUD, DIN-5008/Modern/Classic/Minimalist Layout-Seeder (`_create_*_elements()`), `action_preview_pdf()`.
- **`models/field_registry.py`**: Pure Python. Zentrales Feld-Katalog-Dict pro doc_type. Kein Odoo-Modell.
- **`controllers/main.py`**: JSON-RPC Endpoints für den JS-Editor (load/save/preview).
- **`models/document_layout_element.py`**: `to_editor_dict()` serialisiert für JS, `is_user_created` schützt Elemente vor Reset.

## Element Types

11 Typen: `text`, `field`, `image`, `table`, `line`, `vline`, `shape`, `container`, `barcode`, `qrcode`

Jeder Typ hat eigene Felder + eigenen Renderer in `report_override.py` (`_render_text()`, `_render_table()`, etc.).

## Element Protection (Ghost-System)

Elemente haben drei Kategorien:
- **User-created** (`is_user_created=True`): Vom Editor gespeichert → immer behalten
- **XML-defaults** (haben `ir.model.data`-Eintrag): Aus `data/default_templates.xml` → immer behalten
- **Ghosts** (keines von beiden): Verwaiste Elemente → `ResetLayoutWizard` löscht diese

## Table Rendering (komplex)

`_render_table()` in `report_override.py` handelt: Row-Chunking nach `table_rows_per_page`, wiederholte Header, optionale Artikel (sale.order), Sektionen/Notizen (account.move), Subtotal-Zeilen, 5 Totals-Styles.

## doc_type Values

`invoice` (account.move), `quotation` (sale.order Angebote), `sale` (sale.order bestätigt), `delivery` (stock.picking), `purchase` (purchase.order)

Sale.order wird nach State aufgeteilt: `_split_sale_orders_by_state()` in `report_override.py`.

## Custom Fields

`custom.field.definition` erstellt `ir.model.fields` auf 6 Zielmodellen. Technischer Name = `x_` + auto-generiert. Field Picker im Editor ruft `get_fields_for_layout(doc_type)` auf und merged Custom Fields aus DB mit `field_registry.py`.

## Migrations

Versionen in `migrations/`. Bei strukturellen Änderungen an Default-Elementen: pre-migrate löscht alte, post-migrate setzt Flags. Migrationen müssen bei Versionsbump in `__manifest__.py` angelegt werden.

## PDF Engine

Einstellbar per Firma in Odoo-Einstellungen: `wkhtmltopdf` (Standard) oder `weasyprint` (optional, `pip install weasyprint` im Container). WeasyPrint macht Data-URI-Processing für Bilder.