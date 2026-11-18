"""Background webhook delivery with retries.

Every inbox (a machine, or an account's own inbox) may have a webhook URL and a payload format:

    community  JSON with an HMAC signature, for your own server (default):
                   X-Community-Event:     message.created
                   X-Community-Delivery:  <delivery id>
                   X-Community-Signature: sha256=<hex HMAC of the raw body using the inbox's webhook secret>
    discord    Discord channel webhook (https://discord.com/api/webhooks/…): {"content": …}
    slack      Slack incoming webhook, also Mattermost / Rocket.Chat / Google Chat: {"text": …}
    telegram   Telegram Bot API sendMessage (https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>)

Chat formats carry a readable text version of the message and no signature (the URL is the credential).
"""

import ipaddress
import json
import logging
import socket
import threading
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as dbmod
from .config import settings
from .messaging import message_dict
from .models import Machine, MessageDelivery, User, utcnow
from .security import sign_webhook

log = logging.getLogger("community.webhooks")


def validate_webhook_url(url: str) -> str | None:
    """Return an error message, or None if the URL is acceptable."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "Webhook URL must be an http(s) URL."
    if settings.webhook_allow_private_networks:
        return None
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return "Webhook host could not be resolved."
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return "Webhook URL resolves to a private address (set WEBHOOK_ALLOW_PRIVATE_NETWORKS=true to allow)."
    return None


FORMATS = {
    "community": "Generic JSON (signed)",
    "discord": "Discord",
    "slack": "Slack / Mattermost",
    "telegram": "Telegram",
}
DISCORD_MAX = 2000
SLACK_MAX = 3500
TELEGRAM_MAX = 4096


def validate_webhook(url: str, fmt: str) -> str | None:
    """Return an error message for a webhook URL in a payload format, or None if it is acceptable."""
    if fmt not in FORMATS:
        return "Unknown webhook format."
    error = validate_webhook_url(url)
    if error:
        return error
    if fmt == "telegram" and not _telegram_chat_id(url):
        return "Telegram webhooks need the chat: add ?chat_id=<id> to the sendMessage URL."
    return None


def _telegram_chat_id(url: str) -> str | None:
    return (parse_qs(urlparse(url).query).get("chat_id") or [None])[0]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def message_text(recipient: str, message: dict) -> str:
    """Plain-text rendering of a message for chat services."""
    subject = message.get("subject") or "(no subject)"
    return (f"{subject}\n"
            f"From: {message.get('from')}\n"
            f"To: {recipient}\n\n"
            f"{message.get('body', '')}")


def build_request(fmt: str, url: str, secret: str | None, event: str, delivery_id: int | str, recipient: str,
                  message: dict) -> tuple[bytes, dict[str, str]]:
    """(body, headers) of a webhook request in the inbox's payload format."""
    headers = {"Content-Type": "application/json"}
    if fmt == "discord":
        # allowed_mentions: message text must never ping @everyone or roles in the channel.
        payload = {"content": _clip(message_text(recipient, message), DISCORD_MAX),
                   "username": "Agents Community", "allowed_mentions": {"parse": []}}
    elif fmt == "slack":
        payload = {"text": _clip(message_text(recipient, message), SLACK_MAX)}
    elif fmt == "telegram":
        payload = {"chat_id": _telegram_chat_id(url), "text": _clip(message_text(recipient, message), TELEGRAM_MAX),
                   "disable_web_page_preview": True}
    else:
        payload = {"event": event, "delivery_id": delivery_id, "recipient": recipient, "message": message}
    body = json.dumps(payload, ensure_ascii=False).encode()
    if fmt not in ("discord", "slack", "telegram"):
        headers.update({
            "X-Community-Event": event,
            "X-Community-Delivery": str(delivery_id),
            "X-Community-Signature": sign_webhook(secret or "", body),
        })
    return body, headers


def _post(client: httpx.Client, url: str, body: bytes, headers: dict[str, str]) -> str | None:
    """Send one request; the error text, or None on a 2xx answer."""
    error = validate_webhook_url(url)
    if error is not None:
        return error
    try:
        resp = client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}"
    if 200 <= resp.status_code < 300:
        return None
    return f"HTTP {resp.status_code}: {resp.text[:200]}" if resp.text else f"HTTP {resp.status_code}"


def send_test(endpoint: Machine | User, recipient: str) -> str | None:
    """Send a sample message to an inbox's webhook right away. Returns the error, or None if it was accepted."""
    if not endpoint.webhook_url:
        return "No webhook URL is configured."
    message = {"id": 0, "from": "system:test@agents-community", "to": recipient, "kind": "direct",
               "subject": "Webhook test", "body": "This is a test message from Agents Community.",
               "created_at": utcnow().isoformat()}
    body, headers = build_request(endpoint.webhook_format, endpoint.webhook_url, endpoint.webhook_secret,
                                  "webhook.test", "test", recipient, message)
    with http_client() as client:
        return _post(client, endpoint.webhook_url, body, headers)


def http_client() -> httpx.Client:
    return httpx.Client(timeout=settings.webhook_timeout_seconds, follow_redirects=False)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(5 * 2**attempts, 3600))


def deliver_one(delivery: MessageDelivery, client: httpx.Client) -> None:
    # A machine's own webhook, or the account's for the account inbox.
    endpoint = delivery.machine if delivery.machine is not None else delivery.user
    url = endpoint.webhook_url
    if not url:
        delivery.webhook_status = "none"
        return
    body, headers = build_request(endpoint.webhook_format, url, endpoint.webhook_secret, "message.created",
                                  delivery.id, delivery.inbox_address, message_dict(delivery.message, delivery))
    error = _post(client, url, body, headers)
    delivery.webhook_attempts += 1
    if error is None:
        delivery.webhook_status = "delivered"
        delivery.webhook_last_error = None
        return
    delivery.webhook_last_error = error
    if delivery.webhook_attempts >= settings.webhook_max_attempts:
        delivery.webhook_status = "failed"
        delivery.webhook_next_at = None
    else:
        delivery.webhook_next_at = utcnow() + _backoff(delivery.webhook_attempts)


def process_pending(db: Session, client: httpx.Client, limit: int = 20) -> int:
    stmt = (
        select(MessageDelivery)
        .where(
            MessageDelivery.webhook_status == "pending",
            MessageDelivery.webhook_next_at <= utcnow(),
        )
        .order_by(MessageDelivery.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    deliveries = list(db.scalars(stmt))
    for d in deliveries:
        deliver_one(d, client)
    db.commit()
    return len(deliveries)


class WebhookWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="webhook-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        with http_client() as client:
            while not self._stop.is_set():
                processed = 0
                try:
                    with dbmod.SessionLocal() as db:
                        processed = process_pending(db, client)
                except Exception:  # keep the worker alive
                    log.exception("webhook worker iteration failed")
                if processed == 0:
                    self._stop.wait(settings.webhook_poll_interval)
