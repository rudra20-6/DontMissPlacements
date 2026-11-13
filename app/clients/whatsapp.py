"""WhatsApp Cloud API client (Meta Graph API)."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

# Graph error code returned when a free-form message is sent outside the 24h customer service window.
REENGAGEMENT_ERROR = 131047


class WhatsAppError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class WhatsAppClient:
    def __init__(self):
        s = get_settings()
        self.s = s
        self.url = f"https://graph.facebook.com/{s.whatsapp_api_version}/{s.whatsapp_phone_number_id}/messages"

    @property
    def configured(self) -> bool:
        return bool(self.s.whatsapp_token and self.s.whatsapp_phone_number_id and self.s.whatsapp_recipient)

    def _send(self, message: dict[str, Any]) -> dict:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": self.s.whatsapp_recipient}
        body.update(message)
        if self.s.whatsapp_dry_run or not self.configured:
            log.info("[WhatsApp dry-run] %s", body)
            return {"dry_run": True}
        resp = httpx.post(
            self.url,
            json=body,
            headers={"Authorization": f"Bearer {self.s.whatsapp_token}"},
            timeout=30,
        )
        if resp.status_code >= 400:
            code = None
            try:
                code = resp.json().get("error", {}).get("code")
            except ValueError:
                pass
            raise WhatsAppError(f"WhatsApp HTTP {resp.status_code}: {resp.text[:400]}", code=code)
        return resp.json()

    def send_text(self, text: str) -> dict:
        return self._send({"type": "text", "text": {"body": text[:4096], "preview_url": True}})

    def send_buttons(self, text: str, buttons: list[tuple[str, str]]) -> dict:
        """buttons: list of (id, title). Max 3 buttons, title max 20 chars, body max 1024 chars."""
        first: dict = {}
        if len(text) > 1024:
            # Interactive bodies are limited; send the long part as text first.
            first = self.send_text(text)
            text = "Choose an option:"
        resp = self._send(
            {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": text},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": bid[:256], "title": title[:20]}}
                            for bid, title in buttons[:3]
                        ]
                    },
                },
            }
        )
        return {**resp, "messages": (first.get("messages") or []) + (resp.get("messages") or [])}

    def download_media(self, media_id: str) -> tuple[str, bytes]:
        """An image/PDF the user sent -> (mime type, bytes). Two calls: media id -> temporary URL -> file."""
        headers = {"Authorization": f"Bearer {self.s.whatsapp_token}"}
        meta = httpx.get(f"https://graph.facebook.com/{self.s.whatsapp_api_version}/{media_id}", headers=headers,
                         timeout=30)
        if meta.status_code >= 400:
            raise WhatsAppError(f"media lookup HTTP {meta.status_code}: {meta.text[:300]}")
        info = meta.json()
        resp = httpx.get(info["url"], headers=headers, timeout=60, follow_redirects=True)
        if resp.status_code >= 400:
            raise WhatsAppError(f"media download HTTP {resp.status_code}")
        return info.get("mime_type", ""), resp.content

    def send_template(self, param: str | None = None) -> dict:
        template: dict[str, Any] = {
            "name": self.s.whatsapp_template_name,
            "language": {"code": self.s.whatsapp_template_lang},
        }
        if self.s.whatsapp_template_has_param and param:
            template["components"] = [{"type": "body", "parameters": [{"type": "text", "text": param[:1000]}]}]
        return self._send({"type": "template", "template": template})

    def verify_signature(self, raw_body: bytes, header: str | None) -> bool:
        if not self.s.whatsapp_app_secret:
            return True
        if not header or not header.startswith("sha256="):
            return False
        expected = hmac.new(self.s.whatsapp_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header.split("=", 1)[1])
