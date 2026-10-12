"""Background webhook delivery with retries and HMAC signatures.

Each request carries:
    X-Community-Event:     message.created
    X-Community-Delivery:  <delivery id>
    X-Community-Signature: sha256=<hex HMAC of the raw body using the machine's webhook secret>
"""

import ipaddress
import json
import logging
import socket
import threading
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db as dbmod
from .config import settings
from .messaging import message_dict
from .models import MessageDelivery, utcnow
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


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(5 * 2**attempts, 3600))


def deliver_one(delivery: MessageDelivery, client: httpx.Client) -> None:
    machine = delivery.machine
    payload = {
        "event": "message.created",
        "delivery_id": delivery.id,
        "recipient": machine.address,
        "message": message_dict(delivery.message, delivery),
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    url = machine.webhook_url
    if not url:
        delivery.webhook_status = "none"
        return
    error = validate_webhook_url(url)
    if error is None:
        try:
            resp = client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Community-Event": "message.created",
                    "X-Community-Delivery": str(delivery.id),
                    "X-Community-Signature": sign_webhook(machine.webhook_secret, body),
                },
            )
            if 200 <= resp.status_code < 300:
                delivery.webhook_status = "delivered"
                delivery.webhook_last_error = None
                delivery.webhook_attempts += 1
                return
            error = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"

    delivery.webhook_attempts += 1
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
        with httpx.Client(timeout=settings.webhook_timeout_seconds, follow_redirects=False) as client:
            while not self._stop.is_set():
                processed = 0
                try:
                    with dbmod.SessionLocal() as db:
                        processed = process_pending(db, client)
                except Exception:  # keep the worker alive
                    log.exception("webhook worker iteration failed")
                if processed == 0:
                    self._stop.wait(settings.webhook_poll_interval)
