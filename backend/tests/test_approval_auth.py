"""D+F. Authorization, transactional approval, and append-only audit tests."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import models


def _headers(client):
    r = client.post("/api/auth/login",
                    json={"username": "fleet.manager", "password": "TestPassword123!"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _make_pending_plan(client, incident_id, policy="HOLD_PENDING_VERIFICATION"):
    h = _headers(client)
    client.post(f"/api/incidents/{incident_id}/set-operational-policy", headers=h,
                json={"policy": policy, "acknowledgement": True})
    plan = client.post(f"/api/incidents/{incident_id}/generate-recovery-plan", headers=h).json()
    return h, plan


# ---------------------------------------------------------------------------
# Authentication & authorization
# ---------------------------------------------------------------------------


def test_login_rejects_bad_password(client):
    r = client.post("/api/auth/login", json={"username": "fleet.manager", "password": "wrong"})
    assert r.status_code == 401


def test_login_rejects_unknown_user(client):
    r = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401


@pytest.mark.parametrize("path", [
    "/api/incidents/1/set-operational-policy",
    "/api/incidents/1/generate-recovery-plan",
    "/api/incidents/1/approve-plan",
    "/api/vpic/decode-single-vin",
])
def test_unauthenticated_write_endpoints_rejected(client, path):
    r = client.post(path, json={})
    assert r.status_code == 401


def test_tampered_token_rejected(client):
    h = _headers(client)
    token = h["Authorization"].split(" ", 1)[1]
    body, sig = token.split(".")
    r = client.post("/api/incidents/1/generate-recovery-plan",
                    headers={"Authorization": f"Bearer {body}.{'0' * 64}"})
    assert r.status_code == 401
    r = client.post("/api/incidents/1/generate-recovery-plan",
                    headers={"Authorization": f"Bearer {body[:-2]}xx.{sig}"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Approval transaction
# ---------------------------------------------------------------------------


def test_no_operational_state_change_before_approval(client, incident_id, db):
    h, plan = _make_pending_plan(client, incident_id)
    # Routes and vehicles unchanged after policy + plan generation.
    routes = {r.route_id: r.assigned_vehicle_id for r in db.scalars(select(models.FleetRoute))}
    assert routes["R-101"] is not None and routes["R-102"] is not None
    bus1 = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-BUS-001"))
    assert bus1.availability == "ASSIGNED_TO_ROUTE"
    approvals = list(db.scalars(select(models.Approval)))
    assert approvals == []


def test_invalid_proposal_uid_rejected(client, incident_id):
    h, plan = _make_pending_plan(client, incident_id)
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": "PROP-NOPE-12345678", "plan_hash": plan["plan_hash"],
                          "policy": "HOLD_PENDING_VERIFICATION"})
    assert r.status_code == 409
    assert "not found" in r.json()["detail"]["reason"].lower()


def test_stale_plan_hash_rejected(client, incident_id):
    h, plan = _make_pending_plan(client, incident_id)
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": "f" * 64,
                          "policy": "HOLD_PENDING_VERIFICATION"})
    assert r.status_code == 409
    assert "hash mismatch" in r.json()["detail"]["reason"].lower()


def test_policy_mismatch_rejected(client, incident_id):
    h, plan = _make_pending_plan(client, incident_id, policy="HOLD_PENDING_VERIFICATION")
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "MANAGER_REVIEW_ONLY"})
    assert r.status_code == 409


def test_approval_rechecks_spare_availability(client, incident_id, db):
    """If a proposed spare becomes unavailable between generation and
    approval, the approval must fail and change nothing."""
    h, plan = _make_pending_plan(client, incident_id)
    spare = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-SPARE-01"))
    spare.availability = "IN_MAINTENANCE"
    db.commit()
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "HOLD_PENDING_VERIFICATION"})
    assert r.status_code == 409
    assert "no longer available" in r.json()["detail"]["reason"].lower()
    # Nothing was applied:
    route = db.scalar(select(models.FleetRoute).where(models.FleetRoute.route_id == "R-101"))
    bus1 = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-BUS-001"))
    assert route.assigned_vehicle_id == bus1.id
    assert bus1.availability == "ASSIGNED_TO_ROUTE"


def test_approval_rechecks_route_still_has_original_vehicle(client, incident_id, db):
    h, plan = _make_pending_plan(client, incident_id)
    route = db.scalar(select(models.FleetRoute).where(models.FleetRoute.route_id == "R-101"))
    route.assigned_vehicle_id = None  # simulate concurrent operational change
    db.commit()
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "HOLD_PENDING_VERIFICATION"})
    assert r.status_code == 409
    assert "stale" in r.json()["detail"]["reason"].lower()


def test_stale_infeasible_route_rejects_whole_approval_without_applying_anything(client, incident_id, db):
    """Regression: routes with NO feasible replacement must also be
    re-validated. A stale infeasible route rejects the entire approval and
    rolls back every other change (including feasible reassignments)."""
    h, plan = _make_pending_plan(client, incident_id, policy="HOLD_PENDING_VERIFICATION")
    # R-104 is infeasible in this plan (no compatible spare at South depot).
    r104 = next(a for a in plan["assignments"] if a["route"]["route_id"] == "R-104")
    assert r104["feasible"] is False and r104["replacement_vehicle"] is None
    route = db.scalar(select(models.FleetRoute).where(models.FleetRoute.route_id == "R-104"))
    bus004 = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-BUS-004"))
    other = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-BUS-007"))
    route.assigned_vehicle_id = other.id  # concurrent reassignment since generation
    db.commit()

    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "HOLD_PENDING_VERIFICATION"})
    assert r.status_code == 409
    assert "stale" in r.json()["detail"]["reason"].lower()

    # NOTHING was applied anywhere in the plan:
    db.expire_all()
    r101 = db.scalar(select(models.FleetRoute).where(models.FleetRoute.route_id == "R-101"))
    bus001 = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-BUS-001"))
    spare1 = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-SPARE-01"))
    assert r101.assigned_vehicle_id == bus001.id          # feasible reassignment NOT applied
    assert bus001.availability == "ASSIGNED_TO_ROUTE"     # match bus NOT grounded
    assert spare1.availability == "AVAILABLE"             # spare NOT consumed
    assert bus004.availability == "ASSIGNED_TO_ROUTE"     # held bus NOT changed
    proposal = db.scalar(select(models.RecoveryProposal).where(
        models.RecoveryProposal.proposal_uid == plan["proposal_uid"]))
    assert proposal.status == "PENDING"                   # still approvable after regeneration
    # Rejection was audited:
    events = list(db.scalars(select(models.AuditEvent)))
    rejected = [e for e in events if e.action == "RECOVERY_PLAN_APPROVAL_REJECTED"]
    assert rejected and rejected[0].actor == "fleet.manager"
    assert "stale" in rejected[0].detail.lower()


def test_stale_manager_review_route_rejects_approval_without_removing_anything(client, incident_id, db):
    """Regression: NOT_APPLICABLE (manager-review-only) assignments must also
    be re-validated against the live route state."""
    h, plan = _make_pending_plan(client, incident_id, policy="MANAGER_REVIEW_ONLY")
    flagged = [a for a in plan["assignments"] if a["status"] == "NOT_APPLICABLE"]
    assert {a["route"]["route_id"] for a in flagged} == {"R-104", "R-105"}
    # Concurrent change on a flagged route:
    route = db.scalar(select(models.FleetRoute).where(models.FleetRoute.route_id == "R-105"))
    route.assigned_vehicle_id = None
    db.commit()

    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "MANAGER_REVIEW_ONLY"})
    assert r.status_code == 409
    assert "stale" in r.json()["detail"]["reason"].lower()

    # No operational change anywhere:
    db.expire_all()
    r101 = db.scalar(select(models.FleetRoute).where(models.FleetRoute.route_id == "R-101"))
    r104 = db.scalar(select(models.FleetRoute).where(models.FleetRoute.route_id == "R-104"))
    bus001 = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-BUS-001"))
    bus004 = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-BUS-004"))
    spare1 = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-SPARE-01"))
    assert r101.assigned_vehicle_id == bus001.id
    assert spare1.availability == "AVAILABLE"
    assert bus001.availability == "ASSIGNED_TO_ROUTE"
    assert r104.assigned_vehicle_id == bus004.id
    assert bus004.operational_status != "FLAGGED_FOR_MANAGER_REVIEW"  # flag not applied
    events = list(db.scalars(select(models.AuditEvent)))
    assert any(e.action == "RECOVERY_PLAN_APPROVAL_REJECTED" for e in events)


def test_successful_approval_persists_state_and_audit(client, incident_id, db):
    h, plan = _make_pending_plan(client, incident_id)
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "HOLD_PENDING_VERIFICATION"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approval"]["proposal_uid"] == plan["proposal_uid"]
    assert body["approval"]["policy"] == "HOLD_PENDING_VERIFICATION"
    assert body["incident"]["status"] == "PLAN_APPROVED"

    veh = {v.asset_id: v for v in db.scalars(select(models.FleetVehicle))}
    routes = {rt.route_id: rt for rt in db.scalars(select(models.FleetRoute))}
    # Reassignments applied:
    assert routes["R-101"].assigned_vehicle_id == veh["SYNTHETIC-SPARE-01"].id
    assert routes["R-102"].assigned_vehicle_id == veh["SYNTHETIC-SPARE-02"].id
    # Matched buses grounded:
    assert veh["SYNTHETIC-BUS-001"].availability == "GROUNDED_RECALL"
    assert veh["SYNTHETIC-BUS-002"].operational_status == "GROUNDED_PENDING_REMEDY"
    # Verification-required bus held (policy A):
    assert veh["SYNTHETIC-BUS-004"].availability == "HELD_PENDING_VERIFICATION"
    # Spares consumed:
    assert veh["SYNTHETIC-SPARE-01"].availability == "ASSIGNED_TO_ROUTE"
    # Infeasible routes left unassigned:
    assert routes["R-103"].assigned_vehicle_id is None
    # Proposal + incident state:
    proposal = db.scalar(select(models.RecoveryProposal).where(
        models.RecoveryProposal.proposal_uid == plan["proposal_uid"]))
    assert proposal.status == "APPROVED"
    assert proposal.decided_by == "fleet.manager"
    incident = db.get(models.Incident, incident_id)
    assert incident.status == "PLAN_APPROVED"
    # Approval record:
    approval = db.scalar(select(models.Approval))
    assert approval.result == "APPROVED"
    assert approval.plan_hash == plan["plan_hash"]
    assert approval.previous_state["routes"] and approval.new_state["routes"]


def test_double_approval_rejected(client, incident_id):
    h, plan = _make_pending_plan(client, incident_id)
    payload = {"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
               "policy": "HOLD_PENDING_VERIFICATION"}
    assert client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h, json=payload).status_code == 200
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h, json=payload)
    assert r.status_code == 409


def test_policy_change_supersedes_pending_plan(client, incident_id, db):
    h, plan = _make_pending_plan(client, incident_id, policy="HOLD_PENDING_VERIFICATION")
    # Switch policy: the pending plan (generated under the old policy) must die.
    r = client.post(f"/api/incidents/{incident_id}/set-operational-policy", headers=h,
                    json={"policy": "MANAGER_REVIEW_ONLY", "acknowledgement": True})
    assert r.status_code == 200
    row = db.scalar(select(models.RecoveryProposal).where(
        models.RecoveryProposal.proposal_uid == plan["proposal_uid"]))
    db.refresh(row)
    assert row.status == "SUPERSEDED"
    # Approving the superseded plan is refused:
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "HOLD_PENDING_VERIFICATION"})
    assert r.status_code == 409


def test_manager_review_only_approval_flags_without_removing(client, incident_id, db):
    h, plan = _make_pending_plan(client, incident_id, policy="MANAGER_REVIEW_ONLY")
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "MANAGER_REVIEW_ONLY"})
    assert r.status_code == 200, r.text
    veh = {v.asset_id: v for v in db.scalars(select(models.FleetVehicle))}
    routes = {rt.route_id: rt for rt in db.scalars(select(models.FleetRoute))}
    # Verification-required buses KEEP their assignments, flagged for review:
    assert routes["R-104"].assigned_vehicle_id == veh["SYNTHETIC-BUS-004"].id
    assert veh["SYNTHETIC-BUS-004"].operational_status == "FLAGGED_FOR_MANAGER_REVIEW"
    assert routes["R-105"].assigned_vehicle_id == veh["SYNTHETIC-BUS-005"].id
    # Criteria-match buses still grounded and replaced where feasible:
    assert veh["SYNTHETIC-BUS-001"].availability == "GROUNDED_RECALL"
    assert routes["R-101"].assigned_vehicle_id == veh["SYNTHETIC-SPARE-01"].id


# ---------------------------------------------------------------------------
# Audit trail (append-only)
# ---------------------------------------------------------------------------


def test_audit_events_created_on_approval_with_correct_states(client, incident_id, db):
    h, plan = _make_pending_plan(client, incident_id)
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "HOLD_PENDING_VERIFICATION"})
    assert r.status_code == 200
    events = list(db.scalars(select(models.AuditEvent).order_by(models.AuditEvent.id)))
    actions = [e.action for e in events]
    assert "RECOVERY_PLAN_APPROVED" in actions
    assert "ROUTE_REASSIGNED" in actions
    approved = next(e for e in events if e.action == "RECOVERY_PLAN_APPROVED")
    assert approved.actor == "fleet.manager"
    assert approved.proposal_id == plan["proposal_uid"]
    assert approved.policy == "HOLD_PENDING_VERIFICATION"
    assert approved.result == "APPROVED"
    assert approved.previous_state and approved.new_state
    reassigned = [e for e in events if e.action == "ROUTE_REASSIGNED"]
    for e in reassigned:
        assert e.actor == "fleet.manager"
        assert e.previous_state["route"]["assigned_vehicle"].startswith("SYNTHETIC-BUS")
        assert e.new_state["route"]["assigned_vehicle"].startswith("SYNTHETIC-SPARE")


def test_audit_visible_via_api_immediately_after_approval(client, incident_id):
    h, plan = _make_pending_plan(client, incident_id)
    before = client.get(f"/api/incidents/{incident_id}/audit").json()
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "HOLD_PENDING_VERIFICATION"})
    assert r.status_code == 200
    # The approval response itself carries the fresh audit entries:
    new_in_response = r.json()["audit_events"]
    assert any(e["action"] == "RECOVERY_PLAN_APPROVED" for e in new_in_response)
    after = client.get(f"/api/incidents/{incident_id}/audit").json()
    assert len(after) > len(before)


def test_rejected_approval_is_audited(client, incident_id, db):
    h, plan = _make_pending_plan(client, incident_id)
    client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                json={"proposal_uid": plan["proposal_uid"], "plan_hash": "f" * 64,
                      "policy": "HOLD_PENDING_VERIFICATION"})
    events = list(db.scalars(select(models.AuditEvent)))
    rejected = [e for e in events if e.action == "RECOVERY_PLAN_APPROVAL_REJECTED"]
    assert rejected and rejected[0].result == "REJECTED"
    assert rejected[0].actor == "fleet.manager"


def test_policy_selection_is_audited(client, incident_id, db):
    _make_pending_plan(client, incident_id)
    events = list(db.scalars(select(models.AuditEvent)))
    pol = [e for e in events if e.action == "OPERATIONAL_POLICY_SELECTED"]
    assert pol and pol[0].actor == "fleet.manager"
    assert pol[0].new_state["policy"] == "HOLD_PENDING_VERIFICATION"


def test_audit_log_has_no_mutation_endpoints(client):
    """Append-only: no API route may update or delete audit events."""
    from app.main import app

    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set())
        if "audit" in path:
            assert methods <= {"GET"}, f"audit route {path} exposes {methods}"


def test_audit_module_exposes_no_update_or_delete():
    from app.services import audit as audit_module

    source = open(audit_module.__file__).read()
    assert "db.delete" not in source
    assert ".delete(" not in source
    assert "UPDATE audit_events" not in source.upper()
