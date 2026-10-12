import hashlib
import hmac
import re
import secrets

_PBKDF2_ITER = 390_000

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
TENANCY_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")


def valid_name(value: str) -> bool:
    """Names used inside addresses (user id, machine, department)."""
    return bool(NAME_RE.match(value))


def valid_tenancy_name(value: str) -> bool:
    return bool(TENANCY_RE.match(value))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITER)
    return f"pbkdf2_sha256${_PBKDF2_ITER}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
    return hmac.compare_digest(digest.hex(), digest_hex)


def generate_api_key() -> tuple[str, str, str]:
    """Returns (plaintext_key, prefix, sha256_hash). Only the hash is stored."""
    raw = "ack_" + secrets.token_urlsafe(32)
    return raw, raw[:12], hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_webhook_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(24)


def sign_webhook(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
