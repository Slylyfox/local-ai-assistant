"""Vision tools: screen capture that gets attached directly into the
conversation for a vision-capable model to see.

capture_screen() saves the screenshot and stashes its path for the GUI layer
to pick up (via get_last_capture_path()) and attach as an image on the next
message, since tool results are plain text and can't carry image bytes
themselves."""

import os
import time

from config import DATA_DIR

SCREENSHOT_DIR = os.path.join(DATA_DIR, "screenshots")

_last_capture_path: str | None = None


def capture_screen() -> str:
    """Capture the primary monitor and attach it directly into the conversation."""
    global _last_capture_path
    try:
        import mss
        from PIL import Image
    except ImportError as exc:
        return f"Screen capture requires the 'mss' and 'Pillow' packages: {exc}"

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    filename = f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
    filepath = os.path.join(SCREENSHOT_DIR, filename)

    try:
        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            img.save(filepath, "PNG")
    except Exception as exc:  # noqa: BLE001
        return f"Screen capture failed: {exc}"

    _last_capture_path = filepath
    return f"Screenshot captured ({img.width}x{img.height}) and attached to the conversation for you to view."


def get_last_capture_path() -> str | None:
    """Consumes the path set by the most recent capture_screen() call."""
    global _last_capture_path
    path = _last_capture_path
    _last_capture_path = None
    return path


def register(registry):
    registry.register(
        "capture_screen",
        "Capture a screenshot of the primary monitor and attach it directly into the conversation so a "
        "vision-capable model can see it. Requires the active model to support image input.",
        {"type": "object", "properties": {}, "required": []},
        capture_screen,
        category="vision",
    )
