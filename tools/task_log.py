"""Durable log for background task output (scheduled tasks, directory
watches). Kept separate from the live chat stream per design: automation
output would otherwise clutter an active conversation, so it's routed here
and surfaced in the GUI's Task History tab instead."""

import os
import threading
import time
from typing import Callable

from config import DATA_DIR

LOG_PATH = os.path.join(DATA_DIR, "task_history.log")

_lock = threading.Lock()
_listeners: list[Callable[[str], None]] = []


def log_event(message: str) -> str:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    with _lock:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    for listener in list(_listeners):
        try:
            listener(line)
        except Exception:
            pass
    return line


def read_recent(limit: int = 300) -> list[str]:
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return [line.rstrip("\n") for line in lines[-limit:]]


def clear() -> None:
    with _lock:
        if os.path.exists(LOG_PATH):
            os.remove(LOG_PATH)


def add_listener(callback: Callable[[str], None]) -> None:
    _listeners.append(callback)
