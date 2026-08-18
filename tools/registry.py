"""Central registry that tracks tools, formats their schema for Ollama's
function-calling API, and executes them safely.

Enforcing the "tool execution enabled" toggle and per-call user confirmation
is the GUI layer's job (main.py) — this registry only knows how to describe
and run tools once a caller has already decided to invoke one.
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    func: Callable
    category: str = "general"


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        func: Callable,
        category: str = "general",
    ) -> None:
        self._tools[name] = ToolSpec(
            name=name, description=description, parameters=parameters, func=func, category=category
        )

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def category_of(self, name: str) -> str:
        spec = self._tools.get(name)
        return spec.category if spec else "general"

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def get_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    def call(self, name: str, args: dict) -> str:
        if name not in self._tools:
            return f"Unknown tool: {name}"
        spec = self._tools[name]
        try:
            return spec.func(**(args or {}))
        except TypeError as exc:
            return f"Invalid arguments for tool '{name}': {exc}"
        except Exception as exc:  # noqa: BLE001 - surface any tool failure to the model
            return f"Tool '{name}' raised an error: {exc}"
