"""End-to-end Day-1 acceptance gate through the HTTP API:
fixture-backed evidence -> assessment -> policy -> plan -> approval -> audit,
plus a fully mocked live-NHTSA refresh."""
from __future__ import annotations

import hashlib

from app.services import nhtsa


def test_day1_gate_full_flow_offline(client, incident_id):
    # 1. Incident exists with campaign 26V111 and reviewed evidence.
    inc = client.get(f"/api/incidents/{incident_id}").json()
    assert inc["campaign_number"] == "26V111"
    assert inc["campaign"]["manufacturer"] == "International Motors, LLC"

    ev = client.get(f"/api/incidents/{incident_id}/evidence").json()
    assert ev["report_version"]["review_status"] == "REVIEWED"
    assert ev["report_version"]["document_sha256"] == (
        "a29c04faf2273ba30ecfac16c0e2dfd0550f2aa2d7529e85cdce35844c8125f6")
    assert ev["report_version"]["retrieval_timestamp"]
    feature = [c for c in ev["criteria"] if c["criterion_type"] == "FEATURE_CODE"]
    assert feature[0]["value"]["feature_code"] == "0004100"
    assert feature[0]["source_page"] == 1

    # 2. Deterministic fleet assessment.
    fa = client.post(f"/api/incidents/{incident_id}/assess-fleet").json()
    assert fa["total_vehicles"] == 15
    assert fa["counts"]["CAMPAIGN_CRITERIA_MATCH"] == 3

    # 3. Manager policy + deterministic recovery plan.
    login = client.post("/api/auth/login",
                        json={"username": "fleet.manager", "password": "TestPassword123!"}).json()
    h = {"Authorization": f"Bearer {login['token']}"}
    client.post(f"/api/incidents/{incident_id}/set-operational-policy", headers=h,
                json={"policy": "HOLD_PENDING_VERIFICATION", "acknowledgement": True})
    plan = client.post(f"/api/incidents/{incident_id}/generate-recovery-plan", headers=h).json()
    assert plan["summary"]["routes_disrupted"] == 5
    assert plan["summary"]["routes_recoverable"] == 2

    # 4. Approval persists state and audit events.
    r = client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                    json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                          "policy": "HOLD_PENDING_VERIFICATION"})
    assert r.status_code == 200
    body = r.json()
    assert body["incident"]["status"] == "PLAN_APPROVED"
    assert any(e["action"] == "RECOVERY_PLAN_APPROVED" for e in body["audit_events"])

    # 5. Fleet + route endpoints reflect the persisted post-approval state.
    routes = {r["route_id"]: r for r in client.get("/api/fleet/routes").json()}
    vehicles = {v["asset_id"]: v for v in client.get("/api/fleet/vehicles").json()}
    by_id = {v["id"]: a for a, v in vehicles.items()}
    assert by_id[routes["R-101"]["assigned_vehicle_id"]] == "SYNTHETIC-SPARE-01"
    assert vehicles["SYNTHETIC-BUS-001"]["availability"] == "GROUNDED_RECALL"
    assert vehicles["SYNTHETIC-BUS-004"]["availability"] == "HELD_PENDING_VERIFICATION"

    # 6. Benchmark available through the API.
    b = client.post("/api/benchmarks/run").json()
    assert b["metrics"]["false_confirmation_rate"] == 0.0


def test_live_refresh_flow_with_mocked_nhtsa(client, incident_id, real_pdf_bytes, real_api_payload, monkeypatch):
    monkeypatch.setattr(
        nhtsa, "fetch_campaign",
        lambda cn: nhtsa.CampaignApiResponse(cn, real_api_payload, real_api_payload["results"]))
    monkeypatch.setattr(
        nhtsa, "download_report",
        lambda url, cache_dir=None: nhtsa.ReportDocument(
            url, real_pdf_bytes, hashlib.sha256(real_pdf_bytes).hexdigest()))

    r = client.post(f"/api/incidents/{incident_id}/refresh-recall")
    assert r.status_code == 200
    body = r.json()
    assert body["evidence"]["evidence_source"] == "LIVE_EXTRACTION"
    assert body["evidence"]["using_reviewed_fixture"] is False
    assert body["evidence"]["campaign"]["make"] == "IC BUS"
    assert body["evidence"]["report_version"]["review_status"] == "REVIEWED"

    # Assessment still works against the live-reviewed evidence.
    fa = client.post(f"/api/incidents/{incident_id}/assess-fleet").json()
    assert fa["counts"]["CAMPAIGN_CRITERIA_MATCH"] == 3

    # Audit records the refresh with its provenance.
    aud = client.get(f"/api/incidents/{incident_id}/audit").json()
    refresh_events = [e for e in aud if e["action"] == "RECALL_EVIDENCE_REFRESHED"]
    assert refresh_events and refresh_events[0]["result"] == "OK"
    assert refresh_events[0]["new_state"]["evidence_source"] == "LIVE_EXTRACTION"
