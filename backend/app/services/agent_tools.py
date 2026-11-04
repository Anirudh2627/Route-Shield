"""Read-only tool registry for the RouteShield Incident Response Agent.

SAFETY INVARIANTS (enforced by tests, do not weaken):
  * Every tool here is READ-ONLY. This module never imports or exposes
    approval, policy-selection, plan-generation or any write path.
    `services/approval.apply_approval` remains the ONLY operational writer.
  * Tool arguments are validated against Pydantic models (which also generate
    the JSON schemas advertised to the model) before any execution.
  * Tools return deterministic serializations of persisted state produced by
    the existing services (evidence, assessment, recovery, audit) - the agent
    reuses business logic instead of duplicating it.
  * Results carry stable evidence reference ids (CRIT-n, ASSESS-n, DOC-sha16,
  proposal uids, source URLs) which the grounding validator in agent.py uses
  to verify every factual claim in the agent's final assessment.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .. import models
from . import audit as audit_svc
from . import evidence as evidence_svc
from . import recovery as recovery_svc

# Criterion types that carry narrative text, not applicability rules.
DESCRIPTIVE_TYPES = {
    "DEFECT_DESCRIPTION", "SAFETY_CONSEQUENCE", "REMEDY", "REMEDY_TYPE",
    "COMPONENT", "MANUFACTURER",
}

# Deterministic classification semantics - identical wording everywhere in the
# product so the agent can never "redefine" a category.
CLASSIFICATION_SEMANTICS = {
    "CAMPAIGN_CRITERIA_MATCH": (
        "All REQUIRED reviewed criteria match. This is NOT a VIN-level confirmation "
        "of an open recall."
    ),
    "VERIFICATION_REQUIRED": (
        "Critical evidence missing or ambiguous - the deterministic engine abstains. "
        "Absence of evidence is not evidence of safety."
    ),
    "OUTSIDE_REPORT_VERSION": (
        "Outside this report version's applicability criteria. This does NOT mean "
        "the vehicle is SAFE."
    ),
}

# Defense-in-depth: a registry name containing any of these tokens would be a
# write tool and is rejected at import time.
FORBIDDEN_NAME_TOKENS = (
    "approve", "apply", "assign", "update", "write", "delete", "create",
    "set_policy", "generate", "close", "mark", "mutate", "execute",
)


class ToolExecutionError(Exception):
    """Raised by a handler; the message is returned to the model as a tool
    error (never a stack trace, never internals)."""


def _iso(v) -> Optional[str]:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _short(s: Optional[str], n: int = 400) -> str:
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _incident(db: Session, incident_id: int) -> models.Incident:
    inc = db.get(models.Incident, incident_id)
    if inc is None:
        raise ToolExecutionError(f"No incident with id {incident_id} exists.")
    return inc


# ---------------------------------------------------------------------------
# Argument schemas (also generate the JSON schemas advertised to the model)
# ---------------------------------------------------------------------------


class IncidentArgs(BaseModel):
    incident_id: int = Field(ge=1, description="Id of the recall incident under investigation (1 for the seeded 26V111 incident).")


class RouteImpactArgs(BaseModel):
    incident_id: int = Field(ge=1, description="Id of the recall incident.")
    policy: Optional[Literal["HOLD_PENDING_VERIFICATION", "MANAGER_REVIEW_ONLY"]] = Field(
        default=None,
        description=(
            "Operational policy to preview. Omit to preview under the recorded active "
            "policy, or under BOTH supported policies when none has been recorded."
        ),
    )


class VehicleDetailArgs(BaseModel):
    incident_id: int = Field(ge=1, description="Id of the recall incident.")
    asset_id: str = Field(min_length=1, max_length=64, description="Fleet asset id, e.g. SYNTHETIC-BUS-001.")


class AuditTrailArgs(BaseModel):
    incident_id: int = Field(ge=1, description="Id of the recall incident.")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum number of most-recent events to return.")


# ---------------------------------------------------------------------------
# Tool handlers (all read-only)
# ---------------------------------------------------------------------------


def tool_get_recall_evidence(db: Session, args: IncidentArgs) -> dict:
    inc = _incident(db, args.incident_id)
    payload = evidence_svc.get_evidence_payload(db, inc)
    campaign = payload["campaign"]
    version = payload["report_version"]

    criteria = []
    for c in payload["criteria"]:
        if c.criterion_type in DESCRIPTIVE_TYPES:
            continue
        criteria.append({
            "ref": f"CRIT-{c.id}",
            "type": c.criterion_type,
            "label": c.label,
            "value": c.value,
            "source_document": c.source_document,
            "source_page": c.source_page,
            "source_snippet": _short(c.source_snippet, 300),
            "extraction_method": c.extraction_method,
            "review_status": c.review_status,
        })

    doc_ref = f"DOC-{version.document_sha256[:16]}" if version else None
    refs = [r for r in (doc_ref, version.report_url if version else None) if r]
    refs += [c["ref"] for c in criteria]

    return {
        "incident_id": inc.id,
        "campaign_number": inc.campaign_number,
        "manufacturer": campaign.manufacturer if campaign else "",
        "make": campaign.make if campaign else "",
        "model": campaign.model if campaign else "",
        "model_years": campaign.model_years if campaign else [],
        "component": campaign.component if campaign else "",
        "potential_units_affected": campaign.potential_units_affected if campaign else None,
        "manufacturer_recall_number": campaign.manufacturer_recall_number if campaign else "",
        "defect_summary": _short(campaign.summary, 600) if campaign else "",
        "safety_consequence": _short(campaign.consequence, 600) if campaign else "",
        "remedy": _short(campaign.remedy, 600) if campaign else "",
        "report_version": None if not version else {
            "ref": doc_ref,
            "report_url": version.report_url,
            "document_sha256": version.document_sha256,
            "page_count": version.page_count,
            "provenance": version.provenance,
            "review_status": version.review_status,
            "retrieved_at": _iso(version.retrieval_timestamp),
            "extracted_at": _iso(version.extraction_timestamp),
        },
        "applicability_criteria": criteria,
        "evidence_source": payload["evidence_source"],
        "evidence_banner": payload["evidence_banner"],
        "using_reviewed_fixture": payload["using_reviewed_fixture"],
        "classification_semantics": CLASSIFICATION_SEMANTICS,
        "evidence_refs": refs,
    }


def tool_get_fleet_assessment(db: Session, args: IncidentArgs) -> dict:
    inc = _incident(db, args.incident_id)
    rows = list(db.scalars(
        select(models.VehicleAssessment)
        .where(models.VehicleAssessment.incident_id == inc.id)
        .options(joinedload(models.VehicleAssessment.vehicle))
        .order_by(models.VehicleAssessment.id)
    ))
    if not rows:
        return {
            "incident_id": inc.id,
            "total_vehicles": 0,
            "counts": {},
            "vehicles": [],
            "note": "No deterministic assessment has been run for this incident yet.",
        }
    counts: dict[str, int] = {}
    vehicles = []
    for a in rows:
        counts[a.classification] = counts.get(a.classification, 0) + 1
        v = a.vehicle
        unknown_fields = sorted({
            str(c.get("criterion_label", "")) for c in (a.evidence_detail or [])
            if isinstance(c, dict) and c.get("result") in ("UNKNOWN", "CONFLICT")
        })
        vehicles.append({
            "asset_id": v.asset_id,
            "ref": f"ASSESS-{a.vehicle_id}",
            "make": v.make,
            "model": v.model,
            "model_year": v.model_year,
            "build_date": _iso(v.build_date),
            "brake_feature_code": v.brake_feature_code,
            "role": v.role,
            "availability": v.availability,
            "classification": a.classification,
            "reasons": [_short(r, 240) for r in (a.reasons or [])[:4]],
            "missing_or_conflicting_fields": unknown_fields,
            "vin_evidence_status": a.vin_evidence_status,
            "vin_evidence_note": _short(a.vin_evidence_note, 300),
        })
    return {
        "incident_id": inc.id,
        "engine": "deterministic rule engine v1.2.0 (no LLM involvement)",
        "assessed_at": _iso(max(a.assessed_at for a in rows)),
        "total_vehicles": len(rows),
        "counts": counts,
        "classification_semantics": CLASSIFICATION_SEMANTICS,
        "vehicles": vehicles,
        "evidence_refs": [v["ref"] for v in vehicles],
    }


def tool_preview_route_impact(db: Session, args: RouteImpactArgs) -> dict:
    inc = _incident(db, args.incident_id)
    active = db.scalars(
        select(models.OperationalPolicySelection)
        .where(
            models.OperationalPolicySelection.incident_id == inc.id,
            models.OperationalPolicySelection.superseded.is_(False),
        )
        .order_by(models.OperationalPolicySelection.id.desc())
    ).first()

    if args.policy:
        policies, policy_source = [args.policy], "requested by agent"
    elif active:
        policies, policy_source = [active.policy], f"active recorded policy (selected by {active.selected_by})"
    else:
        policies = ["HOLD_PENDING_VERIFICATION", "MANAGER_REVIEW_ONLY"]
        policy_source = (
            "NO policy recorded - previewed BOTH supported policies. A manager must "
            "explicitly select one before any plan can be generated."
        )

    amap = recovery_svc._assessment_map(db, inc.id)
    spares = recovery_svc._spare_candidates(db, amap)

    previews = []
    for pol in policies:
        rows = recovery_svc.route_impact_rows(db, inc, pol)
        routes_out = []
        unresolved = []
        for r in rows:
            route: models.FleetRoute = r["route"]
            a = r["assessment"]
            assigned = db.get(models.FleetVehicle, route.assigned_vehicle_id) if route.assigned_vehicle_id else None
            candidates = []
            if r["needs_replacement"]:
                for s in spares:
                    comp = recovery_svc._compatibility(route, s)
                    feasible = comp["capacity_ok"] and comp["depot_ok"] and comp["characteristics_ok"]
                    candidates.append({
                        "spare_asset_id": s.asset_id,
                        "capacity_offered": s.passenger_capacity,
                        "capacity_required": route.required_capacity,
                        **comp,
                        "feasible": feasible,
                    })
                if not any(c["feasible"] for c in candidates):
                    unresolved.append(
                        f"{route.route_id}: no eligible spare satisfies capacity/depot/equipment "
                        f"({len(candidates)} eligible spares checked)."
                    )
            routes_out.append({
                "route_id": route.route_id,
                "name": route.name,
                "depot": route.depot,
                "assigned_vehicle": assigned.asset_id if assigned else None,
                "assigned_vehicle_classification": a.classification,
                "impact": r["impact"],
                "needs_replacement": r["needs_replacement"],
                "reason": r["reason"],
                "replacement_candidates": candidates,
            })
        previews.append({
            "policy": pol,
            "impacted_routes": routes_out,
            "unresolved_constraints": unresolved,
        })

    return {
        "incident_id": inc.id,
        "policy_source": policy_source,
        "previews": previews,
        "eligible_spare_pool": [s.asset_id for s in spares],
        "note": (
            "READ-ONLY preview. Nothing has been modified. Operational changes happen "
            "only through the manager approval endpoint after an explicit policy "
            "selection and deterministic plan generation."
        ),
    }


def tool_get_available_spares(db: Session, args: IncidentArgs) -> dict:
    inc = _incident(db, args.incident_id)
    amap = recovery_svc._assessment_map(db, inc.id)
    eligible = {s.id for s in recovery_svc._spare_candidates(db, amap)}
    spares = list(db.scalars(
        select(models.FleetVehicle)
        .where(models.FleetVehicle.role == "SPARE")
        .order_by(models.FleetVehicle.asset_id)
    ))
    out = []
    for s in spares:
        a = amap.get(s.id)
        classification = a.classification if a else None
        reason = ""
        if s.id not in eligible:
            if classification in ("CAMPAIGN_CRITERIA_MATCH", "VERIFICATION_REQUIRED"):
                reason = f"excluded: assessment classification {classification} (affected or unverified vehicles are never replacements)"
            elif s.availability != "AVAILABLE":
                reason = f"excluded: availability {s.availability}"
            else:
                reason = "excluded by recovery engine eligibility rules"
        out.append({
            "asset_id": s.asset_id,
            "ref": f"ASSESS-{s.id}",
            "make": s.make,
            "model": s.model,
            "model_year": s.model_year,
            "passenger_capacity": s.passenger_capacity,
            "depot": s.depot,
            "features": s.features or [],
            "brake_feature_code": s.brake_feature_code,
            "availability": s.availability,
            "assessment_classification": classification,
            "eligible_as_replacement": s.id in eligible,
            "ineligibility_reason": reason,
        })
    return {
        "spares": out,
        "policy_note": (
            "Spares classified CAMPAIGN_CRITERIA_MATCH or VERIFICATION_REQUIRED are never "
            "proposed as replacements. Eligibility is decided by deterministic rules."
        ),
        "evidence_refs": [s["ref"] for s in out],
    }


def tool_get_open_verification_items(db: Session, args: IncidentArgs) -> dict:
    inc = _incident(db, args.incident_id)
    items: list[dict] = []

    rows = list(db.scalars(
        select(models.VehicleAssessment)
        .where(models.VehicleAssessment.incident_id == inc.id)
        .options(joinedload(models.VehicleAssessment.vehicle))
    ))
    for a in rows:
        if a.classification != "VERIFICATION_REQUIRED":
            continue
        unknown = sorted({
            str(c.get("criterion_label", "")) for c in (a.evidence_detail or [])
            if isinstance(c, dict) and c.get("result") in ("UNKNOWN", "CONFLICT")
        })
        items.append({
            "ref": f"VERIF-{a.vehicle_id}",
            "type": "VEHICLE_VERIFICATION",
            "asset_id": a.vehicle.asset_id,
            "status": "OPEN",
            "detail": (
                f"Deterministic assessment abstains: missing/conflicting recorded evidence for "
                f"{', '.join(unknown) if unknown else 'required criteria'}. Resolve by recording "
                f"authoritative values (manufacturer build records or physical inspection) and "
                f"re-running the assessment."
            ),
            "vin_evidence_status": a.vin_evidence_status,
        })

    version = inc.active_report_version
    if version and version.review_status != "REVIEWED":
        items.append({
            "ref": f"DOC-{version.document_sha256[:16]}",
            "type": "EVIDENCE_REVIEW",
            "status": "OPEN",
            "detail": (
                f"Report version {version.document_sha256[:16]}… is {version.review_status}; "
                "a human must review it before its criteria back matching."
            ),
        })

    proposal = recovery_svc.latest_proposal(db, inc.id)
    if proposal and proposal.status == "PENDING":
        items.append({
            "ref": proposal.proposal_uid,
            "type": "PENDING_APPROVAL",
            "status": "OPEN",
            "detail": (
                f"Recovery proposal {proposal.proposal_uid} (policy {proposal.policy}) awaits "
                "manager approval. No operational state changes until approved."
            ),
        })

    held = list(db.scalars(
        select(models.FleetVehicle).where(
            models.FleetVehicle.availability.in_(
                ["GROUNDED_RECALL", "FLAGGED_FOR_MANAGER_REVIEW", "HELD_PENDING_VERIFICATION"]
            )
        ).order_by(models.FleetVehicle.asset_id)
    ))
    for v in held:
        items.append({
            "ref": f"ASSESS-{v.id}",
            "type": "OPERATIONAL_HOLD",
            "asset_id": v.asset_id,
            "status": "OPEN",
            "detail": f"Vehicle availability is {v.availability}; operational status {v.operational_status}.",
        })

    return {
        "incident_id": inc.id,
        "items": items,
        "note": (
            "Verification items stay OPEN until authoritative evidence is recorded. "
            "Absence of evidence is not evidence of safety; the system never auto-resolves them."
        ),
        "evidence_refs": [i["ref"] for i in items],
    }


def tool_get_vehicle_detail(db: Session, args: VehicleDetailArgs) -> dict:
    _incident(db, args.incident_id)  # validates incident existence
    v = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == args.asset_id))
    if v is None:
        raise ToolExecutionError(
            f"No vehicle with asset_id '{args.asset_id}' exists in the fleet register. "
            "Do not invent asset ids; call get_fleet_assessment for the real register."
        )
    a = db.scalar(
        select(models.VehicleAssessment).where(
            models.VehicleAssessment.incident_id == args.incident_id,
            models.VehicleAssessment.vehicle_id == v.id,
        )
    )
    route = db.scalar(select(models.FleetRoute).where(models.FleetRoute.assigned_vehicle_id == v.id))
    events = list(db.scalars(
        select(models.AuditEvent)
        .where(models.AuditEvent.entity_id == v.asset_id)
        .order_by(models.AuditEvent.id.desc())
        .limit(10)
    ))
    return {
        "asset_id": v.asset_id,
        "ref": f"ASSESS-{v.id}",
        "synthetic": v.synthetic,
        "synthetic_label": v.synthetic_label,
        "make": v.make,
        "model": v.model,
        "model_year": v.model_year,
        "build_date": _iso(v.build_date),
        "brake_feature_code": v.brake_feature_code,
        "configuration": v.configuration or {},
        "features": v.features or [],
        "depot": v.depot,
        "passenger_capacity": v.passenger_capacity,
        "role": v.role,
        "availability": v.availability,
        "operational_status": v.operational_status,
        "vin_status": v.vin_status,
        "vin_note": (
            "Synthetic VIN-like identifiers are never transmitted to NHTSA/vPIC; "
            "VIN-level recall confirmation is unavailable for this fleet."
            if v.synthetic else ""
        ),
        "notes": _short(v.notes, 300),
        "assessment": None if a is None else {
            "classification": a.classification,
            "classification_semantics": CLASSIFICATION_SEMANTICS.get(a.classification, ""),
            "reasons": [_short(r, 300) for r in (a.reasons or [])],
            "per_criterion_evidence": a.evidence_detail or [],
            "vin_evidence_status": a.vin_evidence_status,
            "vin_evidence_note": _short(a.vin_evidence_note, 300),
            "assessed_at": _iso(a.assessed_at),
        },
        "assigned_route": None if route is None else {
            "route_id": route.route_id, "name": route.name, "depot": route.depot,
        },
        "recent_audit_events": [
            {"timestamp": _iso(e.timestamp), "actor": e.actor, "action": e.action, "result": e.result,
             "detail": _short(e.detail, 200)}
            for e in events
        ],
        "evidence_refs": [f"ASSESS-{v.id}"] + ([route.route_id] if route else []),
    }


def tool_get_audit_trail(db: Session, args: AuditTrailArgs) -> dict:
    inc = _incident(db, args.incident_id)
    events = audit_svc.list_events(db, incident_id=inc.id, limit=args.limit)
    out = []
    for e in events:
        out.append({
            "timestamp": _iso(e.timestamp),
            "actor": e.actor,
            "action": e.action,
            "entity_type": e.entity_type,
            "entity_id": e.entity_id,
            "result": e.result,
            "policy": e.policy,
            "proposal_uid": e.proposal_id,
            "detail": _short(e.detail, 300),
            "previous_state": _state_summary(e.previous_state),
            "new_state": _state_summary(e.new_state),
        })
    return {
        "incident_id": inc.id,
        "events": out,
        "note": "Append-only audit log. No endpoint can modify or delete these events.",
    }


def _state_summary(state: Optional[dict]) -> Optional[dict]:
    if not state:
        return None
    try:
        text = json.dumps(state, default=str)
    except (TypeError, ValueError):
        return {"_unserializable": True}
    if len(text) <= 400:
        return state
    return {"_truncated": text[:400] + "…"}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Session, BaseModel], dict]
    timeout_seconds: float = 20.0


def _spec(name: str, description: str, args_model: type[BaseModel], handler) -> ToolSpec:
    lowered = name.lower()
    for token in FORBIDDEN_NAME_TOKENS:
        if token in lowered:
            raise RuntimeError(f"Tool name '{name}' contains forbidden write-token '{token}'")
    return ToolSpec(name=name, description=description, args_model=args_model, handler=handler)


REGISTRY: dict[str, ToolSpec] = {
    s.name: s
    for s in (
        _spec(
            "get_recall_evidence",
            "Retrieve the official recall evidence for the incident: NHTSA campaign data, the Part 573 "
            "report version (URL, SHA-256, provenance, review status, retrieval timestamps), the "
            "reviewed applicability criteria with source pages and verbatim snippets, and the current "
            "evidence source (live extraction vs manually reviewed fixture).",
            IncidentArgs,
            tool_get_recall_evidence,
        ),
        _spec(
            "get_fleet_assessment",
            "Retrieve the deterministic per-vehicle classifications for the incident "
            "(CAMPAIGN_CRITERIA_MATCH / VERIFICATION_REQUIRED / OUTSIDE_REPORT_VERSION) with reasons, "
            "missing fields and VIN evidence status. Classifications are produced only by the "
            "deterministic engine - never re-derive or override them.",
            IncidentArgs,
            tool_get_fleet_assessment,
        ),
        _spec(
            "preview_route_impact",
            "Preview which routes are disrupted or flagged under an operational policy, with replacement "
            "candidates (capacity/depot/equipment checks) and unresolved constraints. READ-ONLY: applies "
            "nothing. Omit 'policy' to use the recorded active policy or preview both supported policies.",
            RouteImpactArgs,
            tool_preview_route_impact,
        ),
        _spec(
            "get_available_spares",
            "List spare vehicles with the attributes the recovery engine matches on (capacity, depot, "
            "equipment features, availability) and each spare's eligibility to serve as a replacement, "
            "including deterministic exclusion reasons.",
            IncidentArgs,
            tool_get_available_spares,
        ),
        _spec(
            "get_open_verification_items",
            "List outstanding verification and repair-related work: vehicles awaiting verification "
            "evidence, report versions awaiting human review, recovery proposals awaiting manager "
            "approval, and vehicles under operational hold - each with status and references.",
            IncidentArgs,
            tool_get_open_verification_items,
        ),
        _spec(
            "get_vehicle_detail",
            "Retrieve the full record for ONE vehicle by asset id: configuration, assessment with "
            "per-criterion evidence, route assignment and recent audit events. Use only for vehicles "
            "that appeared in other tool results.",
            VehicleDetailArgs,
            tool_get_vehicle_detail,
        ),
        _spec(
            "get_audit_trail",
            "Retrieve the append-only audit trail for the incident (most recent first): who did what, "
            "when, with which policy/proposal and outcome.",
            AuditTrailArgs,
            tool_get_audit_trail,
        ),
    )
}


def openai_tool_defs() -> list[dict]:
    """Tool definitions in OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.args_model.model_json_schema(),
            },
        }
        for s in REGISTRY.values()
    ]


def parse_args(name: str, raw: dict) -> BaseModel:
    """Validate model-supplied arguments; raises ValueError with a model-safe message."""
    spec = REGISTRY.get(name)
    if spec is None:
        raise ValueError(f"Unknown tool '{name}'. Allowed tools: {sorted(REGISTRY)}.")
    try:
        return spec.args_model.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise ValueError(f"Invalid arguments for '{name}': {exc}") from exc


def execute(db: Session, name: str, args: BaseModel) -> dict:
    spec = REGISTRY[name]
    return spec.handler(db, args)  # type: ignore[arg-type]
