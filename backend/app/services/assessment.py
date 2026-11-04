"""Deterministic vehicle applicability assessment.

CORE PRODUCT PRINCIPLE: Absence of evidence is not evidence of safety.

All matching logic here is deterministic Python. The LLM never decides
whether a bus is affected. Classification vocabulary:

* CAMPAIGN_CRITERIA_MATCH   - all available required criteria match the
                              REVIEWED report version. This is NOT a
                              VIN-level confirmation.
* VERIFICATION_REQUIRED     - critical evidence missing/ambiguous; the
                              system abstains.
* OUTSIDE_REPORT_VERSION    - the vehicle falls outside THIS report
                              version's criteria. This does NOT mean SAFE.

VIN evidence is tracked separately: synthetic vehicles never receive a
VIN-confirmed state; VIN_STATUS_UNAVAILABLE is the honest default.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models

ENGINE_VERSION = "1.2.0"

NOT_SAFE_DISCLAIMER = (
    "OUTSIDE_REPORT_VERSION does NOT mean this vehicle is safe or free of recalls; "
    "it means the criteria documented in this report version do not cover it."
)
NOT_VIN_CONFIRMED_DISCLAIMER = (
    "CAMPAIGN_CRITERIA_MATCH is a match against the reviewed campaign criteria only. "
    "It is NOT a VIN-level confirmation that this individual vehicle has an open recall."
)

SATISFIED = "SATISFIED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
CONFLICT = "CONFLICT"


# ---------------------------------------------------------------------------
# Normalized criteria (built from REVIEWED ExtractedCriterion rows)
# ---------------------------------------------------------------------------


@dataclass
class ProductionRange:
    model_year_start: int
    model_year_end: int
    production_start: date
    production_end: date
    units: int | None
    label: str
    source_page: int | None


@dataclass
class CampaignCriteria:
    make: str = ""
    model: str = ""
    make_model: str = ""
    product_type: str = ""
    ranges: list[ProductionRange] = field(default_factory=list)
    overall_start: date | None = None
    overall_end: date | None = None
    feature_code: str | None = None
    feature_description: str = ""
    exclusion_statement: str = ""
    exclusion_page: int | None = None
    make_model_page: int | None = None
    feature_page: int | None = None
    report_version_id: int | None = None
    document_sha256: str = ""

    @property
    def model_years(self) -> set[int]:
        years: set[int] = set()
        for r in self.ranges:
            years.update(range(r.model_year_start, r.model_year_end + 1))
        return years


def _norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _parse_iso(d: str | None) -> date | None:
    if not d:
        return None
    try:
        return date.fromisoformat(str(d)[:10])
    except ValueError:
        return None


def criteria_from_rows(rows: list[models.ExtractedCriterion], version: models.RecallReportVersion | None = None) -> CampaignCriteria:
    """Build normalized matching criteria from REVIEWED criterion rows."""
    c = CampaignCriteria()
    if version is not None:
        c.report_version_id = version.id
        c.document_sha256 = version.document_sha256
    for row in rows:
        if row.review_status != "REVIEWED":
            continue  # only human-reviewed evidence backs matching
        v = row.value or {}
        if row.criterion_type == "MAKE_MODEL":
            c.make = str(v.get("make", ""))
            c.model = str(v.get("model", ""))
            c.make_model = str(v.get("make_model", ""))
            c.make_model_page = row.source_page
        elif row.criterion_type == "PRODUCT_TYPE":
            c.product_type = str(v.get("product_type", ""))
        elif row.criterion_type == "PRODUCTION_DATE_RANGE":
            ps, pe = _parse_iso(v.get("production_start")), _parse_iso(v.get("production_end"))
            if ps and pe:
                c.ranges.append(ProductionRange(
                    model_year_start=int(v.get("model_year_start", 0)),
                    model_year_end=int(v.get("model_year_end", 0)),
                    production_start=ps, production_end=pe,
                    units=v.get("units"), label=row.label, source_page=row.source_page,
                ))
        elif row.criterion_type == "OVERALL_BUILD_RANGE":
            c.overall_start = _parse_iso(v.get("start"))
            c.overall_end = _parse_iso(v.get("end"))
        elif row.criterion_type == "FEATURE_CODE":
            if c.feature_code is None:  # first reviewed feature-code criterion
                c.feature_code = str(v.get("feature_code", ""))
                c.feature_description = str(v.get("feature_description", ""))
                c.feature_page = row.source_page
        elif row.criterion_type == "EXCLUSION":
            if not c.exclusion_statement:
                c.exclusion_statement = str(v.get("exclusion_statement", ""))
                c.exclusion_page = row.source_page
    c.ranges.sort(key=lambda r: (r.model_year_start, r.production_start))
    return c


# ---------------------------------------------------------------------------
# Pure classification core (used by the API and by the benchmark)
# ---------------------------------------------------------------------------


@dataclass
class CriterionCheck:
    criterion_type: str
    criterion_label: str
    source_page: int | None
    required_value: str
    observed_value: str
    result: str  # SATISFIED | FAILED | UNKNOWN | CONFLICT

    def as_dict(self) -> dict:
        return {
            "criterion_type": self.criterion_type,
            "criterion_label": self.criterion_label,
            "source_page": self.source_page,
            "required_value": self.required_value,
            "observed_value": self.observed_value,
            "result": self.result,
        }


@dataclass
class AssessmentResult:
    classification: str
    reasons: list[str]
    evidence_detail: list[dict]


def _config_text(vehicle: dict) -> str:
    cfg = vehicle.get("configuration") or {}
    if isinstance(cfg, str):
        return cfg
    parts = []
    for k, val in cfg.items():
        parts.append(f"{k}: {val}")
    parts.append(str(vehicle.get("configuration_text", "")))
    return " ".join(parts)


def classify_vehicle(vehicle: dict, criteria: CampaignCriteria) -> AssessmentResult:
    """Deterministically classify ONE vehicle against REVIEWED campaign criteria.

    `vehicle` is a plain dict with keys: make, model, model_year, build_date
    (date|str|None), brake_feature_code (str|None), configuration (dict|str).

    Decision order (evidence-based, deterministic):
      1. Decisive mismatches (positive evidence the vehicle is NOT covered)
         => OUTSIDE_REPORT_VERSION.
      2. Otherwise, missing/ambiguous critical evidence => VERIFICATION_REQUIRED.
      3. Otherwise all criteria satisfied => CAMPAIGN_CRITERIA_MATCH.
    """
    checks: list[CriterionCheck] = []
    reasons: list[str] = []
    decisive_outside = False
    unknown_present = False

    veh_make = _norm(vehicle.get("make"))
    veh_model = _norm(vehicle.get("model"))
    crit_make = _norm(criteria.make)
    crit_model = _norm(criteria.model)
    crit_make_model = _norm(criteria.make_model)

    # --- Make / model ---------------------------------------------------------
    # Incomplete identity information is UNKNOWN evidence, not exclusion.
    # Only positive, decisive conflicts on recorded fields may establish an
    # exclusion; a missing make or model alone must lead to abstention.
    if crit_make_model or (crit_make and crit_model):
        required_mm = criteria.make_model or f"{criteria.make} {criteria.model}".strip()
        display_mm = f"{vehicle.get('make') or ''} {vehicle.get('model') or ''}".strip()
        page_ref = f", p.{criteria.make_model_page}" if criteria.make_model_page else ""
        result, conflict = _identity_check(veh_make, veh_model, crit_make, crit_model, crit_make_model)
        if result == SATISFIED:
            checks.append(CriterionCheck("MAKE_MODEL", "Make / Model", criteria.make_model_page,
                                         required_mm, display_mm or "(not recorded)", SATISFIED))
        elif result == FAILED:
            checks.append(CriterionCheck("MAKE_MODEL", "Make / Model", criteria.make_model_page,
                                         required_mm, display_mm or "(not recorded)", FAILED))
            decisive_outside = True
            raw_make = (vehicle.get("make") or "").strip()
            raw_model = (vehicle.get("model") or "").strip()
            if conflict == "make":
                conflict_text = f"recorded make '{raw_make}' does not match the required make"
            elif conflict == "model":
                conflict_text = f"recorded model '{raw_model}' does not match the required model"
            else:
                conflict_text = f"recorded make/model '{display_mm}' does not match"
            reasons.append(
                f"Outside this report version: {conflict_text} — positive evidence conflicting "
                f"with the reviewed report's involved vehicle list ({required_mm}{page_ref})."
            )
        else:  # UNKNOWN - identity incomplete without decisive conflict
            missing = []
            if not veh_make:
                missing.append("make")
            if not veh_model:
                missing.append("model")
            checks.append(CriterionCheck("MAKE_MODEL", "Make / Model", criteria.make_model_page,
                                         required_mm, display_mm + " (incomplete)" if display_mm else "(not recorded)",
                                         UNKNOWN))
            unknown_present = True
            reasons.append(
                f"Verification required: {' and '.join(missing)} not recorded for this vehicle; "
                f"identity cannot be checked against the reviewed criteria ({required_mm}{page_ref}). "
                "Incomplete identity is not evidence of exclusion."
            )

    # --- Model year ---------------------------------------------------------
    if criteria.model_years:
        my = vehicle.get("model_year")
        required = ", ".join(str(y) for y in sorted(criteria.model_years))
        if my is None:
            checks.append(CriterionCheck("MODEL_YEAR", "Model year", criteria.make_model_page,
                                         f"one of {required}", "(not recorded)", UNKNOWN))
            unknown_present = True
            reasons.append("Verification required: model year is not recorded for this vehicle.")
        elif int(my) in criteria.model_years:
            checks.append(CriterionCheck("MODEL_YEAR", "Model year", criteria.make_model_page,
                                         f"one of {required}", str(my), SATISFIED))
        else:
            checks.append(CriterionCheck("MODEL_YEAR", "Model year", criteria.make_model_page,
                                         f"one of {required}", str(my), FAILED))
            decisive_outside = True
            reasons.append(
                f"Outside this report version: model year {my} is not among the documented "
                f"model years ({required})."
            )

    # --- Production date ----------------------------------------------------
    if criteria.ranges:
        build = vehicle.get("build_date")
        if isinstance(build, str):
            build = _parse_iso(build)
        my = vehicle.get("model_year")
        my_ranges = [r for r in criteria.ranges
                     if my is not None and r.model_year_start <= int(my) <= r.model_year_end]
        any_range = my_ranges or criteria.ranges
        if build is None:
            checks.append(CriterionCheck("PRODUCTION_DATE_RANGE", "Production date range",
                                         any_range[0].source_page if any_range else None,
                                         _ranges_label(any_range), "(not recorded)", UNKNOWN))
            unknown_present = True
            reasons.append(
                "Verification required: manufacturing date unavailable. The reviewed criteria "
                "depend on the production date range and it cannot be checked."
            )
        else:
            in_range = [r for r in any_range if r.production_start <= build <= r.production_end]
            label = _ranges_label(any_range)
            if in_range:
                checks.append(CriterionCheck("PRODUCTION_DATE_RANGE", "Production date range",
                                             in_range[0].source_page, label, build.isoformat(), SATISFIED))
            else:
                # Distinguish precedes / follows / gap for an honest explanation.
                if my_ranges:
                    latest_end = max(r.production_end for r in my_ranges)
                    earliest_start = min(r.production_start for r in my_ranges)
                    if build < earliest_start:
                        rel = f"production date precedes the documented affected production period for MY{my}"
                    elif build > latest_end:
                        rel = f"production date follows the documented affected production period for MY{my}"
                    else:
                        rel = f"production date falls in a gap of the documented production periods for MY{my}"
                else:
                    rel = "production date falls outside every documented production period in this report"
                checks.append(CriterionCheck("PRODUCTION_DATE_RANGE", "Production date range",
                                             any_range[0].source_page, label, build.isoformat(), FAILED))
                decisive_outside = True
                reasons.append(
                    f"Outside this report version: {rel} ({label}"
                    f"{f', p.{any_range[0].source_page}' if any_range and any_range[0].source_page else ''})."
                )

    # --- Brake feature code (configuration criterion) -----------------------
    if criteria.feature_code:
        code = (vehicle.get("brake_feature_code") or "").strip() or None
        cfg_text = _config_text(vehicle).lower()
        feat_desc = (criteria.feature_description or "").lower()
        required = f"{criteria.feature_description} (feature code {criteria.feature_code})".strip()
        if code is None:
            # Narrative mention without a recorded code is ambiguous evidence.
            if feat_desc and feat_desc in cfg_text:
                checks.append(CriterionCheck("FEATURE_CODE", "Brake configuration feature code",
                                             criteria.feature_page, required,
                                             f"(code not recorded; notes mention '{criteria.feature_description}')",
                                             CONFLICT))
                unknown_present = True
                reasons.append(
                    "Verification required: brake feature code is not machine-recorded. Configuration "
                    f"notes mention '{criteria.feature_description}' but the reviewed criterion is the "
                    f"feature code ({criteria.feature_code}"
                    f"{f', p.{criteria.feature_page}' if criteria.feature_page else ''}); narrative text "
                    "alone is not sufficient evidence."
                )
            else:
                checks.append(CriterionCheck("FEATURE_CODE", "Brake configuration feature code",
                                             criteria.feature_page, required, "(not recorded)", UNKNOWN))
                unknown_present = True
                reasons.append(
                    "Verification required: brake configuration feature code is not recorded. The "
                    f"reviewed criteria require {required} and no evidence supports or refutes it."
                )
        elif code.upper() == criteria.feature_code.upper():
            # Positive code match - but check the configuration narrative for conflict.
            conflict_words = _conflicting_brake_terms(cfg_text, criteria.feature_description)
            if conflict_words:
                checks.append(CriterionCheck("FEATURE_CODE", "Brake configuration feature code",
                                             criteria.feature_page, required,
                                             f"{code} (but configuration notes say '{conflict_words}')", CONFLICT))
                unknown_present = True
                reasons.append(
                    f"Verification required: conflicting configuration evidence. Recorded feature code "
                    f"{code} matches {required}, but configuration notes mention '{conflict_words}'. "
                    "Conflicting records must be resolved by a human before this vehicle can be "
                    "classified either way."
                )
            else:
                checks.append(CriterionCheck("FEATURE_CODE", "Brake configuration feature code",
                                             criteria.feature_page, required, code, SATISFIED))
        else:
            checks.append(CriterionCheck("FEATURE_CODE", "Brake configuration feature code",
                                         criteria.feature_page, required, code, FAILED))
            decisive_outside = True
            excl = criteria.exclusion_statement or (
                "All other similar vehicles, which are not subject to this recall, are excluded."
            )
            reasons.append(
                f"Outside this report version: recorded brake configuration code {code} does not match "
                f"the required {required}"
                f"{f' (p.{criteria.feature_page})' if criteria.feature_page else ''}. The report states "
                f"explicitly: \"{excl}\""
            )

    # --- Aggregate ------------------------------------------------------------
    evidence = [c.as_dict() for c in checks]
    if decisive_outside:
        classification = "OUTSIDE_REPORT_VERSION"
        reasons.append(NOT_SAFE_DISCLAIMER)
    elif unknown_present:
        classification = "VERIFICATION_REQUIRED"
        reasons.append(
            "Absence of evidence is not evidence of safety: the system abstains from concluding "
            "that this vehicle is unaffected while critical evidence is missing or ambiguous."
        )
    else:
        classification = "CAMPAIGN_CRITERIA_MATCH"
        matched = [c.criterion_label for c in checks if c.result == SATISFIED]
        reasons.append(
            "Campaign criteria match: " + ", ".join(m.lower() for m in matched) +
            " satisfy the reviewed campaign criteria."
        )
        reasons.append(NOT_VIN_CONFIRMED_DISCLAIMER)
    return AssessmentResult(classification=classification, reasons=reasons, evidence_detail=evidence)


def _ranges_label(ranges: list[ProductionRange]) -> str:
    return "; ".join(
        f"MY{r.model_year_start}" + (f"-{r.model_year_end}" if r.model_year_end != r.model_year_start else "") +
        f": {r.production_start.isoformat()} to {r.production_end.isoformat()}"
        for r in ranges
    )


def _identity_check(
    veh_make: str, veh_model: str, crit_make: str, crit_model: str, crit_make_model: str
) -> tuple[str, str | None]:
    """Three-state make/model identity matching on normalized values.

    Returns (result, conflict_field) where result is SATISFIED / FAILED /
    UNKNOWN and conflict_field names the recorded field that positively
    conflicts ('make', 'model' or 'make_model').

    FAILED is returned ONLY when a RECORDED field decisively conflicts with
    the reviewed criteria. A missing field without a decisive conflict on the
    recorded one yields UNKNOWN - incomplete identity is abstention evidence,
    never exclusion evidence.
    """
    if not veh_make and not veh_model:
        return UNKNOWN, None
    if veh_make and veh_model:
        if crit_make and crit_model:
            ok = veh_make == crit_make and veh_model == crit_model
        else:
            ok = (veh_make + veh_model) == crit_make_model
        if ok:
            return SATISFIED, None
        if crit_make and veh_make != crit_make:
            return FAILED, "make"
        if crit_model and veh_model != crit_model:
            return FAILED, "model"
        return FAILED, "make_model"
    if veh_make:
        # Model not recorded: only a decisive make conflict may exclude.
        if crit_make:
            if veh_make != crit_make:
                return FAILED, "make"
        elif crit_make_model and not crit_make_model.startswith(veh_make):
            return FAILED, "make"
        return UNKNOWN, None
    # Model recorded, make not: only a decisive model conflict may exclude.
    if crit_model:
        if veh_model != crit_model:
            return FAILED, "model"
    elif crit_make_model and not crit_make_model.endswith(veh_model):
        return FAILED, "model"
    return UNKNOWN, None


def _conflicting_brake_terms(cfg_text: str, feature_description: str) -> str:
    """Detect narrative brake terms that contradict the required feature."""
    desc = (feature_description or "").lower()
    if "hydraulic" in desc and re.search(r"\bair\s*brake", cfg_text):
        return "air brake"
    if "air" in desc and "hydraulic" in cfg_text and "hydraulic" not in desc:
        return "hydraulic brake"
    return ""


# ---------------------------------------------------------------------------
# DB-level assessment (persists VehicleAssessment rows for an incident)
# ---------------------------------------------------------------------------


def vin_evidence_for(vehicle: models.FleetVehicle) -> tuple[str, str]:
    """Honest VIN evidence status. Synthetic vehicles can never be
    VIN-confirmed; only a real, individually retrieved source result may
    produce a confirmed state."""
    if vehicle.synthetic:
        return "VIN_STATUS_UNAVAILABLE", (
            "Individual VIN-specific recall verification has not been performed for this "
            "synthetic demonstration record, and synthetic identifiers are never submitted "
            "to NHTSA or vPIC."
        )
    if vehicle.vin_status == "INDIVIDUAL_RECALL_CHECK_DONE":
        return "VIN_RECALL_CHECK_DONE", (
            "An individual VIN recall check was performed against an official source result."
        )
    if vehicle.vin_status == "DECODED_VPIC":
        return "VIN_DECODED_VPIC", (
            "The VIN was decoded via vPIC by an authorized user. A decode confirms vehicle "
            "attributes only - it is NOT recall confirmation."
        )
    return "VIN_STATUS_UNAVAILABLE", (
        "Individual VIN-specific recall verification has not been performed or is unavailable."
    )


def assess_fleet(db: Session, incident: models.Incident) -> list[models.VehicleAssessment]:
    """Run the deterministic assessment for every fleet vehicle and persist it.

    Uses the latest REVIEWED report version. Raises ValueError if no reviewed
    criteria exist (matching must never run on unreviewed evidence).
    """
    from .evidence import latest_reviewed_version

    if incident.campaign_id is None:
        raise ValueError("Incident has no linked campaign; load recall evidence first.")
    version = latest_reviewed_version(db, incident.campaign_id)
    if version is None:
        raise ValueError(
            "No REVIEWED report version exists for this campaign. Criteria require human "
            "review before deterministic matching may run."
        )
    rows = list(db.scalars(
        select(models.ExtractedCriterion).where(models.ExtractedCriterion.report_version_id == version.id)
    ))
    criteria = criteria_from_rows(rows, version)
    if not criteria.ranges or not criteria.feature_code:
        raise ValueError("Reviewed criteria are incomplete (missing production ranges or feature code).")

    vehicles = list(db.scalars(select(models.FleetVehicle).order_by(models.FleetVehicle.asset_id)))
    results: list[models.VehicleAssessment] = []
    for veh in vehicles:
        res = classify_vehicle(
            {
                "make": veh.make,
                "model": veh.model,
                "model_year": veh.model_year,
                "build_date": veh.build_date,
                "brake_feature_code": veh.brake_feature_code,
                "configuration": veh.configuration,
            },
            criteria,
        )
        vin_status, vin_note = vin_evidence_for(veh)
        existing = db.scalar(
            select(models.VehicleAssessment).where(
                models.VehicleAssessment.incident_id == incident.id,
                models.VehicleAssessment.vehicle_id == veh.id,
            )
        )
        if existing:
            existing.classification = res.classification
            existing.reasons = res.reasons
            existing.evidence_detail = res.evidence_detail
            existing.vin_evidence_status = vin_status
            existing.vin_evidence_note = vin_note
            existing.report_version_id = version.id
            existing.assessed_at = datetime.now(timezone.utc)
            results.append(existing)
        else:
            a = models.VehicleAssessment(
                incident_id=incident.id,
                vehicle_id=veh.id,
                classification=res.classification,
                reasons=res.reasons,
                evidence_detail=res.evidence_detail,
                vin_evidence_status=vin_status,
                vin_evidence_note=vin_note,
                report_version_id=version.id,
            )
            db.add(a)
            results.append(a)
    incident.status = "ASSESSED" if incident.status in ("OPEN", "EVIDENCE_LOADED", "ASSESSED") else incident.status
    db.commit()
    return results
