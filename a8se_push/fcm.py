"""Montagem da mensagem (SPEC §4.2) e envio ao FCM respeitando o modo (SPEC §9).

`build_message` é pura e devolve o dicionário no formato exato da SPEC. Os
senders convertem esse dicionário para o Admin SDK só na hora de enviar.

Modos:
- dry-run: `DryRunSender`, que nunca importa nem chama o firebase_admin;
- test:    `FcmSender` com trava: recusa qualquer condição diferente de
           "'news_teste' in topics";
- production: `FcmSender` sem a trava.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from .feed import FeedItem, topic_safe

TOPIC_PREFIX = "news_"
URGENT_TOPIC = "news_urgente"
TEST_TOPIC = "news_teste"
TEST_CONDITION = f"'{TEST_TOPIC}' in topics"
ANDROID_CHANNEL = "news"
BODY_MAX_CHARS = 160
ELLIPSIS = "…"

TEMPORARY = "temporary"
PERMANENT = "permanent"


def category_topic(slug: str) -> str:
    return topic_safe(TOPIC_PREFIX + slug)


def condition_for(slug: str, *, urgent: bool, mode: str) -> str:
    if mode != "production":
        return TEST_CONDITION  # test e dry-run nunca miram tópicos reais
    topic = category_topic(slug)
    if urgent:
        return f"'{topic}' in topics || '{URGENT_TOPIC}' in topics"
    return f"'{topic}' in topics"


def truncate(text: str, limit: int = BODY_MAX_CHARS) -> str:
    """Corta em limite de palavra e acrescenta '…', sem passar de `limit` caracteres."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - len(ELLIPSIS)]
    if " " in cut and not text[len(cut)].isspace():
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.-–—") + ELLIPSIS


def build_message(
    item: FeedItem, slug: str, *, urgent: bool, mode: str, now: datetime, ttl_hours: int
) -> dict:
    topic = category_topic(slug)
    notification = {"title": item.title}
    body = truncate(item.description)
    if body:
        notification["body"] = body
    if item.image:
        notification["image"] = item.image
    ttl = timedelta(hours=ttl_hours)
    return {
        "condition": condition_for(slug, urgent=urgent, mode=mode),
        "notification": notification,
        "data": {
            "type": "news",
            "guid": str(item.guid),
            "url": str(item.url),
            "category": str(slug),
        },
        "android": {
            "priority": "high",
            "ttl": f"{int(ttl.total_seconds())}s",
            "notification": {"channel_id": ANDROID_CHANNEL, "tag": topic},
        },
        "apns": {
            "headers": {
                "apns-collapse-id": topic,
                "apns-expiration": str(int((now + ttl).timestamp())),
            },
            "payload": {"aps": {"mutable-content": 1, "sound": "default"}},
        },
    }


# --- envio -----------------------------------------------------------------


@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None  # TEMPORARY | PERMANENT
    detail: str | None = None


class Sender(Protocol):
    mode: str

    def send(self, message: dict) -> SendResult: ...


@dataclass
class DryRunSender:
    """Não chama o FCM. Guarda o que seria enviado (útil em testes)."""

    mode: str = "dry-run"
    sent: list[dict] = field(default_factory=list)

    def send(self, message: dict) -> SendResult:
        self.sent.append(message)
        return SendResult(ok=True, message_id=None)


class SafetyError(RuntimeError):
    pass


def to_admin_message(message: dict):
    from firebase_admin import messaging

    n = message["notification"]
    a = message["android"]
    ap = message["apns"]
    aps = ap["payload"]["aps"]
    return messaging.Message(
        condition=message["condition"],
        notification=messaging.Notification(
            title=n["title"], body=n.get("body"), image=n.get("image")
        ),
        data=dict(message["data"]),
        android=messaging.AndroidConfig(
            priority=a["priority"],
            ttl=timedelta(seconds=int(a["ttl"].rstrip("s"))),
            notification=messaging.AndroidNotification(
                channel_id=a["notification"]["channel_id"], tag=a["notification"]["tag"]
            ),
        ),
        apns=messaging.APNSConfig(
            headers=dict(ap["headers"]),
            payload=messaging.APNSPayload(
                aps=messaging.Aps(mutable_content=bool(aps["mutable-content"]), sound=aps["sound"])
            ),
        ),
    )


def classify_error(exc: Exception) -> str:
    from firebase_admin import exceptions as fx

    if isinstance(exc, fx.InvalidArgumentError):
        return PERMANENT
    # UNAVAILABLE, INTERNAL, DEADLINE_EXCEEDED, cota, rede, credencial: tenta de novo
    # na próxima execução; o item expira sozinho em 60 min se o problema persistir.
    return TEMPORARY


class FcmSender:
    def __init__(self, mode: str, project_id: str, app=None):
        if mode not in ("test", "production"):
            raise SafetyError(f"FcmSender não pode ser usado no modo {mode!r}")
        self.mode = mode
        self.project_id = project_id
        self._app = app

    def _get_app(self):
        if self._app is None:
            self._app = firebase_app(self.project_id)
        return self._app

    def send(self, message: dict) -> SendResult:
        if self.mode == "test" and message.get("condition") != TEST_CONDITION:
            raise SafetyError("modo test só pode publicar em news_teste")
        if "topic" in message or "token" in message:
            raise SafetyError("mensagem deve usar apenas 'condition'")
        from firebase_admin import messaging

        try:
            message_id = messaging.send(to_admin_message(message), app=self._get_app())
            return SendResult(ok=True, message_id=message_id)
        except Exception as exc:  # noqa: BLE001 - classificamos qualquer falha
            return SendResult(ok=False, error=classify_error(exc), detail=f"{type(exc).__name__}: {exc}")


def firebase_app(project_id: str):
    """App do Admin SDK com as credenciais de GOOGLE_APPLICATION_CREDENTIALS."""
    import firebase_admin
    from firebase_admin import credentials

    try:
        return firebase_admin.get_app()
    except ValueError:
        return firebase_admin.initialize_app(
            credentials.ApplicationDefault(), {"projectId": project_id}
        )


def make_sender(mode: str, project_id: str) -> Sender:
    if mode == "dry-run":
        return DryRunSender()
    return FcmSender(mode, project_id)
