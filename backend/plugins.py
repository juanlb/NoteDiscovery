"""
Simple plugin system for NoteDiscovery
Plugins can hook into events like note save, delete, etc.

Each hook declares its calling convention once, in HOOK_SPECS. A hook either
observes (returns nothing useful) or owns a *subject* — the one argument a
plugin may replace by returning a new value. Plugins that return None leave the
subject as-is, so observing and transforming plugins can coexist on one hook.

Plugins may also expose:
  - setup(ctx)      receive a PluginContext (notes_dir, config, logger)
  - get_routes()    return an APIRouter mounted at /api/plugins/<plugin_id>
  - get_ui()        declare UI actions the app renders, as data rather than code
  - get_mcp_tools() declare MCP tools, served by the plugin's own routes
"""

import json
import logging
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Dict, Optional

from fastapi import APIRouter

from .utils import get_note_content, save_note

logger = logging.getLogger("uvicorn.error")

# Where the app can show a folder action.
UI_ACTION_PLACES = {"homepage", "breadcrumb", "sidebar"}


@dataclass(frozen=True)
class HookSpec:
    """
    Calling convention for one hook.

    subject: name of the argument a plugin may replace by returning a new
             value. None marks the hook as observe-only — returns are ignored
             and dispatch() yields None.
    subject_type: what a replacement must be. A plugin returning anything else
             is rejected and logged, so a malformed return can't reach the
             response and turn into a 500 further down.
    """
    subject: Optional[str] = None
    subject_type: Optional[type] = None


# The complete set of hooks the app dispatches. Anything not listed here is not
# a hook: load_plugins() warns about `on_*` methods that match no entry, which
# catches typos that would otherwise just never fire.
HOOK_SPECS: Dict[str, HookSpec] = {
    "on_app_startup": HookSpec(),
    "on_note_create": HookSpec(subject="initial_content", subject_type=str),
    "on_note_save": HookSpec(subject="content", subject_type=str),
    "on_note_load": HookSpec(subject="content", subject_type=str),
    "on_note_delete": HookSpec(),
    "on_search": HookSpec(subject="results", subject_type=list),
}


@dataclass
class PluginContext:
    """Host facilities handed to a plugin's setup(). Without this a plugin has
    to re-derive things like the vault location from config.yaml itself, which
    silently drifts whenever the app's own resolution changes."""
    notes_dir: Path
    plugins_dir: Path
    config: Dict[str, Any]
    logger: logging.Logger
    # Same paths as the notes API, so hooks run and the index stays current. Writing
    # the file directly skips both, which breaks plugins that transform content.
    read_note: Optional[Callable[[str], Optional[str]]] = None
    write_note: Optional[Callable[[str, str], bool]] = None


class Plugin:
    """Base plugin class"""

    def __init__(self):
        self.name = "Base Plugin"
        self.version = "1.0.0"
        self.enabled = False

    def setup(self, ctx: PluginContext):
        """
        Called once at load time, before any hook fires.

        Args:
            ctx: Vault path, resolved config and a per-plugin logger
        """
        pass

    def get_routes(self) -> APIRouter | None:
        """
        Return an APIRouter to be mounted at /api/plugins/<plugin_id>, or None.

        Routes are mounted at startup regardless of enabled state, so check
        self.enabled inside the handler if the endpoint should go quiet when
        the plugin is toggled off.
        """
        return None

    def get_ui(self) -> Dict | None:
        """
        Declare UI actions for the app to render, or None.

        Plugins hand over data, never code: the app only ever shows a link to one
        of the plugin's own routes. See documentation/PLUGINS.md.
        """
        return None

    def get_mcp_tools(self) -> List[Dict] | None:
        """
        Declare MCP tools, or None.

        The MCP server sends each call to POST /api/plugins/<plugin_id>/mcp/<tool_name>,
        so the plugin serves it from get_routes(). Tool names must start with
        "<plugin_id>_". See documentation/PLUGINS.md.
        """
        return None

    def on_note_save(self, note_path: str, content: str) -> str | None:
        """
        Called when a note is being saved.
        Can optionally transform content before writing to disk (e.g., encrypt).

        Args:
            note_path: Path to the note being saved
            content: Content to be saved

        Returns:
            Transformed content, or None to keep original
        """
        return None

    def on_note_delete(self, note_path: str):
        """Called when a note is deleted"""
        pass

    def on_search(self, query: str, results: List[Dict]) -> List[Dict] | None:
        """
        Called after a search is performed.
        Can optionally replace the result set before it is paginated.

        Args:
            query: The search string as typed
            results: Results from the core search

        Returns:
            A replacement result list, or None to keep the originals
        """
        return None

    def on_note_create(self, note_path: str, initial_content: str) -> str:
        """
        Called when a new note is created (before first save).
        Can modify and return the initial content.

        Args:
            note_path: Path to the new note
            initial_content: The initial content for the note

        Returns:
            Modified content (or return initial_content unchanged)
        """
        return initial_content

    def on_note_load(self, note_path: str, content: str) -> str | None:
        """
        Called when a note is loaded from disk.
        Can optionally transform content before displaying (e.g., decrypt).

        Args:
            note_path: Path to the loaded note
            content: Content loaded from disk

        Returns:
            Transformed content, or None to keep original
        """
        return None

    def on_app_startup(self):
        """
        Called when the application starts up.
        Useful for initialization, sync, health checks, etc.
        """
        pass


class PluginManager:
    """Manages loading and execution of plugins"""

    def __init__(self, plugins_dir: str, notes_dir: str | None = None, config: Dict | None = None):
        self.plugins_dir = Path(plugins_dir)
        self.notes_dir = Path(notes_dir) if notes_dir else None
        self.app_config = config or {}
        self.plugins: Dict[str, Plugin] = {}
        self.config_file = self.plugins_dir / "plugin_config.json"
        self.load_plugins()
        self._apply_saved_state()
        # Save config to create/update the file with current states
        if self.plugins:  # Only save if there are plugins loaded
            self._save_config()

    def load_plugins(self):
        """Load all plugins from the plugins directory"""
        if not self.plugins_dir.exists():
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            self._create_example_plugin()
            return

        # Sorted because hooks chain: dispatch order decides which plugin's
        # transform wins. glob() order is filesystem-dependent, so without this
        # the same plugins could behave differently on NTFS and ext4.
        for plugin_file in sorted(self.plugins_dir.glob("*.py")):
            if plugin_file.stem.startswith("_"):
                continue

            try:
                spec = importlib.util.spec_from_file_location(
                    plugin_file.stem, plugin_file
                )
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # Look for Plugin class in module
                    if hasattr(module, 'Plugin'):
                        plugin = module.Plugin()
                        # Registered only after both succeed, so a plugin is
                        # never half-loaded (present but never given context).
                        self._warn_on_unknown_hooks(plugin_file.stem, plugin)
                        self._setup_plugin(plugin_file.stem, plugin)
                        self.plugins[plugin_file.stem] = plugin
            except Exception as e:
                logger.error("Failed to load plugin %s: %s", plugin_file.stem, e)

    def _warn_on_unknown_hooks(self, plugin_id: str, plugin: Plugin):
        """Flag `on_*` methods that match no hook — almost always a typo, and
        otherwise silent because dispatch only ever looks up known names."""
        try:
            attrs = [a for a in dir(plugin) if a.startswith("on_") and a not in HOOK_SPECS]
        except Exception:
            return
        for attr in attrs:
            if callable(getattr(plugin, attr, None)):
                logger.warning(
                    "Plugin '%s' defines '%s', which is not a known hook and will never run",
                    plugin_id, attr,
                )

    def _setup_plugin(self, plugin_id: str, plugin: Plugin):
        """Hand the plugin its context, if it wants one."""
        setup = getattr(plugin, "setup", None)
        if not callable(setup):
            return
        try:
            setup(PluginContext(
                notes_dir=self.notes_dir or Path("."),
                plugins_dir=self.plugins_dir,
                config=self.app_config,
                # Child of uvicorn.error so plugin output lands in the server
                # log but is attributable to the plugin that produced it.
                logger=logging.getLogger(f"uvicorn.error.plugins.{plugin_id}"),
                read_note=self.read_note,
                write_note=self.write_note,
            ))
        except Exception as e:
            logger.error("Plugin %s failed during setup: %s", plugin_id, e)

    def build_router(self) -> APIRouter:
        """Collect plugin-owned routes into one router for the app to mount.

        Each plugin's routes land under /api/plugins/<plugin_id>/, which is what
        lets a plugin expose an endpoint without core code knowing it exists.
        """
        router = APIRouter()

        for plugin_id, plugin in self.plugins.items():
            get_routes = getattr(plugin, "get_routes", None)
            if not callable(get_routes):
                continue
            try:
                sub_router = get_routes()
            except Exception as e:
                logger.error("Plugin %s failed to provide routes: %s", plugin_id, e)
                continue
            if sub_router is None:
                continue
            if not isinstance(sub_router, APIRouter):
                logger.error(
                    "Plugin %s returned %s from get_routes(), expected APIRouter",
                    plugin_id, type(sub_router).__name__,
                )
                continue

            router.include_router(sub_router, prefix=f"/plugins/{plugin_id}", tags=["Plugins"])
            logger.info("Plugin '%s': mounted routes at /api/plugins/%s", plugin_id, plugin_id)

        return router

    def _create_example_plugin(self):
        """Create an example plugin to show developers how to build plugins"""
        example_plugin = '''"""
Example Plugin for NoteDiscovery
This plugin demonstrates how to create custom plugins.
"""

class Plugin:
    def __init__(self):
        self.name = "Example Plugin"
        self.version = "1.0.0"
        self.enabled = True

    def setup(self, ctx):
        """Runs once at startup. ctx carries notes_dir, config and a logger."""
        self.notes_dir = ctx.notes_dir
        self.log = ctx.logger

    def on_note_save(self, note_path: str, content: str):
        """This runs every time a note is saved"""
        print(f"✓ Plugin: Note saved - {note_path}")

        # Example: Automatically add tags to notes
        # if '#todo' in content:
        #     print("  → Found TODO tag!")

    def on_note_delete(self, note_path: str):
        """This runs when a note is deleted"""
        print(f"✗ Plugin: Note deleted - {note_path}")

    def on_search(self, query: str, results: list):
        """This runs after a search is performed.

        Return a list to replace the results, or None to leave them alone.
        """
        print(f"🔍 Plugin: Search performed for '{query}' ({len(results)} results)")
        return None
'''
        example_path = self.plugins_dir / "example_plugin.py"
        with open(example_path, 'w', encoding='utf-8') as f:
            f.write(example_plugin)

    def list_plugins(self) -> List[Dict]:
        """Get a list of all loaded plugins"""
        plugins = []
        for plugin_id, plugin in self.plugins.items():
            entry = {
                "id": plugin_id,
                "name": plugin.name,
                "version": plugin.version,
                "enabled": plugin.enabled
            }
            ui = self._plugin_ui(plugin_id, plugin) if plugin.enabled else None
            if ui:
                entry["ui"] = ui
            mcp_tools = self._plugin_mcp_tools(plugin_id, plugin) if plugin.enabled else None
            if mcp_tools:
                entry["mcp_tools"] = mcp_tools
            plugins.append(entry)
        return plugins

    def _plugin_ui(self, plugin_id: str, plugin: Plugin) -> Dict | None:
        """The plugin's valid UI actions, or None when it declares none."""
        get_ui = getattr(plugin, "get_ui", None)
        if not callable(get_ui):
            return None
        try:
            declared = get_ui()
            actions = declared.get("folder_actions", []) if declared else []
            valid = [a for a in actions if self._valid_ui_action(plugin_id, a)]
        except Exception as e:
            logger.error("Plugin %s failed to provide UI actions: %s", plugin_id, e)
            return None
        return {"folder_actions": valid} if valid else None

    def _valid_ui_action(self, plugin_id: str, action: Any) -> bool:
        """An action may only link to its own plugin's routes, so a plugin cannot
        turn an app button into a link to another site or to a core endpoint."""
        url = action.get("url") if isinstance(action, dict) else None
        prefix = f"/api/plugins/{plugin_id}/"
        valid = (
            isinstance(url, str)
            and url.startswith(prefix)
            # Browsers normalize %2e%2e, so any escape could hide a way out of the prefix.
            and not any(c in url for c in ("..", "\\", "%"))
            and isinstance(action.get("id"), str)
            and self._valid_ui_label(action.get("label"))
            and self._valid_ui_file(action.get("when_file"))
            and self._valid_ui_file(action.get("unless_file"))
            and self._valid_ui_places(action.get("places"))
        )
        if not valid:
            logger.warning("Plugin '%s' declared an invalid UI action, ignoring it: %r", plugin_id, action)
        return valid

    @staticmethod
    def _valid_ui_label(label: Any) -> bool:
        if isinstance(label, dict):
            return all(isinstance(text, str) for text in label.values())
        return isinstance(label, str)

    @staticmethod
    def _valid_ui_file(name: Any) -> bool:
        """A file directly inside the folder, or None when the action sets no condition."""
        return name is None or (isinstance(name, str) and name != "" and "/" not in name and "\\" not in name)

    @staticmethod
    def _valid_ui_places(places: Any) -> bool:
        return places is None or (isinstance(places, list) and set(places) <= UI_ACTION_PLACES)

    def _plugin_mcp_tools(self, plugin_id: str, plugin: Plugin) -> List[Dict]:
        """The plugin's valid MCP tool definitions."""
        get_tools = getattr(plugin, "get_mcp_tools", None)
        if not callable(get_tools):
            return []
        try:
            tools = get_tools() or []
            valid = [t for t in tools if self._valid_mcp_tool(plugin_id, t)]
        except Exception as e:
            logger.error("Plugin %s failed to provide MCP tools: %s", plugin_id, e)
            return []
        return [
            {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
            for t in valid
        ]

    def _valid_mcp_tool(self, plugin_id: str, tool: Any) -> bool:
        """The name prefix keeps a plugin from shadowing another plugin's tools."""
        valid = (
            isinstance(tool, dict)
            and isinstance(tool.get("name"), str)
            and re.fullmatch(re.escape(plugin_id) + r"_[a-z0-9_]+", tool["name"]) is not None
            and isinstance(tool.get("description"), str)
            and isinstance(tool.get("inputSchema"), dict)
        )
        if not valid:
            logger.warning("Plugin '%s' declared an invalid MCP tool, ignoring it: %r", plugin_id, tool)
        return valid

    def read_note(self, note_path: str) -> Optional[str]:
        """A note's content as GET /api/notes returns it, or None if it does not exist."""
        content = get_note_content(str(self.notes_dir), note_path)
        if content is None:
            return None
        return self.dispatch("on_note_load", note_path=note_path, content=content)

    def write_note(self, note_path: str, content: str) -> bool:
        """Create or replace a note with the same hooks as POST /api/notes."""
        notes_dir = str(self.notes_dir)
        if get_note_content(notes_dir, note_path) is None:
            content = self.dispatch("on_note_create", note_path=note_path, initial_content=content)
        content = self.dispatch("on_note_save", note_path=note_path, content=content)
        return save_note(notes_dir, note_path, content)

    def _load_config(self) -> Dict[str, bool]:
        """Load plugin configuration from JSON file"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error("Failed to load plugin config: %s", e)
        return {}

    def _save_config(self):
        """Save current plugin states to JSON file"""
        try:
            config = {
                plugin_id: plugin.enabled
                for plugin_id, plugin in self.plugins.items()
            }
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            logger.error("Failed to save plugin config: %s", e)

    def _apply_saved_state(self):
        """Apply saved plugin states after loading plugins"""
        saved_config = self._load_config()
        for plugin_id, enabled in saved_config.items():
            if plugin_id in self.plugins:
                self.plugins[plugin_id].enabled = enabled
                logger.info("Plugin '%s': %s (from config)", plugin_id, 'enabled' if enabled else 'disabled')

    def enable_plugin(self, plugin_id: str):
        """Enable a plugin and persist the state"""
        if plugin_id in self.plugins:
            self.plugins[plugin_id].enabled = True
            self._save_config()

    def disable_plugin(self, plugin_id: str):
        """Disable a plugin and persist the state"""
        if plugin_id in self.plugins:
            self.plugins[plugin_id].enabled = False
            self._save_config()

    def dispatch(self, hook_name: str, **kwargs):
        """
        Run a hook on all enabled plugins, in load order.

        For hooks with a subject (see HOOK_SPECS), each plugin receives the
        value left by the previous one and may return a replacement; returning
        None keeps the current value. The final value is returned to the caller.

        For observe-only hooks, returns are ignored and None comes back.

        A plugin that raises is logged and skipped: the subject keeps the last
        good value, so one broken plugin cannot take down the request.

        Args:
            hook_name: Name of the hook to run, must be a key of HOOK_SPECS
            **kwargs: Arguments to pass to the hook

        Returns:
            The final subject value, or None for observe-only hooks
        """
        spec = HOOK_SPECS.get(hook_name)
        if spec is None:
            logger.error("Unknown hook '%s' - not dispatched", hook_name)
            return None

        subject = spec.subject
        value = kwargs.get(subject) if subject else None

        for plugin_id, plugin in self.plugins.items():
            if not plugin.enabled:
                continue
            method = getattr(plugin, hook_name, None)
            if not callable(method):
                continue

            try:
                if subject:
                    returned = method(**{**kwargs, subject: value})
                    if returned is None:
                        continue
                    if spec.subject_type and not isinstance(returned, spec.subject_type):
                        logger.error(
                            "Plugin %s returned %s from %s, expected %s - ignoring",
                            plugin.name, type(returned).__name__, hook_name,
                            spec.subject_type.__name__,
                        )
                        continue
                    value = returned
                else:
                    method(**kwargs)
            except Exception as e:
                logger.error("Plugin %s error in %s: %s", plugin.name, hook_name, e)

        return value

    def run_hook(self, hook_name: str, **kwargs):
        """Deprecated: use dispatch(). Kept so out-of-tree callers keep working.

        Preserves the old signature exactly, including only returning a value
        when 'content' was passed.
        """
        result = self.dispatch(hook_name, **kwargs)
        return result if 'content' in kwargs else None

    def run_hook_with_return(self, hook_name: str, **kwargs):
        """Deprecated: use dispatch()."""
        return self.dispatch(hook_name, **kwargs)
