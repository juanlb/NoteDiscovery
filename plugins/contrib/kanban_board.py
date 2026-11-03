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
         Changing tags in the panel writes them as `tags: [a, b]`, which replaces a
         multi-line list and any comments inside it.
         Saving from the gear button rewrites the `board:` block of _board.md, which
         drops comments inside it and fields with an unknown type. Other keys and the
         free text stay. Column ids and field names are fixed once created, because
         notes and agents use them; rename a column through its display name.
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
and the page offers a button to create it. The gear button edits this schema visually.

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

Clicking a card title opens a panel to edit its status, tags, fields and raw Markdown
body. Everything else, like preview or wikilink autocomplete, stays in the full note.
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
HEADING_PATTERN = re.compile(r"^#[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
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
    # RecursionError: deeply nested input, which one note must not turn into a 500.
    except (yaml.YAMLError, RecursionError):
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
    # An empty list is a valid choice: a board with no finished columns.
    done = _string_list(raw["done"]) if isinstance(raw.get("done"), list) else DEFAULT_DONE
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
    # Frontmatter past the read limit looks absent, and a move would then add a second
    # block in front of the real one.
    truncated = frontmatter is None and len(text) >= MAX_READ_CHARS and text.split("\n")[0].strip() == "---"
    warnings = [] if valid and not truncated else ["invalid_frontmatter"]

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

    def get_ui(self) -> dict:
        """Folder action for core versions with plugin UI actions. Older versions
        never call this, and the board stays reachable by URL."""
        return {
            "folder_actions": [
                {
                    "id": "open_board",
                    "label": {"en": "Open as board", "es": "Abrir como tablero"},
                    "icon": "board",
                    "url": "/api/plugins/kanban_board/board?folder={folder}",
                    "when_file": BOARD_FILE,
                },
                {
                    "id": "create_board",
                    "label": {"en": "Create board", "es": "Crear tablero"},
                    "icon": "board",
                    "url": "/api/plugins/kanban_board/board?folder={folder}&setup=1",
                    "unless_file": BOARD_FILE,
                    "places": ["breadcrumb"],
                },
            ]
        }

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
    // Native controls, like the date picker icon, follow the theme this way.
    document.documentElement.style.colorScheme = /@theme-type:\s*light/.test(theme.css) ? 'light' : 'dark';
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
dialog::backdrop { background: color-mix(in srgb, var(--bg-primary) 60%, transparent); }
dialog h2 { margin: 0 0 12px; font-size: 16px; }
.form-row { display: block; margin-bottom: 12px; font-size: 12px; color: var(--text-secondary); }
.form-row input, .form-row select { display: block; width: 100%; height: 38px; margin-top: 4px; padding: 0 8px; border: 1px solid var(--border-primary); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); font: inherit; font-size: 14px; }
.form-actions { display: flex; justify-content: flex-end; gap: 8px; }
dialog.editor { width: min(640px, 100vw); max-width: 100vw; height: 100dvh; max-height: 100dvh; margin: 0 0 0 auto; padding: 0; border: 0; border-left: 1px solid var(--border-primary); border-radius: 0; outline: none; }
.editor-form { display: flex; flex-direction: column; height: 100%; padding: 16px; overflow-y: auto; }
.editor-head { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 12px; }
dialog.editor h2 { flex: 1; min-width: 0; margin: 4px 0 0; overflow-wrap: anywhere; }
.editor-link { display: inline-flex; flex-shrink: 0; align-items: center; gap: 4px; height: 34px; font-size: 12px; color: var(--accent-primary); text-decoration: none; }
.editor-link:hover { text-decoration: underline; }
.editor-fields { display: grid; flex-shrink: 0; grid-template-columns: 1fr 1fr; gap: 0 12px; }
.editor-fields .form-wide { grid-column: 1 / -1; }
.editor-body { flex: 1 0 240px; width: 100%; padding: 10px; border: 1px solid var(--border-primary); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); font: 13px/1.5 ui-monospace, "SF Mono", Menlo, Consolas, monospace; resize: none; }
.editor-message { flex-shrink: 0; margin: 10px 0 0; color: var(--kanban-error); font-size: 12px; }
.editor-form .form-actions { flex-shrink: 0; margin-top: 12px; }
.btn:disabled { opacity: .4; cursor: not-allowed; }
.settings-section { flex-shrink: 0; margin-bottom: 16px; }
.settings-section h3 { margin: 0; font-size: 13px; font-weight: 600; }
.settings-hint { margin: 2px 0 8px; font-size: 12px; color: var(--text-secondary); }
.settings-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 8px 0; border-top: 1px solid var(--border-primary); }
.settings-input { flex: 1 1 130px; min-width: 0; height: 34px; padding: 0 8px; border: 1px solid var(--border-primary); border-radius: 6px; background: var(--bg-secondary); color: var(--text-primary); font: inherit; }
.settings-id { flex: 1 1 110px; min-width: 0; font: 11px/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace; color: var(--text-secondary); overflow-wrap: anywhere; }
.settings-check { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-secondary); cursor: pointer; }
.settings-select { flex: 0 0 auto; }
.settings-actions { display: inline-flex; gap: 4px; margin-left: auto; }
.settings-add { margin-top: 4px; }

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
  dialog.editor { border-left: 0; }
  .editor-fields { grid-template-columns: 1fr; }
  .editor-body { font-size: 16px; }
  .editor-link { height: 44px; }
  .settings-input { height: 44px; font-size: 16px; }
  .settings-check { min-height: 44px; }
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
    settings: 'Board settings', columns_title: 'Columns', fields_title: 'Fields', add_column: 'Add column', add_field: 'Add field',
    column_name: 'Column name', field_name: 'Field name', finished: 'Finished', move_up: 'Move up', move_down: 'Move down', remove: 'Remove',
    type_date: 'Date', type_text: 'Text', type_list: 'List', field_type: 'Field type', list_values: 'Values, lowest first (comma separated)',
    hint_columns: 'Cards with no status, or an unknown one, go to the first column.',
    hint_fields: 'The first list is the card priority. The first list and the first date sort the cards.',
    comments_warning: 'Saving removes the comments inside the board block of _board.md.',
    board_invalid_edit: 'The _board.md file has invalid YAML. Fix it as text first.', open_board_file: 'Open _board.md',
    tag_invalid: 'The tag {name} has characters that are not allowed. Use letters, numbers, spaces and _ - . / +',
    field_reserved: 'A field cannot be named {name}.', field_duplicate: 'There are two fields named {name}.',
    field_needs_values: 'The list {name} needs at least one value.', needs_column: 'The board needs at least one column.',
    settings_conflict: '_board.md changed while you were editing. Nothing was saved, and your changes are still here.',
    open_full: 'Open full note', close: 'Close', save: 'Save', status: 'Status', tags: 'Tags (comma separated)', body: 'Note text',
    unsaved: 'Discard the unsaved changes?', save_error: 'Could not save the note', open_error: 'Could not open the note',
    edit_conflict: 'The note changed while you were editing. Nothing was saved, and your text is still here so you can copy it.',
    discard_reload: 'Discard and reload', raw_hint: 'The frontmatter is invalid, so this shows the whole file.',
    field_names: { due: 'Due date', priority: 'Priority', owner: 'Owner' },
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
    settings: 'Configuración del tablero', columns_title: 'Columnas', fields_title: 'Campos', add_column: 'Agregar columna', add_field: 'Agregar campo',
    column_name: 'Nombre de la columna', field_name: 'Nombre del campo', finished: 'Terminada', move_up: 'Subir', move_down: 'Bajar', remove: 'Quitar',
    type_date: 'Fecha', type_text: 'Texto', type_list: 'Lista', field_type: 'Tipo de campo', list_values: 'Valores, de menor a mayor (separados por coma)',
    hint_columns: 'Las cards sin status, o con uno desconocido, van a la primera columna.',
    hint_fields: 'La primera lista es la prioridad de la card. La primera lista y la primera fecha ordenan las cards.',
    comments_warning: 'Al guardar se pierden los comentarios del bloque board de _board.md.',
    board_invalid_edit: 'El archivo _board.md tiene YAML inválido. Primero arreglalo como texto.', open_board_file: 'Abrir _board.md',
    tag_invalid: 'El tag {name} tiene caracteres no permitidos. Usá letras, números, espacios y _ - . / +',
    field_reserved: 'Un campo no puede llamarse {name}.', field_duplicate: 'Hay dos campos llamados {name}.',
    field_needs_values: 'La lista {name} necesita al menos un valor.', needs_column: 'El tablero necesita al menos una columna.',
    settings_conflict: '_board.md cambió mientras editabas. No se guardó nada, y tus cambios siguen acá.',
    open_full: 'Abrir nota completa', close: 'Cerrar', save: 'Guardar', status: 'Status', tags: 'Tags (separados por coma)', body: 'Texto de la nota',
    unsaved: '¿Descartar los cambios sin guardar?', save_error: 'No se pudo guardar la nota', open_error: 'No se pudo abrir la nota',
    edit_conflict: 'La nota cambió mientras editabas. No se guardó nada, y tu texto sigue acá para que lo copies.',
    discard_reload: 'Descartar y recargar', raw_hint: 'El frontmatter es inválido, así que se muestra el archivo entero.',
    field_names: { due: 'Vence', priority: 'Prioridad', owner: 'Responsable' },
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

function kanbanFrontmatterEnd(lines) {
  // A BOM hides the frontmatter from core and from the Python side of this plugin.
  if (lines[0].startsWith('﻿') || lines[0].trim() !== '---') return -1;
  return lines.findIndex((line, index) => index > 0 && line.trim() === '---');
}

// Lines that belong to the value of the key above them.
const KANBAN_CONTINUATION = /^(?:[ \t]+\S|-[ \t])/;

// Touches only this field, never the rest of the frontmatter, so comments, order and
// formatting survive. A trailing comment on its line is kept too. An empty value removes
// the field, including a value that spans several lines.
function kanbanSetField(content, key, newValue) {
  const value = newValue ? kanbanYamlValue(newValue) : '';
  const lines = content.split('\n');
  const end = kanbanFrontmatterEnd(lines);

  if (end < 0) return value ? '---\n' + key + ': ' + value + '\n---\n' + content : content;

  const name = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp('^(' + name + '[ \\t]*:[ \\t]*)("(?:[^"\\\\]|\\\\.)*"|\'[^\']*\'|.*?)([ \\t]+#.*)?$');
  const edited = lines.slice(0, 1);
  let found = false;
  for (let i = 1; i < end; i++) {
    const match = pattern.exec(lines[i]);
    if (!match) {
      edited.push(lines[i]);
      continue;
    }
    found = true;
    while (i + 1 < end && KANBAN_CONTINUATION.test(lines[i + 1])) i++;
    if (value) {
      // `status:` with no value has no space after the colon yet.
      const prefix = /\s$/.test(match[1]) ? match[1] : match[1] + ' ';
      edited.push(prefix + value + (match[3] || ''));
    }
  }
  if (!found && value) edited.push(key + ': ' + value);
  return edited.concat(lines.slice(end)).join('\n');
}

// Tags are written unquoted, because core's parse_tags keeps quotes as part of the tag.
function kanbanParseTags(text) {
  const tags = text.split(',')
    .map(tag => tag.replace(/[\[\]]/g, '').trim().replace(/^#+/, '').trim().toLowerCase())
    .filter(Boolean);
  return [...new Set(tags)];
}

// Tags stay plain YAML scalars, because core's parser reads quotes as part of the tag.
function kanbanValidTag(tag) {
  return /^[\p{L}\p{N}_][\p{L}\p{N}_ .\/+-]*$/u.test(tag);
}

function kanbanSetTags(content, tags) {
  const value = tags.length ? 'tags: [' + tags.join(', ') + ']' : '';
  const lines = content.split('\n');
  const end = kanbanFrontmatterEnd(lines);

  if (end < 0) return value ? '---\n' + value + '\n---\n' + content : content;

  const start = lines.findIndex((line, index) => index > 0 && index < end && /^tags[ \t]*:/.test(line));
  if (start < 0) {
    if (value) lines.splice(end, 0, value);
    return lines.join('\n');
  }

  // A multi-line list ends at its last item, so a comment before the next key survives.
  let last = start;
  for (let i = start + 1; i < end && /^(?:\s|-|#|$)/.test(lines[i]); i++) {
    if (KANBAN_CONTINUATION.test(lines[i]) && !/^\s*#/.test(lines[i])) last = i;
  }
  lines.splice(start, last - start + 1, ...(value ? [value] : []));
  return lines.join('\n');
}

// Exact split, so saving an untouched body gives back the same bytes.
function kanbanSplitNote(content) {
  const lines = content.split('\n');
  const end = kanbanFrontmatterEnd(lines);
  if (end < 0) return { head: '', body: content };
  let length = lines.slice(0, end + 1).join('\n').length;
  if (content[length] === '\n') length++;
  return { head: content.slice(0, length), body: content.slice(length) };
}

function kanbanSlug(name) {
  return name.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

function kanbanSplitList(text) {
  return [...new Set(text.split(',').map(value => value.trim()).filter(Boolean))];
}

// The board block runs to its last indented line, so a comment before the next key stays.
function kanbanBoardBlockRange(lines, end) {
  const start = lines.findIndex((line, index) => index > 0 && index < end && /^board[ \t]*:/.test(line));
  if (start < 0) return null;
  let last = start;
  for (let i = start + 1; i < end && /^(\s|#|$)/.test(lines[i]); i++) {
    if (/^\s+\S/.test(lines[i])) last = i;
  }
  return { start, last };
}

function kanbanBoardHasComments(content) {
  const lines = content.split('\n');
  const end = kanbanFrontmatterEnd(lines);
  const range = end < 0 ? null : kanbanBoardBlockRange(lines, end);
  // A # inside a quoted value, like "Review: #1", is not a comment.
  const withoutQuotes = line => line.replace(/"(?:[^"\\]|\\.)*"|'[^']*'/g, '');
  return !!range && lines.slice(range.start, range.last + 1).some(line => /(^|\s)#/.test(withoutQuotes(line)));
}

function kanbanSetBoardBlock(content, block) {
  const lines = content.split('\n');
  const end = kanbanFrontmatterEnd(lines);
  if (end < 0) return '---\n' + block + '\n---\n' + content;
  const range = kanbanBoardBlockRange(lines, end);
  if (range) lines.splice(range.start, range.last - range.start + 1, block);
  else lines.splice(end, 0, block);
  return lines.join('\n');
}

function kanbanBoardBlock(columns, fields) {
  const list = values => '[' + values.map(kanbanYamlValue).join(', ') + ']';
  const lines = ['board:', '  columns: ' + list(columns.map(column => column.id))];
  const labelled = columns.filter(column => column.label);
  if (labelled.length) {
    lines.push('  labels:');
    for (const column of labelled) {
      lines.push('    ' + kanbanYamlValue(column.id) + ': ' + kanbanYamlValue(column.label));
    }
  }
  lines.push('  done: ' + list(columns.filter(column => column.done).map(column => column.id)));
  if (fields.length) {
    lines.push('  fields:');
    for (const field of fields) {
      const spec = field.type === 'list' ? list(field.values) : field.type;
      lines.push('    ' + kanbanYamlValue(field.id) + ': ' + spec);
    }
  } else {
    lines.push('  fields: {}');
  }
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
    rowCount: 0,
    settings: { loading: false, saving: false, invalid: false, conflict: false, error: '', hasFile: false, hasComments: false, columns: [], fields: [], content: '', hash: null, original: '' },
    edit: { title: '', url: '', raw: false, loading: false, saving: false, conflict: false, error: '', status: '', tagsText: '', fields: {}, body: '', original: { fields: {} } },

    init() {
      document.documentElement.lang = text === KANBAN_TEXT.es ? 'es' : 'en';
      document.title = this.folderName();
      this.start();
      // Shows what an agent changed while the tab was in the background.
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          kanbanApplyTheme();
          this.load();
        }
      });
    },

    // setup=1 comes from the "Create board" folder action. createBoardFile never
    // replaces an existing file, so opening this link twice changes nothing.
    async start() {
      await this.load();
      const url = new URL(location.href);
      if (!url.searchParams.has('setup')) return;
      url.searchParams.delete('setup');
      history.replaceState(null, '', url);
      if (this.board && !this.board.has_board_file) await this.createBoardFile();
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
          body: JSON.stringify({ content: kanbanSetField(note.content, 'status', status), base_hash: note.hash }),
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
        // Core ignores base_hash for a new note and answers 409 if the note now exists,
        // which keeps two tabs creating the same name from overwriting each other.
        body: JSON.stringify({ content, base_hash: 'new' }),
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

    // A modified or middle click keeps working as a plain link to the note.
    onTitleClick(event, card) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      this.openEditor(card);
    },

    async fetchNote(path) {
      const response = await fetch(this.noteApiUrl(path) + '?include_backlinks=false');
      if (response.status === 401) location.href = '/login';
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    },

    async openEditor(card) {
      this.edit = { ...this.edit, title: card.title, url: this.noteUrl(card), loading: true, conflict: false, error: '', body: '' };
      this.$refs.editor.showModal();
      this.$refs.editor.focus();
      try {
        let note = await this.fetchNote(card.path);
        // The form starts from the card data, so that data has to match this note.
        if (note.metadata.modified !== card.mtime) {
          await this.load();
          card = this.board.cards.find(c => c.path === card.path);
          if (!card) throw new Error('Note not found');
          note = await this.fetchNote(card.path);
        }
        this.fillEditor(card, note);
      } catch (e) {
        console.error('Failed to open the note:', e);
        this.$refs.editor.close();
        this.showNotice(text.open_error);
        await this.load();
      }
    },

    fillEditor(card, note) {
      const raw = card.warnings.includes('invalid_frontmatter');
      const { head, body } = raw ? { head: '', body: note.content } : kanbanSplitNote(note.content);
      const known = this.board.columns.some(column => column.id === card.status);
      const fields = {};
      for (const field of this.board.fields) fields[field.id] = card.fields[field.id] || '';
      const form = { status: known ? card.status : '', tagsText: card.tags.join(', '), fields, body };

      this.edit = {
        ...this.edit,
        ...form,
        fields: { ...fields },
        title: card.title,
        path: card.path,
        raw,
        head,
        content: note.content,
        hash: note.hash,
        loading: false,
        conflict: false,
        error: '',
        card,
        original: JSON.parse(JSON.stringify(form)),
      };
    },

    editorForm() {
      const { status, tagsText, fields, body } = this.edit;
      return JSON.stringify({ status, tagsText, fields, body });
    },

    editorDirty() {
      return !this.edit.loading && !!this.edit.original.fields && this.editorForm() !== JSON.stringify(this.edit.original);
    },

    closeEditor() {
      if (this.editorDirty() && !confirm(text.unsaved)) return;
      this.$refs.editor.close();
    },

    fieldLabel(field) {
      return text.field_names[field.id] || field.id;
    },

    optionLabel(value) {
      return text.priorities[value] || value;
    },

    // A value outside the list stays selectable, so opening the panel never changes it.
    fieldOptions(field) {
      const current = this.edit.original.fields[field.id];
      return current && !field.values.includes(current) ? [...field.values, current] : field.values;
    },

    // A date the browser cannot show stays editable as text instead of looking empty.
    fieldInputType(field) {
      const value = this.edit.original.fields[field.id];
      return field.type === 'date' && (!value || this.parseDate(value)) ? 'date' : 'text';
    },

    editedContent() {
      const edit = this.edit;
      if (edit.raw) return edit.body;

      // A closing fence on the last line has no newline yet to separate it from the body.
      const separator = edit.head && !edit.head.endsWith('\n') && edit.body ? '\n' : '';
      let content = edit.head + separator + edit.body;
      if (edit.status && edit.status !== edit.original.status) {
        content = kanbanSetField(content, 'status', edit.status);
      }
      for (const field of this.board.fields) {
        // The board can gain a field while the panel is open, through a reload.
        const value = (edit.fields[field.id] || '').trim();
        if (value !== (edit.original.fields[field.id] || '').trim()) {
          content = kanbanSetField(content, field.id, value);
        }
      }
      const tags = kanbanParseTags(edit.tagsText);
      const sameTags = [...tags].sort().join() === [...kanbanParseTags(edit.original.tagsText)].sort().join();
      return sameTags ? content : kanbanSetTags(content, tags);
    },

    async saveEditor() {
      if (this.edit.loading || this.edit.saving || this.edit.conflict) return;
      this.edit.error = '';
      const original = kanbanParseTags(this.edit.original.tagsText || '');
      const badTag = kanbanParseTags(this.edit.tagsText || '').find(tag => !original.includes(tag) && !kanbanValidTag(tag));
      if (badTag && !this.edit.raw) {
        this.edit.error = text.tag_invalid.replace('{name}', badTag);
        return;
      }
      const content = this.editedContent();
      if (content === this.edit.content) {
        this.$refs.editor.close();
        return;
      }

      this.edit.saving = true;
      this.edit.error = '';
      try {
        const response = await fetch(this.noteApiUrl(this.edit.path), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content, base_hash: this.edit.hash }),
        });
        if (response.status === 401) {
          location.href = '/login';
          return;
        }
        if (response.status === 409) {
          this.edit.conflict = true;
          return;
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        this.$refs.editor.close();
        await this.load();
      } catch (e) {
        console.error('Failed to save the note:', e);
        this.edit.error = text.save_error;
      } finally {
        this.edit.saving = false;
      }
    },

    async discardAndReload() {
      await this.load();
      const card = this.board.cards.find(c => c.path === this.edit.path);
      if (!card) {
        this.$refs.editor.close();
        this.showNotice(text.open_error);
        return;
      }
      try {
        this.fillEditor(card, await this.fetchNote(card.path));
      } catch (e) {
        console.error('Failed to reload the note:', e);
        this.edit.error = text.open_error;
      }
    },

    async openSettings() {
      this.settings = { ...this.settings, loading: true, invalid: false, conflict: false, error: '' };
      this.$refs.settings.showModal();
      this.$refs.settings.focus();
      await this.fillSettings();
    },

    async fillSettings() {
      try {
        await this.load();
        const board = this.board;
        const note = board.has_board_file && board.board_file_valid
          ? await this.fetchNote(this.notePath('_board.md'))
          : null;
        const content = note ? note.content : '';

        this.settings = {
          ...this.settings,
          loading: false,
          conflict: false,
          error: '',
          invalid: !board.board_file_valid,
          hasFile: board.has_board_file,
          hasComments: kanbanBoardHasComments(content),
          content,
          hash: note ? note.hash : null,
          columns: board.columns.map(column => ({
            uid: ++this.rowCount,
            id: column.id,
            label: this.columnLabel(column),
            explicitLabel: column.label !== null,
            done: column.done,
            isNew: false,
          })),
          fields: board.fields.map(field => ({
            uid: ++this.rowCount,
            id: field.id,
            type: field.type,
            valuesText: (field.values || []).join(', '),
            isNew: false,
          })),
        };
        this.settings.original = this.settingsForm();
      } catch (e) {
        console.error('Failed to open the board settings:', e);
        this.$refs.settings.close();
        this.showNotice(text.open_error);
      }
    },

    settingsForm() {
      return JSON.stringify([this.settings.columns, this.settings.fields]);
    },

    settingsDirty() {
      return !this.settings.loading && !this.settings.invalid && this.settingsForm() !== this.settings.original;
    },

    closeSettings() {
      if (this.settingsDirty() && !confirm(text.unsaved)) return;
      this.$refs.settings.close();
    },

    addColumn() {
      this.settings.columns.push({ uid: ++this.rowCount, id: '', label: '', explicitLabel: true, done: false, isNew: true });
    },

    addField() {
      this.settings.fields.push({ uid: ++this.rowCount, id: '', type: 'text', valuesText: '', isNew: true });
    },

    moveRow(rows, index, delta) {
      rows.splice(index + delta, 0, rows.splice(index, 1)[0]);
    },

    // Ids come from the name while a column is new, and stay fixed after that.
    settingsColumnIds() {
      const columns = this.settings.columns;
      const used = new Set(columns.filter(column => !column.isNew).map(column => column.id));
      const ids = {};
      for (const column of columns) {
        if (!column.isNew) {
          ids[column.uid] = column.id;
        } else if (column.label.trim()) {
          const base = kanbanSlug(column.label) || 'column';
          let id = base;
          for (let n = 2; used.has(id); n++) id = base + '-' + n;
          used.add(id);
          ids[column.uid] = id;
        }
      }
      return ids;
    },

    columnCardCount(columnId) {
      return this.board ? this.board.cards.filter(card => card.status === columnId).length : 0;
    },

    columnCountLabel(column) {
      const count = column.isNew ? 0 : this.columnCardCount(column.id);
      return count === 1 ? text.cards_one : text.cards_many.replace('{n}', count);
    },

    canRemoveColumn(column) {
      return this.settings.columns.length > 1 && (column.isNew || this.columnCardCount(column.id) === 0);
    },

    // Returns the columns and fields to write, or null after setting the error to show.
    settingsToSave() {
      const ids = this.settingsColumnIds();
      const columns = this.settings.columns
        .filter(column => ids[column.uid])
        .map(column => {
          const label = column.label.trim();
          const fallback = text.columns[ids[column.uid]] || ids[column.uid];
          return {
            id: ids[column.uid],
            // A default name stays out of the file, so it keeps following the language.
            label: column.explicitLabel || label !== fallback ? label : '',
            done: column.done,
          };
        });
      if (!columns.length) {
        this.settings.error = text.needs_column;
        return null;
      }

      const fields = [];
      const names = new Set();
      for (const field of this.settings.fields) {
        const id = field.isNew ? kanbanSlug(field.id) : field.id;
        if (!id) continue;
        const values = kanbanSplitList(field.valuesText);
        let error = '';
        if (id === 'status' || id === 'tags') error = text.field_reserved;
        else if (names.has(id)) error = text.field_duplicate;
        else if (field.type === 'list' && !values.length) error = text.field_needs_values;
        if (error) {
          this.settings.error = error.replace('{name}', id);
          return null;
        }
        names.add(id);
        fields.push({ id, type: field.type, values });
      }
      return { columns, fields };
    },

    async saveSettings() {
      const settings = this.settings;
      if (settings.loading || settings.saving || settings.conflict || settings.invalid) return;
      if (settings.hasFile && !this.settingsDirty()) {
        this.$refs.settings.close();
        return;
      }
      settings.error = '';
      const schema = this.settingsToSave();
      if (!schema) return;

      settings.saving = true;
      const path = this.notePath('_board.md');
      try {
        const content = kanbanSetBoardBlock(settings.content, kanbanBoardBlock(schema.columns, schema.fields));
        const response = await fetch(this.noteApiUrl(path), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          // Without a file, core answers 409 if one appeared since the panel opened.
          body: JSON.stringify({ content, base_hash: settings.hash || 'new' }),
        });
        if (response.status === 401) {
          location.href = '/login';
          return;
        }
        if (response.status === 409) {
          settings.conflict = true;
          return;
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        this.$refs.settings.close();
        await this.load();
      } catch (e) {
        console.error('Failed to save the board settings:', e);
        settings.error = text.save_error;
      } finally {
        settings.saving = false;
      }
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
      <button class="btn btn-icon" type="button" x-cloak x-show="board" @click="openSettings()" :title="text.settings" :aria-label="text.settings">
        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><circle cx="12" cy="12" r="3"/></svg>
      </button>
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
                    <a class="card-title" :href="noteUrl(card)" draggable="false" @click="onTitleClick($event, card)" x-text="card.title"></a>
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

  <dialog class="editor" x-ref="editor" tabindex="-1" @cancel.prevent="closeEditor()" @click.self="closeEditor()">
    <form class="editor-form" @submit.prevent="saveEditor()">
      <div class="editor-head">
        <h2 x-text="edit.title"></h2>
        <a class="editor-link" :href="edit.url" target="_blank" rel="noopener">
          <span x-text="text.open_full"></span>
          <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 5h5v5m0-5l-8 8M10 5H6a1 1 0 00-1 1v12a1 1 0 001 1h12a1 1 0 001-1v-4"/></svg>
        </a>
        <button class="btn btn-icon" type="button" @click="closeEditor()" :title="text.close" :aria-label="text.close">
          <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>

      <p class="state" x-show="edit.loading" x-text="text.loading"></p>

      <template x-if="!edit.loading">
        <div style="display: contents">
          <p class="warning" style="margin: 0 0 10px" x-show="edit.raw" x-text="text.raw_hint"></p>
          <div class="editor-fields" x-show="!edit.raw">
            <label class="form-row">
              <span x-text="text.status"></span>
              <select x-model="edit.status">
                <option value="" disabled>—</option>
                <template x-for="column in board ? board.columns : []" :key="column.id">
                  <option :value="column.id" :selected="column.id === edit.status" x-text="columnLabel(column)"></option>
                </template>
              </select>
            </label>
            <template x-for="field in board ? board.fields : []" :key="field.id">
              <label class="form-row">
                <span x-text="fieldLabel(field)"></span>
                <template x-if="field.type === 'list'">
                  <select x-model="edit.fields[field.id]">
                    <option value="">—</option>
                    <template x-for="value in fieldOptions(field)" :key="value">
                      <option :value="value" :selected="value === edit.fields[field.id]" x-text="optionLabel(value)"></option>
                    </template>
                  </select>
                </template>
                <template x-if="field.type !== 'list'">
                  <input :type="fieldInputType(field)" x-model="edit.fields[field.id]">
                </template>
              </label>
            </template>
            <label class="form-row form-wide">
              <span x-text="text.tags"></span>
              <input type="text" x-model="edit.tagsText">
            </label>
          </div>
          <textarea class="editor-body" x-model="edit.body" :aria-label="text.body"></textarea>
          <p class="editor-message" x-show="edit.conflict" x-text="text.edit_conflict"></p>
          <p class="editor-message" x-show="edit.error" x-text="edit.error"></p>
          <div class="form-actions">
            <button class="btn" type="button" x-show="edit.conflict" @click="discardAndReload()" x-text="text.discard_reload"></button>
            <button class="btn" type="button" @click="closeEditor()" x-text="text.cancel"></button>
            <button class="btn btn-primary" type="submit" :disabled="edit.saving || edit.conflict" x-text="text.save"></button>
          </div>
        </div>
      </template>
    </form>
  </dialog>

  <dialog class="editor" x-ref="settings" tabindex="-1" @cancel.prevent="closeSettings()" @click.self="closeSettings()">
    <form class="editor-form" @submit.prevent="saveSettings()">
      <div class="editor-head">
        <h2 x-text="text.settings"></h2>
        <template x-if="settings.hasFile">
          <a class="editor-link" :href="noteUrl({ path: notePath('_board.md') })" target="_blank" rel="noopener">
            <span x-text="text.open_board_file"></span>
            <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 5h5v5m0-5l-8 8M10 5H6a1 1 0 00-1 1v12a1 1 0 001 1h12a1 1 0 001-1v-4"/></svg>
          </a>
        </template>
        <button class="btn btn-icon" type="button" @click="closeSettings()" :title="text.close" :aria-label="text.close">
          <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>

      <p class="state" x-show="settings.loading" x-text="text.loading"></p>
      <p class="warning" style="margin: 0" x-show="settings.invalid" x-text="text.board_invalid_edit"></p>

      <template x-if="!settings.loading && !settings.invalid">
        <div style="display: contents">
          <p class="warning" style="margin: 0 0 10px" x-show="settings.hasComments" x-text="text.comments_warning"></p>

          <section class="settings-section">
            <h3 x-text="text.columns_title"></h3>
            <p class="settings-hint" x-text="text.hint_columns"></p>
            <template x-for="(column, index) in settings.columns" :key="column.uid">
              <div class="settings-row">
                <input class="settings-input" type="text" x-model="column.label" :aria-label="text.column_name" :placeholder="text.column_name">
                <span class="settings-id" x-text="(settingsColumnIds()[column.uid] || '…') + ' · ' + columnCountLabel(column)"></span>
                <label class="settings-check">
                  <input type="checkbox" x-model="column.done">
                  <span x-text="text.finished"></span>
                </label>
                <span class="settings-actions">
                  <button class="btn btn-icon" type="button" :disabled="index === 0" @click="moveRow(settings.columns, index, -1)" :title="text.move_up" :aria-label="text.move_up"><svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg></button>
                  <button class="btn btn-icon" type="button" :disabled="index === settings.columns.length - 1" @click="moveRow(settings.columns, index, 1)" :title="text.move_down" :aria-label="text.move_down"><svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M6 13l6 6 6-6"/></svg></button>
                  <button class="btn btn-icon" type="button" :disabled="!canRemoveColumn(column)" @click="settings.columns.splice(index, 1)" :title="text.remove" :aria-label="text.remove"><svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg></button>
                </span>
              </div>
            </template>
            <button class="btn settings-add" type="button" @click="addColumn()" x-text="text.add_column"></button>
          </section>

          <section class="settings-section">
            <h3 x-text="text.fields_title"></h3>
            <p class="settings-hint" x-text="text.hint_fields"></p>
            <template x-for="(field, index) in settings.fields" :key="field.uid">
              <div class="settings-row">
                <template x-if="field.isNew">
                  <input class="settings-input" type="text" x-model="field.id" @blur="field.id = kanbanSlug(field.id)" :aria-label="text.field_name" :placeholder="text.field_name">
                </template>
                <template x-if="!field.isNew">
                  <span class="settings-id" x-text="field.id"></span>
                </template>
                <select class="settings-input settings-select" x-model="field.type" :aria-label="text.field_type">
                  <option value="date" :selected="field.type === 'date'" x-text="text.type_date"></option>
                  <option value="text" :selected="field.type === 'text'" x-text="text.type_text"></option>
                  <option value="list" :selected="field.type === 'list'" x-text="text.type_list"></option>
                </select>
                <span class="settings-actions">
                  <button class="btn btn-icon" type="button" :disabled="index === 0" @click="moveRow(settings.fields, index, -1)" :title="text.move_up" :aria-label="text.move_up"><svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg></button>
                  <button class="btn btn-icon" type="button" :disabled="index === settings.fields.length - 1" @click="moveRow(settings.fields, index, 1)" :title="text.move_down" :aria-label="text.move_down"><svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M6 13l6 6 6-6"/></svg></button>
                  <button class="btn btn-icon" type="button" @click="settings.fields.splice(index, 1)" :title="text.remove" :aria-label="text.remove"><svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg></button>
                </span>
                <input class="settings-input" style="flex-basis: 100%" type="text" x-show="field.type === 'list'" x-model="field.valuesText" :aria-label="text.list_values" :placeholder="text.list_values">
              </div>
            </template>
            <button class="btn settings-add" type="button" @click="addField()" x-text="text.add_field"></button>
          </section>

          <p class="editor-message" x-show="settings.conflict" x-text="text.settings_conflict"></p>
          <p class="editor-message" x-show="settings.error" x-text="settings.error"></p>
          <div class="form-actions">
            <button class="btn" type="button" x-show="settings.conflict" @click="fillSettings()" x-text="text.discard_reload"></button>
            <button class="btn" type="button" @click="closeSettings()" x-text="text.cancel"></button>
            <button class="btn btn-primary" type="submit" :disabled="settings.saving || settings.conflict" x-text="text.save"></button>
          </div>
        </div>
      </template>
    </form>
  </dialog>

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
