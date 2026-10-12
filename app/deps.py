import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import ApiKey, Machine, User, utcnow
from .roles import Role
from .security import hash_api_key


class LoginRequired(Exception):
    pass


class SetupRequired(Exception):
    pass


def super_admin_exists(db: Session) -> bool:
    return db.scalar(select(User.id).where(User.role == int(Role.SUPER_ADMIN))) is not None


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


async def csrf_protect(request: Request) -> None:
    if request.method != "POST":
        return
    form = await request.form()
    sent = form.get("csrf_token")
    expected = request.session.get("csrf")
    if not expected or not sent or not secrets.compare_digest(str(sent), expected):
        raise HTTPException(status_code=400, detail="Invalid or missing CSRF token. Reload the page.")


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if uid is None:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        request.session.pop("uid", None)
        return None
    return user


def current_user(
    request: Request, db: Session = Depends(get_db), user: User | None = Depends(optional_user)
) -> User:
    if user is None:
        if not super_admin_exists(db):
            raise SetupRequired()
        raise LoginRequired()
    return user


# ---- API (Bearer token bound to a machine) ----------------------------------

_bearer = HTTPBearer(auto_error=False)


def api_machine(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Machine:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    key = db.scalar(
        select(ApiKey).where(
            ApiKey.key_hash == hash_api_key(creds.credentials), ApiKey.revoked_at.is_(None)
        )
    )
    if key is None or not key.machine.user.is_active:
        raise HTTPException(401, "Invalid or revoked token", headers={"WWW-Authenticate": "Bearer"})
    key.last_used_at = utcnow()
    db.commit()
    return key.machine
