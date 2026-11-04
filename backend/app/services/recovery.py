"""Deterministic route impact analysis and recovery proposal generation.

Design constraints:
* Pure Python, deterministic (greedy best-fit allocation, stable ordering).
* The engine PROPOSES; it never applies. Operational state changes happen
  only through the authenticated approve-plan endpoint.
* Policy-dependent handling of VERIFICATION_REQUIRED vehicles is explicit:
    - HOLD_PENDING_VERIFICATION: vehicle is removed from dispatch allocation
      and its route is treated as disrupted (needs a replacement).
    - MANAGER_REVIEW_ONLY: vehicle keeps its assignment, route is flagged for
      manager review, no replacement is allocated. Selecting this does NOT
  establish that the vehicle is safe.
* CAMPAIGN_CRITERIA_MATCH vehicles are always proposed for removal from
  their route (subject to manager approval of the plan).
* Spare vehicles that are themselves CAMPAIGN_CRITERIA_MATCH are never
  offered as replacements; VERIFICATION_REQUIRED spares are never offered
  either (proposing an unverified vehicle would be an implicit safety call).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models

HOLD = "HOLD_PENDING_VERIFICATION"
REVIEW_ONLY = "MANAGER_REVIEW_ONLY"


def _assessment_map(db: Session, incident_id: int) -> dict[int, models.VehicleAssessment]:
    rows = db.scalars(select(models.VehicleAssessment).where(models.VehicleAssessment.incident_id == incident_id))
    return {a.vehicle_id: a for a in rows}


def route_impact_rows(
    db: Session, incident: models.Incident, policy: str
) -> list[dict]:
    """Compute which routes are impacted under the selected policy."""
    assessments = _assessment_map(db, incident.id)
    routes = list(db.scalars(select(models.FleetRoute).where(models.FleetRoute.is_active.is_(True)).order_by(models.FleetRoute.route_id)))
    rows: list[dict] = []
    for route in routes:
        if route.assigned_vehicle_id is None:
            continue
        a = assessments.get(route.assigned_vehicle_id)
        if a is None:
            continue
        cls = a.classification
        if cls == "CAMPAIGN_CRITERIA_MATCH":
            rows.append({
                "route": route,
                "assessment": a,
                "impact": "DISRUPTED",
                "needs_replacement": True,
                "reason": (
                    "Assigned vehicle matches the reviewed campaign criteria and is proposed for "
                    "removal from service pending the manufacturer remedy."
                ),
            })
        elif cls == "VERIFICATION_REQUIRED":
            if policy == HOLD:
                rows.append({
                    "route": route,
                    "assessment": a,
                    "impact": "DISRUPTED",
                    "needs_replacement": True,
                    "reason": (
                        "Assigned vehicle requires verification and is excluded from the proposed "
                        "dispatch allocation under the selected HOLD_PENDING_VERIFICATION policy "
                        "(precautionary hold; this does not establish that the vehicle is affected)."
                    ),
                })
            else:
                rows.append({
                    "route": route,
                    "assessment": a,
                    "impact": "FLAGGED_REVIEW",
                    "needs_replacement": False,
                    "reason": (
                        "Vehicle flagged for manager review under the selected MANAGER_REVIEW_ONLY "
                        "policy; its existing assignment is retained. Retention does NOT establish "
                        "that the vehicle is safe or free of recall exposure."
                    ),
                })
    return rows


def _spare_candidates(db: Session, assessments: dict[int, models.VehicleAssessment]) -> list[models.FleetVehicle]:
    spares = list(db.scalars(
        select(models.FleetVehicle)
        .where(models.FleetVehicle.role == "SPARE", models.FleetVehicle.availability == "AVAILABLE")
        .order_by(models.FleetVehicle.asset_id)
    ))
    out = []
    for s in spares:
        a = assessments.get(s.id)
        if a is not None and a.classification in ("CAMPAIGN_CRITERIA_MATCH", "VERIFICATION_REQUIRED"):
            continue  # never propose an affected or unverified spare as a replacement
        out.append(s)
    return out


def _compatibility(route: models.FleetRoute, spare: models.FleetVehicle) -> dict:
    capacity_ok = spare.passenger_capacity >= route.required_capacity
    depot_ok = bool(spare.depot) and spare.depot == route.depot
    required = set(route.required_characteristics or [])
    offered = set(spare.features or [])
    characteristics_ok = required.issubset(offered)
    return {
        "capacity_ok": capacity_ok,
        "depot_ok": depot_ok,
        "characteristics_ok": characteristics_ok,
        "missing_characteristics": sorted(required - offered),
    }


def generate_recovery_plan(db: Session, incident: models.Incident) -> models.RecoveryProposal:
    """Build (or rebuild) the deterministic recovery proposal for an incident.

    Requires an active (non-superseded) operational policy selection - the
    system never decides policy itself.
    """
    policy_sel = db.scalar(
        select(models.OperationalPolicySelection)
        .where(
            models.OperationalPolicySelection.incident_id == incident.id,
            models.OperationalPolicySelection.superseded.is_(False),
        )
        .order_by(models.OperationalPolicySelection.id.desc())
        .limit(1)
    )
    if policy_sel is None:
        raise ValueError(
            "No operational policy selected. The fleet manager must explicitly choose how "
            "VERIFICATION_REQUIRED vehicles are handled before a recovery plan can be generated."
        )
    policy = policy_sel.policy
    assessments = _assessment_map(db, incident.id)
    if not assessments:
        raise ValueError("Fleet assessment has not been run for this incident yet.")

    impacts = route_impact_rows(db, incident, policy)
    spares = _spare_candidates(db, assessments)
    reserved: set[int] = set()

    # Supersede any previous pending proposals (history is preserved).
    previous = list(db.scalars(
        select(models.RecoveryProposal).where(
            models.RecoveryProposal.incident_id == incident.id,
            models.RecoveryProposal.status == "PENDING",
        )
    ))
    for p in previous:
        p.status = "SUPERSEDED"

    proposal = models.RecoveryProposal(
        proposal_uid=f"PROP-{incident.campaign_number}-{uuid.uuid4().hex[:8].upper()}",
        incident_id=incident.id,
        policy_selection_id=policy_sel.id,
        policy=policy,
        status="PENDING",
    )
    db.add(proposal)
    db.flush()

    feasible_count = 0
    infeasible_count = 0
    flagged_count = 0
    for impact in impacts:
        route: models.FleetRoute = impact["route"]
        original = db.get(models.FleetVehicle, route.assigned_vehicle_id)
        if not impact["needs_replacement"]:
            flagged_count += 1
            db.add(models.RecoveryAssignment(
                proposal_id=proposal.id,
                route_id=route.id,
                original_vehicle_id=original.id if original else None,
                replacement_vehicle_id=None,
                disruption_reason=impact["reason"],
                capacity_required=route.required_capacity,
                feasible=False,
                unresolved_issues=[],
                status="NOT_APPLICABLE",
            ))
            continue

        # Deterministic greedy best-fit: smallest sufficient surplus first,
        # asset_id as a stable tiebreaker.
        candidates = [s for s in spares if s.id not in reserved]
        scored = []
        for s in candidates:
            compat = _compatibility(route, s)
            if compat["capacity_ok"] and compat["depot_ok"] and compat["characteristics_ok"]:
                scored.append((s.passenger_capacity - route.required_capacity, s.asset_id, s, compat))
        scored.sort(key=lambda t: (t[0], t[1]))

        unresolved: list[str] = []
        if scored:
            _, _, spare, compat = scored[0]
            reserved.add(spare.id)
            feasible_count += 1
            db.add(models.RecoveryAssignment(
                proposal_id=proposal.id,
                route_id=route.id,
                original_vehicle_id=original.id if original else None,
                replacement_vehicle_id=spare.id,
                disruption_reason=impact["reason"],
                capacity_required=route.required_capacity,
                capacity_offered=spare.passenger_capacity,
                capacity_ok=True, depot_ok=True, characteristics_ok=True,
                feasible=True,
                unresolved_issues=[],
                status="PROPOSED",
            ))
        else:
            infeasible_count += 1
            # Explain exactly why nothing fits.
            if not candidates:
                unresolved.append("No spare vehicles remain unallocated in this plan.")
            else:
                cap_fails = [s.asset_id for s in candidates if s.passenger_capacity < route.required_capacity]
                depot_fails = [s.asset_id for s in candidates if s.depot != route.depot]
                char_fails = [
                    s.asset_id for s in candidates
                    if not set(route.required_characteristics or []).issubset(set(s.features or []))
                ]
                if cap_fails:
                    unresolved.append(
                        f"Insufficient capacity: {', '.join(cap_fails)} cannot seat the required "
                        f"{route.required_capacity} passengers."
                    )
                if depot_fails:
                    unresolved.append(
                        f"Depot incompatible: {', '.join(depot_fails)} are not stationed at depot "
                        f"'{route.depot}'."
                    )
                if char_fails:
                    unresolved.append(
                        f"Missing required characteristics: {', '.join(char_fails)} lack one or more of "
                        f"{sorted(route.required_characteristics or [])}."
                    )
                if not unresolved:
                    unresolved.append("No compatible spare available for this route.")
            db.add(models.RecoveryAssignment(
                proposal_id=proposal.id,
                route_id=route.id,
                original_vehicle_id=original.id if original else None,
                replacement_vehicle_id=None,
                disruption_reason=impact["reason"],
                capacity_required=route.required_capacity,
                feasible=False,
                unresolved_issues=unresolved,
                status="PROPOSED",
            ))

    proposal.summary = {
        "policy": policy,
        "routes_disrupted": feasible_count + infeasible_count,
        "routes_flagged_review_only": flagged_count,
        "routes_recoverable": feasible_count,
        "routes_not_recoverable": infeasible_count,
        "spares_considered": len(spares),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    proposal.plan_hash = compute_plan_hash(proposal, db)
    incident.status = "PLAN_PROPOSED"
    db.commit()
    return proposal


def compute_plan_hash(proposal: models.RecoveryProposal, db: Session) -> str:
    """Stable hash of the proposal content, used to detect stale plans at
    approval time (the manager must approve exactly what was generated)."""
    assignments = list(db.scalars(
        select(models.RecoveryAssignment)
        .where(models.RecoveryAssignment.proposal_id == proposal.id)
        .order_by(models.RecoveryAssignment.id)
    ))
    payload = {
        "policy": proposal.policy,
        "assignments": [
            {
                "route_db_id": a.route_id,
                "original_vehicle_db_id": a.original_vehicle_id,
                "replacement_vehicle_db_id": a.replacement_vehicle_id,
                "feasible": a.feasible,
                "status": a.status,
            }
            for a in assignments
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def latest_proposal(db: Session, incident_id: int) -> models.RecoveryProposal | None:
    return db.scalar(
        select(models.RecoveryProposal)
        .where(models.RecoveryProposal.incident_id == incident_id)
        .order_by(models.RecoveryProposal.id.desc())
        .limit(1)
    )
