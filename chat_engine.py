"""Shared streaming chat + tool-calling round trip.

Both the desktop GUI worker and the Telegram bridge drive an Ollama
conversation the same way: stream tokens, detect tool calls (native or
fallback-JSON), get them confirmed, execute, feed results back, repeat until
a final plain-text reply. Keeping this in one place means a fix or behavior
change (e.g. the vision-capability guard) automatically applies to every
surface instead of needing to be ported by hand and risking drift.

Callers supply an EngineHooks bundle so this module has zero GUI/Telegram
awareness — it just drives the loop and reports back through callbacks.
"""

import base64
import json
from dataclasses import dataclass
from typing import Callable, Optional

import tools
from ollama_client import OllamaClient


@dataclass
class EngineHooks:
    on_token: Callable[[str], None]
    on_tool_round: Callable[[str, list], None]  # (assistant_text, tool_calls)
    confirm_tool: Callable[[str, dict, str], bool]  # (name, args, category) -> approved
    on_tool_result: Callable[[str, dict, str, Optional[str], Optional[str]], None]
    # (name, args, result, image_path, image_b64)
    on_done: Callable[[str, bool], None]  # (final_text, was_stopped)
    on_error: Callable[[str], None]
    should_stop: Callable[[], bool] = lambda: False


def run_chat_round_trip(
    client: OllamaClient,
    model: str,
    working_messages: list[dict],
    temperature: float,
    tools_enabled: bool,
    hooks: EngineHooks,
) -> None:
    """Mutates working_messages in place as the conversation progresses."""
    tool_schemas = None
    if tools_enabled:
        if client.model_supports_tools(model):
            tool_schemas = tools.get_tool_schemas()
        else:
            # Ollama 400s any request carrying `tools` if the model wasn't built
            # with tool-calling support. Fall back to describing the tools in
            # plain text; try_parse_fallback_tool_call() picks up the JSON reply
            # this prompts for.
            if working_messages and working_messages[0].get("role") == "system":
                working_messages[0] = dict(working_messages[0])
                working_messages[0]["content"] = (
                    working_messages[0]["content"] + "\n\n" + tools.describe_tools_for_prompt()
                )

    try:
        while True:
            round_tool_calls = None
            round_text = ""

            for event in client.chat_stream(
                model,
                working_messages,
                tools=tool_schemas,
                temperature=temperature,
                should_stop=hooks.should_stop,
            ):
                if hooks.should_stop():
                    break
                etype = event["type"]
                if etype == "token":
                    round_text += event["content"]
                    hooks.on_token(event["content"])
                elif etype == "tool_calls":
                    round_tool_calls = event["calls"]
                elif etype == "error":
                    hooks.on_error(event["message"])
                    return

            if hooks.should_stop():
                hooks.on_done(round_text, True)
                return

            if not round_tool_calls and tools_enabled and round_text.strip():
                round_tool_calls = tools.try_parse_fallback_tool_call(round_text)

            if round_tool_calls:
                working_messages.append(
                    {"role": "assistant", "content": round_text, "tool_calls": round_tool_calls}
                )
                hooks.on_tool_round(round_text, round_tool_calls)

                for call in round_tool_calls:
                    fn = call.get("function", {}) or {}
                    fname = fn.get("name", "")
                    raw_args = fn.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            fargs = json.loads(raw_args)
                        except json.JSONDecodeError:
                            fargs = {}
                    else:
                        fargs = raw_args or {}

                    if tools_enabled:
                        category = tools.registry.category_of(fname)
                        approved = hooks.confirm_tool(fname, fargs, category)
                        result = tools.call_tool(fname, fargs) if approved else "Tool execution was denied by the user."
                    else:
                        result = "Tool execution is disabled in Settings. It cannot run right now."

                    image_b64 = None
                    image_path = None
                    if fname == "capture_screen":
                        image_path = tools.vision_tools.get_last_capture_path()
                        if image_path and client.model_supports_vision(model):
                            try:
                                with open(image_path, "rb") as f:
                                    image_b64 = base64.b64encode(f.read()).decode()
                            except OSError:
                                image_b64 = None
                        elif image_path:
                            result += (
                                f"\n\nNote: '{model}' doesn't support image input, so the screenshot "
                                "could only be saved, not shown to the model. Switch to a vision-capable "
                                "model (e.g. llava, qwen2.5vl, llama3.2-vision) to let it see screenshots."
                            )

                    working_messages.append({"role": "tool", "content": result})
                    if image_b64:
                        working_messages.append(
                            {
                                "role": "user",
                                "content": "[Screenshot attached above for you to analyze]",
                                "images": [image_b64],
                            }
                        )

                    hooks.on_tool_result(fname, fargs, result, image_path, image_b64)
                continue

            hooks.on_done(round_text, False)
            return
    except Exception as exc:  # noqa: BLE001 - surface unexpected failures to the caller
        hooks.on_error(str(exc))
