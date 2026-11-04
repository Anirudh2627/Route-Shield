"""Incident Response Agent endpoints.

Security model:
* GET endpoints are read-only and open (consistent with the other incident
  read endpoints): agent status, investigation list and the persisted trace.
* POST endpoints that trigger model inference or provider probes require the
  authenticated fleet manager - they cost money (LLM calls) and the
  investigation is an operational action even though it writes nothing
  operational.
* There is deliberately NO write endpoint here: the agent cannot create,
  mutate or approve anything. Operational writes remain exclusively on
  POST /api/incidents/{id}/approve-plan.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..auth import get_current_manager
from ..db import get_db
from ..schemas import (
    AgentProbeOut,
    AgentStatusOut,
    InvestigationOut,
    StartInvestigationRequest,
)
from ..services import agent as agent_svc

logger = logging.getLogger("routeshield.agents")
router = APIRouter(prefix="/agents", tags=["agent"])


@router.get("/status", response_model=AgentStatusOut)
def agent_status() -> AgentStatusOut:
    """Readiness of the investigation agent (no LLM call is made)."""
    return AgentStatusOut(**agent_svc.agent_status())


@router.post("/probe", response_model=AgentProbeOut)
def probe(_: models.ManagerUser = Depends(get_current_manager)) -> AgentProbeOut:
    """Live probe of Vultr Serverless Inference + native tool calling.
    Costs one tiny completion; writes nothing."""
    return AgentProbeOut(**agent_svc.probe_provider())


@router.post("/investigations", response_model=InvestigationOut, status_code=202)
def start_investigation(
    req: StartInvestigationRequest,
    manager: models.ManagerUser = Depends(get_current_manager),
    db: Session = Depends(get_db),
) -> InvestigationOut:
    """Launch a bounded agent investigation for an incident (async).

    Returns 202 immediately with the persisted session; clients poll
    GET /api/agents/investigations/{session_uid} for the live trace.
    """
    incident = db.get(models.Incident, req.incident_id)
    if incident is None:
        raise HTTPException(404, f"Incident {req.incident_id} not found")

    session_uid = agent_svc.launch_investigation(
        incident_id=req.incident_id,
        objective=req.objective.strip(),
        requested_by=manager.username,
    )
    session = db.scalar(
        select(models.InvestigationSession).where(
            models.InvestigationSession.session_uid == session_uid
        )
    )
    if session is None:  # pragma: no cover - launch writes the row synchronously
        raise HTTPException(500, "Investigation session failed to persist")
    return InvestigationOut.model_validate(session)


@router.get("/incidents/{incident_id}/investigations", response_model=list[InvestigationOut])
def list_investigations(
    incident_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[InvestigationOut]:
    """Investigation sessions for an incident, most recent first."""
    if db.get(models.Incident, incident_id) is None:
        raise HTTPException(404, f"Incident {incident_id} not found")
    rows = db.execute(
        select(models.InvestigationSession)
        .where(models.InvestigationSession.incident_id == incident_id)
        .options(joinedload(models.InvestigationSession.steps))
        .order_by(models.InvestigationSession.id.desc())
        .limit(limit)
    ).unique().scalars().all()
    return [InvestigationOut.model_validate(r) for r in rows]


@router.get("/investigations/{session_uid}", response_model=InvestigationOut)
def get_investigation(
    session_uid: str,
    db: Session = Depends(get_db),
) -> InvestigationOut:
    """One investigation with its full persisted tool trace."""
    session = db.execute(
        select(models.InvestigationSession)
        .where(models.InvestigationSession.session_uid == session_uid)
        .options(joinedload(models.InvestigationSession.steps))
    ).unique().scalar_one_or_none()
    if session is None:
        raise HTTPException(404, f"Investigation {session_uid} not found")
    return InvestigationOut.model_validate(session)
