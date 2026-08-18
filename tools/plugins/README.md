# Plugins

Drop a `.py` file in this folder to add your own tool(s) — it's auto-loaded
the next time the app starts (or when you click "Reload Plugins" in
Settings). No installation, no registry, no network calls involved: this is
purely local files you put here yourself.

## Format

A plugin file needs a top-level `register(registry)` function, exactly like
every built-in tool module (see `tools/dev_tools.py`, `tools/cve_tools.py`,
etc. for real examples):

```python
def my_tool(some_arg: str) -> str:
    """Do something and return a text result."""
    return f"you said: {some_arg}"


def register(registry):
    registry.register(
        "my_tool",                      # name the model calls it by
        "Explain what this tool does.", # shown to the model, keep it clear
        {
            "type": "object",
            "properties": {
                "some_arg": {"type": "string", "description": "What this argument means."},
            },
            "required": ["some_arg"],
        },
        my_tool,
        category="general",             # groups it in Settings' risk-description UI
    )
```

That's it — the function you register becomes callable by the model,
subject to the same "Enable local tool execution" toggle and per-call
confirmation dialog as every other tool.

## Notes

- One file can register multiple tools — just call `registry.register(...)`
  more than once inside `register()`.
- If a plugin file fails to import or raises inside `register()`, it's
  skipped (logged to Task History) rather than crashing the app — but it's
  still your own code, running with the same privileges as everything else
  here. Don't drop in files you haven't read.
- A tool name matching an existing one (built-in or another plugin)
  overwrites it — last one loaded wins.
