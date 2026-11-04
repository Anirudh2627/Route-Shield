"""The ONLY code path that may modify approved operational state.

ARCHITECTURAL CONSTRAINT (non-negotiable):
    The LLM has no direct write access to operational records. Nothing in the
    LLM layer, no model-generated tool call and no unauthenticated request can
    reach these functions. Only the authenticated manager approval endpoint
    (POST /api/incidents/{id}/approve-plan) calls `apply_approval`, and it does
    so inside a single database transaction with full validation and an
    append-only audit record.

Validation performed before ANY state change:
    1. Proposal exists, belongs to the incident, is PENDING.
    2. plan_hash matches the persisted proposal (stale/tampered plans reject).
    3. The policy presented matches the policy the proposal was generated with
       and the manager's active, non-superseded policy selection.
    4. Re-check that every proposed replacement vehicle is still an AVAILABLE
       spare and is not double-booked within the plan.
    5. Re-check each route still has the original vehicle assigned.
On any failure the transaction is rolled back, an APPROVAL_REJECTED audit
event is recorded, and no operational state changes.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import audit
from .recovery import HOLD, REVIEW_ONLY, compute_plan_hash


class ApprovalRejected(Exception):
    """Raised when an approval request fails validation. Carries a reason."""

    def __init__(self, reason: str, code: str = "APPROVAL_VALIDATION_FAILED"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def _vehicle_state(v: models.FleetVehicle | None) -> dict:
    if v is None:
        return {}
    return {
        "asset_id": v.asset_id,
        "availability": v.availability,
        "operational_status": v.operational_status,
    }


def _route_state(r: models.FleetRoute | None, vehicle: models.FleetVehicle | None) -> dict:
    if r is None:
        return {}
    return {
        "route_id": r.route_id,
        "assigned_vehicle": vehicle.asset_id if vehicle else None,
    }


def apply_approval(db: Session, incident: models.Incident, actor: str, proposal_uid: str, plan_hash: str, policy: str) -> dict:
    """Validate and apply an approved recovery plan in ONE transaction.

    Returns the persisted post-approval state. Raises ApprovalRejected (with
    the session rolled back) when validation fails.
    """
    proposal = db.scalar(
        select(models.RecoveryProposal).where(
            models.RecoveryProposal.incident_id == incident.id,
            models.RecoveryProposal.proposal_uid == proposal_uid,
        )
    )
    if proposal is None:
        _reject(db, incident, actor, proposal_uid, "Proposal not found for this incident.")
    if proposal.status != "PENDING":
        _reject(
            db, incident, actor, proposal_uid,
            f"Proposal is not pending approval (current status: {proposal.status}).",
        )
    if plan_hash != proposal.plan_hash:
        _reject(
            db, incident, actor, proposal_uid,
            "Plan hash mismatch: the proposal content changed since it was presented. "
            "Regenerate the recovery plan and review it again.",
        )
    if policy != proposal.policy:
        _reject(
            db, incident, actor, proposal_uid,
            f"Policy mismatch: proposal was generated under {proposal.policy} but approval "
            f"presented {policy}.",
        )

    active_policy = db.scalar(
        select(models.OperationalPolicySelection)
        .where(
            models.OperationalPolicySelection.incident_id == incident.id,
            models.OperationalPolicySelection.superseded.is_(False),
        )
        .order_by(models.OperationalPolicySelection.id.desc())
        .limit(1)
    )
    if active_policy is None or active_policy.policy != policy:
        _reject(
            db, incident, actor, proposal_uid,
            "The active operational policy selection does not match the proposal. "
            "Re-select the policy and regenerate the plan.",
        )

    assignments = list(db.scalars(
        select(models.RecoveryAssignment)
        .where(models.RecoveryAssignment.proposal_id == proposal.id)
        .order_by(models.RecoveryAssignment.id)
    ))
    if not assignments:
        _reject(db, incident, actor, proposal_uid, "Proposal contains no assignments.")

    # --- Stale-state + availability re-check (state may have changed since
    # generation). EVERY assignment type is re-validated BEFORE anything is
    # applied: feasible replacements, infeasible assignments without a
    # replacement, and NOT_APPLICABLE manager-review-only assignments. Any
    # mismatch rejects the whole approval and rolls back all changes.
    claimed: set[int] = set()
    for a in assignments:
        route = db.get(models.FleetRoute, a.route_id)
        if route is None or route.assigned_vehicle_id != a.original_vehicle_id:
            _reject(
                db, incident, actor, proposal_uid,
                f"Route {route.route_id if route else a.route_id} no longer has the original "
                "vehicle assigned; the plan is stale. Regenerate and review the plan again.",
            )
        if a.status != "PROPOSED" or not a.feasible or a.replacement_vehicle_id is None:
            continue
        spare = db.get(models.FleetVehicle, a.replacement_vehicle_id)
        if spare is None or spare.role != "SPARE" or spare.availability != "AVAILABLE":
            _reject(
                db, incident, actor, proposal_uid,
                f"Replacement vehicle for route {route.route_id} is no longer available "
                f"(expected an AVAILABLE spare).",
            )
        if spare.id in claimed:
            _reject(
                db, incident, actor, proposal_uid,
                f"Replacement vehicle {spare.asset_id} would be double-assigned.",
            )
        claimed.add(spare.id)

    # --- Apply state changes ------------------------------------------------
    previous_state: dict = {"routes": [], "vehicles": []}
    new_state: dict = {"routes": [], "vehicles": []}
    applied: list[dict] = []

    for a in assignments:
        route = db.get(models.FleetRoute, a.route_id)
        original = db.get(models.FleetVehicle, a.original_vehicle_id) if a.original_vehicle_id else None

        if a.status == "NOT_APPLICABLE":
            # MANAGER_REVIEW_ONLY: assignment retained, vehicle flagged.
            if original is not None:
                previous_state["vehicles"].append(_vehicle_state(original))
                original.operational_status = "FLAGGED_FOR_MANAGER_REVIEW"
                new_state["vehicles"].append(_vehicle_state(original))
                applied.append({
                    "route_id": route.route_id,
                    "action": "FLAGGED_FOR_MANAGER_REVIEW",
                    "vehicle": original.asset_id,
                })
            continue

        if not a.feasible or a.replacement_vehicle_id is None:
            # No feasible replacement: route is left without a safe assignment
            # proposal; the original vehicle is still removed from service
            # because the plan was approved as presented.
            if original is not None:
                previous_state["vehicles"].append(_vehicle_state(original))
                if a_disruption_is_match(a):
                    original.availability = "GROUNDED_RECALL"
                    original.operational_status = "GROUNDED_PENDING_REMEDY"
                else:
                    original.availability = "HELD_PENDING_VERIFICATION"
                    original.operational_status = "HELD_PENDING_VERIFICATION"
                new_state["vehicles"].append(_vehicle_state(original))
            previous_state["routes"].append(_route_state(route, original))
            route.assigned_vehicle_id = None
            new_state["routes"].append(_route_state(route, None))
            applied.append({
                "route_id": route.route_id,
                "action": "ROUTE_LEFT_UNASSIGNED",
                "vehicle": original.asset_id if original else None,
                "unresolved_issues": list(a.unresolved_issues or []),
            })
            continue

        spare = db.get(models.FleetVehicle, a.replacement_vehicle_id)
        previous_state["routes"].append(_route_state(route, original))
        previous_state["vehicles"].append(_vehicle_state(original))
        previous_state["vehicles"].append(_vehicle_state(spare))

        route.assigned_vehicle_id = spare.id
        spare.availability = "ASSIGNED_TO_ROUTE"
        spare.operational_status = "IN_SERVICE"
        if original is not None:
            if a_disruption_is_match(a):
                original.availability = "GROUNDED_RECALL"
                original.operational_status = "GROUNDED_PENDING_REMEDY"
            else:
                original.availability = "HELD_PENDING_VERIFICATION"
                original.operational_status = "HELD_PENDING_VERIFICATION"

        new_state["routes"].append(_route_state(route, spare))
        new_state["vehicles"].append(_vehicle_state(original))
        new_state["vehicles"].append(_vehicle_state(spare))
        applied.append({
            "route_id": route.route_id,
            "action": "ROUTE_REASSIGNED",
            "original_vehicle": original.asset_id if original else None,
            "replacement_vehicle": spare.asset_id,
        })
        a.status = "APPLIED"

    for a in assignments:
        if a.status == "PROPOSED" and (not a.feasible or a.replacement_vehicle_id is None):
            a.status = "APPLIED"

    proposal.status = "APPROVED"
    proposal.decided_at = datetime.now(timezone.utc)
    proposal.decided_by = actor
    incident.status = "PLAN_APPROVED"

    approval = models.Approval(
        proposal_id=proposal.id,
        incident_id=incident.id,
        approved_by=actor,
        policy=policy,
        result="APPROVED",
        plan_hash=plan_hash,
        previous_state=previous_state,
        new_state=new_state,
    )
    db.add(approval)
    db.flush()

    audit.record_event(
        db,
        actor=actor,
        action="RECOVERY_PLAN_APPROVED",
        incident_id=incident.id,
        entity_type="RecoveryProposal",
        entity_id=proposal.proposal_uid,
        previous_state=previous_state,
        new_state=new_state,
        policy=policy,
        proposal_id=proposal.proposal_uid,
        result="APPROVED",
        detail=(
            f"Manager approved recovery plan {proposal.proposal_uid} under policy {policy}. "
            f"{len(applied)} operational change(s) applied in one transaction."
        ),
    )
    for change in applied:
        audit.record_event(
            db,
            actor=actor,
            action=change["action"],
            incident_id=incident.id,
            entity_type="FleetRoute",
            entity_id=change["route_id"],
            previous_state={
                "route": next(
                    (r for r in previous_state["routes"] if r.get("route_id") == change["route_id"]), {}
                ),
            },
            new_state={
                "route": next(
                    (r for r in new_state["routes"] if r.get("route_id") == change["route_id"]), {}
                ),
                "change": change,
            },
            policy=policy,
            proposal_id=proposal.proposal_uid,
            result="APPLIED",
            detail=f"Applied as part of approved plan {proposal.proposal_uid}.",
        )

    db.commit()
    return {
        "approval_id": approval.id,
        "proposal_uid": proposal.proposal_uid,
        "policy": policy,
        "applied": applied,
        "previous_state": previous_state,
        "new_state": new_state,
    }


def a_disruption_is_match(a: models.RecoveryAssignment) -> bool:
    """A disrupted route whose original vehicle matched campaign criteria."""
    return "matches the reviewed campaign criteria" in (a.disruption_reason or "")


def _reject(db: Session, incident: models.Incident, actor: str, proposal_uid: str, reason: str) -> None:
    """Roll back and record the rejection (audit events are append-only)."""
    db.rollback()
    audit.record_event(
        db,
        actor=actor,
        action="RECOVERY_PLAN_APPROVAL_REJECTED",
        incident_id=incident.id,
        entity_type="RecoveryProposal",
        entity_id=proposal_uid,
        result="REJECTED",
        detail=reason,
        flush_only=False,
    )
    raise ApprovalRejected(reason)


def recompute_plan_hash(db: Session, proposal: models.RecoveryProposal) -> str:
    return compute_plan_hash(proposal, db)


__all__ = ["apply_approval", "ApprovalRejected", "HOLD", "REVIEW_ONLY"]
