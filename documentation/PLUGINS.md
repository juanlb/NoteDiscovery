# 🔌 Plugin System

NoteDiscovery includes a powerful plugin system that lets you extend functionality without modifying core code.

## How Plugins Work

Plugins are Python files that live in the `plugins/` directory. They use **event hooks** to react to actions in the app:

### Available Hooks

| Hook | When Triggered | Parameters | Can Modify |
|------|----------------|------------|------------|
| `on_note_create` | New note is created | `note_path`, `initial_content` | ✅ Yes (return modified content) |
| `on_note_save` | Note is being saved | `note_path`, `content` | ✅ Yes (return transformed content, or None) |
| `on_note_load` | Note is loaded from disk | `note_path`, `content` | ✅ Yes (return transformed content, or None) |
| `on_note_delete` | Note is deleted | `note_path` | ❌ No |
| `on_search` | Search is performed | `query`, `results` | ✅ Yes (return a replacement result list, or None) |
| `on_app_startup` | App starts up | None | ❌ No |

Every hook that can modify follows the same rule: **return a new value to replace
the one you were given, or return `None` to leave it alone.** Plugins run in
filename order, each receiving what the previous one left behind. A plugin that
raises is logged and skipped — the value keeps its last good state, so one broken
plugin can't take down a request.

Replacements must be the right type (`str` for content hooks, `list` for
`on_search`). Anything else is logged and ignored rather than passed on.

### Search result ordering

Core search results are re-sorted by path before pagination, so that paging
through them stays reproducible while notes are being edited. If your `on_search`
returns its own list, that sort is skipped and your order is preserved — which
also makes the order your responsibility. Return results in a stable order
(due date, name, relevance score), not one that shifts between calls, or paginated
clients can see duplicates.

## Bundled and Contributed Plugins

`note_stats` ships in `plugins/` and loads at startup. Community plugins live in
`plugins/contrib/`, which the loader ignores — install one by copying it into
`plugins/` and restarting. Each carries its own documentation in its docstring;
see [plugins/contrib/README.md](../plugins/contrib/README.md).

## Creating a Plugin

### 1. Create a Python file

```bash
cd notediscovery/plugins
touch my_plugin.py
```

### 2. Define your plugin class

Every plugin must have a `Plugin` class with:
- `name` - Display name
- `version` - Version string
- `enabled` - Whether it's active (default: `True`)

### 3. Implement event hooks

Add methods for the events you want to handle. Method names are checked at load
time: an `on_*` method that isn't a known hook gets a warning in the log rather
than silently never running.

## Plugin Context

If your plugin defines `setup(ctx)`, it's called once at startup before any hook
fires. Use it instead of trying to work out where things live yourself — the
context always agrees with the running app, including `NOTES_DIR` overrides.

| Field | What it is |
|-------|-----------|
| `ctx.notes_dir` | `Path` to the vault the app is actually serving |
| `ctx.plugins_dir` | `Path` to the plugins directory |
| `ctx.config` | The resolved app config dict |
| `ctx.logger` | Logger named for your plugin, output goes to the server log |

```python
class Plugin:
    def __init__(self):
        self.name = "My Plugin"
        self.version = "1.0.0"
        self.enabled = True

    def setup(self, ctx):
        self.notes_dir = ctx.notes_dir
        self.log = ctx.logger
        self.log.info("ready, watching %s", self.notes_dir)
```

## Plugin Routes

A plugin can serve its own HTTP endpoints by returning an `APIRouter` from
`get_routes()`. Routes are mounted under `/api/plugins/<plugin_id>/` and inherit
the app's authentication.

```python
from fastapi import APIRouter

class Plugin:
    # ... name / version / enabled as above ...

    def get_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/summary")
        async def summary():
            if not self.enabled:
                return {"enabled": False}
            return {"enabled": True, "notes": 42}

        return router
```

That endpoint is then reachable at `/api/plugins/my_plugin/summary`. Routes are
mounted at startup whether or not the plugin is enabled, so check `self.enabled`
inside the handler if it should go quiet when toggled off.

The bundled `note_stats` plugin uses both of these — see `plugins/note_stats.py`
for a working example.

## UI Actions

A plugin can add a link to the folder actions of the app by returning data from
`get_ui()`. Plugins never send JavaScript or HTML to the app: this keeps the
extension point small and means an installed plugin cannot change the page.

```python
class Plugin:
    # ... name / version / enabled as above ...

    def get_ui(self):
        return {
            "folder_actions": [
                {
                    "id": "open_board",
                    "label": {"en": "Open as board", "es": "Abrir como tablero"},
                    "icon": "board",
                    "url": "/api/plugins/my_plugin/board?folder={folder}"
                }
            ]
        }
```

| Field | Meaning |
|-------|---------|
| `id` | Unique name for the action |
| `label` | Text, or a dictionary of language code to text. Falls back to `en` |
| `icon` | `board` or `link`. Any other value shows `link` |
| `url` | Must start with `/api/plugins/<plugin_id>/`. `{folder}` becomes the URL-encoded folder path |

An action that breaks these rules is dropped and logged. The action shows on the
folder cards and rows of the homepage, in the homepage breadcrumb and in the sidebar
folder buttons, and it opens in the same tab.

`GET /api/plugins` returns the actions in a `ui` key, only for enabled plugins. The
app reads them once at startup, so toggling a plugin takes effect on the next reload.

## Basic Example: Note Logger

This simple plugin logs note activity to Docker logs (visible with `docker-compose logs -f`):

```python
"""
Note Logger Plugin
Logs all note operations to Docker logs for monitoring
"""

class Plugin:
    def __init__(self):
        self.name = "Note Logger"
        self.version = "1.0.0"
        self.enabled = True
    
    def on_note_save(self, note_path: str, content: str) -> str | None:
        """Log when a note is saved"""
        word_count = len(content.split())
        print(f"💾 Note saved: {note_path} ({word_count} words)")
        return None  # Don't modify content, just observe
    
    def on_note_delete(self, note_path: str):
        """Log when a note is deleted"""
        print(f"🗑️  Note deleted: {note_path}")
    
    def on_search(self, query: str, results: list) -> list | None:
        """Log search queries"""
        print(f"🔍 Search: '{query}' → {len(results)} results")
        return None  # Don't touch the results, just observe
```

### How to see the logs

```bash
# View logs in real-time
docker-compose logs -f

# View logs for specific service
docker-compose logs -f notediscovery
```

## Activating Your Plugin

1. **Place the file** in `plugins/` directory
2. **Restart the app**: `docker-compose restart`
3. **Plugin auto-loads**: Plugins with `enabled = True` will automatically load

### Enable/Disable Plugins via API

Use the API to toggle plugins on/off:

**Linux/Mac:**
```bash
# Enable a plugin
curl -X POST http://localhost:8000/api/plugins/note_logger/toggle \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Disable a plugin
curl -X POST http://localhost:8000/api/plugins/note_logger/toggle \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

**Windows PowerShell:**
```powershell
# Enable a plugin
curl.exe -X POST http://localhost:8000/api/plugins/note_logger/toggle -H "Content-Type: application/json" -d "{\"enabled\": true}"

# Disable a plugin
curl.exe -X POST http://localhost:8000/api/plugins/note_logger/toggle -H "Content-Type: application/json" -d "{\"enabled\": false}"
```

**List all plugins (all platforms):**
```bash
curl http://localhost:8000/api/plugins
```

## Plugin State Persistence

Plugin states (enabled/disabled) are saved in `plugins/plugin_config.json` and persist between restarts.

---

💡 **Tip:** Use `print()` statements in plugins to log to Docker logs for debugging and monitoring!

