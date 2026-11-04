"""B. Vehicle assessment tests: deterministic classification, honest
abstention, and the guarantee that incomplete evidence is NEVER confirmed."""
from __future__ import annotations

import pytest

from app.benchmark.runner import benchmark_criteria
from app.services.assessment import classify_vehicle

CRIT = benchmark_criteria()


def _veh(**kw):
    base = dict(make="IC BUS", model="CESB", model_year=2025, build_date="2024-06-01",
                brake_feature_code="0004100", configuration={})
    base.update(kw)
    return base


def test_exact_criteria_match():
    r = classify_vehicle(_veh(), CRIT)
    assert r.classification == "CAMPAIGN_CRITERIA_MATCH"
    assert any("Campaign criteria match" in x for x in r.reasons)
    assert any("NOT a VIN-level confirmation" in x for x in r.reasons)
    assert all(c["result"] == "SATISFIED" for c in r.evidence_detail)


def test_match_is_never_presented_as_vin_confirmed():
    r = classify_vehicle(_veh(), CRIT)
    joined = " ".join(r.reasons).lower()
    assert "not a vin-level confirmation" in joined
    assert "vin-confirmed" not in joined and "vin confirmed" not in joined


def test_missing_build_date_abstains():
    r = classify_vehicle(_veh(build_date=None), CRIT)
    assert r.classification == "VERIFICATION_REQUIRED"
    assert any("manufacturing date unavailable" in x.lower() for x in r.reasons)


def test_missing_configuration_abstains():
    r = classify_vehicle(_veh(brake_feature_code=None), CRIT)
    assert r.classification == "VERIFICATION_REQUIRED"
    assert any("feature code is not recorded" in x for x in r.reasons)


def test_missing_everything_abstains_never_confirms():
    r = classify_vehicle(_veh(model_year=None, build_date=None, brake_feature_code=None,
                              make=None, model=None), CRIT)
    assert r.classification == "VERIFICATION_REQUIRED"


def test_outside_production_range_precedes():
    r = classify_vehicle(_veh(build_date="2024-02-04"), CRIT)  # day before MY2025 window
    assert r.classification == "OUTSIDE_REPORT_VERSION"
    assert any("precedes the documented affected production period" in x for x in r.reasons)
    assert any("does NOT mean this vehicle is safe" in x for x in r.reasons)


def test_outside_production_range_follows():
    r = classify_vehicle(_veh(build_date="2024-10-03"), CRIT)  # day after MY2025 window
    assert r.classification == "OUTSIDE_REPORT_VERSION"
    assert any("follows the documented affected production period" in x for x in r.reasons)


def test_model_year_outside_report():
    r = classify_vehicle(_veh(model_year=2022, build_date="2021-06-01"), CRIT)
    assert r.classification == "OUTSIDE_REPORT_VERSION"


def test_explicit_configuration_exclusion():
    r = classify_vehicle(_veh(brake_feature_code="0004200",
                              configuration={"brake_system": "Air Brake"}), CRIT)
    assert r.classification == "OUTSIDE_REPORT_VERSION"
    assert any("excluded" in x for x in r.reasons)


def test_conflicting_attributes_abstain():
    r = classify_vehicle(_veh(configuration={"brake_system": "Air Brake (retrofit note)"}), CRIT)
    assert r.classification == "VERIFICATION_REQUIRED"
    assert any("conflicting configuration evidence" in x.lower() for x in r.reasons)


# ---------------------------------------------------------------------------
# Identity (make/model) evidence handling: incomplete identity abstains;
# only positive conflicts exclude.
# ---------------------------------------------------------------------------


def test_missing_make_with_consistent_model_abstains():
    r = classify_vehicle(_veh(make=None), CRIT)
    assert r.classification == "VERIFICATION_REQUIRED"
    assert any("make" in x and "not recorded" in x for x in r.reasons)
    identity = next(c for c in r.evidence_detail if c["criterion_type"] == "MAKE_MODEL")
    assert identity["result"] == "UNKNOWN"


def test_missing_model_with_consistent_make_abstains():
    r = classify_vehicle(_veh(model=None), CRIT)
    assert r.classification == "VERIFICATION_REQUIRED"
    assert any("model" in x and "not recorded" in x for x in r.reasons)
    identity = next(c for c in r.evidence_detail if c["criterion_type"] == "MAKE_MODEL")
    assert identity["result"] == "UNKNOWN"


def test_missing_make_and_model_abstains():
    r = classify_vehicle(_veh(make=None, model=None), CRIT)
    assert r.classification == "VERIFICATION_REQUIRED"
    assert any("make and model not recorded" in x for x in r.reasons)


def test_explicit_make_and_model_mismatch_is_outside():
    r = classify_vehicle(_veh(make="BLUE BIRD", model="VISION"), CRIT)
    assert r.classification == "OUTSIDE_REPORT_VERSION"
    assert any("positive evidence" in x for x in r.reasons)
    identity = next(c for c in r.evidence_detail if c["criterion_type"] == "MAKE_MODEL")
    assert identity["result"] == "FAILED"


def test_missing_make_but_conflicting_model_is_outside():
    """Incomplete identity may still exclude when the RECORDED field
    positively conflicts (sufficient positive evidence)."""
    r = classify_vehicle(_veh(make=None, model="VISION"), CRIT)
    assert r.classification == "OUTSIDE_REPORT_VERSION"
    assert any("recorded model 'VISION'" in x for x in r.reasons)


def test_missing_model_but_conflicting_make_is_outside():
    r = classify_vehicle(_veh(make="BLUE BIRD", model=None), CRIT)
    assert r.classification == "OUTSIDE_REPORT_VERSION"
    assert any("recorded make 'BLUE BIRD'" in x for x in r.reasons)


@pytest.mark.parametrize("missing", [
    {"make": None},
    {"model": None},
    {"make": None, "model": None},
    {"make": ""},
    {"model": ""},
    {"make": "", "model": ""},
])
def test_incomplete_identity_is_never_classified_outside(missing):
    """Regression: a missing make or model alone must NEVER produce
    OUTSIDE_REPORT_VERSION - incomplete identity is abstention evidence."""
    r = classify_vehicle(_veh(**missing), CRIT)
    assert r.classification != "OUTSIDE_REPORT_VERSION"
    assert r.classification == "VERIFICATION_REQUIRED"


def test_narrative_without_code_abstains():
    r = classify_vehicle(_veh(brake_feature_code=None,
                              configuration={"note": "Hydraulic Brake per work order"}), CRIT)
    assert r.classification == "VERIFICATION_REQUIRED"
    assert any("not sufficient evidence" in x for x in r.reasons)


@pytest.mark.parametrize("missing", [
    {"build_date": None},
    {"brake_feature_code": None},
    {"build_date": None, "brake_feature_code": None},
    {"model_year": None},
    {"make": "", "model": ""},
])
def test_no_false_confirmation_on_incomplete_evidence(missing):
    """The core safety property: incomplete evidence must NEVER produce
    CAMPAIGN_CRITERIA_MATCH."""
    r = classify_vehicle(_veh(**missing), CRIT)
    assert r.classification != "CAMPAIGN_CRITERIA_MATCH"
    assert r.classification in ("VERIFICATION_REQUIRED", "OUTSIDE_REPORT_VERSION")


def test_every_classification_explains_itself():
    variants = [
        _veh(),
        _veh(build_date=None),
        _veh(build_date="2024-02-04"),
        _veh(brake_feature_code="0004200"),
    ]
    for v in variants:
        r = classify_vehicle(v, CRIT)
        assert r.reasons, "classification without explanation"
        assert r.evidence_detail, "classification without per-criterion evidence"
        for c in r.evidence_detail:
            assert c["result"] in ("SATISFIED", "FAILED", "UNKNOWN", "CONFLICT")


def test_api_fleet_assessment_matches_service(client, incident_id):
    resp = client.post(f"/api/incidents/{incident_id}/assess-fleet")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_vehicles"] == 15
    assert body["counts"]["CAMPAIGN_CRITERIA_MATCH"] == 3
    assert body["counts"]["VERIFICATION_REQUIRED"] == 4
    assert body["counts"]["OUTSIDE_REPORT_VERSION"] == 8
    assert body["vin_status_counts"] == {"VIN_STATUS_UNAVAILABLE": 15}
    assert body["banner"].startswith("SYNTHETIC DEMONSTRATION DATA")
    for a in body["assessments"]:
        assert a["reasons"]
        assert a["classification_explanation"]
        assert a["vin_evidence_status"] == "VIN_STATUS_UNAVAILABLE"
