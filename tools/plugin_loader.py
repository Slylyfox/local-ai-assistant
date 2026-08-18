"""Discovers and loads .py files from tools/plugins/ as tool providers.

Each plugin is loaded via importlib as a standalone module (not a
package-relative import) so one malformed plugin file can't break the
import of the whole `tools` package. A plugin that fails to load is skipped
and reported, never allowed to crash the app."""

import importlib.util
import os

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")


def discover_plugin_files() -> list[str]:
    if not os.path.isdir(PLUGINS_DIR):
        return []
    return sorted(
        f for f in os.listdir(PLUGINS_DIR)
        if f.endswith(".py") and not f.startswith("_")
    )


def load_plugins(registry) -> list[dict]:
    """Returns a list of {"file", "ok", "error", "tool_names_added"} per plugin file."""
    results = []
    for filename in discover_plugin_files():
        filepath = os.path.join(PLUGINS_DIR, filename)
        before = set(registry.names())
        try:
            module_name = f"local_ai_assistant_plugin_{os.path.splitext(filename)[0]}"
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            register_fn = getattr(module, "register", None)
            if register_fn is None:
                results.append(
                    {"file": filename, "ok": False, "error": "no register(registry) function found", "tool_names_added": []}
                )
                continue

            register_fn(registry)
            added = sorted(set(registry.names()) - before)
            results.append({"file": filename, "ok": True, "error": None, "tool_names_added": added})
        except Exception as exc:  # noqa: BLE001 - one bad plugin must never break the app
            results.append({"file": filename, "ok": False, "error": str(exc), "tool_names_added": []})
    return results
