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

Agents (MCP): on core versions with plugin MCP tools, this plugin adds
kanban_board_list, kanban_board_get, kanban_board_update_card, kanban_board_create_card
and kanban_board_configure. A typical flow:
  1. kanban_board_get(folder, status="todo") to see the cards.
  2. get_note(path) to read a card, then kanban_board_update_card(path, status="doing").
  3. Work, then append_to_note(path, ...) with the result.
  4. kanban_board_update_card(path, status="done").
Pass expected_mtime from kanban_board_get to refuse a card that changed in between.
Suggested convention: `owner: agent` marks the cards an agent can take without asking.
The title is the first "# heading", or the file name when there is none.

Clicking a card title opens a panel with its status, tags and fields, and the note
rendered as Markdown. An Edit button shows the source, and switching saves. The rendering
is simpler than the app's: no Mermaid, math, callouts, code highlighting or ![[media]],
and a [[wikilink]] needs the full path. For those, the panel links to the full note.
"""

import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import HTMLResponse

from backend.utils import note_name_conflict, parse_tags, resolve_vault_folder

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


# Frontmatter edits for the MCP tools. These mirror the kanbanSet* functions in the page
# below, which do the same job for the browser: keep the two in step.
PLAIN_YAML_VALUE = re.compile(r"^\w[\w .-]*$")
VALID_TAG = re.compile(r"^\w[\w ./+-]*$")
CONTINUATION = re.compile(r"^(?:[ \t]+\S|-[ \t])")
FIELD_VALUE = r"""("(?:[^"\\]|\\.)*"|'[^']*'|.*?)"""
RESERVED_FIELDS = {"status", "tags"}


def _yaml_value(value: str) -> str:
    return value if PLAIN_YAML_VALUE.match(value) else json.dumps(value, ensure_ascii=False)


def _frontmatter_end(lines: list[str]) -> int:
    if lines[0].startswith("﻿") or lines[0].strip() != "---":
        return -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return i
    return -1


def _set_field(content: str, key: str, new_value: str) -> str:
    """Touch only this field. An empty value removes it, with any continuation lines."""
    value = _yaml_value(new_value) if new_value else ""
    lines = content.split("\n")
    end = _frontmatter_end(lines)
    if end < 0:
        return f"---\n{key}: {value}\n---\n{content}" if value else content

    pattern = re.compile(rf"^({re.escape(key)}[ \t]*:[ \t]*){FIELD_VALUE}([ \t]+#.*)?$")
    edited = lines[:1]
    found = False
    i = 1
    while i < end:
        match = pattern.match(lines[i])
        i += 1
        if not match:
            edited.append(lines[i - 1])
            continue
        found = True
        while i < end and CONTINUATION.match(lines[i]):
            i += 1
        if value:
            prefix = match[1] if match[1][-1].isspace() else match[1] + " "
            edited.append(prefix + value + (match[3] or ""))
    if not found and value:
        edited.append(f"{key}: {value}")
    return "\n".join(edited + lines[end:])


def _set_tags(content: str, tags: list[str]) -> str:
    """Write the tags unquoted, because parse_tags reads quotes as part of the tag."""
    value = f"tags: [{', '.join(tags)}]" if tags else ""
    lines = content.split("\n")
    end = _frontmatter_end(lines)
    if end < 0:
        return f"---\n{value}\n---\n{content}" if value else content

    start = next((i for i in range(1, end) if re.match(r"^tags[ \t]*:", lines[i])), None)
    if start is None:
        if value:
            lines.insert(end, value)
        return "\n".join(lines)

    last = start
    i = start + 1
    while i < end and re.match(r"^(?:\s|-|#|$)", lines[i]):
        if CONTINUATION.match(lines[i]) and not re.match(r"^\s*#", lines[i]):
            last = i
        i += 1
    lines[start:last + 1] = [value] if value else []
    return "\n".join(lines)


def _split_note(content: str) -> tuple[str, str]:
    """Return (head with frontmatter, body), so head + body gives back the same text."""
    lines = content.split("\n")
    end = _frontmatter_end(lines)
    if end < 0:
        return "", content
    length = len("\n".join(lines[:end + 1]))
    if content[length:length + 1] == "\n":
        length += 1
    return content[:length], content[length:]


def _with_body(content: str, body: str) -> str:
    head, _ = _split_note(content)
    separator = "\n" if head and not head.endswith("\n") and body else ""
    return head + separator + body


def _board_block_range(lines: list[str], end: int) -> tuple[int, int] | None:
    start = next((i for i in range(1, end) if re.match(r"^board[ \t]*:", lines[i])), None)
    if start is None:
        return None
    last = start
    i = start + 1
    while i < end and re.match(r"^(?:\s|#|$)", lines[i]):
        if re.match(r"^\s+\S", lines[i]):
            last = i
        i += 1
    return start, last


def _board_has_comments(content: str) -> bool:
    lines = content.split("\n")
    end = _frontmatter_end(lines)
    block = _board_block_range(lines, end) if end >= 0 else None
    if block is None:
        return False
    unquoted = (re.sub(r""""(?:[^"\\]|\\.)*"|'[^']*'""", "", line) for line in lines[block[0]:block[1] + 1])
    return any(re.search(r"(^|\s)#", line) for line in unquoted)


def _set_board_block(content: str, block: str) -> str:
    lines = content.split("\n")
    end = _frontmatter_end(lines)
    if end < 0:
        return f"---\n{block}\n---\n{content}"
    found = _board_block_range(lines, end)
    if found:
        lines[found[0]:found[1] + 1] = [block]
    else:
        lines.insert(end, block)
    return "\n".join(lines)


def _board_block(columns: list[dict], fields: list[dict]) -> str:
    def as_list(values):
        return "[" + ", ".join(_yaml_value(v) for v in values) + "]"

    lines = ["board:", "  columns: " + as_list([c["id"] for c in columns])]
    labelled = [c for c in columns if c.get("label")]
    if labelled:
        lines.append("  labels:")
        lines += [f"    {_yaml_value(c['id'])}: {_yaml_value(c['label'])}" for c in labelled]
    lines.append("  done: " + as_list([c["id"] for c in columns if c.get("done")]))
    if fields:
        lines.append("  fields:")
        for field in fields:
            spec = as_list(field["values"]) if field["type"] == "list" else field["type"]
            lines.append(f"    {_yaml_value(field['id'])}: {spec}")
    else:
        lines.append("  fields: {}")
    return "\n".join(lines)


def _slug(name: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFD", name) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")


def _fail(status_code: int, detail: str):
    raise HTTPException(status_code=status_code, detail=detail)


def _clean_text(value, name: str) -> str:
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        _fail(400, f"{name} must be text on one line")
    return value.strip()


def _clean_status(status, config: dict) -> str:
    ids = [column["id"] for column in config["columns"]]
    if status not in ids:
        _fail(400, f"Unknown status {status!r}. This board has: {', '.join(ids)}")
    return status


def _clean_tags(tags) -> list[str]:
    if not isinstance(tags, list):
        _fail(400, "tags must be a list")
    cleaned = []
    for tag in tags:
        tag = _clean_text(tag, "tag").lstrip("#").strip().lower()
        if not VALID_TAG.match(tag):
            _fail(400, f"Invalid tag {tag!r}. Use letters, numbers, spaces and _ - . / +")
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned


def _clean_fields(fields, config: dict) -> dict[str, str]:
    if not isinstance(fields, dict):
        _fail(400, "fields must be an object")
    known = {field["id"]: field for field in config["fields"]}
    cleaned = {}
    for field_id, value in fields.items():
        field = known.get(field_id)
        if field is None:
            _fail(400, f"Unknown field {field_id!r}. This board has: {', '.join(known) or 'no fields'}")
        value = "" if value is None else _clean_text(value, field_id)
        if value and field["type"] == "list" and value not in field["values"]:
            _fail(400, f"{field_id} must be one of: {', '.join(field['values'])}")
        if value and field["type"] == "date":
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                _fail(400, f"{field_id} must be a date as YYYY-MM-DD")
        cleaned[field_id] = value
    return cleaned


MCP_TOOLS = [
    {
        "name": "kanban_board_list",
        "description": "List the Kanban boards in the vault: the folders that have a _board.md, with their columns. A folder without _board.md can still be used as a board with the default columns.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "kanban_board_get",
        "description": "Read a folder as a Kanban board: its columns, fields and cards. Each card has path, title, status, column, tags, fields, mtime and warnings. Cards do not include the note text, so use get_note to read it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Folder path. An empty string is the vault root."},
                "status": {"type": "string", "description": "Only return the cards shown in this column."},
            },
            "required": ["folder"],
        },
    },
    {
        "name": "kanban_board_update_card",
        "description": "Change a card. Send only what should change. Use status to move the card to another column. Only the affected frontmatter lines change, and everything else in the note stays. To add to the note text without replacing it, use append_to_note.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Card path, as kanban_board_get returns it."},
                "status": {"type": "string", "description": "New column id."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "The full new list of tags."},
                "fields": {"type": "object", "description": "Fields to change, by id. Use null or an empty string to remove a field. Only fields of the board are accepted."},
                "body": {"type": "string", "description": "New note text, replacing everything after the frontmatter."},
                "expected_mtime": {"type": "string", "description": "The card mtime you read. If the card changed since then, nothing is written and the call fails."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "kanban_board_create_card",
        "description": "Create a card in a board folder. The file name comes from the title and never replaces an existing note.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Board folder path."},
                "title": {"type": "string", "description": "Card title, written as the first heading."},
                "status": {"type": "string", "description": "Column id. Default: the first column."},
                "tags": {"type": "array", "items": {"type": "string"}},
                "fields": {"type": "object", "description": "Field values, by id. Only fields of the board are accepted."},
                "body": {"type": "string", "description": "Note text after the heading."},
            },
            "required": ["folder", "title"],
        },
    },
    {
        "name": "kanban_board_configure",
        "description": "Change a board's columns and fields in its _board.md, creating the file if needed. Send the full new list of what you change; a list you omit stays as it is. Order matters: cards with no status go to the first column, the first list field is the priority, and the first list and date fields sort the cards. Existing column ids and field ids cannot be renamed, because notes use them. A column with cards cannot be removed. This rewrites the board block, which removes comments inside it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Board folder path."},
                "columns": {
                    "type": "array",
                    "description": "Columns in order. To keep a column, send its id. For a new column, send a label and the id comes from it.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string", "description": "Display name. An empty string removes it."},
                            "done": {"type": "boolean", "description": "Cards here show as finished."},
                        },
                    },
                },
                "fields": {
                    "type": "array",
                    "description": "Fields in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"type": "string", "enum": ["date", "text", "list"]},
                            "values": {"type": "array", "items": {"type": "string"}, "description": "Allowed values for a list, lowest first."},
                        },
                        "required": ["id", "type"],
                    },
                },
            },
            "required": ["folder"],
        },
    },
]


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
        # Absent on core versions without plugin MCP tools. The tools stay off there.
        self.read_note = getattr(ctx, "read_note", None)
        self.write_note = getattr(ctx, "write_note", None)

    def get_mcp_tools(self) -> list[dict]:
        return MCP_TOOLS if self.write_note else []

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

        @router.post("/mcp/{tool}")
        def mcp_tool(tool: str, args: dict = Body(default={})):
            handlers = {
                "kanban_board_list": self._tool_list,
                "kanban_board_get": self._tool_get,
                "kanban_board_update_card": self._tool_update_card,
                "kanban_board_create_card": self._tool_create_card,
                "kanban_board_configure": self._tool_configure,
            }
            if not self.enabled or not self.write_note or tool not in handlers:
                _fail(404, "Tool not available")
            return handlers[tool](args)

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

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(Path(self.notes_dir).resolve()).as_posix()

    def _tool_folder(self, args: dict) -> Path:
        folder = args.get("folder")
        if not isinstance(folder, str):
            _fail(400, "folder is required")
        return self._resolve_folder(folder)

    def _tool_list(self, args: dict) -> dict:
        boards = []
        for root, dirnames, filenames in os.walk(self.notes_dir):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            if BOARD_FILE in filenames:
                folder = Path(root)
                config = _read_board_config(folder)
                rel_folder = self._relative(folder)
                boards.append({
                    "folder": "" if rel_folder == "." else rel_folder,
                    "valid": config["board_file_valid"],
                    "columns": [column["id"] for column in config["columns"]],
                })
        return {"boards": boards}

    def _tool_get(self, args: dict) -> dict:
        data = self._board_data(self._tool_folder(args))
        data.pop("board_template", None)
        status = args.get("status")
        if status is not None:
            _clean_status(status, data)
            data["cards"] = [card for card in data["cards"] if card["column"] == status]
        return data

    def _card_target(self, rel_path) -> tuple[Path, Path, str]:
        """Return (note file, its folder, its path in the vault) for a card."""
        if not isinstance(rel_path, str) or not rel_path.endswith(".md"):
            _fail(400, "path must be a .md note")
        pure = PurePosixPath(rel_path)
        if pure.name[0] in "_.":
            _fail(400, f"{pure.name} is not a card")
        parent = "" if str(pure.parent) == "." else str(pure.parent)
        folder = self._resolve_folder(parent)
        target = folder / pure.name
        if target.is_symlink() or not target.is_file():
            _fail(404, "Card not found")
        return target, folder, self._relative(target)

    def _save(self, rel_path: str, content: str):
        if not self.write_note(rel_path, content):
            _fail(500, "Failed to save the note")

    def _tool_update_card(self, args: dict) -> dict:
        target, folder, rel_path = self._card_target(args.get("path"))
        config = _read_board_config(folder)
        card = _read_card(target, rel_path, config)
        if args.get("expected_mtime") and args["expected_mtime"] != card["mtime"]:
            _fail(409, "The card changed since you read it. Nothing was written. Read it again.")
        if "invalid_frontmatter" in card["warnings"]:
            _fail(409, "This card has invalid frontmatter. Fix it with edit_note first.")

        # Validate everything first, so a bad value never leaves half a change.
        status = _clean_status(args["status"], config) if "status" in args else None
        fields = _clean_fields(args["fields"], config) if "fields" in args else {}
        tags = _clean_tags(args["tags"]) if "tags" in args else None
        if "body" in args and not isinstance(args["body"], str):
            _fail(400, "body must be text")

        content = self.read_note(rel_path)
        if content is None:
            _fail(404, "Card not found")
        updated = _with_body(content, args["body"]) if "body" in args else content
        if status is not None and status != card["status"]:
            updated = _set_field(updated, "status", status)
        for field_id, value in fields.items():
            if value != card["fields"].get(field_id, ""):
                updated = _set_field(updated, field_id, value)
        if tags is not None and sorted(tags) != sorted(card["tags"]):
            updated = _set_tags(updated, tags)

        if updated != content:
            self._save(rel_path, updated)
        return _read_card(target, rel_path, config)

    def _free_note_name(self, folder: Path, title: str) -> str:
        """A file name for this title that no note uses yet."""
        clean = re.sub(r'[\\/:*?"<>|#\x00-\x1f]', " ", title)
        clean = re.sub(r"\s+", " ", clean).strip().lstrip("_. ")
        base = clean[:80].strip() or "card"
        notes_dir = str(self.notes_dir)
        for i in range(1, 100):
            name = f"{base}.md" if i == 1 else f"{base} {i}.md"
            rel_path = self._relative(folder / name)
            if not (folder / name).exists() and not note_name_conflict(notes_dir, rel_path):
                return name
        _fail(409, "No free note name for this title")

    def _tool_create_card(self, args: dict) -> dict:
        folder = self._tool_folder(args)
        config = _read_board_config(folder)
        title = _clean_text(args.get("title"), "title")
        if not title:
            _fail(400, "title is required")
        status = _clean_status(args["status"], config) if "status" in args else config["columns"][0]["id"]
        fields = _clean_fields(args.get("fields") or {}, config)
        tags = _clean_tags(args.get("tags") or [])
        body = args.get("body") or ""
        if not isinstance(body, str):
            _fail(400, "body must be text")

        content = f"---\nstatus: {_yaml_value(status)}\n---\n# {title}\n"
        if body:
            content += f"\n{body.rstrip()}\n"
        for field_id, value in fields.items():
            content = _set_field(content, field_id, value)
        if tags:
            content = _set_tags(content, tags)

        target = folder / self._free_note_name(folder, title)
        rel_path = self._relative(target)
        self._save(rel_path, content)
        return _read_card(target, rel_path, _read_board_config(folder))

    def _configured_columns(self, requested, current: dict, cards: list[dict]) -> list[dict]:
        if not isinstance(requested, list) or not requested:
            _fail(400, "columns must be a list with at least one column")
        existing = {column["id"]: column for column in current["columns"]}
        used = set(existing)
        columns = []
        for item in requested:
            if not isinstance(item, dict):
                _fail(400, "Each column must be an object")
            label = _clean_text(item["label"], "label") if "label" in item else None
            column_id = _clean_text(item["id"], "id") if item.get("id") else None
            if column_id is None:
                if not label:
                    _fail(400, "A new column needs an id or a label")
                base = _slug(label) or "column"
                column_id = base
                n = 2
                while column_id in used:
                    column_id = f"{base}-{n}"
                    n += 1
                used.add(column_id)
            if column_id in [column["id"] for column in columns]:
                _fail(400, f"Column {column_id!r} appears twice")
            old = existing.get(column_id, {})
            columns.append({
                "id": column_id,
                "label": old.get("label") if label is None else label,
                "done": bool(item.get("done", old.get("done", False))),
            })

        kept = {column["id"] for column in columns}
        for column_id in existing.keys() - kept:
            count = sum(1 for card in cards if card["status"] == column_id)
            if count:
                _fail(409, f"Column {column_id!r} has {count} card(s). Move them first.")
        return columns

    def _configured_fields(self, requested, current: dict) -> list[dict]:
        if not isinstance(requested, list):
            _fail(400, "fields must be a list")
        existing = {field["id"] for field in current["fields"]}
        fields = []
        for item in requested:
            if not isinstance(item, dict):
                _fail(400, "Each field must be an object")
            raw_id = _clean_text(item.get("id"), "id")
            field_id = raw_id if raw_id in existing else _slug(raw_id)
            if not field_id:
                _fail(400, f"Invalid field id {raw_id!r}")
            if field_id in RESERVED_FIELDS:
                _fail(400, f"A field cannot be named {field_id}")
            if field_id in [field["id"] for field in fields]:
                _fail(400, f"Field {field_id!r} appears twice")
            field_type = item.get("type")
            if field_type not in ("date", "text", "list"):
                _fail(400, f"Field {field_id!r} needs a type: date, text or list")
            values = []
            if field_type == "list":
                for value in item.get("values") or []:
                    value = _clean_text(value, "value")
                    if value and value not in values:
                        values.append(value)
                if not values:
                    _fail(400, f"The list {field_id!r} needs at least one value")
            fields.append({"id": field_id, "type": field_type, "values": values})
        return fields

    def _tool_configure(self, args: dict) -> dict:
        folder = self._tool_folder(args)
        current = _read_board_config(folder)
        if not current["board_file_valid"]:
            _fail(409, "_board.md has invalid YAML. Fix it with edit_note first.")
        if "columns" not in args and "fields" not in args and current["has_board_file"]:
            _fail(400, "Send columns, fields or both")

        cards = self._board_data(folder)["cards"]
        columns = (self._configured_columns(args["columns"], current, cards)
                   if "columns" in args else current["columns"])
        fields = (self._configured_fields(args["fields"], current)
                  if "fields" in args else current["fields"])
        fields = [{"values": [], **field} for field in fields]

        rel_path = self._relative(folder / BOARD_FILE)
        content = (self.read_note(rel_path) or "") if current["has_board_file"] else ""
        comments_removed = _board_has_comments(content)
        self._save(rel_path, _set_board_block(content, _board_block(columns, fields)))

        config = _read_board_config(folder)
        return {
            "columns": config["columns"],
            "fields": config["fields"],
            "comments_removed": comments_removed,
        }

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
.editor-tabs { display: flex; flex-shrink: 0; gap: 4px; margin-bottom: 8px; }
.editor-tab { height: 30px; padding: 0 12px; border: 1px solid var(--border-primary); border-radius: 6px; background: var(--bg-secondary); color: var(--text-secondary); }
.editor-tab[aria-pressed="true"] { border-color: var(--accent-primary); color: var(--text-primary); font-weight: 600; }
.editor-view { flex: 1 0 240px; min-height: 0; padding: 10px 14px; border: 1px solid var(--border-primary); border-radius: 6px; overflow-y: auto; overflow-wrap: anywhere; }
.editor-view.markdown-preview { font-size: 14px; line-height: 1.6; }
.editor-view.markdown-preview h1 { margin-top: 0; font-size: 20px; }
.editor-view.markdown-preview h2 { margin-top: 16px; font-size: 16px; }
.editor-view.markdown-preview h3 { margin-top: 12px; font-size: 14px; }
.editor-empty { color: var(--text-secondary); font-size: 13px; }
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
  .editor-tab { height: 44px; flex: 1; }
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
    view_note: 'View', edit_note: 'Edit', empty_note: 'This note has no text.',
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
    view_note: 'Ver', edit_note: 'Editar', empty_note: 'Esta nota no tiene texto.',
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

// A smaller version of the app's preview. Code stays out of the wikilink step, and the
// sanitized output is what the panel shows. Links open a new tab, so the board stays.
function kanbanRenderMarkdown(body) {
  const code = [];
  const protect = match => {
    code.push(match);
    return '\x00CODE' + (code.length - 1) + '\x00';
  };
  const source = body
    .replace(/```[\s\S]*?```/g, protect)
    .replace(/`[^`]+`/g, protect)
    .replace(/\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]/g, (match, target, label) => {
      const url = '/' + target.trim().replace(/\.md$/, '').split('/').map(encodeURIComponent).join('/');
      const text = (label || target.trim().split('/').pop()).replace(/[\[\]]/g, '');
      return '[' + text + '](' + url + ')';
    })
    .replace(/\x00CODE(\d+)\x00/g, (match, index) => code[index]);

  const holder = document.createElement('div');
  holder.innerHTML = DOMPurify.sanitize(marked.parse(source));
  holder.querySelectorAll('a').forEach(link => {
    link.target = '_blank';
    link.rel = 'noopener';
  });
  return holder.innerHTML;
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
    edit: { mode: 'view', html: '', title: '', url: '', raw: false, loading: false, saving: false, conflict: false, error: '', status: '', tagsText: '', fields: {}, body: '', original: { fields: {} } },

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
      this.edit = { ...this.edit, mode: 'view', title: card.title, url: this.noteUrl(card), loading: true, conflict: false, error: '', body: '' };
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
        // Broken frontmatter has to be fixed in the source, and the markdown libraries
        // could be missing on a broken install.
        mode: raw || typeof marked === 'undefined' ? 'edit' : this.edit.mode,
        html: raw || typeof marked === 'undefined' || this.edit.mode !== 'view' ? '' : kanbanRenderMarkdown(body),
        original: JSON.parse(JSON.stringify(form)),
      };
    },

    canRender() {
      return !this.edit.raw && typeof marked !== 'undefined';
    },

    // Switching saves, so the panel never shows a view that differs from the note. If the
    // save fails, the panel stays where it is, with the text and the error.
    async setEditorMode(mode) {
      if (mode === this.edit.mode || this.edit.saving) return;
      if (this.editorDirty() && !(await this.saveEditor(true))) return;
      if (mode === 'view') this.edit.html = kanbanRenderMarkdown(this.edit.body);
      this.edit.mode = mode;
      if (mode === 'edit') this.$nextTick(() => this.$refs.editorBody?.focus());
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

    // Returns whether the note on disk now matches the form. With keepOpen, the panel
    // stays and the saved note becomes the new starting point.
    async saveEditor(keepOpen = false) {
      if (this.edit.loading || this.edit.saving || this.edit.conflict) return false;
      this.edit.error = '';
      const original = kanbanParseTags(this.edit.original.tagsText || '');
      const badTag = kanbanParseTags(this.edit.tagsText || '').find(tag => !original.includes(tag) && !kanbanValidTag(tag));
      if (badTag && !this.edit.raw) {
        this.edit.error = text.tag_invalid.replace('{name}', badTag);
        return false;
      }
      const content = this.editedContent();
      if (content === this.edit.content) {
        if (!keepOpen) this.$refs.editor.close();
        return true;
      }

      this.edit.saving = true;
      try {
        const response = await fetch(this.noteApiUrl(this.edit.path), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content, base_hash: this.edit.hash }),
        });
        if (response.status === 401) {
          location.href = '/login';
          return false;
        }
        if (response.status === 409) {
          this.edit.conflict = true;
          return false;
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        if (keepOpen) {
          const saved = await response.json();
          const { status, tagsText, fields, body } = this.edit;
          this.edit.hash = saved.hash;
          this.edit.content = content;
          this.edit.head = this.edit.raw ? '' : kanbanSplitNote(content).head;
          this.edit.original = JSON.parse(JSON.stringify({ status, tagsText, fields, body }));
        } else {
          this.$refs.editor.close();
        }
        await this.load();
        if (keepOpen) {
          const card = this.board.cards.find(c => c.path === this.edit.path);
          if (card) this.edit.title = card.title;
        }
        return true;
      } catch (e) {
        console.error('Failed to save the note:', e);
        this.edit.error = text.save_error;
        return false;
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
<link rel="stylesheet" href="/static/markdown-preview.css">
<script src="/static/vendor/marked/marked.min.js"></script>
<script src="/static/vendor/dompurify/purify.min.js"></script>
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
          <template x-if="canRender()">
            <div class="editor-tabs">
              <button class="editor-tab" type="button" :aria-pressed="edit.mode === 'view'" @click="setEditorMode('view')" x-text="text.view_note"></button>
              <button class="editor-tab" type="button" :aria-pressed="edit.mode === 'edit'" @click="setEditorMode('edit')" x-text="text.edit_note"></button>
            </div>
          </template>
          <div class="editor-view markdown-preview" x-show="edit.mode === 'view'">
            <p class="editor-empty" x-show="!edit.body.trim()" x-text="text.empty_note"></p>
            <div x-html="edit.html"></div>
          </div>
          <textarea class="editor-body" x-ref="editorBody" x-show="edit.mode === 'edit'" x-model="edit.body" :aria-label="text.body"></textarea>
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
