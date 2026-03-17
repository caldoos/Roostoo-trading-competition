from __future__ import annotations

from typing import Any

import requests


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.session = requests.Session()

    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        if not self.enabled():
            return
        self.send_to(self.chat_id, text)

    def send_to(self, chat_id: str | int, text: str) -> None:
        if not self.enabled():
            return
        self.session.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=20,
        ).raise_for_status()

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        if not self.enabled():
            return []
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        response = self.session.get(
            f"https://api.telegram.org/bot{self.token}/getUpdates",
            params=params,
            timeout=timeout + 20 if timeout > 0 else 20,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            return []
        result = payload.get("result", [])
        return result if isinstance(result, list) else []
