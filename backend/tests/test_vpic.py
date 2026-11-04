"""E. VIN handling tests: synthetic VINs never submitted, one-at-a-time,
caching, and unknown fields staying unknown (never inferred)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app import models
from app.services import vpic_service
from app.services.vpic_service import VINPolicyError, decode_single_vin


def _headers(client):
    r = client.post("/api/auth/login",
                    json={"username": "fleet.manager", "password": "TestPassword123!"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---------------------------------------------------------------------------
# Synthetic protection
# ---------------------------------------------------------------------------


def test_synthetic_vin_never_submitted_to_vpic(db, monkeypatch):
    """Guard 1: VIN strings containing synthetic markers are refused before
    any HTTP layer is touched."""
    def _forbidden(*a, **k):
        raise AssertionError("vPIC must never be called for synthetic VINs")

    monkeypatch.setattr(vpic_service.httpx, "Client", _forbidden)
    with pytest.raises(VINPolicyError):
        decode_single_vin(db, "SYNTHETIC0000004", None, "tester")
    with pytest.raises(VINPolicyError):
        decode_single_vin(db, "DEMO-0000000000001", None, "tester")


def test_fleet_synthetic_vin_never_submitted(db, monkeypatch):
    """Guard 2: VINs belonging to synthetic fleet records are refused even
    when they look like plausible VIN strings."""
    veh = db.scalar(select(models.FleetVehicle).where(models.FleetVehicle.asset_id == "SYNTHETIC-BUS-004"))
    veh.vin = "4X4MNBCC7SYNTH04"[:17]
    veh.synthetic = True
    db.commit()

    def _forbidden(*a, **k):
        raise AssertionError("vPIC must never be called for synthetic fleet VINs")

    monkeypatch.setattr(vpic_service.httpx, "Client", _forbidden)
    with pytest.raises(VINPolicyError):
        decode_single_vin(db, veh.vin, None, "tester")


def test_api_blocks_synthetic_vin(client, manager_headers, monkeypatch):
    def _forbidden(*a, **k):
        raise AssertionError("no outbound vPIC call allowed")

    monkeypatch.setattr(vpic_service.httpx, "Client", _forbidden)
    # Charset-valid synthetic marker (SAMPLE...) must hit the policy guard:
    r = client.post("/api/vpic/decode-single-vin", headers=manager_headers,
                    json={"vin": "SAMPLE00000000000"})
    assert r.status_code == 422
    assert "synthetic" in r.json()["detail"].lower()
    # The seeded synthetic fleet VIN is rejected outright (invalid VIN charset):
    r = client.post("/api/vpic/decode-single-vin", headers=manager_headers,
                    json={"vin": "SYNTHETIC-DEMO-VIN-0004"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# One-at-a-time interface
# ---------------------------------------------------------------------------


def test_schema_accepts_exactly_one_vin(client, manager_headers):
    r = client.post("/api/vpic/decode-single-vin", headers=manager_headers,
                    json={"vin": ["1HGCM82633A004352", "5UXWX7C55BA123456"]})
    assert r.status_code == 422  # a list is not a VIN


def test_schema_validates_vin_format(client, manager_headers):
    r = client.post("/api/vpic/decode-single-vin", headers=manager_headers,
                    json={"vin": "1HGCM82633IOQ435"})  # I, O, Q invalid in VINs
    assert r.status_code == 422
    r = client.post("/api/vpic/decode-single-vin", headers=manager_headers, json={"vin": "TOOSHORT"})
    assert r.status_code == 422


def test_no_bulk_endpoint_exists(client):
    from app.main import app

    paths = [getattr(r, "path", "") for r in app.routes]
    assert not any("bulk" in p.lower() for p in paths)
    vin_paths = [p for p in paths if "vin" in p.lower()]
    assert all("single" in p for p in vin_paths if p.startswith("/api/vpic"))


# ---------------------------------------------------------------------------
# Decoding with mocked vPIC: caching + unknown fields
# ---------------------------------------------------------------------------


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, *a, **k):
        _FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        assert "DecodeVinValuesExtended" in url
        # vPIC-style response: many fields, several empty (unavailable).
        return _FakeResponse({"Results": [{
            "VIN": "1HGCM82633A004352",
            "Make": "HONDA",
            "Model": "Accord",
            "ModelYear": "2003",
            "VehicleType": "PASSENGER CAR",
            "BrakeSystemType": "",          # NOT returned by vPIC
            "EngineConfiguration": "",      # NOT returned
            "ErrorCode": "0 - VIN decoded clean",
            "ErrorText": "",
        }]})


@pytest.fixture
def fake_vpic(monkeypatch):
    _FakeClient.instances = []
    monkeypatch.setattr(vpic_service.httpx, "Client", _FakeClient)
    monkeypatch.setattr(vpic_service, "_throttle", lambda: None)
    return _FakeClient


def test_decode_caches_results(db, fake_vpic):
    first = decode_single_vin(db, "1HGCM82633A004352", "2003", "tester")
    assert first["cached"] is False
    assert len(fake_vpic.instances) == 1
    second = decode_single_vin(db, "1HGCM82633A004352", "2003", "tester")
    assert second["cached"] is True
    assert len(fake_vpic.instances) == 1  # no second outbound request
    assert second["decoded_values"]["Make"] == "HONDA"


def test_missing_decoded_fields_remain_unknown_and_are_not_inferred(db, fake_vpic):
    result = decode_single_vin(db, "1HGCM82633A004352", None, "tester")
    assert "BrakeSystemType" in result["unavailable_fields"]
    assert "BrakeSystemType" not in result["decoded_values"]
    # Nothing brake-related was invented:
    dumped = json.dumps(result["decoded_values"]).lower()
    assert "hydraulic" not in dumped and "0004100" not in dumped


def test_decode_is_audited_as_not_recall_confirmation(db, fake_vpic):
    decode_single_vin(db, "1HGCM82633A004352", None, "tester")
    events = list(db.scalars(select(models.AuditEvent)))
    ev = [e for e in events if e.action == "VIN_DECODED_VPIC"]
    assert ev and ev[0].actor == "tester"
    assert "NOT recall confirmation" in ev[0].detail


def test_vpic_failure_surfaces_as_502(client, manager_headers, monkeypatch):
    import httpx

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise httpx.ConnectError("vpic unreachable")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(vpic_service.httpx, "Client", _Boom)
    monkeypatch.setattr(vpic_service, "_throttle", lambda: None)
    r = client.post("/api/vpic/decode-single-vin", headers=manager_headers,
                    json={"vin": "1HGCM82633A004352"})
    assert r.status_code == 502


def test_synthetic_fleet_never_auto_decoded(client, incident_id, db):
    """No workflow step (assessment, refresh, plan, approval) may create a
    VINDecodeCache entry - decodes are human-triggered only."""
    h = _headers(client)
    client.post(f"/api/incidents/{incident_id}/assess-fleet")
    client.post(f"/api/incidents/{incident_id}/set-operational-policy", headers=h,
                json={"policy": "HOLD_PENDING_VERIFICATION", "acknowledgement": True})
    plan = client.post(f"/api/incidents/{incident_id}/generate-recovery-plan", headers=h).json()
    client.post(f"/api/incidents/{incident_id}/approve-plan", headers=h,
                json={"proposal_uid": plan["proposal_uid"], "plan_hash": plan["plan_hash"],
                      "policy": "HOLD_PENDING_VERIFICATION"})
    assert list(db.scalars(select(models.VINDecodeCache))) == []
    # All synthetic vehicles remain VIN_STATUS_UNAVAILABLE:
    fa = client.get(f"/api/incidents/{incident_id}/fleet-assessment").json()
    assert fa["vin_status_counts"] == {"VIN_STATUS_UNAVAILABLE": 15}
