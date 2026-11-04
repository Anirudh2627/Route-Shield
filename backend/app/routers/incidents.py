"""Incident workflow endpoints.

Security model:
* Read/derived endpoints (list, get, evidence, assessment) are open within the
  deployment - they never change operational state.
* Decision endpoints (set policy, generate plan, APPROVE plan) require the
  authenticated fleet manager. The approve endpoint is the ONLY path that
  modifies operational state, and it re-validates everything inside a single
  transaction before applying changes.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..auth import get_current_manager
from ..db import get_db
from ..schemas import (
    CLASSIFICATION_EXPLANATIONS,
    POLICY_EXPLANATIONS,
    ApprovePlanRequest,
    AssessmentOut,
    AuditEventOut,
    CriterionOut,
    EvidenceOut,
    FleetAssessmentSummary,
    IncidentOut,
    PolicySelectionOut,
    ProposalOut,
    RefreshResult,
    SetPolicyRequest,
)
from ..services import audit, assessment as assessment_svc, evidence as evidence_svc, recovery as recovery_svc
from ..services.approval import ApprovalRejected, apply_approval

logger = logging.getLogger("routeshield.incidents")
router = APIRouter(prefix="/incidents", tags=["incidents"])


def _get_incident(db: Session, incident_id: int) -> models.Incident:
    incident = db.get(models.Incident, incident_id)
    if incident is None:
        raise HTTPException(404, f"Incident {incident_id} not found")
    return incident


def _active_policy(db: Session, incident_id: int) -> models.OperationalPolicySelection | None:
    return db.scalar(
        select(models.OperationalPolicySelection)
        .where(
            models.OperationalPolicySelection.incident_id == incident_id,
            models.OperationalPolicySelection.superseded.is_(False),
        )
        .order_by(models.OperationalPolicySelection.id.desc())
        .limit(1)
    )


def _proposal_out(p: models.RecoveryProposal | None) -> dict | None:
    if p is None:
        return None
    d = ProposalOut.model_validate(p).model_dump()
    d["policy_explanation"] = POLICY_EXPLANATIONS.get(p.policy, "")
    return d


def _incident_out(db: Session, incident: models.Incident) -> dict:
    d = IncidentOut.model_validate(incident).model_dump()
    policy = _active_policy(db, incident.id)
    d["active_policy"] = (
        {**PolicySelectionOut.model_validate(policy).model_dump(),
         "policy_explanation": POLICY_EXPLANATIONS.get(policy.policy, "")}
        if policy else None
    )
    d["latest_proposal"] = _proposal_out(recovery_svc.latest_proposal(db, incident.id))
    return d


def _assessment_out(a: models.VehicleAssessment) -> dict:
    d = AssessmentOut.model_validate(a).model_dump()
    d["classification_explanation"] = CLASSIFICATION_EXPLANATIONS.get(a.classification, "")
    return d


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@router.get("", summary="List incidents")
def list_incidents(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(models.Incident).order_by(models.Incident.id)).all()
    return [_incident_out(db, r) for r in rows]


@router.get("/{incident_id}", summary="Incident detail (header data, policy, latest proposal)")
def get_incident(incident_id: int, db: Session = Depends(get_db)) -> dict:
    return _incident_out(db, _get_incident(db, incident_id))


@router.get("/{incident_id}/evidence", response_model=EvidenceOut, summary="Recall evidence with provenance")
def get_evidence(incident_id: int, db: Session = Depends(get_db)) -> dict:
    incident = _get_incident(db, incident_id)
    payload = evidence_svc.get_evidence_payload(db, incident)
    out = {
        "incident_id": incident.id,
        "campaign": payload["campaign"],
        "report_version": payload["report_version"],
        "criteria": payload["criteria"],
        "evidence_source": payload["evidence_source"],
        "evidence_banner": payload["evidence_banner"],
        "using_reviewed_fixture": payload["using_reviewed_fixture"],
    }
    return out


@router.get("/{incident_id}/audit", response_model=list[AuditEventOut], summary="Append-only audit trail")
def get_audit(incident_id: int, limit: int = 200, db: Session = Depends(get_db)) -> list[models.AuditEvent]:
    _get_incident(db, incident_id)
    return audit.list_events(db, incident_id=incident_id, limit=min(limit, 1000))


@router.get("/{incident_id}/fleet-assessment", response_model=FleetAssessmentSummary,
            summary="Latest deterministic fleet assessment")
def get_fleet_assessment(incident_id: int, db: Session = Depends(get_db)) -> dict:
    incident = _get_incident(db, incident_id)
    rows = list(db.scalars(
        select(models.VehicleAssessment)
        .where(models.VehicleAssessment.incident_id == incident.id)
        .options(joinedload(models.VehicleAssessment.vehicle))
        .order_by(models.VehicleAssessment.id)
    ))
    counts = {"CAMPAIGN_CRITERIA_MATCH": 0, "VERIFICATION_REQUIRED": 0, "OUTSIDE_REPORT_VERSION": 0}
    vin_counts: dict[str, int] = {}
    for r in rows:
        counts[r.classification] = counts.get(r.classification, 0) + 1
        vin_counts[r.vin_evidence_status] = vin_counts.get(r.vin_evidence_status, 0) + 1
    assessed_at = max((r.assessed_at for r in rows), default=None)
    return {
        "incident_id": incident.id,
        "assessed_at": assessed_at,
        "total_vehicles": len(rows),
        "counts": counts,
        "vin_status_counts": vin_counts,
        "assessments": [_assessment_out(r) for r in rows],
    }


@router.get("/{incident_id}/recovery-plan", summary="Latest recovery proposal (if any)")
def get_recovery_plan(incident_id: int, db: Session = Depends(get_db)) -> dict:
    incident = _get_incident(db, incident_id)
    p = recovery_svc.latest_proposal(db, incident.id)
    if p is None:
        raise HTTPException(404, "No recovery plan has been generated for this incident yet.")
    return _proposal_out(p)


# ---------------------------------------------------------------------------
# Evidence refresh (live NHTSA pipeline with explicit fallback)
# ---------------------------------------------------------------------------


@router.post("/{incident_id}/refresh-recall", response_model=RefreshResult,
             summary="Retrieve campaign + report from NHTSA (falls back to reviewed fixture)")
def refresh_recall(incident_id: int, db: Session = Depends(get_db)) -> dict:
    incident = _get_incident(db, incident_id)
    try:
        outcome = evidence_svc.refresh_recall_evidence(db, incident.campaign_number)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    incident.active_report_version_id = outcome.report_version_id
    incident.evidence_source = outcome.evidence_source
    if incident.status in ("OPEN", "EVIDENCE_LOADED"):
        incident.status = "EVIDENCE_LOADED"
    audit.record_event(
        db, actor="system", action="RECALL_EVIDENCE_REFRESHED", incident_id=incident.id,
        entity_type="RecallReportVersion", entity_id=str(outcome.report_version_id),
        new_state={
            "evidence_source": outcome.evidence_source,
            "document_sha256": outcome.document_sha256,
            "document_changed": outcome.document_changed,
            "llm_supplementary": outcome.llm_supplementary,
        },
        result="OK" if outcome.evidence_source == "LIVE_EXTRACTION" else "FALLBACK_FIXTURE",
        detail=outcome.live_error or "Live NHTSA retrieval succeeded.",
        flush_only=False,
    )
    payload = evidence_svc.get_evidence_payload(db, incident)
    return {
        "incident_id": incident.id,
        "evidence": {
            "incident_id": incident.id,
            "campaign": payload["campaign"],
            "report_version": payload["report_version"],
            "criteria": payload["criteria"],
            "evidence_source": payload["evidence_source"],
            "evidence_banner": payload["evidence_banner"],
            "using_reviewed_fixture": payload["using_reviewed_fixture"],
            "live_retrieval_error": outcome.live_error,
        },
        "message": outcome.banner,
    }


# ---------------------------------------------------------------------------
# Deterministic assessment
# ---------------------------------------------------------------------------


@router.post("/{incident_id}/assess-fleet", response_model=FleetAssessmentSummary,
             summary="Run the deterministic vehicle assessment (no LLM involved)")
def assess_fleet(incident_id: int, db: Session = Depends(get_db)) -> dict:
    incident = _get_incident(db, incident_id)
    try:
        rows = assessment_svc.assess_fleet(db, incident)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    audit.record_event(
        db, actor="system", action="FLEET_ASSESSED", incident_id=incident.id,
        entity_type="Incident", entity_id=str(incident.id),
        new_state={"assessed_vehicles": len(rows)},
        result="OK",
        detail=f"Deterministic assessment of {len(rows)} synthetic fleet records "
               f"(engine v{assessment_svc.ENGINE_VERSION}).",
        flush_only=False,
    )
    return get_fleet_assessment(incident_id, db)


# ---------------------------------------------------------------------------
# Manager decision endpoints (authenticated)
# ---------------------------------------------------------------------------


@router.post("/{incident_id}/set-operational-policy", response_model=PolicySelectionOut,
             summary="Fleet manager selects handling policy for VERIFICATION_REQUIRED vehicles")
def set_policy(
    incident_id: int,
    payload: SetPolicyRequest,
    db: Session = Depends(get_db),
    manager: models.ManagerUser = Depends(get_current_manager),
) -> dict:
    incident = _get_incident(db, incident_id)
    previous = _active_policy(db, incident.id)
    if previous is not None:
        previous.superseded = True
    # A policy change invalidates any pending proposal generated under the old
    # policy: mark it SUPERSEDED so it can no longer be approved as-is.
    for p in db.scalars(
        select(models.RecoveryProposal).where(
            models.RecoveryProposal.incident_id == incident.id,
            models.RecoveryProposal.status == "PENDING",
        )
    ):
        p.status = "SUPERSEDED"
    sel = models.OperationalPolicySelection(
        incident_id=incident.id,
        policy=payload.policy,
        selected_by=manager.username,
        acknowledgement_text=POLICY_EXPLANATIONS[payload.policy],
    )
    db.add(sel)
    db.flush()
    audit.record_event(
        db, actor=manager.username, action="OPERATIONAL_POLICY_SELECTED", incident_id=incident.id,
        entity_type="OperationalPolicySelection", entity_id=str(sel.id),
        previous_state={"policy": previous.policy} if previous else None,
        new_state={"policy": sel.policy},
        policy=sel.policy, result="SELECTED",
        detail="Explicit fleet-manager policy selection; never chosen automatically by the system.",
    )
    db.commit()
    return {**PolicySelectionOut.model_validate(sel).model_dump(),
            "policy_explanation": POLICY_EXPLANATIONS[sel.policy]}


@router.post("/{incident_id}/generate-recovery-plan", summary="Generate the deterministic recovery proposal")
def generate_plan(
    incident_id: int,
    db: Session = Depends(get_db),
    manager: models.ManagerUser = Depends(get_current_manager),
) -> dict:
    incident = _get_incident(db, incident_id)
    try:
        proposal = recovery_svc.generate_recovery_plan(db, incident)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    audit.record_event(
        db, actor=manager.username, action="RECOVERY_PLAN_GENERATED", incident_id=incident.id,
        entity_type="RecoveryProposal", entity_id=proposal.proposal_uid,
        new_state=proposal.summary, policy=proposal.policy, proposal_id=proposal.proposal_uid,
        result="PENDING_APPROVAL",
        detail="Deterministic engine produced this proposal. No operational state has changed; "
               "manager approval is required.",
        flush_only=False,
    )
    return _proposal_out(proposal)


@router.post("/{incident_id}/approve-plan", summary="APPROVE and apply the recovery plan (manager only)")
def approve_plan(
    incident_id: int,
    payload: ApprovePlanRequest,
    db: Session = Depends(get_db),
    manager: models.ManagerUser = Depends(get_current_manager),
) -> dict:
    """The ONLY endpoint that may modify approved operational state.

    Validates the pending proposal, the selected policy, re-checks spare
    availability, applies changes in one transaction, writes append-only
    audit events, and returns the persisted state.
    """
    incident = _get_incident(db, incident_id)
    try:
        result = apply_approval(db, incident, manager.username, payload.proposal_uid,
                                payload.plan_hash, payload.policy)
    except ApprovalRejected as exc:
        raise HTTPException(409, detail={"reason": exc.reason, "code": exc.code})
    fresh_events = audit.list_events(db, incident_id=incident.id, limit=50)
    proposal = db.scalar(
        select(models.RecoveryProposal).where(models.RecoveryProposal.proposal_uid == payload.proposal_uid)
    )
    return {
        "approval": result,
        "proposal": _proposal_out(proposal),
        "incident": _incident_out(db, incident),
        "audit_events": [AuditEventOut.model_validate(e).model_dump() for e in fresh_events],
    }
