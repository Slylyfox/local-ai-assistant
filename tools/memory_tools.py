"""Persistent memory that survives across sessions and app restarts —
distinct from a session's conversation history, which only lasts as long as
that session. Useful for carrying facts across an ongoing engagement
(targets, credentials found, findings-so-far) without re-explaining context
every time a new chat is started."""

import db

MAX_VALUE_CHARS = 4000


def remember(key: str, value: str) -> str:
    """Store or update a fact under a short key for later recall."""
    value = value[:MAX_VALUE_CHARS]
    db.set_memory(key, value)
    return f"Remembered '{key}'."


def recall(query: str) -> str:
    """Search stored memories by key or value substring."""
    rows = db.search_memories(query)
    if not rows:
        return f"No memories match '{query}'."
    lines = [f"- {r['key']}: {r['value']}" for r in rows]
    return "\n".join(lines)


def forget(key: str) -> str:
    """Delete a stored memory by exact key."""
    if db.delete_memory(key):
        return f"Forgot '{key}'."
    return f"No memory found with key '{key}'."


def list_memories() -> str:
    """List every stored memory."""
    rows = db.list_memories()
    if not rows:
        return "No memories stored yet."
    return "\n".join(f"- {r['key']}: {r['value']}" for r in rows)


def register(registry):
    registry.register(
        "remember",
        "Store or update a fact in persistent memory under a short key, so it can be "
        "recalled in future sessions.",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short identifier for this fact, e.g. 'target_ip'."},
                "value": {"type": "string", "description": "The fact to remember."},
            },
            "required": ["key", "value"],
        },
        remember,
        category="memory",
    )
    registry.register(
        "recall",
        "Search persistent memory by key or value substring.",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Text to search for."}},
            "required": ["query"],
        },
        recall,
        category="memory",
    )
    registry.register(
        "forget",
        "Delete a stored memory by its exact key.",
        {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Exact key of the memory to delete."}},
            "required": ["key"],
        },
        forget,
        category="memory",
    )
    registry.register(
        "list_memories",
        "List every fact currently stored in persistent memory.",
        {"type": "object", "properties": {}, "required": []},
        list_memories,
        category="memory",
    )
