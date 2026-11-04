"""Regression tests for the bounded Incident Response Agent.

Covers:
* read-only tool registry integrity (names, schemas, argument validation),
* write-path isolation (static + behavioural),
* the bounded investigation loop via the clearly-labelled STUB provider
  (never presented as real inference): happy path, grounding rejection,
  classification-override rejection, invalid JSON finals, max iterations,
  provider errors, unconfigured provider, JSON-protocol downgrade, timeouts,
* persistence of sessions/steps and that operational tables are untouched,
* the /api/agents endpoints (auth required to start; open reads).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import replace

import pytest

from app import models
from app.services import agent as agent_svc
from app.services import agent_tools

STUB_LABEL = StubChatProvider_TEST_LABEL = "STUB-PROVIDER-TEST-ONLY"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def first_incident(db) -> models.Incident:
    return db.query(models.Incident).order_by(models.Incident.id).first()


def seeded_classifications(db, incident_id: int) -> dict[str, str]:
    out = agent_tools.tool_get_fleet_assessment(
        db, agent_tools.IncidentArgs(incident_id=incident_id)
    )
    return {v["asset_id"]: v["classification"] for v in out["vehicles"]}


def grounded_final_payload(db, incident: models.Incident) -> dict:
    """Build a final assessment strictly from real tool outputs of this db."""
    evidence = agent_tools.tool_get_recall_evidence(
        db, agent_tools.IncidentArgs(incident_id=incident.id)
    )
    assess = agent_tools.tool_get_fleet_assessment(
        db, agent_tools.IncidentArgs(incident_id=incident.id)
    )
    preview = agent_tools.tool_preview_route_impact(
        db,
        agent_tools.RouteImpactArgs(
            incident_id=incident.id, policy="HOLD_PENDING_VERIFICATION"
        ),
    )
    doc_ref = (evidence.get("report_version") or {}).get("ref", "DOC-0123456789abcdef")
    v0 = assess["vehicles"][0]
    facts = [
        {
            "statement": f"Campaign {evidence['campaign_number']} applies to model years "
            f"{evidence['model_years']}.",
            "evidence_refs": [doc_ref],
        },
        {
            "statement": f"{v0['asset_id']} is classified {v0['classification']} by the "
            "deterministic engine.",
            "evidence_refs": [v0["ref"]],
        },
    ]
    affected = []
    candidates = []
    for pv in preview["previews"]:
        for r in pv["impacted_routes"]:
            if r["route_id"] not in affected:
                affected.append(r["route_id"])
            feasible = [c for c in r["replacement_candidates"] if c["feasible"]]
            if feasible:
                candidates.append({
                    "route_id": r["route_id"],
                    "spare_asset_id": feasible[0]["spare_asset_id"],
                    "feasibility_note": "deterministic preview marked this spare feasible",
                })
            else:
                candidates.append({
                    "route_id": r["route_id"],
                    "spare_asset_id": None,
                    "feasibility_note": "no feasible replacement in the deterministic preview",
                })
    refs = list(dict.fromkeys(
        [doc_ref, v0["ref"]] + [r for r in assess["evidence_refs"][:5]] + affected
    ))
    return {
        "incident_summary": (
            f"Investigation of campaign {evidence['campaign_number']} over "
            f"{assess['total_vehicles']} synthetic fleet records."
        ),
        "objective_interpretation": "Assess recall exposure and route impact for the incident.",
        "facts": facts,
        "inferences": [],
        "vehicle_classifications": [
            {"asset_id": v0["asset_id"], "classification": v0["classification"],
             "basis_ref": v0["ref"]},
        ],
        "affected_routes": affected,
        "recovery_candidates": candidates,
        "unresolved_constraints": [],
        "unknowns_and_missing_evidence": [
            "VIN-level recall confirmation is unavailable for synthetic fleet records."
        ],
        "proposed_next_actions": [
            {
                "action": "Manager should review the deterministic assessment and select an "
                "operational policy before any plan generation.",
                "rationale": "Policy selection and approvals are human-only decisions.",
                "requires_human_approval": True,
            }
        ],
        "uncertainty_notes": evidence.get("evidence_banner", ""),
        "evidence_references": [
            {"ref_id": r, "description": "observed in tool output", "source": ""}
            for r in refs
        ],
        "human_approval_required": True,
    }


def operational_snapshot(db) -> dict[str, list]:
    """Column-level dump of every operational (non-agent) table."""
    from sqlalchemy import inspect as sa_inspect

    tables = [
        models.FleetVehicle, models.FleetRoute, models.VehicleAssessment,
        models.OperationalPolicySelection, models.RecoveryProposal, models.Approval,
        models.AuditEvent, models.ExtractedCriterion, models.RecallReportVersion,
    ]
    out: dict[str, list] = {}
    for t in tables:
        rows = []
        for row in db.query(t).all():
            attrs = sa_inspect(row).mapper.column_attrs
            rows.append(json.dumps(
                {a.key: repr(getattr(row, a.key)) for a in attrs}, sort_keys=True
            ))
        out[t.__name__] = sorted(rows)
    return out


# ---------------------------------------------------------------------------
# Tool registry integrity
# ---------------------------------------------------------------------------


def test_registry_is_read_only_by_name():
    forbidden = ("approve", "apply", "assign", "update", "write", "delete",
                 "create", "set_policy", "generate", "close", "mark", "mutate")
    for name in agent_tools.REGISTRY:
        assert not any(tok in name.lower() for tok in forbidden), name


def test_registry_schemas_and_args_validation(db):
    defs = agent_tools.openai_tool_defs()
    assert {d["function"]["name"] for d in defs} == set(agent_tools.REGISTRY)
    for d in defs:
        assert d["type"] == "function"
        assert d["function"]["parameters"]["type"] == "object"

    with pytest.raises(ValueError):
        agent_tools.parse_args("assign_route", {})
    with pytest.raises(ValueError):
        agent_tools.parse_args("get_recall_evidence", {"incident_id": 0})
    with pytest.raises(ValueError):
        agent_tools.parse_args("get_recall_evidence", {"incident_id": "one"})
    args = agent_tools.parse_args("get_recall_evidence", {"incident_id": 1})
    assert args.incident_id == 1


def test_agent_modules_never_import_write_paths():
    import app.services.agent as a
    import app.services.agent_tools as t
    for module in (a, t):
        src = open(module.__file__).read()
        for line in src.splitlines():
            if re.match(r"^\s*(from|import)\s+", line):
                assert "approval" not in line, f"{module.__name__}: {line}"
                assert "apply_approval" not in line
    # And no write-ish call sneaks in via attribute use.
    assert not hasattr(t, "apply_approval")


# ---------------------------------------------------------------------------
# Investigation loop (STUB provider - clearly labelled, tests only)
# ---------------------------------------------------------------------------


def _happy_script(db, incident) -> list[dict]:
    return [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": grounded_final_payload(db, incident)},
    ]


def test_happy_path_investigation_persists_trace(db):
    incident = first_incident(db)
    provider = agent_svc.StubChatProvider(_happy_script(db, incident))
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Assess recall exposure and route impact.",
        requested_by="fleet.manager", provider=provider,
    )
    assert session.status == "COMPLETED"
    assert session.stop_reason == "MODEL_STOPPED"
    assert STUB_LABEL in session.model  # never presentable as real inference
    assert session.tool_calls_count == 3
    assert session.iterations_used == 4
    kinds = [(s.kind, s.tool_name or s.status) for s in session.steps]
    assert kinds[0] == ("TOOL_CALL", "get_recall_evidence")
    assert kinds[1] == ("TOOL_CALL", "get_fleet_assessment")
    assert kinds[2] == ("TOOL_CALL", "preview_route_impact")
    assert kinds[-1] == ("FINAL", "OK")
    assert session.final_result is not None
    assert session.final_result["objective_interpretation"]
    assert session.duration_ms is not None and session.duration_ms >= 0
    # Native protocol: tool definitions were passed to the provider.
    assert provider.received_tool_defs[0] is not None
    # Tool results were actually delivered to the model.
    second_call = provider.received_messages[2]
    assert any(m.get("role") == "tool" for m in second_call)


def test_operational_state_untouched_by_investigation(db):
    incident = first_incident(db)
    before = operational_snapshot(db)
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Read-only investigation.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(_happy_script(db, incident)),
    )
    assert session.status == "COMPLETED"
    after = operational_snapshot(db)
    assert before == after


def test_grounding_rejects_fabricated_assets_after_retries(db):
    incident = first_incident(db)
    bad = grounded_final_payload(db, incident)
    bad["vehicle_classifications"] = [
        {"asset_id": "SYNTHETIC-BUS-999", "classification": "CAMPAIGN_CRITERIA_MATCH",
         "basis_ref": "ASSESS-1"},
    ]
    script = [
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"final": bad},   # rejected attempt 1
        {"final": bad},   # rejected attempt 2 -> investigation fails
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test grounding.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "GROUNDING_VALIDATION_FAILED"
    assert session.final_result is None  # ungrounded output never becomes fact
    assert session.grounding_violations
    assert any("SYNTHETIC-BUS-999" in v for v in session.grounding_violations)
    validation_steps = [s for s in session.steps if s.kind == "VALIDATION"]
    assert len(validation_steps) == 2


def test_grounding_rejects_classification_override(db):
    incident = first_incident(db)
    engine = seeded_classifications(db, incident.id)
    asset, real = next(iter(engine.items()))
    other = next(c for c in ("CAMPAIGN_CRITERIA_MATCH", "VERIFICATION_REQUIRED",
                             "OUTSIDE_REPORT_VERSION") if c != real)
    payload = grounded_final_payload(db, incident)
    payload["vehicle_classifications"] = [
        {"asset_id": asset, "classification": other, "basis_ref": "ASSESS-1"},
    ]
    script = [
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"final": payload},
        {"final": payload},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test override rejection.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "GROUNDING_VALIDATION_FAILED"
    assert any("contradicts the deterministic engine" in v
               for v in session.grounding_violations)


def test_corrected_final_is_accepted(db):
    incident = first_incident(db)
    bad = grounded_final_payload(db, incident)
    bad["evidence_references"] = [
        {"ref_id": "CRIT-424242", "description": "fabricated", "source": ""}
    ]
    good = grounded_final_payload(db, incident)
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": bad},    # rejected once
        {"final": good},   # corrected -> accepted
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test correction path.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    assert session.stop_reason == "MODEL_STOPPED"
    assert [s for s in session.steps if s.kind == "VALIDATION"]
    assert session.final_result is not None


def test_invalid_json_final_fails_after_retries(db):
    incident = first_incident(db)
    script = [
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"raw": "I think the fleet is fine, trust me."},
        {"raw": "{\"action\": \"final\", but broken"},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test invalid finals.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "INVALID_FINAL_OUTPUT"
    assert session.final_result is None


def test_max_iterations_enforced_at_eight(db):
    incident = first_incident(db)
    script = [
        {"tool": "get_open_verification_items", "args": {"incident_id": incident.id}}
        for _ in range(8)
    ] + [{"final": grounded_final_payload(db, incident)}]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Loop forever.",
        requested_by="fleet.manager",
        provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "MAX_ITERATIONS"
    assert session.iterations_used == 8
    assert session.final_result is None
    assert session.tool_calls_count == 8


def test_provider_error_is_sanitized_and_persisted(db):
    incident = first_incident(db)
    script = [{"error": "upstream exploded (sk-abcdef1234567890)"}]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test provider failure.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "PROVIDER_ERROR"
    assert "sk-***" in session.provider_error
    assert "sk-abcdef" not in session.provider_error


def test_unconfigured_provider_stops_honestly(db):
    incident = first_incident(db)
    provider = agent_svc.VultrChatProvider()
    provider.api_key = ""
    provider.model = ""
    assert not provider.configured()
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="No LLM configured.",
        requested_by="fleet.manager", provider=provider,
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "MODEL_NOT_CONFIGURED"
    assert session.final_result is None
    assert "not configured" in session.provider_error.lower()


def test_json_protocol_downgrade_when_native_unsupported(db):
    incident = first_incident(db)

    class NativeRejectStub(agent_svc.StubChatProvider):
        def chat(self, messages, tool_defs):
            if tool_defs is not None:
                raise agent_svc.ProviderToolingUnsupported("400 tools unsupported")
            return super().chat(messages, tool_defs)

    provider = NativeRejectStub([
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": grounded_final_payload(db, incident)},
    ])
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test JSON fallback.",
        requested_by="fleet.manager", provider=provider,
    )
    assert session.status == "COMPLETED"
    assert session.stop_reason == "MODEL_STOPPED"
    # The native attempt raised before the stub recorded it; every recorded
    # call (3 tool rounds + final) therefore used the JSON protocol.
    assert provider.received_tool_defs and all(
        td is None for td in provider.received_tool_defs
    )


def test_unknown_tool_and_bad_args_feed_errors_back(db):
    incident = first_incident(db)
    script = [
        {"tool": "assign_route_to_bus", "args": {"incident_id": incident.id}},  # write-ish name
        {"tool": "get_recall_evidence", "args": {"incident_id": 0}},            # invalid args
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": grounded_final_payload(db, incident)},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test tool errors.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    statuses = [(s.tool_name, s.status) for s in session.steps if s.kind == "TOOL_CALL"]
    assert ("assign_route_to_bus", "INVALID_TOOL_CALL") in statuses
    assert ("get_recall_evidence", "INVALID_ARGS") in statuses
    assert ("get_recall_evidence", "OK") in statuses


def test_tool_timeout_is_recorded_and_survivable(db, monkeypatch):
    import threading as _t

    incident = first_incident(db)
    orig = agent_tools.REGISTRY["get_open_verification_items"]

    def slow(db, args):
        stop = _t.Event()
        stop.wait(2.0)
        return {}

    sleepy = replace(orig, timeout_seconds=0.2, handler=slow)
    monkeypatch.setitem(agent_tools.REGISTRY, "get_open_verification_items", sleepy)
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"tool": "get_open_verification_items", "args": {"incident_id": incident.id}},
        {"final": grounded_final_payload(db, incident)},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test timeout.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    steps = [s for s in session.steps if s.kind == "TOOL_CALL"]
    timed_out = next(s for s in steps if s.tool_name == "get_open_verification_items")
    assert timed_out.status == "TIMEOUT"
    assert timed_out.duration_ms < 2000


# ---------------------------------------------------------------------------
# Tool-session lifecycle (thread-safety regression tests)
# ---------------------------------------------------------------------------


def test_tool_calls_run_on_isolated_private_sessions(db, monkeypatch):
    """Regression: each tool execution must run on its own short-lived
    Session - never on the orchestrator's session (SQLAlchemy Sessions are
    not thread-safe) - with the whole lifecycle owned by the worker thread."""
    incident = first_incident(db)
    orig = agent_tools.REGISTRY["get_recall_evidence"]
    seen: list[dict] = []

    def recording(tool_db, args):
        result = orig.handler(tool_db, args)  # real read path on the private session
        seen.append({
            "session": tool_db,
            "in_txn": tool_db.in_transaction(),
            "bind": tool_db.get_bind(),
            "campaign": result["campaign_number"],
        })
        return result

    monkeypatch.setitem(
        agent_tools.REGISTRY, "get_recall_evidence", replace(orig, handler=recording)
    )
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": grounded_final_payload(db, incident)},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Session isolation.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    assert session.stop_reason == "MODEL_STOPPED"
    assert len(seen) == 2
    # The worker never received the orchestrator's session ...
    assert all(s["session"] is not db for s in seen)
    # ... and every call got a fresh session of its own.
    assert seen[0]["session"] is not seen[1]["session"]
    # Bound to the same engine: tools observe the same database state.
    assert all(s["bind"] is db.get_bind() for s in seen)
    assert all(s["campaign"] == incident.campaign_number for s in seen)
    # The private session was live (mid-transaction) inside the handler ...
    assert all(s["in_txn"] for s in seen)
    # ... and is deterministically closed afterwards: fut.result() can only
    # return after the worker thread's `finally: tool_db.close()` ran.
    assert all(not s["session"].in_transaction() for s in seen)


def test_timed_out_tool_is_isolated_and_cannot_poison_later_writes(db, monkeypatch):
    """Regression: a timed-out worker is abandoned but keeps running in the
    background on its OWN session; it can neither touch the orchestrator's
    session nor interfere with the writes the orchestrator makes afterwards
    (step persistence, session finalisation)."""
    import threading as _t
    from sqlalchemy import select as _select

    incident = first_incident(db)
    orig = agent_tools.REGISTRY["get_open_verification_items"]
    seen: dict = {}
    worker_done = _t.Event()

    def slow_rogue(tool_db, args):
        seen["session"] = tool_db
        tool_db.scalar(_select(models.Incident).limit(1))  # open a read txn
        seen["in_txn"] = tool_db.in_transaction()
        _t.Event().wait(0.6)  # orchestrator abandons this call at 0.2s
        # Worst case: the abandoned worker keeps using its own session long
        # after the orchestrator moved on.
        try:
            tool_db.rollback()  # discard the stale read transaction
            again = orig.handler(tool_db, args)  # fresh read, same private session
            seen["rogue_read_ok"] = isinstance(again, dict)
        except Exception as exc:  # pragma: no cover - must not happen
            seen["rogue_exc"] = exc
        finally:
            worker_done.set()
        return {}

    monkeypatch.setitem(
        agent_tools.REGISTRY,
        "get_open_verification_items",
        replace(orig, timeout_seconds=0.2, handler=slow_rogue),
    )
    script = [
        {"tool": "get_open_verification_items", "args": {"incident_id": incident.id}},
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": grounded_final_payload(db, incident)},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Timeout isolation.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    # The orchestrator's writes after the abandonment all landed: steps and
    # final result were persisted while the rogue worker was still asleep.
    assert session.status == "COMPLETED"
    assert session.stop_reason == "MODEL_STOPPED"
    assert session.tool_calls_count == 4
    steps = [s for s in session.steps if s.kind == "TOOL_CALL"]
    timed_out = next(s for s in steps if s.tool_name == "get_open_verification_items")
    assert timed_out.status == "TIMEOUT"
    assert timed_out.duration_ms < 600  # returned at the 0.2s timeout, not 0.6s
    assert [s.status for s in steps[-3:]] == ["OK", "OK", "OK"]
    assert session.final_result is not None
    # Isolation proof: the rogue handler did NOT receive the investigation's
    # session, and its own session was live (mid-transaction) during the call.
    assert seen["session"] is not db
    assert seen["in_txn"] is True
    # Wait for the abandoned worker to finish its post-timeout activity ...
    assert worker_done.wait(5.0)
    deadline = time.time() + 5.0
    while seen["session"].in_transaction() and time.time() < deadline:
        time.sleep(0.01)
    assert not seen["session"].in_transaction()
    # ... then verify the rogue kept using its OWN session without error
    # (it never touched the orchestrator's session or its writes).
    assert "rogue_exc" not in seen and seen.get("rogue_read_ok") is True


# ---------------------------------------------------------------------------
# Grounding resolution + schema-aware recovery (regression tests)
# ---------------------------------------------------------------------------


def _canonical_index(db, incident) -> agent_svc.RefIndex:
    """RefIndex built exactly as the runner builds it: from real tool outputs."""
    index = agent_svc.RefIndex()
    index.observe("get_recall_evidence", agent_tools.tool_get_recall_evidence(
        db, agent_tools.IncidentArgs(incident_id=incident.id)))
    index.observe("get_fleet_assessment", agent_tools.tool_get_fleet_assessment(
        db, agent_tools.IncidentArgs(incident_id=incident.id)))
    index.observe("preview_route_impact", agent_tools.tool_preview_route_impact(
        db, agent_tools.RouteImpactArgs(
            incident_id=incident.id, policy="HOLD_PENDING_VERIFICATION")))
    return index


def test_invented_evidence_references_rejected(db):
    """Refs that were never returned by a tool are rejected - in facts, in the
    evidence_references bibliography AND as a classification basis_ref."""
    incident = first_incident(db)
    assess = agent_tools.tool_get_fleet_assessment(
        db, agent_tools.IncidentArgs(incident_id=incident.id))
    v0 = assess["vehicles"][0]
    payload = grounded_final_payload(db, incident)
    payload["facts"][0]["evidence_refs"] = ["DOC-deadbeefdeadbeef", "CRIT-424242"]
    payload["evidence_references"] = [
        {"ref_id": "CRIT-424242", "description": "invented", "source": ""},
        {"ref_id": "ASSESS-424242", "description": "invented", "source": ""},
    ]
    payload["vehicle_classifications"] = [
        {"asset_id": v0["asset_id"], "classification": v0["classification"],
         "basis_ref": "ASSESS-424242"},
    ]
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": payload},
        {"final": payload},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test invented refs.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "GROUNDING_VALIDATION_FAILED"
    assert session.final_result is None  # invented evidence never becomes fact
    violations = session.grounding_violations
    assert any("DOC-deadbeefdeadbeef" in v for v in violations)
    assert any("CRIT-424242" in v for v in violations)
    assert any("basis_ref 'ASSESS-424242'" in v for v in violations)
    # Observability: every failed attempt persisted with retry metadata.
    steps = [s for s in session.steps if s.kind == "VALIDATION"]
    assert len(steps) == 2
    assert [s.result["attempt"] for s in steps] == [1, 2]
    assert all(s.result["max_retries"] == agent_svc.MAX_FINAL_RETRIES for s in steps)
    assert all(isinstance(s.result["model_round"], int) for s in steps)
    assert all(s.duration_ms >= 0 for s in steps)


def test_canonical_evidence_references_accepted(db):
    """Every cited ref resolves against canonical ids returned by real tool
    outputs -> accepted."""
    incident = first_incident(db)
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Assess recall exposure.",
        requested_by="fleet.manager",
        provider=agent_svc.StubChatProvider(_happy_script(db, incident)),
    )
    assert session.status == "COMPLETED"
    assert session.stop_reason == "MODEL_STOPPED"
    index = _canonical_index(db, incident)
    cited = [r["ref_id"] for r in session.final_result["evidence_references"]]
    assert cited and all(r in index.refs for r in cited)
    for fact in session.final_result["facts"]:
        assert fact["evidence_refs"]
        assert all(r in index.refs for r in fact["evidence_refs"])


def test_oversized_objective_interpretation_truncated_and_recorded(db):
    """An oversized free-text field is deterministically truncated to the
    schema max_length, recorded, then accepted by full validation."""
    incident = first_incident(db)
    payload = grounded_final_payload(db, incident)
    payload["objective_interpretation"] = "Ramble " * 300  # 2100 chars > 1000
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": payload},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test oversized field.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    assert len(session.final_result["objective_interpretation"]) == 1000
    repair_steps = [
        s for s in session.steps if s.kind == "VALIDATION" and s.status == "SCHEMA_REPAIR"
    ]
    assert len(repair_steps) == 1
    assert any(
        "objective_interpretation truncated from 2100 to 1000" in r
        for r in repair_steps[0].result["repairs"]
    )
    assert repair_steps[0].result["model_round"] == 4
    assert repair_steps[0].duration_ms >= 0
    final_step = next(s for s in session.steps if s.kind == "FINAL")
    assert "1 deterministic repair" in final_step.explanation


def test_unexpected_json_schema_fields_dropped_and_recorded(db):
    """$defs-style JSON-Schema echo fields are dropped deterministically and
    the drop is recorded; the assessment is then validated normally."""
    incident = first_incident(db)
    payload = grounded_final_payload(db, incident)
    payload["$defs"] = {"AgentFact": {"type": "object"}}
    payload["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": payload},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test schema echo fields.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    assert "$defs" not in session.final_result
    assert "$schema" not in session.final_result
    repair_steps = [
        s for s in session.steps if s.kind == "VALIDATION" and s.status == "SCHEMA_REPAIR"
    ]
    assert len(repair_steps) == 1
    repairs = repair_steps[0].result["repairs"]
    assert any("dropped non-assessment field '$defs'" in r for r in repairs)
    assert any("dropped non-assessment field '$schema'" in r for r in repairs)


def test_multiple_simultaneous_validation_errors_reported(db):
    """One rejection message lists EVERY schema error with its field path and
    the fix; repairs already applied are noted too."""
    incident = first_incident(db)
    bad = grounded_final_payload(db, incident)
    bad["objective_interpretation"] = "x" * 1500  # oversized -> repaired
    bad["confidence"] = "high"                    # unexpected field
    bad["facts"] = [{"statement": "", "evidence_refs": []}]  # too-short statement
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"final": bad},
        {"final": bad},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test multi-error report.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "INVALID_FINAL_OUTPUT"
    steps = [
        s for s in session.steps
        if s.kind == "VALIDATION" and s.status == agent_svc.STOP_INVALID_FINAL
    ]
    assert len(steps) == 2
    first = steps[0]
    assert "confidence: unexpected field" in first.explanation
    assert "facts.0.statement" in first.explanation
    assert "at least 1 character" in first.explanation
    assert "objective_interpretation truncated from 1500 to 1000" in first.explanation
    assert first.result["attempt"] == 1
    assert first.result["max_retries"] == agent_svc.MAX_FINAL_RETRIES
    assert first.result["repairs"]
    assert session.final_result is None


def test_retry_budget_exhaustion_is_recorded(db):
    """After MAX_FINAL_RETRIES rejected attempts the investigation fails
    honestly - validation is never bypassed to accept a failed response."""
    incident = first_incident(db)
    bad = grounded_final_payload(db, incident)
    bad["confidence"] = "high"  # unrepairable unexpected field
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"final": bad},
        {"final": bad},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test retry budget.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "INVALID_FINAL_OUTPUT"
    assert session.final_result is None
    assert session.iterations_used == 3  # 1 tool round + 2 rejected final rounds
    steps = [s for s in session.steps if s.kind == "VALIDATION"]
    assert [s.result["attempt"] for s in steps] == [1, 2]
    assert [s.result["model_round"] for s in steps] == [2, 3]
    assert session.tool_calls_count == 1


def test_valid_corrected_assessment_accepted_after_schema_rejection(db):
    """A schema-rejected draft followed by a corrected final is accepted -
    the correction loop works end to end."""
    incident = first_incident(db)
    bad = grounded_final_payload(db, incident)
    bad["confidence"] = "high"
    good = grounded_final_payload(db, incident)
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": bad},
        {"final": good},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test correction after rejection.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    assert session.stop_reason == "MODEL_STOPPED"
    rejected = [s for s in session.steps if s.kind == "VALIDATION"
                and s.status == agent_svc.STOP_INVALID_FINAL]
    assert len(rejected) == 1 and rejected[0].result["attempt"] == 1
    final_step = next(s for s in session.steps if s.kind == "FINAL")
    assert final_step.status == "OK"
    assert session.final_result is not None
    assert session.final_result["incident_summary"] == good["incident_summary"]


def test_tool_execution_error_surfaced_not_crashing(db):
    incident = first_incident(db)
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"tool": "get_vehicle_detail",
         "args": {"incident_id": incident.id, "asset_id": "SYNTHETIC-DOES-NOT-EXIST"}},
        {"final": grounded_final_payload(db, incident)},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Test tool error surface.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    steps = [s for s in session.steps if s.kind == "TOOL_CALL"]
    failed_step = next(s for s in steps if s.tool_name == "get_vehicle_detail")
    assert failed_step.status == "TOOL_ERROR"
    assert "No vehicle with asset_id" in failed_step.explanation


# ---------------------------------------------------------------------------
# P0 canonical-reference grounding (staging trace INV-7CADE61A643F)
#
# Regressions for the real Qwen failure on Vultr staging: the model cited
# spare ASSET ids as evidence references and emitted composite route
# description strings instead of exact route_id values.
# ---------------------------------------------------------------------------


def _spares_output(db, incident) -> dict:
    return agent_tools.tool_get_available_spares(
        db, agent_tools.IncidentArgs(incident_id=incident.id)
    )


def _impacted_rows(preview: dict) -> list[dict]:
    return [r for pv in preview["previews"] for r in pv["impacted_routes"]]


def test_ref_index_collects_nested_refs_and_restricts_route_source(db):
    """The reference index must collect canonical evidence references from
    bare-string 'evidence_refs' lists (previously invisible) AND restrict the
    canonical ROUTE source to successful preview_route_impact outputs."""
    incident = first_incident(db)
    index = agent_svc.RefIndex()

    spares = _spares_output(db, incident)
    index.observe("get_available_spares", spares)
    spare_refs = {s["ref"] for s in spares["spares"]}
    spare_ids = {s["asset_id"] for s in spares["spares"]}
    eligible_ids = {s["asset_id"] for s in spares["spares"] if s["eligible_as_replacement"]}
    assert spare_refs and spare_refs == set(spares["evidence_refs"])
    assert spare_refs <= index.refs       # per-object ref fields AND the bare list
    assert spare_ids <= index.assets      # asset ids are identities, never refs
    assert not (spare_ids & index.refs)
    assert index.eligible_spares == eligible_ids

    # A route id seen via get_vehicle_detail is citable evidence but is NOT a
    # canonical route reference: only route-impact previews ground routes.
    detail = agent_tools.tool_get_vehicle_detail(
        db, agent_tools.VehicleDetailArgs(incident_id=incident.id, asset_id="SYNTHETIC-BUS-001")
    )
    index.observe("get_vehicle_detail", detail)
    assigned = (detail.get("assigned_route") or {}).get("route_id")
    assert assigned
    assert assigned in index.refs
    assert index.routes == set()

    preview = agent_tools.tool_preview_route_impact(
        db, agent_tools.RouteImpactArgs(incident_id=incident.id,
                                        policy="HOLD_PENDING_VERIFICATION")
    )
    index.observe("preview_route_impact", preview)
    preview_routes = {r["route_id"] for r in _impacted_rows(preview)}
    assert preview_routes and preview_routes <= index.routes
    # Engine classifications of assigned vehicles are recorded from route rows.
    for r in _impacted_rows(preview):
        if r["assigned_vehicle"] and r["assigned_vehicle_classification"]:
            assert index.classifications[r["assigned_vehicle"]] == \
                r["assigned_vehicle_classification"]


def test_spare_evidence_ref_vs_asset_id_at_validator_level(db):
    """ASSESS-n is the evidence reference for a spare; SYNTHETIC-SPARE-xx is
    the asset identity. The validator must accept the former for citations and
    reject the latter with type-aware, actionable feedback."""
    incident = first_incident(db)
    index = _canonical_index(db, incident)
    spares = _spares_output(db, incident)
    index.observe("get_available_spares", spares)
    s0 = spares["spares"][0]

    good = grounded_final_payload(db, incident)
    good["facts"].append({
        "statement": f"Spare {s0['asset_id']} is eligible as a replacement per the "
                     "recovery engine.",
        "evidence_refs": [s0["ref"]],        # ASSESS-n: canonical evidence reference
    })
    good["evidence_references"].append(
        {"ref_id": s0["ref"], "description": "spare assessment record", "source": ""})
    assert agent_svc.validate_grounding(
        agent_svc.AgentFinalAssessment.model_validate(good), index) == []

    bad = grounded_final_payload(db, incident)
    bad["facts"].append({
        "statement": f"Spare {s0['asset_id']} is eligible as a replacement per the "
                     "recovery engine.",
        "evidence_refs": [s0["asset_id"]],   # asset id cited as evidence
    })
    bad["evidence_references"].append(
        {"ref_id": s0["asset_id"], "description": "spare vehicle", "source": ""})
    violations = agent_svc.validate_grounding(
        agent_svc.AgentFinalAssessment.model_validate(bad), index)
    asset_v = next(v for v in violations if s0["asset_id"] in v)
    assert "ASSET ID" in asset_v
    assert "Available evidence refs" in asset_v
    # The old feedback falsely claimed the id was "never returned by a tool";
    # the new feedback must not repeat that mistake.
    assert "was never returned by a tool" not in asset_v


def test_spare_asset_id_accepted_as_asset_identifier(db):
    """Where the schema expects an asset id (recovery_candidates[].spare_asset_id)
    the spare ASSET id is correct and accepted end to end."""
    incident = first_incident(db)
    spares = _spares_output(db, incident)
    eligible = next(s for s in spares["spares"] if s["eligible_as_replacement"])
    payload = grounded_final_payload(db, incident)
    payload["recovery_candidates"].append({
        "route_id": payload["affected_routes"][0],
        "spare_asset_id": eligible["asset_id"],
        "feasibility_note": f"Spare {eligible['asset_id']} is eligible per the recovery engine.",
    })
    payload["evidence_references"].append(
        {"ref_id": eligible["ref"], "description": "spare assessment record", "source": ""})
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"tool": "get_available_spares", "args": {"incident_id": incident.id}},
        {"final": payload},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Spare asset id as asset identifier.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    assert session.stop_reason == "MODEL_STOPPED"
    candidates = session.final_result["recovery_candidates"]
    assert any(c["spare_asset_id"] == eligible["asset_id"] for c in candidates)
    # ... and no evidence citation anywhere is an asset id (distinction kept).
    cited = [r for f in session.final_result["facts"] for r in f["evidence_refs"]]
    cited += [r["ref_id"] for r in session.final_result["evidence_references"]]
    assert cited and not any(r.startswith("SYNTHETIC-") for r in cited)


def test_spare_asset_id_as_evidence_reference_rejected_in_loop(db):
    """The exact INV-7CADE61A643F failure mode A: citing SYNTHETIC-SPARE-xx as
    an evidence reference is rejected with actionable feedback; after the
    retry budget the investigation fails honestly."""
    incident = first_incident(db)
    s0 = _spares_output(db, incident)["spares"][0]
    bad = grounded_final_payload(db, incident)
    bad["facts"].append({
        "statement": f"Spare {s0['asset_id']} is eligible as a replacement for the "
                     "disrupted routes.",
        "evidence_refs": [s0["asset_id"]],
    })
    bad["evidence_references"].append(
        {"ref_id": s0["asset_id"], "description": "spare vehicle", "source": ""})
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"tool": "get_available_spares", "args": {"incident_id": incident.id}},
        {"final": bad},
        {"final": bad},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Spare asset id as evidence.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "GROUNDING_VALIDATION_FAILED"
    assert session.final_result is None
    violations = session.grounding_violations
    assert any(s0["asset_id"] in v and "ASSET ID" in v for v in violations)
    assert any(s0["ref"] in v for v in violations)  # feedback lists canonical refs
    steps = [s for s in session.steps if s.kind == "VALIDATION"]
    assert [s.result["attempt"] for s in steps] == [1, 2]


def test_composite_route_description_rejected_in_loop(db):
    """The exact INV-7CADE61A643F failure mode B: a composite route description
    in affected_routes is rejected, and the feedback names the contained
    canonical route id(s) and the exact route_id values available."""
    incident = first_incident(db)
    preview = agent_tools.tool_preview_route_impact(
        db, agent_tools.RouteImpactArgs(incident_id=incident.id,
                                        policy="HOLD_PENDING_VERIFICATION")
    )
    rows = _impacted_rows(preview)
    assert rows
    r0 = rows[0]
    composite = (f"{r0['route_id']} {r0['name']} (DISRUPTED under both policies; assigned "
                 f"{r0['assigned_vehicle']}, {r0['assigned_vehicle_classification']})")
    bad = grounded_final_payload(db, incident)
    bad["affected_routes"] = [composite]
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"final": bad},
        {"final": bad},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Composite route id.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "GROUNDING_VALIDATION_FAILED"
    assert session.final_result is None
    violations = session.grounding_violations
    target = next(v for v in violations if v.startswith("affected_routes[0]"))
    assert composite in target
    assert "composite description containing route id(s)" in target
    assert r0["route_id"] in target                 # the exact id it should have used
    assert "Exact route_ids returned by preview_route_impact" in target


def test_route_id_grounding_requires_successful_route_impact(db, monkeypatch):
    """Route references are grounded ONLY by successful preview_route_impact
    outputs: a route id seen via another tool, or from a FAILED preview call,
    must be rejected."""
    incident = first_incident(db)
    route = db.query(models.FleetRoute).filter(
        models.FleetRoute.assigned_vehicle_id.isnot(None)).first()
    vehicle = db.get(models.FleetVehicle, route.assigned_vehicle_id)

    # (a) route id observed only via get_vehicle_detail -> not canonical
    bad = grounded_final_payload(db, incident)
    bad["affected_routes"] = [route.route_id]
    script = [
        {"tool": "get_vehicle_detail",
         "args": {"incident_id": incident.id, "asset_id": vehicle.asset_id}},
        {"final": bad},
        {"final": bad},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Route from wrong tool.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "GROUNDING_VALIDATION_FAILED"
    target = next(v for v in session.grounding_violations
                  if v.startswith("affected_routes[0]"))
    assert route.route_id in target and "preview_route_impact" in target

    # (b) a FAILED preview grounds nothing
    orig = agent_tools.REGISTRY["preview_route_impact"]

    def broken(tool_db, args):
        raise agent_tools.ToolExecutionError("preview service unavailable")

    monkeypatch.setitem(
        agent_tools.REGISTRY, "preview_route_impact", replace(orig, handler=broken))
    bad2 = grounded_final_payload(db, incident)
    script2 = [
        {"tool": "preview_route_impact", "args": {"incident_id": incident.id}},
        {"final": bad2},
        {"final": bad2},
    ]
    session2 = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Route from failed preview.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script2),
    )
    assert session2.status == "FAILED"
    assert session2.stop_reason == "GROUNDING_VALIDATION_FAILED"
    steps = [s for s in session2.steps if s.kind == "TOOL_CALL"]
    assert steps[0].status == "TOOL_ERROR"
    target2 = next(v for v in session2.grounding_violations
                   if v.startswith("affected_routes[0]"))
    assert "no route objects were returned by a successful preview_route_impact" in target2


def test_route_ids_accepted_and_policies_kept_distinct(db):
    """Exact route_id values from a BOTH-policy preview are accepted, and the
    two operational policies plus the engine's distinct classifications are
    preserved verbatim in the accepted assessment."""
    incident = first_incident(db)
    preview = agent_tools.tool_preview_route_impact(
        db, agent_tools.RouteImpactArgs(incident_id=incident.id))
    policies = {pv["policy"] for pv in preview["previews"]}
    assert policies == {"HOLD_PENDING_VERIFICATION", "MANAGER_REVIEW_ONLY"}
    rows = _impacted_rows(preview)
    exact_routes = list(dict.fromkeys(r["route_id"] for r in rows))

    assess = agent_tools.tool_get_fleet_assessment(
        db, agent_tools.IncidentArgs(incident_id=incident.id))
    engine = {v["asset_id"]: v["classification"] for v in assess["vehicles"]}
    ref_by_asset = {v["asset_id"]: v["ref"] for v in assess["vehicles"]}
    picks: dict[str, str] = {}
    for asset, cls in engine.items():
        picks.setdefault(cls, asset)
    assert len(picks) >= 1

    payload = grounded_final_payload(db, incident)
    payload["affected_routes"] = exact_routes
    payload["vehicle_classifications"] = [
        {"asset_id": asset, "classification": cls, "basis_ref": ref_by_asset[asset]}
        for cls, asset in picks.items()
    ]
    for pv in preview["previews"]:
        impacted = [r["route_id"] for r in pv["impacted_routes"]]
        payload["facts"].append({
            "statement": f"Under {pv['policy']}, {len(impacted)} routes were reviewed: "
                         f"{', '.join(impacted)}.",
            "evidence_refs": impacted[:4],
        })
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact", "args": {"incident_id": incident.id}},
        {"final": payload},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Both policies, exact route ids.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    assert session.stop_reason == "MODEL_STOPPED"
    result = session.final_result
    assert result["affected_routes"] == exact_routes
    assert all(r == r.strip() and r.startswith("R-") for r in result["affected_routes"])
    claims = {c["asset_id"]: c["classification"] for c in result["vehicle_classifications"]}
    assert claims == {asset: cls for cls, asset in picks.items()}   # engine values, distinct
    assert set(claims.values()) == set(picks.keys())
    fact_text = " ".join(f["statement"] for f in result["facts"])
    assert "HOLD_PENDING_VERIFICATION" in fact_text and "MANAGER_REVIEW_ONLY" in fact_text


def test_fabricated_spare_and_route_references_rejected(db):
    """Fabricated spare and route references remain rejected - the new
    type-aware feedback must not loosen any acceptance rule."""
    incident = first_incident(db)
    bad = grounded_final_payload(db, incident)
    bad["affected_routes"] = bad["affected_routes"] + ["R-999"]
    bad["recovery_candidates"].append({
        "route_id": "R-999",
        "spare_asset_id": "SYNTHETIC-SPARE-99",
        "feasibility_note": "invented",
    })
    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact",
         "args": {"incident_id": incident.id, "policy": "HOLD_PENDING_VERIFICATION"}},
        {"tool": "get_available_spares", "args": {"incident_id": incident.id}},
        {"final": bad},
        {"final": bad},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Fabricated ids.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "FAILED"
    assert session.stop_reason == "GROUNDING_VALIDATION_FAILED"
    assert session.final_result is None
    violations = session.grounding_violations
    assert any("R-999" in v for v in violations)
    assert any("SYNTHETIC-SPARE-99" in v for v in violations)


def test_trace_inv_7cade61a643f_reproduced_and_corrected(db):
    """End-to-end mocked-provider reproduction of staging trace INV-7CADE61A643F
    (Qwen on Vultr): draft final cites spare asset ids as evidence and emits
    composite route strings -> grounding rejects once with type-aware feedback;
    the corrected canonical assessment is then accepted."""
    incident = first_incident(db)
    preview = agent_tools.tool_preview_route_impact(
        db, agent_tools.RouteImpactArgs(incident_id=incident.id))  # both policies, as in the trace
    rows = _impacted_rows(preview)
    spares = _spares_output(db, incident)
    s0 = spares["spares"][0]

    # Draft with BOTH failure modes exactly as observed in the trace.
    draft = grounded_final_payload(db, incident)
    draft["facts"].append({
        "statement": f"Replacement spares identified: {s0['asset_id']} is eligible as a "
                     "replacement for the disrupted routes.",
        "evidence_refs": [s0["asset_id"]],                       # failure mode A
    })
    draft["evidence_references"].append(
        {"ref_id": s0["asset_id"], "description": "spare vehicle", "source": ""})
    draft["affected_routes"] = [
        f"{r['route_id']} {r['name']} (DISRUPTED under both policies; assigned "
        f"{r['assigned_vehicle']}, {r['assigned_vehicle_classification']})"
        for r in rows[:3]
    ]                                                            # failure mode B

    # Corrected assessment: canonical refs, exact route ids, per-policy facts.
    good = grounded_final_payload(db, incident)
    good["facts"].append({
        "statement": f"Spare {s0['asset_id']} (assessment record {s0['ref']}) is "
        f"{'eligible' if s0['eligible_as_replacement'] else 'not eligible'} as a replacement.",
        "evidence_refs": [s0["ref"]],
    })
    for pv in preview["previews"]:
        impacted = [r["route_id"] for r in pv["impacted_routes"]]
        good["facts"].append({
            "statement": f"Under {pv['policy']}, {len(impacted)} routes were reviewed: "
                         f"{', '.join(impacted)}.",
            "evidence_refs": impacted[:4],
        })
    good["evidence_references"].append(
        {"ref_id": s0["ref"], "description": "spare assessment record", "source": ""})
    good["affected_routes"] = list(dict.fromkeys(r["route_id"] for r in rows))

    script = [
        {"tool": "get_recall_evidence", "args": {"incident_id": incident.id}},
        {"tool": "get_fleet_assessment", "args": {"incident_id": incident.id}},
        {"tool": "preview_route_impact", "args": {"incident_id": incident.id}},
        {"tool": "get_available_spares", "args": {"incident_id": incident.id}},
        {"final": draft},
        {"final": good},
    ]
    session = agent_svc.run_investigation(
        db, incident_id=incident.id, objective="Assess recall exposure and route impact.",
        requested_by="fleet.manager", provider=agent_svc.StubChatProvider(script),
    )
    assert session.status == "COMPLETED"
    assert session.stop_reason == "MODEL_STOPPED"
    assert agent_svc.StubChatProvider.TEST_LABEL in session.model  # never real inference
    assert session.tool_calls_count == 4
    assert session.iterations_used == 6

    # The rejected draft is persisted with its full violation set and retry metadata.
    gsteps = [s for s in session.steps if s.kind == "VALIDATION"
              and s.status == agent_svc.STOP_GROUNDING_FAILED]
    assert len(gsteps) == 1
    rejected = gsteps[0].result["violations"]
    assert gsteps[0].result["attempt"] == 1
    assert gsteps[0].result["model_round"] == 5
    assert any(s0["asset_id"] in v and "ASSET ID" in v for v in rejected)
    assert any("composite description containing route id(s)" in v for v in rejected)

    # The accepted result is fully canonical.
    result = session.final_result
    assert result["affected_routes"] == list(dict.fromkeys(r["route_id"] for r in rows))
    cited = [r for f in result["facts"] for r in f["evidence_refs"]]
    cited += [r["ref_id"] for r in result["evidence_references"]]
    cited += [c["basis_ref"] for c in result["vehicle_classifications"] if c["basis_ref"]]
    assert cited and not any(r.startswith("SYNTHETIC-") for r in cited)
    assert s0["ref"] in cited
    fact_text = " ".join(f["statement"] for f in result["facts"])
    assert "HOLD_PENDING_VERIFICATION" in fact_text and "MANAGER_REVIEW_ONLY" in fact_text
    final_step = next(s for s in session.steps if s.kind == "FINAL")
    assert final_step.status == "OK"


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


def test_status_endpoint_reports_no_write_tools(client):
    resp = client.get("/api/agents/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["write_tools_exposed"] is False
    assert body["max_iterations"] == 8
    assert "get_recall_evidence" in body["tools"]


def test_start_investigation_requires_manager(client):
    resp = client.post("/api/agents/investigations",
                       json={"incident_id": 1, "objective": "Investigate exposure."})
    assert resp.status_code == 401


def test_probe_requires_manager(client):
    assert client.post("/api/agents/probe").status_code == 401


def test_start_and_poll_investigation(client, manager_headers, incident_id, monkeypatch):
    from app.services import agent as agent_mod
    from app.db import SessionLocal
    from app.seed import seed_all
    from app.db import Base, engine

    # Deterministic seed for the background thread (the `client` fixture's db
    # fixture already seeded the same file, but re-seed defensively).
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_all(db)
    incident = db.query(models.Incident).order_by(models.Incident.id).first()

    stub = agent_svc.StubChatProvider(_happy_script(db, incident))
    db.close()
    monkeypatch.setattr(agent_mod, "VultrChatProvider", lambda: stub)

    resp = client.post(
        "/api/agents/investigations",
        json={"incident_id": incident.id, "objective": "Assess exposure end-to-end."},
        headers=manager_headers,
    )
    assert resp.status_code == 202, resp.text
    uid = resp.json()["session_uid"]
    assert uid.startswith("INV-")

    deadline = time.time() + 15
    data = None
    while time.time() < deadline:
        got = client.get(f"/api/agents/investigations/{uid}")
        assert got.status_code == 200
        data = got.json()
        if data["status"] != "INVESTIGATING":
            break
        time.sleep(0.05)
    assert data is not None and data["status"] == "COMPLETED"
    assert data["stop_reason"] == "MODEL_STOPPED"
    assert STUB_LABEL in data["model"]
    assert data["steps"] and data["steps"][0]["tool_name"] == "get_recall_evidence"

    listing = client.get(f"/api/agents/incidents/{incident.id}/investigations")
    assert listing.status_code == 200
    assert any(i["session_uid"] == uid for i in listing.json())


def test_investigation_404s(client, manager_headers):
    assert client.get("/api/agents/investigations/INV-nope").status_code == 404
    resp = client.post("/api/agents/investigations",
                       json={"incident_id": 9999, "objective": "Missing incident."},
                       headers=manager_headers)
    assert resp.status_code == 404


def test_agents_router_has_no_mutation_verbs():
    from app.main import app
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/agents"):
            methods = {m for m in getattr(route, "methods", set()) if m != "HEAD"}
            assert methods <= {"GET", "POST"}, (path, methods)
            if "POST" in methods:
                assert path in {"/api/agents/probe", "/api/agents/investigations"}
