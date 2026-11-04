"""Append-only audit log.

This module can only CREATE and READ AuditEvent rows. There is deliberately
no update or delete function anywhere in the codebase, and no API endpoint
exposes mutation of audit events.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models


def record_event(
    db: Session,
    *,
    actor: str,
    action: str,
    incident_id: int | None = None,
    entity_type: str = "",
    entity_id: str = "",
    previous_state: dict | None = None,
    new_state: dict | None = None,
    policy: str | None = None,
    proposal_id: str | None = None,
    result: str = "",
    detail: str = "",
    flush_only: bool = True,
) -> models.AuditEvent:
    """Append one audit event. Never mutates existing events."""
    event = models.AuditEvent(
        event_uid=f"AUD-{uuid.uuid4().hex[:12].upper()}",
        timestamp=datetime.now(timezone.utc),
        actor=actor,
        incident_id=incident_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        previous_state=previous_state,
        new_state=new_state,
        policy=policy,
        proposal_id=proposal_id,
        result=result,
        detail=detail,
    )
    db.add(event)
    if flush_only:
        db.flush()
    else:
        db.commit()
    return event


def list_events(db: Session, incident_id: int | None = None, limit: int = 200) -> list[models.AuditEvent]:
    stmt = select(models.AuditEvent).order_by(models.AuditEvent.id.desc()).limit(limit)
    if incident_id is not None:
        stmt = stmt.where(models.AuditEvent.incident_id == incident_id)
    return list(db.scalars(stmt))
