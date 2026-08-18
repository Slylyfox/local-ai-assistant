"""Thin client for the Telegram Bot API, used to bridge the assistant to a
Telegram chat from the user's phone.

Uses long-polling (getUpdates) exclusively — the app makes outbound HTTPS
requests to Telegram asking "anything new?" on a loop. No inbound port,
webhook, or public endpoint is ever opened, so nothing needs exposing on the
user's router/firewall.

Access control is a numeric-user-ID allowlist: messages and button taps from
anyone else are silently dropped, without a reply, so the bot doesn't even
confirm to a stranger that it's listening."""

import threading
import time
from typing import Callable, Optional

import requests

API_ROOT = "https://api.telegram.org"
LONG_POLL_TIMEOUT = 25
REQUEST_TIMEOUT = LONG_POLL_TIMEOUT + 10
MAX_MESSAGE_CHARS = 3500


class TelegramBridge:
    def __init__(
        self,
        token: str,
        allowed_user_ids: set,
        on_message: Optional[Callable[[int, str], None]] = None,
        on_callback: Optional[Callable[[dict], None]] = None,
    ):
        self.token = token
        self.allowed_user_ids = allowed_user_ids
        self.on_message = on_message
        self.on_callback = on_callback
        self.api_base = f"{API_ROOT}/bot{token}"
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    # ----------------------------------------------------------------- API

    def get_me(self) -> Optional[dict]:
        try:
            resp = requests.get(f"{self.api_base}/getMe", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("result") if data.get("ok") else None
        except requests.RequestException:
            return None

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> None:
        text = text or "(empty response)"
        chunks = [text[i : i + MAX_MESSAGE_CHARS] for i in range(0, len(text), MAX_MESSAGE_CHARS)] or [text]
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk}
            if reply_markup and i == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            try:
                requests.post(f"{self.api_base}/sendMessage", json=payload, timeout=15)
            except requests.RequestException:
                pass

    def send_photo(self, chat_id: int, filepath: str) -> None:
        try:
            with open(filepath, "rb") as f:
                requests.post(
                    f"{self.api_base}/sendPhoto", data={"chat_id": chat_id}, files={"photo": f}, timeout=30
                )
        except (requests.RequestException, OSError):
            pass

    def answer_callback(self, callback_query_id: str, text: Optional[str] = None) -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            requests.post(f"{self.api_base}/answerCallbackQuery", json=payload, timeout=10)
        except requests.RequestException:
            pass

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            requests.post(
                f"{self.api_base}/editMessageText",
                json={"chat_id": chat_id, "message_id": message_id, "text": text},
                timeout=10,
            )
        except requests.RequestException:
            pass

    # ------------------------------------------------------------- polling

    def _poll_loop(self) -> None:
        offset = None
        while not self._stop_event.is_set():
            try:
                params = {"timeout": LONG_POLL_TIMEOUT}
                if offset is not None:
                    params["offset"] = offset
                resp = requests.get(f"{self.api_base}/getUpdates", params=params, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException:
                time.sleep(3)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                try:
                    self._handle_update(update)
                except Exception:  # noqa: BLE001 - one bad update must not kill polling
                    continue

    def _handle_update(self, update: dict) -> None:
        if "message" in update:
            msg = update["message"]
            user_id = msg.get("from", {}).get("id")
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            if user_id not in self.allowed_user_ids:
                return
            if text and self.on_message:
                self.on_message(chat_id, text)
        elif "callback_query" in update:
            cq = update["callback_query"]
            user_id = cq.get("from", {}).get("id")
            if user_id not in self.allowed_user_ids:
                self.answer_callback(cq.get("id", ""))
                return
            if self.on_callback:
                self.on_callback(cq)
