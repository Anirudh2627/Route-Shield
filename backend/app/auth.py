"""Authentication for the fleet-manager account.

MVP approach: one seeded manager account (password from environment; a random
password is generated and logged ONCE at startup when unset - never
hardcoded), PBKDF2 password hashing, and HMAC-signed bearer tokens.

Only authenticated managers can:
* set the operational policy,
* generate recovery plans,
* approve plans (the sole write path for operational state),
* trigger VIN decodes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .config import get_settings
from .db import get_db

logger = logging.getLogger("routeshield.auth")

PBKDF2_ROUNDS = 240_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(base64.b64encode(dk).decode(), hash_b64)
    except (ValueError, TypeError):
        return False


def issue_token(user: models.ManagerUser) -> tuple[str, datetime]:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.token_ttl_minutes)
    payload = {
        "sub": user.username,
        "uid": user.id,
        "role": user.role,
        "exp": int(expires.timestamp()),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")
    sig = hmac.new(settings.resolved_auth_secret().encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}", expires


def verify_token(token: str) -> dict:
    settings = get_settings()
    try:
        body, sig = token.split(".")
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed token")
    expected = hmac.new(settings.resolved_auth_secret().encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token signature")
    padded = body + "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed token payload")
    if payload.get("exp", 0) < datetime.now(timezone.utc).timestamp():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    return payload


def ensure_manager_account(db: Session) -> models.ManagerUser:
    """Idempotently create the manager account from environment config."""
    settings = get_settings()
    username = settings.manager_username
    user = db.scalar(select(models.ManagerUser).where(models.ManagerUser.username == username))
    password = settings.manager_password
    if user is None:
        if not password:
            password = secrets.token_urlsafe(12)
            logger.warning(
                "ROUTESHIELD_MANAGER_PASSWORD not set. Generated one-time development password "
                "for manager '%s': %s  (set the env var for persistent deployments)",
                username, password,
            )
        user = models.ManagerUser(
            username=username,
            password_hash=hash_password(password),
            display_name="Fleet Manager",
            role="FLEET_MANAGER",
        )
        db.add(user)
        db.commit()
    elif password and not verify_password(password, user.password_hash):
        # Env password changed (e.g. rotated): re-hash to match environment.
        user.password_hash = hash_password(password)
        db.commit()
    return user


def login(db: Session, username: str, password: str) -> tuple[models.ManagerUser, str, datetime]:
    user = db.scalar(select(models.ManagerUser).where(models.ManagerUser.username == username))
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    token, expires = issue_token(user)
    return user, token, expires


def get_current_manager(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> models.ManagerUser:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication required: pass 'Authorization: Bearer <token>' from POST /api/auth/login.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = verify_token(authorization.split(" ", 1)[1].strip())
    user = db.scalar(select(models.ManagerUser).where(models.ManagerUser.username == payload.get("sub")))
    if user is None or user.role != "FLEET_MANAGER":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manager role required")
    return user
