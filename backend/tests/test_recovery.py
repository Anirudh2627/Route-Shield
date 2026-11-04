"""C. Route recovery engine tests: capacity, depot compatibility, duplicate
assignment prevention, and policy-dependent handling."""
from __future__ import annotations

import pytest

from app import models
from app.services import recovery
from sqlalchemy import select


def _login_and_set_policy(client, incident_id, policy):
    headers = {"Authorization": "Bearer " + client.post(
        "/api/auth/login",
        json={"username": "fleet.manager", "password": "TestPassword123!"}).json()["token"]}
    r = client.post(f"/api/incidents/{incident_id}/set-operational-policy", headers=headers,
                    json={"policy": policy, "acknowledgement": True})
    assert r.status_code == 200, r.text
    return headers


def _plan(client, incident_id, headers):
    r = client.post(f"/api/incidents/{incident_id}/generate-recovery-plan", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_plan_requires_explicit_policy_first(client, incident_id):
    headers = {"Authorization": "Bearer " + client.post(
        "/api/auth/login",
        json={"username": "fleet.manager", "password": "TestPassword123!"}).json()["token"]}
    r = client.post(f"/api/incidents/{incident_id}/generate-recovery-plan", headers=headers)
    assert r.status_code == 409
    assert "policy" in r.json()["detail"].lower()


def test_hold_policy_disrupts_verification_required_routes(client, incident_id):
    headers = _login_and_set_policy(client, incident_id, "HOLD_PENDING_VERIFICATION")
    plan = _plan(client, incident_id, headers)
    disrupted = {a["route"]["route_id"] for a in plan["assignments"]
                 if a["status"] == "PROPOSED"}
    # 3 criteria-match routes + 2 verification-required routes (BUS-004, BUS-005)
    assert disrupted == {"R-101", "R-102", "R-103", "R-104", "R-105"}
    assert plan["summary"]["routes_disrupted"] == 5
    assert plan["summary"]["routes_flagged_review_only"] == 0


def test_review_only_policy_retains_assignments_and_flags(client, incident_id):
    headers = _login_and_set_policy(client, incident_id, "MANAGER_REVIEW_ONLY")
    plan = _plan(client, incident_id, headers)
    flagged = {a["route"]["route_id"] for a in plan["assignments"] if a["status"] == "NOT_APPLICABLE"}
    disrupted = {a["route"]["route_id"] for a in plan["assignments"] if a["status"] == "PROPOSED"}
    assert flagged == {"R-104", "R-105"}
    assert disrupted == {"R-101", "R-102", "R-103"}
    for a in plan["assignments"]:
        if a["status"] == "NOT_APPLICABLE":
            assert "does NOT establish" in a["disruption_reason"]


def test_sufficient_spare_capacity_produces_feasible_replacement(client, incident_id):
    headers = _login_and_set_policy(client, incident_id, "HOLD_PENDING_VERIFICATION")
    plan = _plan(client, incident_id, headers)
    r101 = next(a for a in plan["assignments"] if a["route"]["route_id"] == "R-101")
    assert r101["feasible"] is True
    assert r101["replacement_vehicle"]["asset_id"] == "SYNTHETIC-SPARE-01"
    assert r101["capacity_offered"] >= r101["capacity_required"]
    assert r101["capacity_ok"] and r101["depot_ok"] and r101["characteristics_ok"]


def test_insufficient_capacity_is_reported_not_forced(client, incident_id):
    headers = _login_and_set_policy(client, incident_id, "HOLD_PENDING_VERIFICATION")
    plan = _plan(client, incident_id, headers)
    r104 = next(a for a in plan["assignments"] if a["route"]["route_id"] == "R-104")
    assert r104["feasible"] is False
    assert r104["replacement_vehicle"] is None
    assert any("Insufficient capacity" in i for i in r104["unresolved_issues"])


def test_depot_incompatibility_is_reported(client, incident_id):
    headers = _login_and_set_policy(client, incident_id, "HOLD_PENDING_VERIFICATION")
    plan = _plan(client, incident_id, headers)
    r103 = next(a for a in plan["assignments"] if a["route"]["route_id"] == "R-103")
    assert r103["feasible"] is False
    assert any("Depot incompatible" in i for i in r103["unresolved_issues"])


def test_no_duplicate_spare_assignment(client, incident_id):
    headers = _login_and_set_policy(client, incident_id, "HOLD_PENDING_VERIFICATION")
    plan = _plan(client, incident_id, headers)
    replacements = [a["replacement_vehicle"]["asset_id"] for a in plan["assignments"]
                    if a.get("replacement_vehicle")]
    assert len(replacements) == len(set(replacements))


def test_unavailable_spare_is_excluded(client, incident_id, db):
    spare = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-SPARE-01"))
    spare.availability = "IN_MAINTENANCE"
    db.commit()
    headers = _login_and_set_policy(client, incident_id, "HOLD_PENDING_VERIFICATION")
    plan = _plan(client, incident_id, headers)
    r101 = next(a for a in plan["assignments"] if a["route"]["route_id"] == "R-101")
    assert r101["feasible"] is False
    assert r101["replacement_vehicle"] is None


def test_verification_required_spare_is_never_proposed(client, incident_id, db):
    """Proposing an unverified spare would be an implicit safety decision."""
    spare = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-SPARE-02"))
    # Turn SPARE-02 (IC BUS CESB MY2022, out of report) into a match:
    spare.model_year = 2026
    from datetime import date
    spare.build_date = date(2025, 1, 10)
    spare.brake_feature_code = "0004100"
    db.commit()
    r = client.post(f"/api/incidents/{incident_id}/assess-fleet")
    assert r.status_code == 200
    headers = _login_and_set_policy(client, incident_id, "HOLD_PENDING_VERIFICATION")
    plan = _plan(client, incident_id, headers)
    for a in plan["assignments"]:
        assert (a.get("replacement_vehicle") or {}).get("asset_id") != "SYNTHETIC-SPARE-02"


def test_regeneration_supersedes_pending_proposal(client, incident_id):
    headers = _login_and_set_policy(client, incident_id, "HOLD_PENDING_VERIFICATION")
    p1 = _plan(client, incident_id, headers)
    p2 = _plan(client, incident_id, headers)
    assert p1["proposal_uid"] != p2["proposal_uid"]
    # plan content identical => identical hash (deterministic engine)
    assert p1["plan_hash"] == p2["plan_hash"]
    latest = client.get(f"/api/incidents/{incident_id}/recovery-plan").json()
    assert latest["proposal_uid"] == p2["proposal_uid"]


def test_plan_is_deterministic(client, incident_id):
    headers = _login_and_set_policy(client, incident_id, "HOLD_PENDING_VERIFICATION")
    a = _plan(client, incident_id, headers)
    b = _plan(client, incident_id, headers)
    strip = lambda p: [{k: v for k, v in x.items() if k not in ("id", "proposal")} for x in p["assignments"]]
    assert strip(a) == strip(b)
