"""Database initialization and idempotent seeding.

ALL seeded fleet records are synthetic and flagged:
    "SYNTHETIC DEMONSTRATION DATA — NOT REAL VEHICLES"
Synthetic asset IDs (SYNTHETIC-BUS-xxx / SYNTHETIC-SPARE-xx) are never
submitted to NHTSA or vPIC.

Repeated seeding never creates duplicate incidents, vehicles or routes:
every seed step is keyed by a unique business identifier and skipped when
the record already exists.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .auth import ensure_manager_account
from .services.evidence import seed_evidence_from_fixture

logger = logging.getLogger("routeshield.seed")

SYNTHETIC_LABEL = "SYNTHETIC DEMONSTRATION DATA — NOT REAL VEHICLES"
DEMO_CAMPAIGN = "26V111"

# ---------------------------------------------------------------------------
# Synthetic fleet register (12 route buses + 3 spares) - designed to cover
# every assessment category against campaign 26V111 (IC BUS CESB, MY2023-2027,
# Hydraulic Brake feature code 0004100, per-model-year production windows).
# ---------------------------------------------------------------------------

VEHICLES: list[dict] = [
    # --- Assigned to routes -------------------------------------------------
    dict(asset_id="SYNTHETIC-BUS-001", make="IC BUS", model="CESB", model_year=2026,
         build_date=date(2025, 3, 14), brake_feature_code="0004100",
         configuration={"brake_system": "Hydraulic", "feature_code_source": "build sheet"},
         depot="North", passenger_capacity=72, role="ROUTE_BUS", availability="ASSIGNED_TO_ROUTE",
         features=["wheelchair_lift"], notes="Morning elementary run."),
    dict(asset_id="SYNTHETIC-BUS-002", make="IC BUS", model="CESB", model_year=2025,
         build_date=date(2024, 6, 20), brake_feature_code="0004100",
         configuration={"brake_system": "Hydraulic"},
         depot="Central", passenger_capacity=78, role="ROUTE_BUS", availability="ASSIGNED_TO_ROUTE",
         features=[], notes=""),
    dict(asset_id="SYNTHETIC-BUS-003", make="IC BUS", model="CESB", model_year=2024,
         build_date=date(2023, 9, 28), brake_feature_code="0004100",
         configuration={"brake_system": "Hydraulic"},
         depot="Central", passenger_capacity=48, role="ROUTE_BUS", availability="ASSIGNED_TO_ROUTE",
         features=[], notes="Small-capacity primary school run."),
    dict(asset_id="SYNTHETIC-BUS-004", make="IC BUS", model="CESB", model_year=2027,
         build_date=date(2025, 11, 18), brake_feature_code=None,
         configuration={"brake_system": "not recorded", "note": "build sheet page missing"},
         depot="South", passenger_capacity=66, role="ROUTE_BUS", availability="ASSIGNED_TO_ROUTE",
         features=[], vin="SYNTHETIC-DEMO-VIN-0004", vin_status="NOT_PERFORMED",
         notes="Brake feature code not recorded in fleet system."),
    dict(asset_id="SYNTHETIC-BUS-005", make="IC BUS", model="CESB", model_year=2025,
         build_date=None, brake_feature_code="0004100",
         configuration={"brake_system": "Hydraulic", "note": "build date field empty after migration"},
         depot="North", passenger_capacity=72, role="ROUTE_BUS", availability="ASSIGNED_TO_ROUTE",
         features=["wheelchair_lift"], notes="Manufacturing date unavailable."),
    dict(asset_id="SYNTHETIC-BUS-006", make="IC BUS", model="CESB", model_year=2023,
         build_date=date(2022, 11, 30), brake_feature_code="0004100",
         configuration={"brake_system": "Hydraulic"},
         depot="South", passenger_capacity=54, role="ROUTE_BUS", availability="ASSIGNED_TO_ROUTE",
         features=[], notes="Early MY2023 production."),
    # --- Unassigned pool ----------------------------------------------------
    dict(asset_id="SYNTHETIC-BUS-007", make="IC BUS", model="CESB", model_year=2024,
         build_date=date(2024, 3, 2), brake_feature_code="0004100",
         configuration={"brake_system": "Hydraulic"},
         depot="Central", passenger_capacity=72, role="ROUTE_BUS", availability="AVAILABLE",
         features=[], notes="Built outside the MY2024 documented window."),
    dict(asset_id="SYNTHETIC-BUS-008", make="BLUE BIRD", model="VISION", model_year=2025,
         build_date=date(2024, 8, 1), brake_feature_code=None,
         configuration={"brake_system": "Air"},
         depot="South", passenger_capacity=72, role="ROUTE_BUS", availability="AVAILABLE",
         features=["wheelchair_lift"], notes="Different manufacturer."),
    dict(asset_id="SYNTHETIC-BUS-009", make="IC BUS", model="CESB", model_year=2026,
         build_date=date(2025, 1, 10), brake_feature_code="0004200",
         configuration={"brake_system": "Air Brake"},
         depot="North", passenger_capacity=72, role="ROUTE_BUS", availability="AVAILABLE",
         features=[], notes="Air-brake configuration."),
    dict(asset_id="SYNTHETIC-BUS-010", make="IC BUS", model="CESB", model_year=2025,
         build_date=date(2024, 5, 5), brake_feature_code=None,
         configuration={"brake_system": "unknown", "note": "Hydraulic Brake per work order #4412"},
         depot="Central", passenger_capacity=66, role="ROUTE_BUS", availability="AVAILABLE",
         features=[], notes="Narrative mentions hydraulic brakes; feature code not machine-recorded."),
    dict(asset_id="SYNTHETIC-BUS-011", make="IC BUS", model="CESB", model_year=2027,
         build_date=date(2026, 3, 15), brake_feature_code="0004100",
         configuration={"brake_system": "Hydraulic"},
         depot="South", passenger_capacity=66, role="ROUTE_BUS", availability="AVAILABLE",
         features=[], notes="Built after the documented suspect window closed."),
    dict(asset_id="SYNTHETIC-BUS-012", make="IC BUS", model="CESB", model_year=2023,
         build_date=date(2022, 12, 18), brake_feature_code="0004100",
         configuration={"brake_system": "Air Brake (retrofit note, unverified)"},
         depot="North", passenger_capacity=54, role="ROUTE_BUS", availability="IN_MAINTENANCE",
         features=[], notes="Conflicting records: code says hydraulic, retrofit note says air."),
    # --- Spares ---------------------------------------------------------------
    dict(asset_id="SYNTHETIC-SPARE-01", make="IC BUS", model="CE", model_year=2019,
         build_date=date(2018, 6, 1), brake_feature_code="0004200",
         configuration={"brake_system": "Air Brake"},
         depot="North", passenger_capacity=66, role="SPARE", availability="AVAILABLE",
         features=["wheelchair_lift"], notes="Spare pool - North depot."),
    dict(asset_id="SYNTHETIC-SPARE-02", make="IC BUS", model="CESB", model_year=2022,
         build_date=date(2021, 7, 15), brake_feature_code="0004100",
         configuration={"brake_system": "Hydraulic"},
         depot="Central", passenger_capacity=72, role="SPARE", availability="AVAILABLE",
         features=[], notes="Spare pool - Central depot. MY2022 predates the campaign."),
    dict(asset_id="SYNTHETIC-SPARE-03", make="BLUE BIRD", model="VISION", model_year=2021,
         build_date=date(2020, 9, 10), brake_feature_code=None,
         configuration={"brake_system": "Air"},
         depot="South", passenger_capacity=48, role="SPARE", availability="AVAILABLE",
         features=["wheelchair_lift"], notes="Spare pool - South depot (small capacity)."),
]

ROUTES: list[dict] = [
    dict(route_id="R-101", name="Northside Elementary Run", depot="North",
         required_capacity=65, required_characteristics=["wheelchair_lift"], assigned="SYNTHETIC-BUS-001"),
    dict(route_id="R-102", name="Central Middle School Run", depot="Central",
         required_capacity=72, required_characteristics=[], assigned="SYNTHETIC-BUS-002"),
    dict(route_id="R-103", name="East Valley Primary Run", depot="Central",
         required_capacity=48, required_characteristics=[], assigned="SYNTHETIC-BUS-003"),
    dict(route_id="R-104", name="Southgate High School Run", depot="South",
         required_capacity=60, required_characteristics=[], assigned="SYNTHETIC-BUS-004"),
    dict(route_id="R-105", name="Riverbend Elementary Run", depot="North",
         required_capacity=72, required_characteristics=["wheelchair_lift"], assigned="SYNTHETIC-BUS-005"),
    dict(route_id="R-106", name="Hillcrest Kindergarten Run", depot="South",
         required_capacity=48, required_characteristics=[], assigned="SYNTHETIC-BUS-006"),
]


def seed_fleet(db: Session) -> None:
    existing = db.scalar(select(func.count()).select_from(models.FleetVehicle))
    if existing:
        return
    for v in VEHICLES:
        db.add(models.FleetVehicle(
            synthetic=True, synthetic_label=SYNTHETIC_LABEL,
            manufacturer=v.get("make", ""), **{k: val for k, val in v.items()},
        ))
    db.flush()
    for r in ROUTES:
        veh = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == r["assigned"]))
        db.add(models.FleetRoute(
            route_id=r["route_id"], name=r["name"], depot=r["depot"],
            required_capacity=r["required_capacity"],
            required_characteristics=r["required_characteristics"],
            assigned_vehicle_id=veh.id if veh else None,
            is_active=True, synthetic=True,
        ))
    db.commit()
    logger.info("Seeded %d synthetic vehicles and %d synthetic routes", len(VEHICLES), len(ROUTES))


def seed_incident(db: Session, campaign_number: str = DEMO_CAMPAIGN) -> models.Incident:
    """Idempotently ensure the demo incident exists with reviewed evidence and
    an initial deterministic assessment (read-only derived data). Policy
    selection, proposals and approvals are NEVER auto-created."""
    incident = db.scalar(
        select(models.Incident).where(models.Incident.campaign_number == campaign_number)
    )
    version = seed_evidence_from_fixture(db, campaign_number)
    if incident is None:
        campaign = db.scalar(
            select(models.RecallCampaign).where(models.RecallCampaign.campaign_number == campaign_number)
        )
        incident = models.Incident(
            campaign_number=campaign_number,
            campaign_id=campaign.id if campaign else None,
            active_report_version_id=version.id if version else None,
            status="EVIDENCE_LOADED" if version else "OPEN",
            evidence_source="MANUALLY_REVIEWED_FIXTURE" if version else "",
        )
        db.add(incident)
        db.commit()
        logger.info("Seeded incident for campaign %s", campaign_number)
    elif version and incident.active_report_version_id is None:
        incident.active_report_version_id = version.id
        incident.evidence_source = incident.evidence_source or "MANUALLY_REVIEWED_FIXTURE"
        db.commit()

    # Initial assessment (deterministic, derived data - safe to auto-run).
    has_assessments = db.scalar(
        select(func.count()).select_from(models.VehicleAssessment).where(
            models.VehicleAssessment.incident_id == incident.id
        )
    )
    if version and not has_assessments:
        from .services.assessment import assess_fleet
        try:
            assess_fleet(db, incident)
            logger.info("Initial fleet assessment computed for incident %s", incident.id)
        except ValueError as exc:
            logger.warning("Initial assessment skipped: %s", exc)
    return incident


def seed_all(db: Session) -> None:
    ensure_manager_account(db)
    seed_fleet(db)
    seed_incident(db)
