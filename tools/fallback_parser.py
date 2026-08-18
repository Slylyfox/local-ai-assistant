"""Best-effort tool-call detection for models that don't populate Ollama's
structured `message.tool_calls` field and instead emit the call as raw JSON
(optionally wrapped in <tool_call> tags) in the message content, per their
chat template. Common with smaller/uncensored local models."""

import json
import re
from typing import Optional

_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)


def try_parse_fallback_tool_call(text: str, valid_names) -> Optional[list[dict]]:
    """Returns calls shaped like Ollama's native tool_calls
    ([{"function": {"name": ..., "arguments": {...}}}, ...]), or None."""
    text = text.strip()
    if not text:
        return None

    tag_matches = _TOOL_CALL_TAG_RE.findall(text)
    candidates = [m.strip() for m in tag_matches] if tag_matches else [text]

    calls = []
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if name not in valid_names:
                continue
            calls.append({"function": {"name": name, "arguments": item.get("arguments", {})}})

    return calls or None
