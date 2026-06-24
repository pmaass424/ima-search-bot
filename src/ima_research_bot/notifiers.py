from pathlib import Path
from typing import Any
import time

import requests


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_text(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        for chunk in _chunks(text, 3900):
            response = _request_with_retry(
                "post",
                url,
                json={"chat_id": self.chat_id, "text": chunk},
                timeout=30,
            )
            response.raise_for_status()

    def send_audio(self, path: Path, caption: str = "") -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendAudio"
        with path.open("rb") as f:
            response = _request_with_retry(
                "post",
                url,
                data={"chat_id": self.chat_id, "caption": caption[:900]},
                files={"audio": (path.name, f, "audio/mpeg")},
                timeout=120,
                attempts=1,
            )
        response.raise_for_status()

    def get_updates(self, offset: int = 0, timeout: int = 30) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        payload: dict[str, Any] = {"timeout": timeout}
        if offset:
            payload["offset"] = offset
        response = _request_with_retry("get", url, params=payload, timeout=timeout + 10)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            return []
        return list(data.get("result") or [])

    def delete_webhook(self) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/deleteWebhook"
        response = _request_with_retry(
            "post",
            url,
            json={"drop_pending_updates": False},
            timeout=30,
        )
        response.raise_for_status()


class WeComNotifier:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send_text(self, text: str) -> None:
        if not self.enabled:
            return
        for chunk in _chunks(text, 3500):
            response = _request_with_retry(
                "post",
                self.webhook_url,
                json={"msgtype": "markdown", "markdown": {"content": chunk}},
                timeout=30,
            )
            response.raise_for_status()


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _request_with_retry(method: str, url: str, attempts: int = 3, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            last_exc = requests.HTTPError(f"transient HTTP {response.status_code}", response=response)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
        if attempt < attempts:
            time.sleep(2 ** (attempt - 1))
    if last_exc:
        raise last_exc
    raise RuntimeError("request retry exhausted without response")
