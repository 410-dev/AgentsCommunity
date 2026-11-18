import os
import secrets
from contextvars import ContextVar


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://community:community@db:5432/community"
    )
    # Used to sign session cookies. Must be stable across restarts in production.
    secret_key: str = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
    session_https_only: bool = _bool("SESSION_HTTPS_ONLY", False)
    webhook_worker_enabled: bool = _bool("WEBHOOK_WORKER_ENABLED", True)
    webhook_allow_private_networks: bool = _bool("WEBHOOK_ALLOW_PRIVATE_NETWORKS", False)
    webhook_timeout_seconds: float = float(os.getenv("WEBHOOK_TIMEOUT_SECONDS", "10"))
    webhook_max_attempts: int = int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "6"))
    webhook_poll_interval: float = float(os.getenv("WEBHOOK_POLL_INTERVAL", "2"))
    # Absolute links in notifications (e.g. https://community.example.com). Defaults to the URL of the
    # request that caused the notification.
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")


settings = Settings()

# Base URL of the request being handled (set by a middleware in main.py), for absolute links.
request_base_url: ContextVar[str | None] = ContextVar("request_base_url", default=None)


def absolute_url(path: str) -> str:
    """`path` on this server: PUBLIC_BASE_URL, else the current request's base URL, else the bare path."""
    base = settings.public_base_url or (request_base_url.get() or "").rstrip("/")
    return base + path if base else path
