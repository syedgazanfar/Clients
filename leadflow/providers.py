"""WhatsApp providers: WA-AKG and WAHA self-hosted gateways, official Cloud API, dry-run."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import Settings

log = logging.getLogger("leadflow.providers")


class SendError(Exception):
    pass


@dataclass
class Inbound:
    phone: str
    text: str
    provider_id: str | None = None


class ConsoleProvider:
    """Dry-run provider: logs messages instead of sending. Used for local testing."""

    name = "console"

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, to: str, text: str, kind: str = "reply", name: str = "") -> str | None:
        self.sent.append({"to": to, "text": text, "kind": kind})
        log.info("[dry-run] to %s (%s): %s", to, kind, text)
        return None


class AkgProvider:
    """WA-AKG (github.com/mrifqidaffaaditya/WA-AKG) self-hosted WhatsApp gateway."""

    name = "akg"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)

    def send(self, to: str, text: str, kind: str = "reply", name: str = "") -> str | None:
        url = (
            f"{self.settings.akg_url}/api/messages/{self.settings.akg_session}"
            f"/{to}@s.whatsapp.net/send"
        )
        try:
            response = self.client.post(
                url,
                json={"message": {"text": text}},
                headers={"X-API-Key": self.settings.akg_api_key},
            )
            response.raise_for_status()
            data = response.json().get("data") or {}
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise SendError(f"WA-AKG send failed: {exc}") from exc
        return (data.get("key") or {}).get("id")

    def register_webhook(self, url: str) -> None:
        """Tell the gateway to deliver incoming messages to our webhook URL."""
        response = self.client.post(
            f"{self.settings.akg_url}/api/webhooks/{self.settings.akg_session}",
            json={
                "name": "Leadflow",
                "url": url,
                "secret": self.settings.akg_webhook_secret,
                "events": ["message.received"],
            },
            headers={"X-API-Key": self.settings.akg_api_key},
        )
        response.raise_for_status()

    @staticmethod
    def parse(payload: dict, session: str = "") -> list[Inbound]:
        if payload.get("event") != "message.received":
            return []
        if session and payload.get("sessionId") != session:
            return []
        data = payload.get("data") or {}
        key = data.get("key") or {}
        text = data.get("content")
        if key.get("fromMe") or data.get("isGroup") or data.get("type") != "TEXT":
            return []
        if not isinstance(text, str) or not text.strip():
            return []
        # The gateway may report a privacy id (@lid) in some fields; use the phone JID.
        candidates = [data.get("from"), key.get("remoteJid"), data.get("remoteJidAlt")]
        sender = next(
            (str(c) for c in candidates if str(c or "").endswith("@s.whatsapp.net")), ""
        )
        if not sender:
            return []
        return [
            Inbound(
                phone=sender.split("@", 1)[0].split(":", 1)[0],
                text=text.strip(),
                provider_id=str(key["id"]) if key.get("id") else None,
            )
        ]


class WahaProvider:
    """WAHA (github.com/devlikeapro/waha) self-hosted WhatsApp HTTP API."""

    name = "waha"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=20)

    def send(self, to: str, text: str, kind: str = "reply", name: str = "") -> str | None:
        headers = {}
        if self.settings.waha_api_key:
            headers["X-Api-Key"] = self.settings.waha_api_key
        body = {
            "session": self.settings.waha_session,
            "chatId": f"{to}@c.us",
            "text": text,
        }
        try:
            response = self.client.post(
                f"{self.settings.waha_url}/api/sendText", json=body, headers=headers
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SendError(f"WAHA send failed: {exc}") from exc
        return None

    @staticmethod
    def parse(payload: dict) -> list[Inbound]:
        if payload.get("event") != "message":
            return []
        data = payload.get("payload") or {}
        sender = str(data.get("from") or "")
        text = data.get("body")
        if data.get("fromMe") or not isinstance(text, str) or not text.strip():
            return []
        # Only direct chats: skip groups, broadcasts, newsletters.
        if not sender.endswith(("@c.us", "@s.whatsapp.net")):
            return []
        message_id = data.get("id")
        if isinstance(message_id, dict):
            message_id = message_id.get("_serialized") or message_id.get("id")
        return [
            Inbound(
                phone=sender.split("@", 1)[0],
                text=text.strip(),
                provider_id=str(message_id) if message_id else None,
            )
        ]


class CloudProvider:
    """Official WhatsApp Business Cloud API (Meta)."""

    name = "cloud"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=20)

    def _template_for(self, kind: str) -> str:
        if kind == "welcome":
            return self.settings.cloud_welcome_template
        if kind == "followup":
            return self.settings.cloud_followup_template
        return ""

    def build_body(self, to: str, text: str, kind: str, name: str) -> dict:
        """Business-initiated messages must use an approved template on the Cloud API."""
        template = self._template_for(kind)
        if template:
            return {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": template,
                    "language": {"code": self.settings.cloud_template_lang},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [{"type": "text", "text": name or "there"}],
                        }
                    ],
                },
            }
        return {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }

    def send(self, to: str, text: str, kind: str = "reply", name: str = "") -> str | None:
        url = (
            f"https://graph.facebook.com/{self.settings.cloud_api_version}"
            f"/{self.settings.cloud_phone_id}/messages"
        )
        try:
            response = self.client.post(
                url,
                json=self.build_body(to, text, kind, name),
                headers={"Authorization": f"Bearer {self.settings.cloud_token}"},
            )
            response.raise_for_status()
            messages = response.json().get("messages") or []
        except (httpx.HTTPError, ValueError) as exc:
            raise SendError(f"Cloud API send failed: {exc}") from exc
        return messages[0].get("id") if messages else None

    @staticmethod
    def parse(payload: dict) -> list[Inbound]:
        inbound: list[Inbound] = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                for message in (change.get("value") or {}).get("messages") or []:
                    if message.get("type") != "text":
                        continue
                    text = (message.get("text") or {}).get("body") or ""
                    if text.strip() and message.get("from"):
                        inbound.append(
                            Inbound(
                                phone=str(message["from"]),
                                text=text.strip(),
                                provider_id=message.get("id"),
                            )
                        )
        return inbound


def build_provider(settings: Settings):
    if settings.whatsapp_provider == "akg":
        return AkgProvider(settings)
    if settings.whatsapp_provider == "waha":
        return WahaProvider(settings)
    if settings.whatsapp_provider == "cloud":
        return CloudProvider(settings)
    if settings.whatsapp_provider == "console":
        return ConsoleProvider()
    raise ValueError(f"Unknown WHATSAPP_PROVIDER: {settings.whatsapp_provider}")
