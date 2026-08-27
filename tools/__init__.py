"""Modular local tool-execution framework.

Tools are registered on a shared ToolRegistry, which formats their schema
for Ollama's function-calling API and executes them. Enforcing the "tool
execution enabled" toggle and per-call user confirmation is the GUI layer's
job (main.py) — this package only describes and runs tools once a caller has
already decided to invoke one.
"""

from typing import Optional

from config import load_config as _load_config

from .registry import ToolRegistry
from .fallback_parser import try_parse_fallback_tool_call as _try_parse_fallback_tool_call
from . import dev_tools, recon_tools, security_utils, vision_tools, scheduler_tools
from . import web_search_tools, memory_tools, report_tools, cve_tools, hexstrike_tools
from . import state, task_log, file_ingest, sandbox, plugin_loader

registry = ToolRegistry()
dev_tools.register(registry)
recon_tools.register(registry)
security_utils.register(registry)
vision_tools.register(registry)
scheduler_tools.register(registry)
web_search_tools.register(registry)
memory_tools.register(registry)
report_tools.register(registry)
cve_tools.register(registry)
hexstrike_tools.register(registry)

loaded_plugins: list[dict] = []
if _load_config().plugins_enabled:
    loaded_plugins = plugin_loader.load_plugins(registry)


def reload_plugins() -> list[dict]:
    global loaded_plugins
    loaded_plugins = plugin_loader.load_plugins(registry)
    return loaded_plugins


def shutdown_scheduler() -> None:
    scheduler_tools.shutdown()


def get_tool_schemas() -> list[dict]:
    return registry.get_schemas()


def call_tool(name: str, args: dict) -> str:
    return registry.call(name, args)


def try_parse_fallback_tool_call(text: str) -> Optional[list[dict]]:
    return _try_parse_fallback_tool_call(text, registry.names())


def describe_tools_for_prompt() -> str:
    """Plain-text tool description for models with no native tool-calling
    template (Ollama capability list lacks "tools"). Paired with
    fallback_parser, this lets those models still request tool calls by
    emitting the described JSON shape as their reply content."""
    lines = [
        "You have access to local tools, but most messages do not need one — "
        "reply normally in plain language unless the user's request genuinely "
        "requires reading/writing a file, running code, or a similar local action.",
        "",
        "When (and only when) a tool is actually needed, reply with ONLY a "
        "valid JSON object (no other text before or after it, no markdown "
        "fences) in this exact form:",
        '{"name": "<tool_name>", "arguments": {"<param>": <value>, ...}}',
        "",
        "Available tools:",
    ]
    for spec in registry.specs():
        props = spec.parameters.get("properties", {})
        arg_list = ", ".join(props.keys())
        lines.append(f"- {spec.name}({arg_list}): {spec.description}")
    return "\n".join(lines)
