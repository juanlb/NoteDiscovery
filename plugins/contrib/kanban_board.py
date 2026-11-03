"""
Kanban Board
Shows a folder as a Kanban board: each note is a card, its frontmatter `status` is the column.

Trigger: open /api/plugins/kanban_board/board?folder=<folder>
Install: cp plugins/contrib/kanban_board.py plugins/
Author:  @juanlb
Caveats: Only reads the notes directly inside the folder, never subfolders.
         Moving a card edits only the `status:` line, through core's save endpoint,
         so save hooks and the index behave as with a normal edit. Core saves with
         LF line endings, so a move also converts a CRLF note to LF, as any save does.
         Reads frontmatter straight from disk, because plugins have no access to
         on_note_load. Not compatible with plugins that transform note content on
         disk (for example, encryption): those cards land in the first column.
         Without a session, opening the board shows a JSON 401 instead of the login
         page, because core only redirects non-API paths.

Board schema (optional): <folder>/_board.md

    ---
    board:
      columns: [backlog, todo, doing, done]   # column order = status values
      labels: {todo: To do, doing: In progress}   # optional display names
      done: [done]                            # columns shown as finished
      fields:                                 # custom fields shown on the card
        due: date                             # YYYY-MM-DD
        priority: [low, med, high]            # allowed values, lowest first
        owner: text
    ---

Without this file the board uses exactly the columns, done list and fields above,
and the page offers a button to create it.

Card: any .md in the folder. Files starting with "_" or "." are not cards.

    ---
    status: todo
    tags: [foxie, bug]
    due: 2026-11-10
    priority: high
    owner: agent
    ---
    # Fix SSO login

A missing or unknown status puts the card in the first column, so no card is lost.
The title is the first "# heading", or the file name when there is none.
"""

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from backend.utils import parse_tags, resolve_vault_folder

logger = logging.getLogger("uvicorn.error")

BOARD_FILE = "_board.md"

# Cards only need the frontmatter and the first heading, so a large note costs the
# same as a small one.
MAX_READ_CHARS = 16 * 1024

DEFAULT_COLUMNS = ["backlog", "todo", "doing", "done"]
DEFAULT_DONE = ["done"]
DEFAULT_FIELDS = {"due": "date", "priority": ["low", "med", "high"], "owner": "text"}

# What the "Create _board.md" button writes.
BOARD_TEMPLATE = """---
board:
  columns: [backlog, todo, doing, done]
  done: [done]
  fields:
    due: date
    priority: [low, med, high]
    owner: text
---
Board settings for this folder. The kanban_board plugin ignores this text.
"""

ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEADING_PATTERN = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _read_head(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(MAX_READ_CHARS)


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter, body).

    Frontmatter counts only when the first line opens it and a closing --- follows,
    the same rule as core's parse_tags: an unterminated block is body text.
    """
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def _parse_yaml(frontmatter: str | None) -> tuple[dict, bool]:
    """Return (values, is_valid).

    BaseLoader keeps every scalar as the text in the file. SafeLoader would turn
    `due: 2026-11-10` into a date and `status: on` into True, and neither would then
    match the columns or the line a move edits.
    """
    if frontmatter is None:
        return {}, True
    try:
        data = yaml.load(frontmatter, Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        return {}, False
    if data is None or data == "":
        return {}, True
    return (data, True) if isinstance(data, dict) else ({}, False)


def _first_heading(body: str) -> str | None:
    open_fence = None
    for line in body.split("\n"):
        fence = FENCE_PATTERN.match(line)
        if fence:
            if open_fence is None:
                open_fence = fence.group(1)[0]
            elif fence.group(1)[0] == open_fence:
                open_fence = None
            continue
        if open_fence is None:
            heading = HEADING_PATTERN.match(line)
            if heading:
                return heading.group(1)
    return None


def _string_list(value) -> list[str]:
    """The non-empty, unique strings of a YAML list, in order."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(v for v in value if isinstance(v, str) and v))


def _normalize_fields(raw) -> list[dict]:
    fields = []
    for field_id, spec in raw.items():
        values = _string_list(spec)
        if values:
            fields.append({"id": field_id, "type": "list", "values": values})
        elif spec in ("date", "text"):
            fields.append({"id": field_id, "type": spec})
    return fields


def _read_board_config(folder: Path) -> dict:
    """Columns and fields from _board.md. Anything missing or malformed falls back
    to the defaults, so a typo in the schema never hides the cards."""
    board_path = folder / BOARD_FILE
    has_file = board_path.is_file() and not board_path.is_symlink()
    raw = {}
    valid = True

    if has_file:
        frontmatter, _ = _split_frontmatter(_read_head(board_path))
        values, valid = _parse_yaml(frontmatter)
        raw = values.get("board")
        if not isinstance(raw, dict):
            raw = {}

    columns = _string_list(raw.get("columns")) or DEFAULT_COLUMNS
    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    done = _string_list(raw.get("done")) or DEFAULT_DONE
    raw_fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else DEFAULT_FIELDS

    return {
        "has_board_file": has_file,
        "board_file_valid": valid,
        "columns": [
            {
                "id": column,
                # None lets the page translate the default column names.
                "label": labels[column] if isinstance(labels.get(column), str) else None,
                "done": column in done,
            }
            for column in columns
        ],
        "fields": _normalize_fields(raw_fields),
    }


def _read_card(path: Path, rel_path: str, config: dict) -> dict:
    # Same string core returns as metadata.modified from GET /api/notes/{path}. The
    # page compares both to notice that a card changed after the board loaded.
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    text = _read_head(path)
    frontmatter, body = _split_frontmatter(text)
    values, valid = _parse_yaml(frontmatter)
    warnings = [] if valid else ["invalid_frontmatter"]

    column_ids = [column["id"] for column in config["columns"]]
    status = values.get("status")
    status = status if isinstance(status, str) else ""
    if status and status not in column_ids:
        warnings.append("unknown_status")

    fields = {}
    for field in config["fields"]:
        value = values.get(field["id"])
        if isinstance(value, str) and value:
            fields[field["id"]] = value

    return {
        "path": rel_path,
        "name": path.name,
        "title": _first_heading(body) or path.stem,
        "status": status,
        "column": status if status in column_ids else column_ids[0],
        "tags": parse_tags(text),
        "fields": fields,
        "mtime": mtime,
        "warnings": warnings,
    }


def _card_sort_key(card: dict, config: dict) -> tuple:
    """Highest priority first, then earliest date, then file name."""
    weight = -1
    due = "￿"
    list_field = next((f for f in config["fields"] if f["type"] == "list"), None)
    date_field = next((f for f in config["fields"] if f["type"] == "date"), None)

    if list_field:
        value = card["fields"].get(list_field["id"])
        if value in list_field["values"]:
            weight = list_field["values"].index(value)
    if date_field:
        value = card["fields"].get(date_field["id"], "")
        if ISO_DATE_PATTERN.match(value):
            due = value

    return (-weight, due, card["name"].casefold())


class Plugin:
    def __init__(self):
        self.name = "Kanban Board"
        self.version = "1.0.0"
        self.enabled = True
        # Replaced in setup().
        self.notes_dir = Path(".")

    def setup(self, ctx):
        """Take the vault path from the host, and the per-plugin logger."""
        global logger
        logger = ctx.logger
        self.notes_dir = ctx.notes_dir

    def get_routes(self) -> APIRouter:
        """Expose the board page and its data at /api/plugins/kanban_board/."""
        router = APIRouter()

        # Plain def on purpose: FastAPI runs these in a worker thread, so reading a
        # folder of notes does not block the event loop.
        @router.get("/board", response_class=HTMLResponse)
        def board(folder: str = ""):
            self._resolve_folder(folder)
            return HTMLResponse(BOARD_PAGE, headers={"Cache-Control": "no-cache"})

        @router.get("/data")
        def data(folder: str = ""):
            return self._board_data(self._resolve_folder(folder))

        return router

    def _resolve_folder(self, folder: str) -> Path:
        if not self.enabled:
            raise HTTPException(status_code=404, detail="Plugin disabled")
        try:
            target = resolve_vault_folder(str(self.notes_dir), folder)
        except ValueError:  # For example, a null byte in the path.
            target = None
        if target is None:
            raise HTTPException(status_code=404, detail="Folder not found")
        return target

    def _board_data(self, folder: Path) -> dict:
        config = _read_board_config(folder)
        root = Path(self.notes_dir).resolve()
        cards = []

        with os.scandir(folder) as entries:
            for entry in entries:
                # Symlinks are skipped: one could point outside the vault.
                if (entry.name[0] in "_."
                        or not entry.name.endswith(".md")
                        or not entry.is_file(follow_symlinks=False)):
                    continue
                path = Path(entry.path)
                try:
                    rel_path = path.resolve().relative_to(root).as_posix()
                    cards.append(_read_card(path, rel_path, config))
                except (OSError, ValueError) as e:
                    logger.warning("kanban_board skipped %s: %s", entry.name, e)

        cards.sort(key=lambda card: _card_sort_key(card, config))

        rel_folder = folder.resolve().relative_to(root).as_posix()
        return {
            "folder": "" if rel_folder == "." else rel_folder,
            **config,
            "board_template": BOARD_TEMPLATE,
            "cards": cards,
        }


# The whole page lives here so the plugin stays one file. Colors come only from theme
# variables, and Alpine is the copy the app already serves, so it works offline.
BOARD_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kanban Board</title>
<link rel="icon" href="/static/favicon.svg">
<script>
// Same theme lookup as the preload script in index.html.
function kanbanApplyTheme() {
  function getJson(url) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url, false);
    xhr.send();
    return xhr.status === 200 ? JSON.parse(xhr.responseText) : null;
  }
  try {
    var id = localStorage.getItem('noteDiscoveryTheme');
    if (!id) {
      var config = getJson('/api/config');
      id = (config && config.defaultTheme) || 'light';
    }
    if (document.documentElement.getAttribute('data-theme') === id) return;
    var theme = getJson('/api/themes/' + encodeURIComponent(id));
    if (!theme) {
      id = 'light';
      theme = getJson('/api/themes/light');
    }
    if (!theme) return;
    var style = document.getElementById('dynamic-theme');
    if (!style) {
      style = document.createElement('style');
      style.id = 'dynamic-theme';
      document.head.appendChild(style);
    }
    style.textContent = theme.css;
    document.documentElement.setAttribute('data-theme', id);
  } catch (e) {
    console.error('Failed to load theme:', e);
  }
}
kanbanApplyTheme();
</script>
<style>
[x-cloak] { display: none !important; }
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  /* Theme status colors are tuned for icons and borders, and some are too pale for
     small text on a light theme. Pulling them toward the text color fixes that on
     light and dark themes alike. */
  --kanban-error: color-mix(in srgb, var(--error) 60%, var(--text-primary));
  --kanban-warning: color-mix(in srgb, var(--warning) 40%, var(--text-primary));
  display: flex;
  flex-direction: column;
  background: var(--bg-primary);
  color: var(--text-primary);
  font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
a { color: inherit; }
button, input { font: inherit; color: inherit; }
button { cursor: pointer; }
svg { flex-shrink: 0; }
:focus-visible { outline: 2px solid var(--accent-primary); outline-offset: 1px; }

.top { padding: 14px 20px 0; }
.crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; font-size: 12px; color: var(--text-secondary); }
.crumbs a { display: inline-flex; align-items: center; gap: 4px; color: var(--text-secondary); text-decoration: none; }
.crumbs a:hover { color: var(--accent-primary); }
.head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 6px; }
.head h1 { flex: 1 1 auto; min-width: 0; margin: 0; font-size: 20px; font-weight: 700; overflow-wrap: anywhere; }
.search { flex: 0 1 240px; min-width: 0; height: 34px; padding: 0 10px; border: 1px solid var(--border-primary); border-radius: 6px; background: var(--bg-secondary); }
.search::placeholder { color: var(--text-tertiary); }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 34px; padding: 0 12px; border: 1px solid var(--border-primary); border-radius: 6px; background: var(--bg-secondary); color: var(--text-secondary); }
.btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.btn-icon { width: 34px; padding: 0; }
.btn-primary { border-color: var(--accent-primary); background: var(--accent-primary); color: var(--bg-primary); font-weight: 600; }
.btn-primary:hover { border-color: var(--accent-hover); background: var(--accent-hover); color: var(--bg-primary); }
.column-add { display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; padding: 0; border: 0; border-radius: 6px; background: none; color: var(--text-tertiary); }
.column-add:hover { background: var(--bg-hover); color: var(--text-primary); }
dialog { width: min(420px, calc(100vw - 32px)); padding: 16px; border: 1px solid var(--border-primary); border-radius: 8px; background: var(--bg-primary); color: var(--text-primary); box-shadow: var(--shadow-md); }
dialog::backdrop { background: rgba(0, 0, 0, .4); }
dialog h2 { margin: 0 0 12px; font-size: 16px; }
.form-row { display: block; margin-bottom: 12px; font-size: 12px; color: var(--text-secondary); }
.form-row input, .form-row select { display: block; width: 100%; height: 38px; margin-top: 4px; padding: 0 8px; border: 1px solid var(--border-primary); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); font: inherit; font-size: 14px; }
.form-actions { display: flex; justify-content: flex-end; gap: 8px; }

.filters { display: flex; align-items: center; gap: 10px; padding: 10px 20px; }
.chips { display: flex; flex: 1; min-width: 0; flex-wrap: wrap; gap: 6px; }
.chip-btn { height: 26px; padding: 0 10px; border: 1px solid transparent; border-radius: 9999px; background: var(--bg-tertiary); color: var(--text-primary); font-size: 12px; white-space: nowrap; }
.chip-btn:hover { background: var(--bg-hover); }
.chip-btn[aria-pressed="true"] { background: var(--accent-primary); color: var(--bg-primary); }
.count { font-size: 12px; color: var(--text-secondary); white-space: nowrap; }

.pills { display: none; }
.board { flex: 1; min-height: 0; display: flex; align-items: stretch; gap: 12px; padding: 0 20px 16px; overflow-x: auto; }
.column { flex: 0 0 290px; display: flex; flex-direction: column; min-height: 0; border-radius: 8px; background: var(--bg-secondary); border: 1px solid var(--border-primary); }
.column-head { display: flex; align-items: center; gap: 8px; padding: 10px 12px; font-weight: 600; }
.column-head h2 { flex: 1; min-width: 0; margin: 0; font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.dot { width: 8px; height: 8px; flex-shrink: 0; border-radius: 50%; background: var(--accent-primary); }
.dot-first { background: var(--text-tertiary); }
.dot-done { background: var(--success); }
.column-count { font-size: 12px; font-weight: 400; color: var(--text-secondary); }
.cards { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 8px; padding: 0 8px 8px; overflow-y: auto; }
.empty-column { padding: 16px 8px; border: 1px dashed var(--border-primary); border-radius: 6px; color: var(--text-secondary); font-size: 12px; text-align: center; }

.card { display: flex; flex-direction: column; flex-shrink: 0; gap: 6px; padding: 10px; border: 1px solid var(--border-primary); border-radius: 8px; background: var(--bg-primary); box-shadow: var(--shadow-sm); }
.card:hover { box-shadow: var(--shadow-md); border-color: var(--border-secondary); }
.card-top { display: flex; align-items: flex-start; gap: 8px; }
.card-title { flex: 1; min-width: 0; font-weight: 600; text-decoration: none; overflow-wrap: anywhere; }
.card-title:hover { color: var(--accent-primary); }
.card-done .card-title { text-decoration: line-through; color: var(--text-secondary); }
.card-done { opacity: .85; }
.priority { flex-shrink: 0; padding: 1px 7px; border-radius: 9999px; font-size: 11px; font-weight: 600; background: var(--bg-tertiary); color: var(--text-secondary); }
.priority-high { color: var(--kanban-error); background: color-mix(in srgb, var(--error) 14%, transparent); }
.priority-med { color: var(--kanban-warning); background: color-mix(in srgb, var(--warning) 14%, transparent); }
.priority-low { color: var(--text-secondary); }
.tags { display: flex; flex-wrap: wrap; gap: 4px; }
.tag { padding: 1px 7px; border-radius: 9999px; background: var(--bg-tertiary); color: var(--text-primary); font-size: 11px; overflow-wrap: anywhere; }
.meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; font-size: 12px; color: var(--text-secondary); }
.meta-item { display: inline-flex; align-items: center; gap: 4px; min-width: 0; overflow-wrap: anywhere; }
.date-soon { color: var(--kanban-warning); font-weight: 600; }
.date-late { color: var(--kanban-error); font-weight: 600; }
.file { font: 10px/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace; color: var(--text-secondary); overflow-wrap: anywhere; }
.warning { display: flex; align-items: center; gap: 4px; font-size: 11px; color: var(--kanban-warning); overflow-wrap: anywhere; }

.card[draggable="true"] { cursor: grab; }
.card-moving { opacity: .5; }
.column-drop { border-color: var(--accent-primary); box-shadow: inset 0 0 0 1px var(--accent-primary); }
.card-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.card-actions { display: flex; flex-shrink: 0; align-items: center; gap: 6px; }
.move-select { max-width: 110px; height: 24px; border: 1px solid var(--border-primary); border-radius: 6px; background: var(--bg-secondary); color: var(--text-secondary); font: inherit; font-size: 11px; cursor: pointer; }
.move-next { display: none; }
.toast { position: fixed; left: 50%; bottom: 20px; z-index: 10; transform: translateX(-50%); max-width: calc(100vw - 32px); padding: 8px 14px; border: 1px solid var(--border-primary); border-radius: 8px; background: var(--bg-secondary); color: var(--text-primary); box-shadow: var(--shadow-md); }
@media (hover: hover) and (min-width: 641px) {
  /* Out of the way until needed, and still reachable with the keyboard. */
  .move-select { opacity: 0; }
  .card:hover .move-select, .move-select:focus { opacity: 1; }
}

.state { margin: auto; padding: 32px 20px; color: var(--text-secondary); text-align: center; }

@media (max-width: 640px) {
  html, body { height: auto; }
  .top { padding: 12px 16px 0; }
  .head h1 { flex-basis: 100%; }
  .search { flex: 1 1 0; height: 44px; font-size: 16px; }
  .btn { height: 44px; }
  .btn-icon { width: 44px; }
  .form-row input, .form-row select { height: 44px; font-size: 16px; }
  .filters { padding: 10px 16px; }
  .chips { flex-wrap: nowrap; overflow-x: auto; padding-bottom: 2px; }
  .chip-btn { height: 44px; padding: 0 12px; }
  .crumbs a { min-height: 44px; }
  .pills { display: flex; gap: 6px; padding: 0 16px 10px; overflow-x: auto; }
  .pill { display: inline-flex; flex-shrink: 0; align-items: center; gap: 6px; min-height: 44px; padding: 0 12px; border: 1px solid var(--border-primary); border-radius: 9999px; background: var(--bg-secondary); color: var(--text-secondary); white-space: nowrap; }
  .pill[aria-pressed="true"] { border-color: var(--accent-primary); color: var(--text-primary); font-weight: 600; }
  .board { display: block; padding: 0 16px 16px; overflow: visible; }
  .column { display: none; background: none; border: 0; }
  .column-active { display: flex; }
  .column-head { display: none; }
  .cards { padding: 0; overflow: visible; }
  .card-title { padding: 12px 0; }
  .card-top { align-items: center; }
  .card-foot { flex-wrap: wrap; }
  .card-actions { width: 100%; }
  .move-select { flex: 1; max-width: none; height: 44px; font-size: 14px; }
  .move-next { display: inline-flex; flex: 2; min-width: 0; height: 44px; }
  .move-next span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
}
</style>
<script>
const KANBAN_TEXT = {
  en: {
    back: 'Back to notes', root: 'All notes', search: 'Search cards', reload: 'Reload', all: 'All',
    cards_one: '1 card', cards_many: '{n} cards', loading: 'Loading board…',
    no_notes: 'No notes in this folder', no_cards: 'No cards', no_matches: 'No cards match the filter',
    load_error: 'Could not load the board', move_to: 'Move to…', move_to_column: 'Move to {column}',
    conflict: 'The card changed, reloading', move_error: 'Could not move the card',
    new_card: 'New card', new_card_in: 'New card in {column}', title: 'Title', column: 'Column', create: 'Create', cancel: 'Cancel',
    create_error: 'Could not create the note', create_board: 'Create _board.md', invalid_frontmatter: 'Invalid frontmatter',
    unknown_status: 'Unknown status: {status}', invalid_board: 'Invalid _board.md, using the default columns',
    columns: { backlog: 'Backlog', todo: 'To do', doing: 'In progress', done: 'Done' },
    priorities: { low: 'Low', med: 'Medium', medium: 'Medium', high: 'High' },
  },
  es: {
    back: 'Volver a las notas', root: 'Todas las notas', search: 'Buscar cards', reload: 'Recargar', all: 'Todas',
    cards_one: '1 card', cards_many: '{n} cards', loading: 'Cargando el tablero…',
    no_notes: 'No hay notas en esta carpeta', no_cards: 'Sin cards', no_matches: 'Ninguna card coincide con el filtro',
    load_error: 'No se pudo cargar el tablero', move_to: 'Mover a…', move_to_column: 'Mover a {column}',
    conflict: 'La card cambió, recargando', move_error: 'No se pudo mover la card',
    new_card: 'Nueva card', new_card_in: 'Nueva card en {column}', title: 'Título', column: 'Columna', create: 'Crear', cancel: 'Cancelar',
    create_error: 'No se pudo crear la nota', create_board: 'Crear _board.md', invalid_frontmatter: 'Frontmatter inválido',
    unknown_status: 'Status desconocido: {status}', invalid_board: '_board.md inválido, se usan las columnas por defecto',
    columns: { backlog: 'Backlog', todo: 'Por hacer', doing: 'En curso', done: 'Hecho' },
    priorities: { low: 'Baja', med: 'Media', medium: 'Media', high: 'Alta' },
  },
};

function kanbanLocale() {
  try {
    return localStorage.getItem('locale') || 'en-US';
  } catch (e) {
    return 'en-US';
  }
}

// Case and accent insensitive, like the app's own search.
function kanbanFold(text) {
  return text.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();
}

function kanbanYamlValue(status) {
  return /^[\p{L}\p{N}_][\p{L}\p{N}_ .-]*$/u.test(status) ? status : JSON.stringify(status);
}

// Touches only the status line, never the rest of the frontmatter, so comments, order
// and formatting survive. A trailing comment on that line is kept too.
function kanbanSetStatus(content, status) {
  const value = kanbanYamlValue(status);
  const lines = content.split('\n');
  const end = lines[0].trim() === '---'
    ? lines.findIndex((line, index) => index > 0 && line.trim() === '---')
    : -1;

  if (end < 0) return '---\nstatus: ' + value + '\n---\n' + content;

  let found = false;
  for (let i = 1; i < end; i++) {
    const match = /^(status[ \t]*:[ \t]*)(.*?)([ \t]+#.*)?$/.exec(lines[i]);
    if (match) {
      // `status:` with no value has no space after the colon yet.
      const prefix = /\s$/.test(match[1]) ? match[1] : match[1] + ' ';
      lines[i] = prefix + value + (match[3] || '');
      found = true;
    }
  }
  if (!found) lines.splice(end, 0, 'status: ' + value);
  return lines.join('\n');
}

function kanbanBoard() {
  const locale = kanbanLocale();
  const text = locale.toLowerCase().startsWith('es') ? KANBAN_TEXT.es : KANBAN_TEXT.en;

  return {
    text,
    folder: new URLSearchParams(location.search).get('folder') || '',
    board: null,
    loading: true,
    failed: false,
    query: '',
    activeTag: null,
    activeColumn: null,
    draggedPath: null,
    dropColumn: null,
    movingPath: null,
    notice: '',
    newTitle: '',
    newColumn: null,

    init() {
      document.documentElement.lang = text === KANBAN_TEXT.es ? 'es' : 'en';
      document.title = this.folderName();
      this.load();
      // Shows what an agent changed while the tab was in the background.
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          kanbanApplyTheme();
          this.load();
        }
      });
    },

    async load() {
      try {
        const response = await fetch('/api/plugins/kanban_board/data?folder=' + encodeURIComponent(this.folder));
        if (response.status === 401) {
          location.href = '/login';
          return;
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        this.board = await response.json();
        this.failed = false;
        const ids = this.board.columns.map(column => column.id);
        if (!ids.includes(this.activeColumn)) this.activeColumn = ids[0];
        if (this.activeTag && !this.allTags().includes(this.activeTag)) this.activeTag = null;
      } catch (e) {
        console.error('Failed to load the board:', e);
        // A failed background reload keeps the cards that are already on screen.
        this.failed = !this.board;
      } finally {
        this.loading = false;
      }
    },

    folderName() {
      return this.folder.split('/').pop() || text.root;
    },

    parentPath() {
      return this.folder.split('/').slice(0, -1).join(' / ');
    },

    allTags() {
      const tags = new Set();
      for (const card of this.board.cards) card.tags.forEach(tag => tags.add(tag));
      return [...tags].sort((a, b) => a.localeCompare(b));
    },

    matches(card) {
      if (this.activeTag && !card.tags.includes(this.activeTag)) return false;
      const query = kanbanFold(this.query.trim());
      if (!query) return true;
      const haystack = [card.title, card.name, ...card.tags, ...Object.values(card.fields)].join(' ');
      return kanbanFold(haystack).includes(query);
    },

    visibleCards() {
      return this.board.cards.filter(card => this.matches(card));
    },

    columnCards(columnId) {
      return this.visibleCards().filter(card => card.column === columnId);
    },

    countLabel() {
      const count = this.visibleCards().length;
      return count === 1 ? text.cards_one : text.cards_many.replace('{n}', count);
    },

    columnLabel(column) {
      return column.label || text.columns[column.id] || column.id;
    },

    dotClass(column, index) {
      if (column.done) return 'dot-done';
      return index === 0 ? 'dot-first' : '';
    },

    noteUrl(card) {
      const path = card.path.replace(/\.md$/, '');
      return '/' + path.split('/').map(encodeURIComponent).join('/');
    },

    listField() {
      return this.board.fields.find(field => field.type === 'list');
    },

    priorityValue(card) {
      const field = this.listField();
      return field ? card.fields[field.id] : undefined;
    },

    priorityLabel(card) {
      const value = this.priorityValue(card);
      return text.priorities[value] || value;
    },

    // The list runs lowest to highest, so position gives the color.
    priorityClass(card) {
      const values = this.listField().values;
      const index = values.indexOf(this.priorityValue(card));
      if (index < 0) return '';
      if (index === values.length - 1 && values.length > 1) return 'priority-high';
      return index === 0 && values.length > 1 ? 'priority-low' : 'priority-med';
    },

    footerFields(card) {
      return this.board.fields
        .filter(field => card.fields[field.id] && field !== this.listField())
        .map(field => ({ id: field.id, type: field.type, value: card.fields[field.id] }));
    },

    parseDate(value) {
      const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
      if (!match) return null;
      const date = new Date(+match[1], +match[2] - 1, +match[3]);
      return date.getMonth() === +match[2] - 1 ? date : null;
    },

    dateLabel(value) {
      const date = this.parseDate(value);
      if (!date) return value;
      const options = { day: 'numeric', month: 'short' };
      if (date.getFullYear() !== new Date().getFullYear()) options.year = 'numeric';
      return date.toLocaleDateString(locale, options);
    },

    dateClass(value, card) {
      const date = this.parseDate(value);
      const column = this.board.columns.find(c => c.id === card.column);
      if (!date || column.done) return '';
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const days = Math.round((date - today) / 86400000);
      if (days < 0) return 'date-late';
      return days <= 3 ? 'date-soon' : '';
    },

    isDone(card) {
      return this.board.columns.find(column => column.id === card.column).done;
    },

    // The status of a card with broken YAML cannot be read back, so a move would
    // change the file and leave the card where it is.
    canMove(card) {
      return !card.warnings.includes('invalid_frontmatter');
    },

    moveTargets(card) {
      return this.board.columns.filter(column => column.id !== card.status);
    },

    nextColumn(card) {
      const columns = this.board.columns;
      return columns[columns.findIndex(column => column.id === card.column) + 1];
    },

    moveToLabel(column) {
      return text.move_to_column.replace('{column}', this.columnLabel(column));
    },

    showNotice(message) {
      this.notice = message;
      clearTimeout(this.noticeTimer);
      this.noticeTimer = setTimeout(() => { this.notice = ''; }, 4000);
    },

    onDrop(columnId) {
      const card = this.board.cards.find(c => c.path === this.draggedPath);
      this.draggedPath = null;
      this.dropColumn = null;
      if (card && card.column !== columnId) this.move(card, columnId);
    },

    // Two checks keep a move from overwriting someone else's edit. The mtime covers
    // the time since the board loaded, and base_hash makes core answer 409 for a
    // write that lands between this GET and the POST.
    async move(card, status) {
      if (this.movingPath || card.status === status || !this.canMove(card)) return;
      if (!this.board.columns.some(column => column.id === status)) return;
      this.movingPath = card.path;
      const url = this.noteApiUrl(card.path);

      try {
        const current = await fetch(url + '?include_backlinks=false');
        if (current.status === 401) {
          location.href = '/login';
          return;
        }
        if (current.status === 404) {
          this.showNotice(text.conflict);
          return;
        }
        if (!current.ok) throw new Error('HTTP ' + current.status);
        const note = await current.json();
        if (note.metadata.modified !== card.mtime) {
          this.showNotice(text.conflict);
          return;
        }

        const saved = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: kanbanSetStatus(note.content, status), base_hash: note.hash }),
        });
        if (saved.status === 401) {
          location.href = '/login';
          return;
        }
        if (saved.status === 409) {
          this.showNotice(text.conflict);
          return;
        }
        if (!saved.ok) throw new Error('HTTP ' + saved.status);
      } catch (e) {
        console.error('Failed to move the card:', e);
        this.showNotice(text.move_error);
      } finally {
        this.movingPath = null;
        await this.load();
      }
    },

    openNewCard(columnId) {
      this.newTitle = '';
      this.newColumn = columnId;
      this.$refs.newCard.showModal();
    },

    newCardLabel(column) {
      return text.new_card_in.replace('{column}', this.columnLabel(column));
    },

    noteApiUrl(path) {
      return '/api/notes/' + path.split('/').map(encodeURIComponent).join('/');
    },

    notePath(name) {
      return this.folder ? this.folder + '/' + name : name;
    },

    // Core's save overwrites without asking, so a new note must find a free name first.
    async freeNotePath(title) {
      const clean = title.replace(/[\\/:*?"<>|#\p{C}]/gu, ' ').replace(/\s+/g, ' ').trim().replace(/^[_.\s]+/, '');
      const base = clean.slice(0, 80).trim() || 'card';
      for (let i = 1; i < 100; i++) {
        const path = this.notePath((i === 1 ? base : base + ' ' + i) + '.md');
        const response = await fetch(this.noteApiUrl(path) + '?include_backlinks=false');
        if (response.status === 404) return path;
        if (!response.ok) throw new Error('HTTP ' + response.status);
      }
      throw new Error('No free note name');
    },

    async saveNewNote(path, content) {
      const response = await fetch(this.noteApiUrl(path), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      if (response.status === 401) location.href = '/login';
      else if (!response.ok) throw new Error('HTTP ' + response.status);
    },

    async createCard() {
      const title = this.newTitle.trim();
      if (!title) return;
      this.$refs.newCard.close();
      try {
        const path = await this.freeNotePath(title);
        const content = '---\nstatus: ' + kanbanYamlValue(this.newColumn) + '\n---\n# ' + title + '\n';
        await this.saveNewNote(path, content);
        this.activeColumn = this.newColumn;
      } catch (e) {
        console.error('Failed to create the card:', e);
        this.showNotice(text.create_error);
      }
      await this.load();
    },

    async createBoardFile() {
      try {
        const path = this.notePath('_board.md');
        const existing = await fetch(this.noteApiUrl(path) + '?include_backlinks=false');
        if (existing.status === 404) await this.saveNewNote(path, this.board.board_template);
      } catch (e) {
        console.error('Failed to create _board.md:', e);
        this.showNotice(text.create_error);
      }
      await this.load();
    },

    warningText(card, warning) {
      if (warning === 'unknown_status') return text.unknown_status.replace('{status}', card.status);
      return text[warning] || warning;
    },
  };
}
</script>
<script defer src="/static/vendor/alpinejs/cdn.min.js"></script>
</head>
<body x-data="kanbanBoard()">
  <header class="top">
    <nav class="crumbs">
      <a href="/">
        <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 19l-7-7 7-7"/></svg>
        <span x-text="text.back"></span>
      </a>
      <span x-cloak x-show="parentPath()" x-text="parentPath()"></span>
    </nav>
    <div class="head">
      <h1 x-text="folderName()"></h1>
      <input class="search" type="search" x-model="query" :placeholder="text.search" :aria-label="text.search">
      <template x-if="board && !board.has_board_file">
        <button class="btn" type="button" @click="createBoardFile()" x-text="text.create_board"></button>
      </template>
      <button class="btn btn-primary" type="button" x-cloak x-show="board" @click="openNewCard(activeColumn)" x-text="text.new_card"></button>
      <button class="btn btn-icon" type="button" @click="load()" :title="text.reload" :aria-label="text.reload">
        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4v5h5M20 20v-5h-5"/><path d="M5.5 15a7.5 7.5 0 0012.8 2.5L20 15M18.5 9A7.5 7.5 0 005.7 6.5L4 9"/></svg>
      </button>
    </div>
  </header>

  <p class="state" x-show="loading" x-text="text.loading"></p>
  <p class="state" x-cloak x-show="failed" x-text="text.load_error"></p>

  <template x-if="board">
    <div style="display: contents">
      <div class="filters">
        <div class="chips">
          <button class="chip-btn" type="button" :aria-pressed="activeTag === null" @click="activeTag = null" x-text="text.all"></button>
          <template x-for="tag in allTags()" :key="tag">
            <button class="chip-btn" type="button" :aria-pressed="activeTag === tag" @click="activeTag = tag" x-text="tag"></button>
          </template>
        </div>
        <span class="count" x-text="countLabel()"></span>
      </div>

      <p class="warning" style="padding: 0 20px 8px" x-show="!board.board_file_valid" x-text="text.invalid_board"></p>

      <p class="state" x-show="board.cards.length === 0" x-text="text.no_notes"></p>

      <div class="pills" x-show="board.cards.length > 0">
        <template x-for="(column, index) in board.columns" :key="column.id">
          <button class="pill" type="button" :aria-pressed="activeColumn === column.id" @click="activeColumn = column.id">
            <span class="dot" :class="dotClass(column, index)"></span>
            <span x-text="columnLabel(column)"></span>
            <span class="column-count" x-text="columnCards(column.id).length"></span>
          </button>
        </template>
      </div>

      <main class="board" x-show="board.cards.length > 0">
        <template x-for="(column, index) in board.columns" :key="column.id">
          <section class="column"
                   :class="{ 'column-active': activeColumn === column.id, 'column-drop': dropColumn === column.id }"
                   @dragover.prevent="dropColumn = column.id"
                   @drop.prevent="onDrop(column.id)">
            <div class="column-head">
              <span class="dot" :class="dotClass(column, index)"></span>
              <h2 x-text="columnLabel(column)"></h2>
              <span class="column-count" x-text="columnCards(column.id).length"></span>
              <button class="column-add" type="button" @click="openNewCard(column.id)" :title="newCardLabel(column)" :aria-label="newCardLabel(column)">
                <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
              </button>
            </div>
            <div class="cards">
              <p class="empty-column" style="margin: 0" x-show="columnCards(column.id).length === 0" x-text="query || activeTag ? text.no_matches : text.no_cards"></p>
              <template x-for="card in columnCards(column.id)" :key="card.path">
                <article class="card"
                         :class="{ 'card-done': isDone(card), 'card-moving': movingPath === card.path || draggedPath === card.path }"
                         :draggable="canMove(card)"
                         @dragstart="draggedPath = card.path; $event.dataTransfer.effectAllowed = 'move'; $event.dataTransfer.setData('text/plain', card.path)"
                         @dragend="draggedPath = null; dropColumn = null">
                  <div class="card-top">
                    <a class="card-title" :href="noteUrl(card)" draggable="false" x-text="card.title"></a>
                    <template x-if="priorityValue(card)">
                      <span class="priority" :class="priorityClass(card)" x-text="priorityLabel(card)"></span>
                    </template>
                  </div>
                  <div class="tags" x-show="card.tags.length > 0">
                    <template x-for="tag in card.tags" :key="tag">
                      <span class="tag" x-text="tag"></span>
                    </template>
                  </div>
                  <div class="meta" x-show="footerFields(card).length > 0">
                    <template x-for="field in footerFields(card)" :key="field.id">
                      <span class="meta-item" :title="field.id" :class="field.type === 'date' && dateClass(field.value, card)">
                        <template x-if="field.type === 'date'">
                          <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18"/></svg>
                        </template>
                        <template x-if="field.id === 'owner'">
                          <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0116 0"/></svg>
                        </template>
                        <span x-text="field.type === 'date' ? dateLabel(field.value) : field.value"></span>
                      </span>
                    </template>
                  </div>
                  <template x-for="warning in card.warnings" :key="warning">
                    <p class="warning" style="margin: 0">
                      <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
                      <span x-text="warningText(card, warning)"></span>
                    </p>
                  </template>
                  <div class="card-foot">
                    <span class="file" x-text="card.name"></span>
                    <div class="card-actions" x-show="canMove(card)">
                      <select class="move-select" :aria-label="text.move_to" @change="move(card, $event.target.value); $event.target.value = ''">
                        <option value="" selected disabled x-text="text.move_to"></option>
                        <template x-for="target in moveTargets(card)" :key="target.id">
                          <option :value="target.id" x-text="columnLabel(target)"></option>
                        </template>
                      </select>
                      <template x-if="nextColumn(card)">
                        <button class="btn move-next" type="button" @click="move(card, nextColumn(card).id)">
                          <span x-text="moveToLabel(nextColumn(card))"></span>
                          <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
                        </button>
                      </template>
                    </div>
                  </div>
                </article>
              </template>
            </div>
          </section>
        </template>
      </main>
    </div>
  </template>

  <dialog x-ref="newCard">
    <form @submit.prevent="createCard()">
      <h2 x-text="text.new_card"></h2>
      <label class="form-row">
        <span x-text="text.title"></span>
        <input type="text" required maxlength="200" x-model="newTitle">
      </label>
      <label class="form-row">
        <span x-text="text.column"></span>
        <select x-model="newColumn">
          <template x-for="column in board ? board.columns : []" :key="column.id">
            <option :value="column.id" :selected="column.id === newColumn" x-text="columnLabel(column)"></option>
          </template>
        </select>
      </label>
      <div class="form-actions">
        <button class="btn" type="button" @click="$refs.newCard.close()" x-text="text.cancel"></button>
        <button class="btn btn-primary" type="submit" x-text="text.create"></button>
      </div>
    </form>
  </dialog>

  <div class="toast" role="status" x-cloak x-show="notice" x-text="notice"></div>
</body>
</html>
"""
