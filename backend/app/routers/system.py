"""System endpoints: health and authentication."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import __version__, models
from ..auth import login as do_login
from ..config import get_settings
from ..db import get_db
from ..schemas import HealthOut, LoginRequest, LoginResponse
from ..services import llm_extraction

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthOut, summary="Deployment health check")
def health(db: Session = Depends(get_db)) -> HealthOut:
    settings = get_settings()
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:  # noqa: BLE001
        db_status = "unreachable"
    return HealthOut(
        status="ok" if db_status == "ok" else "degraded",
        app="RouteShield",
        version=__version__,
        environment=settings.environment,
        database=db_status,
        utc_time=datetime.now(timezone.utc),
        llm_configured=llm_extraction.llm_available(),
    )


@router.post("/auth/login", response_model=LoginResponse, summary="Manager login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user, token, expires = do_login(db, payload.username, payload.password)
    return LoginResponse(
        token=token,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        expires_at=expires,
    )
