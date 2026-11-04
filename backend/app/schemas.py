"""Pydantic schemas: API request/response models and the strict LLM
extraction schema.

The LLM extraction schema (`LLMRecallExtraction`) is deliberately strict:
the model must return structured JSON, every field is validated by Pydantic,
and any invalid output is rejected outright (falling back to rule-based
extraction). The LLM never returns classifications or operational actions.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Classification / policy vocabularies (single source of truth)
# ---------------------------------------------------------------------------

Classification = Literal[
    "CAMPAIGN_CRITERIA_MATCH",
    "VERIFICATION_REQUIRED",
    "OUTSIDE_REPORT_VERSION",
]

VinEvidenceStatus = Literal[
    "VIN_STATUS_UNAVAILABLE",
    "VIN_DECODED_VPIC",
    "VIN_RECALL_CHECK_DONE",
]

OperationalPolicy = Literal["HOLD_PENDING_VERIFICATION", "MANAGER_REVIEW_ONLY"]

CLASSIFICATION_EXPLANATIONS: dict[str, str] = {
    "CAMPAIGN_CRITERIA_MATCH": (
        "All available required campaign criteria match the reviewed report. "
        "This is a criteria match only — it is NOT a VIN-level confirmation that "
        "this individual vehicle has an open recall."
    ),
    "VERIFICATION_REQUIRED": (
        "Critical evidence is missing or ambiguous. The system abstains from "
        "concluding either way — absence of evidence is not evidence of safety."
    ),
    "OUTSIDE_REPORT_VERSION": (
        "The vehicle falls outside the applicability criteria documented in THIS "
        "report version. This does NOT mean the vehicle is safe or free of other "
        "recalls — it means this report's criteria do not cover it."
    ),
}

POLICY_EXPLANATIONS: dict[str, str] = {
    "HOLD_PENDING_VERIFICATION": (
        "Vehicles classified VERIFICATION_REQUIRED are excluded from the proposed "
        "dispatch allocation until further verification. This is a precautionary "
        "hold; it does not establish that the vehicle is affected."
    ),
    "MANAGER_REVIEW_ONLY": (
        "Vehicles classified VERIFICATION_REQUIRED are flagged for a manager's "
        "review WITHOUT automatically removing their existing assignment. "
        "Selecting this option does NOT establish that the vehicle is safe or "
        "free of recall exposure."
    ),
}


# ---------------------------------------------------------------------------
# Strict schema for LLM-assisted extraction (Vultr Serverless Inference)
# ---------------------------------------------------------------------------


class LLMProductionRange(BaseModel):
    model_year: int = Field(ge=1980, le=2100)
    production_start: str = Field(description="Date as printed in the report, e.g. 'Feb 05, 2024'")
    production_end: str
    units: Optional[int] = Field(default=None, ge=0)
    source_page: Optional[int] = Field(default=None, ge=1)
    source_snippet: str = Field(default="", max_length=1200)


class LLMRecallExtraction(BaseModel):
    """Strict structured output the LLM must produce from report text.

    The LLM extracts *what the document says*. It must not invent criteria;
    fields it cannot support from the text must be left null/empty.
    """

    manufacturer: Optional[str] = Field(default=None, max_length=255)
    manufacturer_recall_number: Optional[str] = Field(default=None, max_length=64)
    submission_date: Optional[str] = Field(default=None, max_length=64)
    make: Optional[str] = Field(default=None, max_length=128)
    model: Optional[str] = Field(default=None, max_length=128)
    product_type: Optional[str] = Field(default=None, max_length=128)
    total_units_affected: Optional[int] = Field(default=None, ge=0)
    production_ranges: list[LLMProductionRange] = Field(default_factory=list, max_length=40)
    overall_build_start: Optional[str] = Field(default=None, max_length=64)
    overall_build_end: Optional[str] = Field(default=None, max_length=64)
    feature_codes: list[str] = Field(default_factory=list, max_length=20)
    feature_code_context: Optional[str] = Field(default=None, max_length=1200)
    exclusions: list[str] = Field(default_factory=list, max_length=20)
    defect_description: Optional[str] = Field(default=None, max_length=4000)
    safety_consequence: Optional[str] = Field(default=None, max_length=4000)
    remedy: Optional[str] = Field(default=None, max_length=4000)
    components: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("feature_codes", mode="before")
    @classmethod
    def _coerce_codes(cls, v):
        if v is None:
            return []
        return v


# ---------------------------------------------------------------------------
# API response models
# ---------------------------------------------------------------------------


class CriterionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    criterion_type: str
    label: str
    value: dict
    source_document: str
    source_page: Optional[int]
    source_snippet: str
    extraction_method: str
    confidence: float
    review_status: str


class ReportVersionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    report_url: str
    document_sha256: str
    page_count: Optional[int]
    submission_date: str
    amendment_info: str
    retrieval_timestamp: datetime
    extraction_timestamp: Optional[datetime]
    provenance: str
    review_status: str
    notes: str


class CampaignOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    campaign_number: str
    manufacturer: str
    make: str
    model: str
    model_years: list
    component: str
    summary: str
    consequence: str
    remedy: str
    potential_units_affected: Optional[int]
    manufacturer_recall_number: str
    report_received_date: str
    api_retrieved_at: Optional[datetime]


class EvidenceOut(BaseModel):
    incident_id: int
    campaign: Optional[CampaignOut]
    report_version: Optional[ReportVersionOut]
    criteria: list[CriterionOut]
    evidence_source: str  # LIVE_EXTRACTION | MANUALLY_REVIEWED_FIXTURE
    evidence_banner: str  # human-facing provenance statement
    using_reviewed_fixture: bool
    live_retrieval_error: Optional[str] = None


class VehicleOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    asset_id: str
    synthetic: bool
    synthetic_label: str
    manufacturer: str
    make: str
    model: str
    model_year: Optional[int]
    build_date: Optional[date]
    configuration: Optional[dict]
    brake_feature_code: Optional[str]
    vin: Optional[str]
    vin_status: str
    depot: str
    passenger_capacity: int
    role: str
    availability: str
    operational_status: str
    features: list
    notes: str


class RouteOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    route_id: str
    name: str
    depot: str
    required_capacity: int
    required_characteristics: list
    assigned_vehicle_id: Optional[int]
    is_active: bool
    synthetic: bool


class AssessmentOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    vehicle_id: int
    classification: str
    classification_explanation: str = ""
    reasons: list
    evidence_detail: list
    vin_evidence_status: str
    vin_evidence_note: str
    assessed_at: datetime
    vehicle: Optional[VehicleOut] = None


class FleetAssessmentSummary(BaseModel):
    incident_id: int
    assessed_at: Optional[datetime]
    total_vehicles: int
    counts: dict[str, int]
    vin_status_counts: dict[str, int]
    assessments: list[AssessmentOut]
    banner: str = "SYNTHETIC DEMONSTRATION DATA — NOT REAL VEHICLES"


class PolicySelectionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    policy: str
    policy_explanation: str = ""
    selected_by: str
    selected_at: datetime
    superseded: bool
    acknowledgement_text: str


class AssignmentOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    route_id: int
    route: Optional[RouteOut] = None
    original_vehicle_id: Optional[int]
    original_vehicle: Optional[VehicleOut] = None
    replacement_vehicle_id: Optional[int]
    replacement_vehicle: Optional[VehicleOut] = None
    disruption_reason: str
    capacity_required: int
    capacity_offered: Optional[int]
    capacity_ok: Optional[bool]
    depot_ok: Optional[bool]
    characteristics_ok: Optional[bool]
    feasible: bool
    unresolved_issues: list
    status: str


class ProposalOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    proposal_uid: str
    incident_id: int
    policy: str
    policy_explanation: str = ""
    status: str
    plan_hash: str
    summary: dict
    generated_at: datetime
    decided_at: Optional[datetime]
    decided_by: Optional[str]
    assignments: list[AssignmentOut] = []


class IncidentOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    campaign_number: str
    status: str
    evidence_source: str
    created_at: datetime
    updated_at: datetime
    campaign: Optional[CampaignOut] = None
    active_report_version: Optional[ReportVersionOut] = None
    active_policy: Optional[PolicySelectionOut] = None
    latest_proposal: Optional[ProposalOut] = None


class AuditEventOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    event_uid: str
    timestamp: datetime
    actor: str
    incident_id: Optional[int]
    action: str
    entity_type: str
    entity_id: str
    previous_state: Optional[dict]
    new_state: Optional[dict]
    policy: Optional[str]
    proposal_id: Optional[str]
    result: str
    detail: str


class ApprovalOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    proposal_id: int
    incident_id: int
    approved_by: str
    approved_at: datetime
    policy: str
    result: str
    plan_hash: str
    previous_state: dict
    new_state: dict


# ---------------------------------------------------------------------------
# API request models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    username: str
    display_name: str
    role: str
    expires_at: datetime


class SetPolicyRequest(BaseModel):
    policy: OperationalPolicy
    acknowledgement: bool = Field(
        description="Manager acknowledges the policy explanation, including that "
        "MANAGER_REVIEW_ONLY does not establish that a vehicle is safe."
    )

    @field_validator("acknowledgement")
    @classmethod
    def _must_ack(cls, v: bool) -> bool:
        if not v:
            raise ValueError("Manager must acknowledge the policy explanation before selecting it.")
        return v


class ApprovePlanRequest(BaseModel):
    proposal_uid: str = Field(min_length=1)
    plan_hash: str = Field(min_length=16, max_length=64)
    policy: OperationalPolicy


class DecodeVinRequest(BaseModel):
    vin: str = Field(min_length=11, max_length=17)
    model_year: Optional[str] = Field(default=None, max_length=4)

    @field_validator("vin")
    @classmethod
    def _vin_charset(cls, v: str) -> str:
        v = v.strip().upper()
        allowed = set("ABCDEFGHJKLMNPRSTUVWXYZ0123456789")  # VINs exclude I, O, Q
        bad = [c for c in v if c not in allowed]
        if bad:
            raise ValueError(f"VIN contains invalid characters: {', '.join(sorted(set(bad)))}")
        if len(v) != 17:
            raise ValueError("A full VIN must be 17 characters.")
        return v


class VINDecodeOut(BaseModel):
    vin: str
    requested_model_year: str
    decoded_values: dict
    unavailable_fields: list
    decoded_at: datetime
    source: str
    requested_by: str
    cached: bool = False
    limitations: str = (
        "vPIC decodes the VIN string only. It does NOT confirm recall status and may not "
        "return brake configuration or feature codes; missing fields remain UNKNOWN and are "
        "never inferred. A decode result is never automatically converted into recall "
        "confirmation."
    )


class BenchmarkRunOut(BaseModel):
    model_config = {"from_attributes": True}

    id: Optional[int] = None
    run_at: datetime
    engine_version: str
    campaign_number: str
    sample_count: int
    duration_ms: int
    metrics: dict
    confusion_matrix: dict
    cases: list = []
    label: str


class HealthOut(BaseModel):
    status: str
    app: str
    version: str
    environment: str
    database: str
    utc_time: datetime
    llm_configured: bool
    nhtsa_reachable: Optional[bool] = None


class RefreshResult(BaseModel):
    incident_id: int
    evidence: EvidenceOut
    message: str
