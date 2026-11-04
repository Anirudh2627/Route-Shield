"""SQLAlchemy ORM models - the RouteShield data model.

Design rules enforced by this schema:
* Evidence provenance is first-class: every extracted criterion carries its
  source document, page number, snippet, extraction method, confidence and
  review status.
* Report versions are immutable records; a changed report creates a NEW
  version marked as requiring review instead of overwriting reviewed criteria.
* The audit log is append-only at the application level: no router or service
  ever updates or deletes AuditEvent rows.
* Operational state (routes, vehicle assignments) is only mutated through the
  authenticated approval flow (services/approval.py).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Recall evidence
# ---------------------------------------------------------------------------


class RecallCampaign(Base):
    """A recall campaign as published by NHTSA (campaign number is unique)."""

    __tablename__ = "recall_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    manufacturer: Mapped[str] = mapped_column(String(255))
    make: Mapped[str] = mapped_column(String(128), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    model_years: Mapped[list] = mapped_column(JSON, default=list)
    component: Mapped[str] = mapped_column(String(512), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    consequence: Mapped[str] = mapped_column(Text, default="")
    remedy: Mapped[str] = mapped_column(Text, default="")
    potential_units_affected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manufacturer_recall_number: Mapped[str] = mapped_column(String(64), default="")
    report_received_date: Mapped[str] = mapped_column(String(32), default="")
    api_source_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    api_retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    report_versions: Mapped[list[RecallReportVersion]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", order_by="RecallReportVersion.id"
    )


class RecallReportVersion(Base):
    """One retrieved version of the official Part 573 report document.

    Versions are immutable. If the published document changes (different
    SHA-256), a new version row is created with status PENDING_REVIEW and the
    previously REVIEWED version is kept intact.
    """

    __tablename__ = "recall_report_versions"
    __table_args__ = (UniqueConstraint("campaign_id", "document_sha256", name="uq_report_version_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("recall_campaigns.id"), index=True)
    report_url: Mapped[str] = mapped_column(String(1024))
    document_sha256: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submission_date: Mapped[str] = mapped_column(String(64), default="")
    amendment_info: Mapped[str] = mapped_column(String(255), default="")
    retrieval_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    extraction_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # LIVE_PDF_EXTRACTED | MANUALLY_REVIEWED_FIXTURE | CACHED_PDF_EXTRACTED
    provenance: Mapped[str] = mapped_column(String(64))
    # REVIEWED | PENDING_REVIEW  (changed document => PENDING_REVIEW, never silent replace)
    review_status: Mapped[str] = mapped_column(String(32), default="PENDING_REVIEW")
    notes: Mapped[str] = mapped_column(Text, default="")

    campaign: Mapped[RecallCampaign] = relationship(back_populates="report_versions")
    criteria: Mapped[list[ExtractedCriterion]] = relationship(
        back_populates="report_version", cascade="all, delete-orphan", order_by="ExtractedCriterion.id"
    )


class ExtractedCriterion(Base):
    """A single applicability criterion extracted from a report version.

    Every criterion preserves its evidence: page number, verbatim snippet,
    extraction method, confidence and human review status.
    """

    __tablename__ = "extracted_criteria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_version_id: Mapped[int] = mapped_column(
        ForeignKey("recall_report_versions.id"), index=True
    )
    # MAKE | MODEL | PRODUCT_TYPE | MODEL_YEAR_RANGE | PRODUCTION_DATE_RANGE |
    # FEATURE_CODE | EXCLUSION | POPULATION
    criterion_type: Mapped[str] = mapped_column(String(48), index=True)
    label: Mapped[str] = mapped_column(String(255))
    value: Mapped[dict] = mapped_column(JSON)
    source_document: Mapped[str] = mapped_column(String(1024))
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_snippet: Mapped[str] = mapped_column(Text, default="")
    # RULE_BASED_PDF | LLM_ASSISTED | MANUALLY_REVIEWED
    extraction_method: Mapped[str] = mapped_column(String(48))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # PENDING_REVIEW | REVIEWED | NEEDS_REVIEW
    review_status: Mapped[str] = mapped_column(String(32), default="PENDING_REVIEW")
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    report_version: Mapped[RecallReportVersion] = relationship(back_populates="criteria")


# ---------------------------------------------------------------------------
# Fleet (all seeded records are synthetic and flagged as such)
# ---------------------------------------------------------------------------


class FleetVehicle(Base):
    __tablename__ = "fleet_vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    synthetic_label: Mapped[str] = mapped_column(
        String(255), default="SYNTHETIC DEMONSTRATION DATA — NOT REAL VEHICLES"
    )
    manufacturer: Mapped[str] = mapped_column(String(255), default="")
    make: Mapped[str] = mapped_column(String(128), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    model_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    build_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    configuration: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    brake_feature_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # NOT_PERFORMED | DECODED_VPIC | INDIVIDUAL_RECALL_CHECK_DONE | UNAVAILABLE
    vin_status: Mapped[str] = mapped_column(String(48), default="NOT_PERFORMED")
    depot: Mapped[str] = mapped_column(String(64), default="")
    passenger_capacity: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(32), default="ROUTE_BUS")  # ROUTE_BUS | SPARE
    # AVAILABLE | ASSIGNED_TO_ROUTE | IN_MAINTENANCE | GROUNDED_RECALL |
    # FLAGGED_FOR_MANAGER_REVIEW
    availability: Mapped[str] = mapped_column(String(48), default="AVAILABLE")
    operational_status: Mapped[str] = mapped_column(String(64), default="IN_SERVICE")
    features: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["wheelchair_lift"]
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class FleetRoute(Base):
    __tablename__ = "fleet_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    depot: Mapped[str] = mapped_column(String(64), default="")
    required_capacity: Mapped[int] = mapped_column(Integer, default=0)
    required_characteristics: Mapped[list] = mapped_column(JSON, default=list)
    assigned_vehicle_id: Mapped[int | None] = mapped_column(
        ForeignKey("fleet_vehicles.id"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    assigned_vehicle: Mapped[FleetVehicle | None] = relationship()


# ---------------------------------------------------------------------------
# Incident workflow
# ---------------------------------------------------------------------------


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (UniqueConstraint("campaign_number", name="uq_incident_campaign"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_number: Mapped[str] = mapped_column(String(32), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("recall_campaigns.id"), nullable=True)
    active_report_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("recall_report_versions.id"), nullable=True
    )
    # OPEN | EVIDENCE_LOADED | ASSESSED | PLAN_PROPOSED | PLAN_APPROVED
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    # Evidence provenance currently backing this incident:
    # LIVE_EXTRACTION | MANUALLY_REVIEWED_FIXTURE
    evidence_source: Mapped[str] = mapped_column(String(48), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    campaign: Mapped[RecallCampaign | None] = relationship()
    active_report_version: Mapped[RecallReportVersion | None] = relationship()


class VehicleAssessment(Base):
    """Deterministic classification of one vehicle against one incident."""

    __tablename__ = "vehicle_assessments"
    __table_args__ = (
        UniqueConstraint("incident_id", "vehicle_id", name="uq_assessment_incident_vehicle"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("fleet_vehicles.id"), index=True)
    # CAMPAIGN_CRITERIA_MATCH | VERIFICATION_REQUIRED | OUTSIDE_REPORT_VERSION
    classification: Mapped[str] = mapped_column(String(48), index=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    # Per-criterion evidence: [{criterion, page, vehicle_value, result}]
    evidence_detail: Mapped[list] = mapped_column(JSON, default=list)
    # VIN_STATUS_UNAVAILABLE | VIN_DECODED_VPIC | VIN_RECALL_CHECK_DONE
    vin_evidence_status: Mapped[str] = mapped_column(String(48), default="VIN_STATUS_UNAVAILABLE")
    vin_evidence_note: Mapped[str] = mapped_column(Text, default="")
    report_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("recall_report_versions.id"), nullable=True
    )
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    vehicle: Mapped[FleetVehicle] = relationship()


class OperationalPolicySelection(Base):
    """Explicit fleet-manager policy for VERIFICATION_REQUIRED vehicles."""

    __tablename__ = "operational_policy_selections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    # HOLD_PENDING_VERIFICATION | MANAGER_REVIEW_ONLY
    policy: Mapped[str] = mapped_column(String(48))
    selected_by: Mapped[str] = mapped_column(String(128))
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledgement_text: Mapped[str] = mapped_column(Text, default="")


class RecoveryProposal(Base):
    """A deterministic recovery plan awaiting manager approval.

    The LLM cannot create, mutate or approve these rows - only the
    generate-recovery-plan endpoint (deterministic engine) creates them and
    only the approve-plan endpoint (authenticated manager) applies them.
    """

    __tablename__ = "recovery_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    policy_selection_id: Mapped[int | None] = mapped_column(
        ForeignKey("operational_policy_selections.id"), nullable=True
    )
    policy: Mapped[str] = mapped_column(String(48), default="")
    # PENDING | APPROVED | REJECTED | SUPERSEDED
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    plan_hash: Mapped[str] = mapped_column(String(64), default="")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    assignments: Mapped[list[RecoveryAssignment]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan", order_by="RecoveryAssignment.id"
    )


class RecoveryAssignment(Base):
    """One route-level element of a recovery proposal."""

    __tablename__ = "recovery_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("recovery_proposals.id"), index=True)
    route_id: Mapped[int] = mapped_column(ForeignKey("fleet_routes.id"))
    original_vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("fleet_vehicles.id"), nullable=True)
    replacement_vehicle_id: Mapped[int | None] = mapped_column(
        ForeignKey("fleet_vehicles.id"), nullable=True
    )
    disruption_reason: Mapped[str] = mapped_column(Text, default="")
    capacity_required: Mapped[int] = mapped_column(Integer, default=0)
    capacity_offered: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capacity_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    depot_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    characteristics_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feasible: Mapped[bool] = mapped_column(Boolean, default=False)
    unresolved_issues: Mapped[list] = mapped_column(JSON, default=list)
    # PROPOSED | APPLIED | NOT_APPLICABLE
    status: Mapped[str] = mapped_column(String(32), default="PROPOSED")

    proposal: Mapped[RecoveryProposal] = relationship(back_populates="assignments")
    route: Mapped[FleetRoute] = relationship()
    original_vehicle: Mapped[FleetVehicle | None] = relationship(foreign_keys=[original_vehicle_id])
    replacement_vehicle: Mapped[FleetVehicle | None] = relationship(foreign_keys=[replacement_vehicle_id])


class Approval(Base):
    """Record of a manager approval decision on a proposal."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("recovery_proposals.id"), index=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    approved_by: Mapped[str] = mapped_column(String(128))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    policy: Mapped[str] = mapped_column(String(48))
    result: Mapped[str] = mapped_column(String(32))  # APPROVED | REJECTED
    plan_hash: Mapped[str] = mapped_column(String(64), default="")
    previous_state: Mapped[dict] = mapped_column(JSON, default=dict)
    new_state: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditEvent(Base):
    """Append-only audit log.

    NOTHING in this codebase updates or deletes these rows. There is no API
    endpoint that mutates audit events.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    previous_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    policy: Mapped[str | None] = mapped_column(String(48), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    engine_version: Mapped[str] = mapped_column(String(32), default="")
    campaign_number: Mapped[str] = mapped_column(String(32), default="")
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    confusion_matrix: Mapped[dict] = mapped_column(JSON, default=dict)
    cases: Mapped[list] = mapped_column(JSON, default=list)
    label: Mapped[str] = mapped_column(
        String(255), default="SYNTHETIC TEST BENCHMARK — NOT PRODUCTION FLEET ACCURACY"
    )


class VINDecodeCache(Base):
    """Cache of human-triggered vPIC decodes. Synthetic VINs are never sent
    to vPIC and therefore never appear here."""

    __tablename__ = "vin_decode_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vin: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    requested_model_year: Mapped[str] = mapped_column(String(8), default="")
    decoded_values: Mapped[dict] = mapped_column(JSON, default=dict)
    unavailable_fields: Mapped[list] = mapped_column(JSON, default=list)
    decoded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(64), default="vpic.nhtsa.dot.gov")
    requested_by: Mapped[str] = mapped_column(String(128), default="")


class ManagerUser(Base):
    __tablename__ = "manager_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(32), default="FLEET_MANAGER")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# AI incident-response agent (investigations are READ-ONLY; the agent never
# writes operational state - the approval endpoint remains the only writer)
# ---------------------------------------------------------------------------


class InvestigationSession(Base):
    """One bounded agent investigation: objective in, grounded assessment out.

    Statuses: INVESTIGATING | COMPLETED | FAILED. The full tool trace lives in
    InvestigationStep rows, persisted incrementally so the UI can follow live.
    """

    __tablename__ = "investigation_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_uid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    objective: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(128), default="")
    # INVESTIGATING | COMPLETED | FAILED
    status: Mapped[str] = mapped_column(String(32), default="INVESTIGATING", index=True)
    # MODEL_STOPPED | MAX_ITERATIONS | PROVIDER_ERROR | MODEL_NOT_CONFIGURED |
    # INVALID_FINAL_OUTPUT | GROUNDING_VALIDATION_FAILED | TIME_BUDGET_EXCEEDED
    stop_reason: Mapped[str] = mapped_column(String(48), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    iterations_used: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Sanitized provider diagnostics (never contains credentials)
    provider_error: Mapped[str] = mapped_column(Text, default="")
    # Validated structured final assessment (AgentFinalAssessment dump)
    final_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    grounding_violations: Mapped[list] = mapped_column(JSON, default=list)

    incident: Mapped[Incident] = relationship()
    steps: Mapped[list[InvestigationStep]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="InvestigationStep.seq"
    )


class InvestigationStep(Base):
    """One step of the agent trace: model narration, tool call, tool result,
    validation outcome or terminal error. Append-only while investigating."""

    __tablename__ = "investigation_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("investigation_sessions.id"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    # MODEL_MESSAGE | TOOL_CALL | VALIDATION | FINAL | ERROR
    kind: Mapped[str] = mapped_column(String(32))
    tool_name: Mapped[str] = mapped_column(String(64), default="")
    tool_args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # OK | INVALID_TOOL_CALL | INVALID_ARGS | TOOL_ERROR | PROVIDER_ERROR | TIMEOUT
    status: Mapped[str] = mapped_column(String(32), default="")
    # Model's one-sentence rationale for the action (not hidden chain-of-thought)
    explanation: Mapped[str] = mapped_column(Text, default="")
    # Structured tool result (capped) or error/validation detail
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[InvestigationSession] = relationship(back_populates="steps")
