"""Thin Python client around a locally hosted Ollama API."""

import json
import re
from typing import Callable, Generator, Optional

import requests

DEFAULT_TIMEOUT = 10
STREAM_READ_TIMEOUT = 300
PULL_TIMEOUT = (10, 120)  # (connect, per-read) — pulls can run long but must keep progressing

LIBRARY_SEARCH_URL = "https://ollama.com/search"
LIBRARY_TIMEOUT = 15
_LIBRARY_CARD_SPLIT_RE = re.compile(r'<li\s+class="flex items-baseline border-b border-neutral-200 py-6">')
_LIBRARY_NAME_RE = re.compile(r'<a href="/library/([a-zA-Z0-9_.\-]+)"')
_LIBRARY_DESC_RE = re.compile(r'<p class="max-w-lg break-words text-neutral-800 text-md">(.*?)</p>', re.DOTALL)
_LIBRARY_TAG_RE = re.compile(r'<span\s+class="inline-flex[^"]*">([^<]+)</span>')
_LIBRARY_PULLS_RE = re.compile(r'<span\s*>([\d.]+[KMB]?)</span>\s*<span class="hidden sm:flex">&nbsp;Pulls</span>')


class OllamaError(Exception):
    pass


class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url.rstrip("/")
        self._capabilities_cache: dict[str, list[str]] = {}

    def set_base_url(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._capabilities_cache.clear()

    def get_model_capabilities(self, model: str) -> list[str]:
        if model in self._capabilities_cache:
            return self._capabilities_cache[model]
        try:
            resp = requests.post(
                f"{self.base_url}/api/show", json={"name": model}, timeout=DEFAULT_TIMEOUT
            )
            resp.raise_for_status()
            caps = resp.json().get("capabilities") or []
        except requests.RequestException:
            caps = []
        self._capabilities_cache[model] = caps
        return caps

    def model_supports_vision(self, model: str) -> bool:
        return "vision" in self.get_model_capabilities(model)

    def model_supports_tools(self, model: str) -> bool:
        return "tools" in self.get_model_capabilities(model)

    def check_connection(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=DEFAULT_TIMEOUT)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def list_models(self) -> list[str]:
        resp = requests.get(f"{self.base_url}/api/tags", timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return sorted(m["name"] for m in data.get("models", []))

    def pull_model(
        self,
        name: str,
        on_progress: Optional[Callable[[dict], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Streams a model download. Returns {"ok": True} on success or
        {"ok": False, "error": msg} otherwise — never raises."""
        try:
            with requests.post(
                f"{self.base_url}/api/pull",
                json={"name": name, "stream": True},
                stream=True,
                timeout=PULL_TIMEOUT,
            ) as resp:
                try:
                    resp.raise_for_status()
                except requests.HTTPError as exc:
                    body = resp.text.strip()
                    return {"ok": False, "error": f"{exc} — {body}" if body else str(exc)}
                for line in resp.iter_lines():
                    if should_stop and should_stop():
                        return {"ok": False, "error": "cancelled"}
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        return {"ok": False, "error": chunk["error"]}
                    if on_progress:
                        on_progress(chunk)
                    if chunk.get("status") == "success":
                        self._capabilities_cache.pop(name, None)
                        return {"ok": True}
            return {"ok": False, "error": "stream ended without a success status"}
        except requests.RequestException as exc:
            return {"ok": False, "error": str(exc)}

    def delete_model(self, name: str) -> dict:
        try:
            resp = requests.delete(f"{self.base_url}/api/delete", json={"name": name}, timeout=DEFAULT_TIMEOUT)
            if resp.status_code == 200:
                self._capabilities_cache.pop(name, None)
                return {"ok": True}
            return {"ok": False, "error": resp.text.strip() or f"HTTP {resp.status_code}"}
        except requests.RequestException as exc:
            return {"ok": False, "error": str(exc)}

    def search_library(self, query: str = "") -> list[dict]:
        """Best-effort search of Ollama's public model library (ollama.com) —
        there's no public JSON API for this, so it parses the search page's
        HTML. Returns [] on any failure (network, or ollama.com changing its
        markup) rather than raising, since this is a discovery convenience,
        not core functionality — Add/Remove use the stable native API and
        aren't affected if this ever breaks."""
        try:
            params = {"q": query} if query else None
            resp = requests.get(LIBRARY_SEARCH_URL, params=params, timeout=LIBRARY_TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except requests.RequestException:
            return []

        results = []
        for block in _LIBRARY_CARD_SPLIT_RE.split(html)[1:]:
            name_m = _LIBRARY_NAME_RE.search(block)
            if not name_m:
                continue
            desc_m = _LIBRARY_DESC_RE.search(block)
            desc = re.sub(r"\s+", " ", desc_m.group(1)).strip() if desc_m else ""
            tags = _LIBRARY_TAG_RE.findall(block)
            pulls_m = _LIBRARY_PULLS_RE.search(block)
            results.append(
                {
                    "name": name_m.group(1),
                    "description": desc,
                    "tags": tags,
                    "pulls": pulls_m.group(1) if pulls_m else "?",
                }
            )
        return results

    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.7,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Generator[dict, None, None]:
        """Stream a chat completion.

        Yields dicts of the form:
          {"type": "token", "content": str}
          {"type": "tool_calls", "calls": [...]}
          {"type": "done", "stats": {...}}
          {"type": "error", "message": str}
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if tools:
            payload["tools"] = tools

        try:
            with requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                stream=True,
                timeout=STREAM_READ_TIMEOUT,
            ) as resp:
                try:
                    resp.raise_for_status()
                except requests.HTTPError as exc:
                    body = resp.text.strip()
                    detail = f"{exc} — {body}" if body else str(exc)
                    yield {"type": "error", "message": detail}
                    return
                for line in resp.iter_lines():
                    if should_stop and should_stop():
                        break
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("error"):
                        yield {"type": "error", "message": chunk["error"]}
                        return

                    message = chunk.get("message", {})
                    tool_calls = message.get("tool_calls")
                    content = message.get("content")

                    if tool_calls:
                        yield {"type": "tool_calls", "calls": tool_calls}
                    elif content:
                        yield {"type": "token", "content": content}

                    if chunk.get("done"):
                        yield {
                            "type": "done",
                            "stats": {
                                "total_duration": chunk.get("total_duration"),
                                "eval_count": chunk.get("eval_count"),
                            },
                        }
                        return
        except requests.RequestException as exc:
            yield {"type": "error", "message": str(exc)}
