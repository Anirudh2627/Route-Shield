"""Bounded incident-response agent (the orchestrator).

SAFETY ARCHITECTURE (non-negotiable, enforced by tests):

* The agent is a NEW ENTRY POINT into the existing workflow - never a new
  execution or approval system. It calls ONLY the read-only tool registry in
  `services/agent_tools.py` and never imports any write path (approval,
  policy selection, plan generation).
* The model NEVER writes operational state. The only DB rows this module
  creates are its own `InvestigationSession` / `InvestigationStep` trace.
  Fleet, routes, assessments, policies, proposals and approvals are read-only
  here; `services/approval.apply_approval` remains the ONLY operational
  writer, reachable exclusively through the authenticated manager endpoint.
* Classifications are produced by the deterministic engine. The agent must
  repeat them verbatim or stay silent; the deterministic grounding validator
  rejects any fabricated asset id, route id, evidence ref, or classification
  that contradicts the engine. Ungrounded output is never shown as fact.
* The loop is bounded: max `agent_max_iterations` model rounds (hard cap 8),
  per-call LLM timeout, per-tool timeout, and a wall-clock budget for the
  whole investigation. Every step is persisted incrementally so the UI can
  follow the live trace and so the audit trail of the agent itself is
  complete.

PROVIDERS:
* `VultrChatProvider` - OpenAI-compatible Vultr Serverless Inference with
  native function calling. If the deployed model rejects the `tools`
  parameter, the orchestrator downgrades to a strict JSON protocol
  ({"action": "tool"|"final", ...}) for the rest of the session.
* `StubChatProvider` - scripted turns for automated tests ONLY. It carries an
  explicit TEST-ONLY model label that is persisted with the session, so stub
  results can never be presented as real AI inference. Production code paths
  never construct it.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .. import models
from ..config import get_settings
from ..schemas import (
    AgentFact,
    AgentFinalAssessment,
    AgentInference,
    AgentRecommendation,
    AgentRecoveryCandidate,
)
from . import agent_tools

logger = logging.getLogger("routeshield.agent")

STOP_MODEL_STOPPED = "MODEL_STOPPED"
STOP_MAX_ITERATIONS = "MAX_ITERATIONS"
STOP_PROVIDER_ERROR = "PROVIDER_ERROR"
STOP_MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"
STOP_INVALID_FINAL = "INVALID_FINAL_OUTPUT"
STOP_GROUNDING_FAILED = "GROUNDING_VALIDATION_FAILED"
STOP_TIME_BUDGET = "TIME_BUDGET_EXCEEDED"

# Corrective retries the model gets for a broken final answer (invalid JSON
# or ungrounded claims) before the investigation fails honestly.
MAX_FINAL_RETRIES = 2

# Hard ceiling on the number of model rounds regardless of configuration.
HARD_MAX_ITERATIONS = 8

# Model-facing tool payload cap (protects the context window; persistence
# keeps the same cap so the stored trace equals what the model saw).
MAX_TOOL_PAYLOAD_CHARS = 40_000

_REF_PATTERNS = (
    re.compile(r"^CRIT-\d+$"),
    re.compile(r"^ASSESS-\d+$"),
    re.compile(r"^VERIF-\d+$"),
    re.compile(r"^DOC-[0-9a-f]{16}$"),
    re.compile(r"^PROP-[A-Z0-9-]+$"),
    re.compile(r"^R-\d+$"),
)

# ---------------------------------------------------------------------------
# Schema-aware recovery (deterministic, recorded, never bypasses validation)
# ---------------------------------------------------------------------------

# JSON-Schema echo keys that models sometimes copy into their answer body.
# They are metadata noise, never assessment content.
_SCHEMA_NOISE_KEYS = frozenset({"$defs", "$schema", "definitions", "$id", "$comment"})


def _schema_maxlen(model, field_name: str) -> int | None:
    """max_length constraint of a str field, read from the pydantic model."""
    info = model.model_fields.get(field_name)
    if info is None:
        return None
    for meta in info.metadata:
        max_len = getattr(meta, "max_length", None)
        if max_len:
            return int(max_len)
    return None


# Top-level FREE-TEXT fields that may be deterministically truncated when
# oversized. Evidence-bearing id/ref fields are NEVER repaired: a truncated
# id would be a corrupted id, and schema + grounding validation must reject
# those instead. Fixed Literal[True] approval flags are likewise never touched.
_REPAIR_TOP_TEXT = ("incident_summary", "objective_interpretation", "uncertainty_notes")

# Nested model fields (list[<model>]) whose free-text members may be truncated.
_REPAIR_NESTED_TEXT = {
    "facts": (AgentFact, ("statement",)),
    "inferences": (AgentInference, ("statement", "basis")),
    "proposed_next_actions": (AgentRecommendation, ("action", "rationale")),
    "recovery_candidates": (AgentRecoveryCandidate, ("feasibility_note",)),
}

# Concrete limit tables, derived once from the live schema so they cannot
# drift from schemas.py.
_REPAIR_TOP_LIMITS = {f: _schema_maxlen(AgentFinalAssessment, f) for f in _REPAIR_TOP_TEXT}
_REPAIR_NESTED_LIMITS = {
    list_field: (model, {f: _schema_maxlen(model, f) for f in fields})
    for list_field, (model, fields) in _REPAIR_NESTED_TEXT.items()
}


def _repair_final_payload(payload: dict) -> tuple[dict, list[str]]:
    """Deterministic pre-validation canonicalization of a final answer.

    Safe repairs ONLY, each one recorded by the caller in the trace:
    * drop JSON-Schema echo keys ($defs and friends);
    * truncate oversized FREE-TEXT strings to the schema max_length.

    A repair only ever REMOVES or SHORTENS content - it can never create a
    claim, an id, or an approval. The repaired payload must still pass the
    full schema validation AND the deterministic grounding validator, so a
    repair can never make an ungrounded or unsafe answer acceptable. This
    runs before validation (canonicalization), not instead of it.
    """
    repairs: list[str] = []
    cleaned: dict = {}
    for key, value in payload.items():
        if key in _SCHEMA_NOISE_KEYS:
            repairs.append(f"dropped non-assessment field '{key}' (JSON-Schema echo)")
        else:
            cleaned[key] = value
    for field_name, limit in _REPAIR_TOP_LIMITS.items():
        value = cleaned.get(field_name)
        if limit and isinstance(value, str) and len(value) > limit:
            cleaned[field_name] = value[:limit]
            repairs.append(
                f"{field_name} truncated from {len(value)} to {limit} chars (schema max_length)"
            )
    for list_field, (model, fields) in _REPAIR_NESTED_LIMITS.items():
        items = cleaned.get(list_field)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for text_field, limit in fields.items():
                value = item.get(text_field)
                if limit and isinstance(value, str) and len(value) > limit:
                    item[text_field] = value[:limit]
                    repairs.append(
                        f"{list_field}[{i}].{text_field} truncated from {len(value)} to "
                        f"{limit} chars (schema max_length)"
                    )
    return cleaned, repairs


def _format_schema_error(exc: ValidationError, repairs: list[str]) -> str:
    """Model-actionable, deterministic description of every schema error.

    One clause per error with the exact field path and the fix, so a bounded
    retry can actually correct the payload (unlike a raw exception dump).
    """
    errors = exc.errors()
    parts: list[str] = []
    for e in errors[:20]:
        loc = ".".join(str(p) for p in e.get("loc", ())) or "<payload>"
        etype = e.get("type", "")
        msg = e.get("msg", "invalid value")
        if etype == "extra_forbidden":
            parts.append(f"{loc}: unexpected field, not part of the assessment schema - remove it")
        elif etype == "string_too_long":
            parts.append(f"{loc}: {msg} - shorten this field")
        elif etype == "missing":
            parts.append(f"{loc}: required field is missing - add it")
        elif etype == "literal_error":
            parts.append(f"{loc}: {msg} - this value is fixed by the schema and must not be changed")
        else:
            parts.append(f"{loc}: {msg}")
    if len(errors) > 20:
        parts.append(f"... and {len(errors) - 20} more errors")
    text = "final assessment failed schema validation: " + "; ".join(parts)
    if repairs:
        text += " [deterministic repairs applied before validation: " + "; ".join(repairs) + "]"
    return text


def sanitize_error(exc: Exception) -> str:
    """One-line error text safe to persist and show (never credentials)."""
    text = str(exc).replace("\n", " ")[:400]
    for token in re.findall(r"(sk-[A-Za-z0-9_-]{8,})", text):
        text = text.replace(token, "sk-***")
    return text


# ---------------------------------------------------------------------------
# Provider layer
# ---------------------------------------------------------------------------


@dataclass
class ToolCallReq:
    call_id: str
    name: str
    arguments: dict


@dataclass
class ProviderTurn:
    """One model response, normalized across protocols."""

    content: str = ""
    tool_calls: list[ToolCallReq] = field(default_factory=list)
    protocol: str = "native"  # native | json
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str = ""
    model: str = ""


class ProviderError(Exception):
    """Sanitized provider failure (never contains credentials)."""


class ProviderToolingUnsupported(ProviderError):
    """The deployed model/endpoint rejected native tool calling."""


class ChatProvider(Protocol):
    """Minimal provider interface consumed by the orchestrator."""

    model_label: str

    def chat(self, messages: list[dict], tool_defs: list[dict] | None) -> ProviderTurn:
        """Run one chat completion. `tool_defs=None` requests the strict JSON
        protocol (tool schemas are embedded in the system prompt instead)."""
        ...


def final_assessment_schema() -> dict:
    """JSON schema of the strict final assessment, advertised to the model."""
    return AgentFinalAssessment.model_json_schema()


def build_system_prompt(tool_defs: list[dict]) -> str:
    schema = json.dumps(final_assessment_schema(), indent=1)
    tools = "\n".join(
        f"- {d['function']['name']}: {d['function']['description']}" for d in tool_defs
    )
    return f"""You are the RouteShield Incident Response Agent for a school-bus fleet responding to \
NHTSA recall campaign 26V111 (International/IC Bus CESB hydraulic brakes). You investigate a recall \
incident by calling READ-ONLY tools, then produce a grounded structured assessment.

HARD CONSTRAINTS (violations end the investigation):
* You are a READ-ONLY investigator. You cannot and must not change any operational state: no route \
reassignment, no policy selection, no approvals, no plan generation. Every operational change goes \
through the fleet manager's approval endpoint, outside this conversation.
* Vehicle classifications (CAMPAIGN_CRITERIA_MATCH / VERIFICATION_REQUIRED / OUTSIDE_REPORT_VERSION) \
are computed by a deterministic rule engine. Report them EXACTLY as the tools returned them; never \
re-derive, override or reinterpret them.
* Never invent asset ids, route ids, evidence references, numbers or recall facts. Every factual \
statement in the final output must cite evidence reference ids that appeared verbatim in tool \
results (e.g. CRIT-12, ASSESS-3, DOC-<sha16>, VERIF-7, PROP-...).
* Absence of evidence is not evidence of safety. If evidence is missing or ambiguous, say so in \
"unknowns_and_missing_evidence" instead of concluding.
* All fleet data is SYNTHETIC DEMONSTRATION DATA. Surface the evidence banner / provenance the \
tools give you; never present fleet records as real vehicles.

CLASSIFICATION SEMANTICS (fixed wording, use as-is):
* CAMPAIGN_CRITERIA_MATCH: All REQUIRED reviewed criteria match. This is NOT a VIN-level \
confirmation of an open recall.
* VERIFICATION_REQUIRED: Critical evidence missing or ambiguous - the deterministic engine \
abstains. Absence of evidence is not evidence of safety.
* OUTSIDE_REPORT_VERSION: Outside this report version's applicability criteria. This does NOT \
mean the vehicle is SAFE.

IDENTIFIER TYPES (never mix them - mixing them fails grounding validation):
* asset_id (e.g. SYNTHETIC-BUS-001, SYNTHETIC-SPARE-01) identifies a fleet VEHICLE. Use it ONLY \
where the schema expects an asset id: vehicle_classifications[].asset_id and \
recovery_candidates[].spare_asset_id. An asset_id is NEVER an evidence reference, even when the \
same tool object also contains a ref.
* evidence reference (the fields tools name "ref" or "evidence_refs", e.g. CRIT-12, ASSESS-13, \
VERIF-7, DOC-<sha16>, PROP-...) is the citation id of a record. Use it in facts[].evidence_refs, \
evidence_references[].ref_id and vehicle_classifications[].basis_ref. When a spare or vehicle \
object shows BOTH an asset_id and a ref, cite the REF as evidence and use the asset_id only to \
name the vehicle itself.
* route_id (e.g. R-101) is the EXACT route identifier from the structured route objects of \
preview_route_impact. affected_routes[] and recovery_candidates[].route_id must contain ONLY \
exact route_id values like "R-101". Never put route names or composite strings such as "R-101 \
Northside Elementary Run (DISRUPTED...)" into identifier fields; route names, impacts and \
policy discussions belong in facts/inferences instead.
* The two operational policies are distinct - HOLD_PENDING_VERIFICATION and \
MANAGER_REVIEW_ONLY. Report per-policy outcomes separately; never merge them into one claim.

TOOLS (read-only, {len(tool_defs)} available):
{tools}

WORKFLOW:
* You have at most {HARD_MAX_ITERATIONS} model rounds. Be efficient: gather evidence, then finish.
* Typical flow: get_recall_evidence -> get_fleet_assessment -> preview_route_impact -> optionally \
get_vehicle_detail / get_available_spares / get_open_verification_items -> final assessment.
* When you have enough evidence, stop calling tools and emit the final assessment.

FINAL ANSWER (strict): reply with a single JSON object matching exactly this schema - no prose, no \
markdown fences, no extra keys:
{schema}

Every fact must cite at least one evidence ref that a tool actually returned. Every proposed action \
requires_human_approval=true (the schema enforces it). affected_routes must contain only exact \
route_id values (e.g. R-101) returned by preview_route_impact - never route names or composite \
strings."""


_JSON_PROTOCOL_INSTRUCTIONS = """TOOL PROTOCOL (strict JSON mode - this endpoint does not support native function calling):
Every reply MUST be exactly ONE JSON object and nothing else:
* To call a tool: {"action": "tool", "tool": "<tool name>", "args": {<arguments>}}
* To finish: {"action": "final", "assessment": {<final assessment object exactly per the schema above>}}
Tool results are returned to you as user messages containing one JSON object under "tool_result".
Never emit prose, markdown, or more than one object."""


class VultrChatProvider:
    """OpenAI-compatible chat provider backed by Vultr Serverless Inference."""

    def __init__(self, model: str | None = None):
        settings = get_settings()
        self.api_key = settings.vultr_inference_api_key
        self.base_url = settings.vultr_inference_base_url
        self.model = model or settings.vultr_inference_model
        self.timeout = settings.agent_llm_timeout_seconds
        self.model_label = f"vultr:{self.model}" if self.model else "vultr:<unconfigured>"

    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    def chat(self, messages: list[dict], tool_defs: list[dict] | None) -> ProviderTurn:
        from openai import OpenAI

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=1,
        )
        kwargs: dict = {"model": self.model, "messages": messages, "temperature": 0}
        if tool_defs is not None:
            kwargs["tools"] = tool_defs
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - normalized to sanitized errors
            msg = sanitize_error(exc)
            lowered = msg.lower()
            # Endpoints/models that reject the tools parameter entirely.
            if tool_defs is not None and (
                "tool" in lowered and any(code in msg for code in ("400", "404", "422"))
            ):
                raise ProviderToolingUnsupported(msg) from exc
            raise ProviderError(msg) from exc

        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        turn = ProviderTurn(
            content=choice.message.content or "",
            protocol="native" if tool_defs is not None else "json",
            finish_reason=choice.finish_reason or "",
            model=getattr(resp, "model", "") or self.model,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
        )
        for tc in choice.message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": (tc.function.arguments or "")[:400]}
            turn.tool_calls.append(
                ToolCallReq(call_id=tc.id, name=tc.function.name, arguments=args)
            )
        return turn


class StubChatProvider:
    """Scripted provider for automated tests ONLY.

    Never constructed by production code paths; its model label carries an
    explicit TEST-ONLY marker that is persisted with the session, so stub
    runs can never be presented as real AI inference.
    """

    TEST_LABEL = "STUB-PROVIDER-TEST-ONLY"

    def __init__(self, turns: list[dict]):
        self._turns = list(turns)
        self.received_messages: list[list[dict]] = []
        self.received_tool_defs: list[list[dict] | None] = []
        self.model_label = f"{self.TEST_LABEL}:scripted"

    def configured(self) -> bool:
        return True

    def chat(self, messages: list[dict], tool_defs: list[dict] | None) -> ProviderTurn:
        self.received_messages.append(json.loads(json.dumps(messages)))
        self.received_tool_defs.append(
            json.loads(json.dumps(tool_defs)) if tool_defs is not None else None
        )
        if not self._turns:
            raise ProviderError("Stub script exhausted: no scripted turns remain.")
        spec = self._turns.pop(0)
        turn = ProviderTurn(model=self.model_label)
        if "tool" in spec:
            turn.tool_calls.append(
                ToolCallReq(
                    call_id=f"stub-{uuid.uuid4().hex[:8]}",
                    name=spec["tool"],
                    arguments=spec.get("args", {}),
                )
            )
            turn.finish_reason = "tool_calls"
        elif "raw" in spec:  # verbatim model content (used to test rejection paths)
            turn.content = spec["raw"]
            turn.finish_reason = "stop"
        elif "final" in spec:
            # Flat form (what a model would actually emit under the documented
            # contract) - exercises the liberal acceptance path.
            turn.content = json.dumps({"action": "final", **spec["final"]})
            turn.finish_reason = "stop"
        elif "error" in spec:
            raise ProviderError(str(spec["error"]))
        return turn


# ---------------------------------------------------------------------------
# Deterministic grounding: the set of things the model may legitimately reference
# ---------------------------------------------------------------------------


ROUTE_IMPACT_TOOL = "preview_route_impact"


@dataclass
class RefIndex:
    """Everything the model observed in real tool results this session.

    refs:
        Canonical EVIDENCE-reference ids returned by tools (CRIT-n, ASSESS-n,
        VERIF-n, DOC-<sha16>, proposal uids, route ids). Collected from
        "ref"/"proposal_uid"/"basis_ref" values AND from bare-string
        "evidence_refs" lists (pattern-guarded, so URLs never count).
    assets:
        Fleet asset ids (vehicle identities, e.g. SYNTHETIC-BUS-001).
    routes:
        Canonical route ids observed in SUCCESSFUL preview_route_impact route
        objects ONLY - the structured route-impact source of truth for
        affected_routes / recovery candidates. A route id seen elsewhere (e.g.
        a vehicle's assigned route in get_vehicle_detail) is NOT sufficient
        grounding for a route reference.
    classifications:
        asset_id -> deterministic engine classification (from fleet assessment
        rows and from route-impact rows' assigned_vehicle_classification).
    eligible_spares:
        Spares the deterministic engine marked eligible/feasible.
    """

    refs: set[str] = field(default_factory=set)
    assets: set[str] = field(default_factory=set)
    routes: set[str] = field(default_factory=set)
    classifications: dict[str, str] = field(default_factory=dict)
    eligible_spares: set[str] = field(default_factory=set)
    tools_used: set[str] = field(default_factory=set)

    def observe(self, tool_name: str, result: dict) -> None:
        self.tools_used.add(tool_name)
        self._walk(result, key=None, route_source=(tool_name == ROUTE_IMPACT_TOOL))

    def _walk(self, node, key: str | None = None, *, route_source: bool) -> None:
        if isinstance(node, dict):
            asset_id = node.get("asset_id")
            if isinstance(asset_id, str) and isinstance(node.get("classification"), str):
                self.classifications[asset_id] = node["classification"]
            assigned = node.get("assigned_vehicle")
            if isinstance(assigned, str) and isinstance(
                node.get("assigned_vehicle_classification"), str
            ):
                # Route-impact rows carry the engine classification of the
                # assigned vehicle - engine-authoritative, same as above.
                self.classifications[assigned] = node["assigned_vehicle_classification"]
            spare = node.get("spare_asset_id")
            if isinstance(spare, str) and (
                node.get("feasible") is True
                or node.get("eligible_as_replacement") is True
            ):
                self.eligible_spares.add(spare)
            if node.get("eligible_as_replacement") is True and isinstance(asset_id, str):
                self.eligible_spares.add(asset_id)
            for k, v in node.items():
                self._observe_scalar(k, v, route_source=route_source)
                self._walk(v, key=k, route_source=route_source)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    self._observe_list_scalar(key, item)
                else:
                    self._walk(item, key=key, route_source=route_source)

    def _observe_scalar(self, key: str, value, *, route_source: bool) -> None:
        if not isinstance(value, str):
            return
        if key in ("ref", "proposal_uid", "basis_ref") or any(p.match(value) for p in _REF_PATTERNS):
            self.refs.add(value)
        if key in ("asset_id", "spare_asset_id", "assigned_vehicle", "original_vehicle"):
            self.assets.add(value)
        if key == "route_id" and route_source:
            self.routes.add(value)

    def _observe_list_scalar(self, key: str | None, item: str) -> None:
        """Bare strings inside tool-output lists (previously invisible).

        "evidence_refs" entries are indexed only when they match a canonical
        evidence-reference shape, so URLs/free text never become citations.
        "eligible_spare_pool" is the recovery engine's authoritative eligible
        pool from preview_route_impact.
        """
        if key == "evidence_refs" and any(p.match(item) for p in _REF_PATTERNS):
            self.refs.add(item)
        elif key == "eligible_spare_pool":
            self.eligible_spares.add(item)


def _available(ids, limit: int = 12) -> str:
    """Bounded, model-actionable listing of the canonical ids of one type."""
    ordered = sorted(ids)
    if not ordered:
        return "(none were returned by tools this session)"
    text = ", ".join(ordered[:limit])
    if len(ordered) > limit:
        text += f" (+{len(ordered) - limit} more)"
    return text


def _route_feedback(candidate: str, index: RefIndex) -> str:
    """Actionable, deterministic detail for a rejected route reference."""
    if not index.routes:
        return (
            "no route objects were returned by a successful preview_route_impact call this "
            "session - call it successfully before citing routes"
        )
    containing = [r for r in sorted(index.routes) if r != candidate and r in candidate]
    if containing:
        return (
            f"this is a composite description containing route id(s) {', '.join(containing)} - "
            "identifier fields must hold EXACTLY the route_id value; put names, impacts and "
            "policy discussion in facts/inferences instead. Exact route_ids returned by "
            f"preview_route_impact: {_available(index.routes)}"
        )
    return f"exact route_ids returned by preview_route_impact: {_available(index.routes)}"


def validate_grounding(final: AgentFinalAssessment, index: RefIndex) -> list[str]:
    """Deterministic grounding check against observed tool outputs.

    Returns a list of violations; empty list == grounded. Every violation is
    model-actionable: it names the expected identifier TYPE and the canonical
    ids of that type the tools actually returned, so a bounded retry can fix
    the citation instead of guessing.
    """
    violations: list[str] = []

    def _evidence_ref_violation(where: str, ref: str) -> str:
        if ref in index.assets:
            return (
                f"{where}: evidence ref '{ref}' is an ASSET ID, not an evidence reference - "
                "cite the evidence ref the tool returned for the record (see the ref/"
                f"evidence_refs fields next to it). Available evidence refs: {_available(index.refs)}"
            )
        if ref in index.routes:
            return (
                f"{where}: evidence ref '{ref}' is a ROUTE ID, not an evidence reference - "
                f"cite the evidence ref the tool returned for this statement. "
                f"Available evidence refs: {_available(index.refs)}"
            )
        return (
            f"{where}: evidence ref '{ref}' was never returned by a tool. "
            f"Available evidence refs: {_available(index.refs)}"
        )

    for i, fact in enumerate(final.facts):
        if not fact.evidence_refs:
            violations.append(f"facts[{i}]: no evidence_refs cited")
        for ref in fact.evidence_refs:
            if ref not in index.refs:
                violations.append(_evidence_ref_violation(f"facts[{i}]", ref))

    for i, claim in enumerate(final.vehicle_classifications):
        if claim.asset_id not in index.assets:
            violations.append(
                f"vehicle_classifications[{i}]: asset '{claim.asset_id}' never appeared in a "
                f"tool result. Assets observed this session: {_available(index.assets)}"
            )
        elif claim.asset_id in index.classifications and (
            index.classifications[claim.asset_id] != claim.classification
        ):
            violations.append(
                f"vehicle_classifications[{i}]: classification '{claim.classification}' for "
                f"'{claim.asset_id}' contradicts the deterministic engine value "
                f"'{index.classifications[claim.asset_id]}'"
            )
        # basis_ref is an evidence id: it must resolve to something a tool
        # actually returned, never an invented id (checked only when cited).
        if claim.basis_ref and claim.basis_ref not in index.refs:
            violations.append(
                f"vehicle_classifications[{i}]: basis_ref '{claim.basis_ref}' was never "
                f"returned by a tool. Available evidence refs: {_available(index.refs)}"
            )

    for i, route_id in enumerate(final.affected_routes):
        if route_id not in index.routes:
            violations.append(
                f"affected_routes[{i}]: '{route_id}' is not a canonical route_id returned by a "
                f"successful preview_route_impact call - {_route_feedback(route_id, index)}"
            )

    for i, cand in enumerate(final.recovery_candidates):
        if cand.route_id not in index.routes:
            violations.append(
                f"recovery_candidates[{i}]: '{cand.route_id}' is not a canonical route_id "
                f"returned by a successful preview_route_impact call - "
                f"{_route_feedback(cand.route_id, index)}"
            )
        if cand.spare_asset_id:
            if cand.spare_asset_id not in index.assets:
                violations.append(
                    f"recovery_candidates[{i}]: spare '{cand.spare_asset_id}' never appeared in "
                    f"a tool result. Assets observed this session: {_available(index.assets)}"
                )
            elif cand.spare_asset_id not in index.eligible_spares:
                violations.append(
                    f"recovery_candidates[{i}]: spare '{cand.spare_asset_id}' was never shown as "
                    "an eligible/feasible replacement by a tool. Eligible spares observed: "
                    f"{_available(index.eligible_spares)}"
                )

    for i, ref in enumerate(final.evidence_references):
        if ref.ref_id not in index.refs:
            violations.append(_evidence_ref_violation(f"evidence_references[{i}]", ref.ref_id))

    return violations


# ---------------------------------------------------------------------------
# Conversation history (protocol-agnostic, rendered per provider call)
# ---------------------------------------------------------------------------


@dataclass
class _HistoryEvent:
    role: str  # user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCallReq] = field(default_factory=list)
    tool_call_id: str = ""


class _Conversation:
    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
        self.events: list[_HistoryEvent] = []

    def user(self, content: str) -> None:
        self.events.append(_HistoryEvent(role="user", content=content))

    def assistant_tool_calls(self, calls: list[ToolCallReq]) -> None:
        self.events.append(_HistoryEvent(role="assistant", tool_calls=calls))

    def assistant(self, content: str) -> None:
        self.events.append(_HistoryEvent(role="assistant", content=content))

    def tool(self, call_id: str, payload: str) -> None:
        self.events.append(_HistoryEvent(role="tool", tool_call_id=call_id, content=payload))

    def render(self, protocol: str) -> list[dict]:
        if protocol == "json":
            # Strict JSON protocol: the tool-contract replaces the native
            # `tools` parameter and is appended to the system prompt.
            system = self.system_prompt + "\n\n" + _JSON_PROTOCOL_INSTRUCTIONS
        else:
            system = self.system_prompt
        if protocol == "native":
            msgs: list[dict] = [{"role": "system", "content": system}]
            for ev in self.events:
                if ev.role == "assistant" and ev.tool_calls:
                    msgs.append({
                        "role": "assistant",
                        "content": ev.content or None,
                        "tool_calls": [
                            {
                                "id": tc.call_id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in ev.tool_calls
                        ],
                    })
                elif ev.role == "tool":
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": ev.tool_call_id,
                        "content": ev.content,
                    })
                else:
                    msgs.append({"role": ev.role, "content": ev.content})
            return msgs
        # Strict JSON protocol: everything collapses to system/user turns.
        msgs = [{"role": "system", "content": system}]
        for ev in self.events:
            if ev.role == "assistant":
                if ev.tool_calls:
                    msgs.append({"role": "assistant", "content": json.dumps({
                        "action": "tool",
                        "tool": ev.tool_calls[0].name,
                        "args": ev.tool_calls[0].arguments,
                    })})
                elif ev.content:
                    msgs.append({"role": "assistant", "content": ev.content})
            elif ev.role == "tool":
                try:
                    payload = json.loads(ev.content)
                except json.JSONDecodeError:
                    payload = {"_unparseable": ev.content[:400]}
                msgs.append({"role": "user", "content": json.dumps({"tool_result": payload})})
            else:
                msgs.append({"role": "user", "content": ev.content})
        return msgs


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _extract_json_object(raw: str) -> tuple[dict | None, str]:
    """Best-effort extraction of one JSON object from model content."""
    text = _strip_fences(raw)
    candidates = [text]
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj, ""
    return None, "content is not a single JSON object"


# ---------------------------------------------------------------------------
# The bounded investigation loop
# ---------------------------------------------------------------------------


class InvestigationRunner:
    def __init__(
        self,
        db: Session,
        provider: ChatProvider,
        *,
        max_iterations: int | None = None,
        total_budget_seconds: float | None = None,
    ):
        self.db = db
        self.provider = provider
        settings = get_settings()
        configured = max_iterations or settings.agent_max_iterations
        self.max_iterations = max(1, min(int(configured), HARD_MAX_ITERATIONS))
        self.total_budget = total_budget_seconds or settings.agent_total_budget_seconds
        self.tool_defs = agent_tools.openai_tool_defs()
        self.system_prompt = build_system_prompt(self.tool_defs)
        if isinstance(provider, StubChatProvider):
            # Tests may exercise the JSON protocol explicitly; production
            # always starts native and downgrades on demand.
            pass
        self.index = RefIndex()
        self.protocol = "native"
        self._started = time.monotonic()
        # Read-only tools run on their own short-lived Sessions, never on
        # `self.db` (see _execute_tool_call). The factory is bound to the
        # SAME engine as the investigation session, so tool calls observe
        # exactly the state the orchestrator sees; sessionmaker itself is
        # thread-safe and meant to be used from worker threads.
        self._tool_session_factory = sessionmaker(
            bind=db.get_bind(), autoflush=False, expire_on_commit=False
        )

    # -- helpers -----------------------------------------------------------

    def _budget_left(self) -> float:
        return self.total_budget - (time.monotonic() - self._started)

    def _step(self, session: models.InvestigationSession, seq: int, kind: str, **kw) -> None:
        self.db.add(models.InvestigationStep(session_id=session.id, seq=seq, kind=kind, **kw))
        self.db.commit()

    def _execute_tool_call(self, name: str, args) -> dict:
        """Worker-thread entry point for one read-only tool call.

        SESSION LIFECYCLE (regression-covered in tests/test_agent.py):

        * The callable receives NO reference to `self.db`. SQLAlchemy
          Sessions are not thread-safe, and the orchestrator thread keeps
          using `self.db` (committing steps, finalising the session) while
          - and after - a tool runs in the worker thread.
        * A private Session is created here and closed in the `finally`
          below, entirely inside this worker thread. `fut.result()` can
          only return after close() has run, and a timed-out (abandoned)
          worker closes its own session when its handler eventually
          returns - so a hung call can never leak an open transaction into
          the pool or touch the orchestrator's session and its later
          writes.
        * Tools are read-only by registry contract: nothing here ever
          commits. close() discards the read transaction; the connection
          goes back to the pool untouched.
        """
        tool_db = self._tool_session_factory()
        try:
            return agent_tools.execute(tool_db, name, args)
        finally:
            tool_db.close()

    def _run_tool(self, name: str, args_raw: dict) -> tuple[dict | None, str | None, str]:
        """Execute one tool call with validation + timeout.

        Returns (result, error, status). Exactly one of result/error is None.
        """
        if not isinstance(args_raw, dict):
            return None, f"INVALID_ARGS: arguments must be a JSON object, got {type(args_raw).__name__}.", "INVALID_ARGS"
        spec = agent_tools.REGISTRY.get(name)
        if spec is None:
            return (
                None,
                f"Unknown tool '{name}'. Allowed tools: {sorted(agent_tools.REGISTRY)}.",
                "INVALID_TOOL_CALL",
            )
        try:
            args = agent_tools.parse_args(name, args_raw)
        except ValueError as exc:
            return None, f"INVALID_ARGS: {exc}", "INVALID_ARGS"
        # Read-only DB work in a worker thread so a hung handler cannot wedge
        # the investigation. The worker runs the tool on its OWN session
        # (created and closed inside _execute_tool_call) - never on `self.db`:
        # a timed-out worker keeps running in the background after
        # shutdown(wait=False), and sharing the orchestrator's Session with it
        # would interleave two threads on one Session/connection. Shutdown
        # stays non-blocking: the loop continues immediately.
        ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
        try:
            fut = ex.submit(self._execute_tool_call, name, args)
            try:
                return fut.result(timeout=spec.timeout_seconds), None, "OK"
            except FutureTimeoutError:
                return (
                    None,
                    f"TOOL_TIMEOUT: '{name}' exceeded {spec.timeout_seconds:.0f}s and was abandoned.",
                    "TIMEOUT",
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the model, not the stack
                return None, f"TOOL_ERROR: {sanitize_error(exc)}", "TOOL_ERROR"
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    def _chat_round(self, conversation: _Conversation) -> ProviderTurn:
        tool_defs = self.tool_defs if self.protocol == "native" else None
        try:
            return self.provider.chat(conversation.render(self.protocol), tool_defs)
        except ProviderToolingUnsupported:
            if self.protocol == "native":
                self.protocol = "json"  # downgrade for the rest of the session
                return self.provider.chat(conversation.render("json"), None)
            raise

    def _cap_payload(self, result: dict) -> dict:
        text = json.dumps(result, default=str)
        if len(text) <= MAX_TOOL_PAYLOAD_CHARS:
            return result
        return {
            "_truncated": True,
            "_note": "payload exceeded the trace cap; shown truncated",
            "_payload": text[:MAX_TOOL_PAYLOAD_CHARS],
        }

    # -- final-answer handling ------------------------------------------------

    def _accept_final(self, raw: str) -> tuple[AgentFinalAssessment | None, str, list[str]]:
        """Parse, deterministically repair, then schema-validate a final answer.

        Returns (assessment, error, repairs). Exactly one of assessment/error
        is None; repairs lists every deterministic pre-validation edit (it is
        empty or non-empty independently of the outcome and is persisted by
        the caller either way). A failed response is never accepted: the
        repaired payload must still pass the full schema here and the
        grounding validator in the caller.
        """
        obj, err = _extract_json_object(raw)
        if obj is None:
            return None, f"final answer {err}", []
        if obj.get("action") == "tool":
            return None, (
                "final answer used the tool action; to call tools reply with tool calls, and "
                "when done emit the final assessment object"
            ), []
        if obj.get("action") == "final":
            nested = obj.get("assessment")
            if isinstance(nested, dict):
                payload = nested
            else:  # flat form: {"action": "final", <schema fields>}
                payload = {k: v for k, v in obj.items() if k != "action"}
        else:
            payload = obj  # bare assessment object (native protocol contract)
        if not isinstance(payload, dict):
            return None, "final answer must be the assessment JSON object", []
        payload, repairs = _repair_final_payload(payload)
        try:
            return AgentFinalAssessment.model_validate(payload), "", repairs
        except ValidationError as exc:
            return None, _format_schema_error(exc, repairs), repairs
        except Exception as exc:  # noqa: BLE001 - pydantic edge cases
            return (
                None,
                f"final assessment failed schema validation: {sanitize_error(exc)}",
                repairs,
            )

    # -- main loop ---------------------------------------------------------

    def run(self, session: models.InvestigationSession) -> models.InvestigationSession:
        session.model = self.provider.model_label  # stub label carries TEST-ONLY marker
        self.db.commit()

        conversation = _Conversation(self.system_prompt)
        conversation.user(
            f"INVESTIGATION OBJECTIVE: {session.objective}\n"
            f"Incident under investigation: id={session.incident_id} "
            f"(campaign {session.incident.campaign_number}). Investigate with the read-only "
            "tools, then produce the final assessment JSON."
        )

        stop_reason = ""
        provider_error = ""
        final_result: dict | None = None
        violations: list[str] = []
        seq = 0

        try:
            configured = getattr(self.provider, "configured", None)
            if callable(configured) and not configured():
                stop_reason = STOP_MODEL_NOT_CONFIGURED
                provider_error = (
                    "VULTR_INFERENCE_API_KEY / VULTR_INFERENCE_MODEL are not configured. "
                    "No inference was performed and nothing was simulated."
                )
            else:
                stop_reason, final_result, violations, seq = self._loop(
                    session, conversation, seq
                )
        except ProviderError as exc:
            stop_reason = STOP_PROVIDER_ERROR
            provider_error = sanitize_error(exc)
        except Exception as exc:  # noqa: BLE001 - investigations must reach a terminal state
            logger.exception("Investigation %s crashed", session.session_uid)
            stop_reason = STOP_PROVIDER_ERROR
            provider_error = f"unexpected error: {sanitize_error(exc)}"

        completed = final_result is not None and not violations and stop_reason == STOP_MODEL_STOPPED
        session.status = "COMPLETED" if completed else "FAILED"
        session.stop_reason = stop_reason
        session.provider_error = provider_error
        session.grounding_violations = violations
        session.final_result = final_result if completed else None
        session.finished_at = models.utcnow()
        session.duration_ms = int((time.monotonic() - self._started) * 1000)
        self.db.commit()
        return session

    def _loop(
        self,
        session: models.InvestigationSession,
        conversation: _Conversation,
        seq: int,
    ):
        invalid_final_retries = 0
        grounding_retries = 0
        stop_reason = ""
        final_result: dict | None = None
        violations: list[str] = []

        for iteration in range(1, self.max_iterations + 1):
            if self._budget_left() <= 0:
                stop_reason = STOP_TIME_BUDGET
                break

            turn = self._chat_round(conversation)
            session.iterations_used = iteration
            if turn.prompt_tokens:
                session.prompt_tokens = (session.prompt_tokens or 0) + turn.prompt_tokens
            if turn.completion_tokens:
                session.completion_tokens = (session.completion_tokens or 0) + turn.completion_tokens
            self.db.commit()

            # Persist narration (not hidden chain-of-thought: the model is
            # instructed to give one-sentence rationale only).
            if turn.content.strip() and turn.tool_calls:
                seq += 1
                self._step(
                    session, seq, "MODEL_MESSAGE", status="OK",
                    explanation=turn.content.strip()[:2000],
                )
                conversation.assistant(turn.content.strip())

            if turn.tool_calls:
                conversation.assistant_tool_calls(turn.tool_calls)
                for tc in turn.tool_calls:
                    if self._budget_left() <= 0:
                        stop_reason = STOP_TIME_BUDGET
                        break
                    seq += 1
                    t0 = time.monotonic()
                    result, error, status = self._run_tool(tc.name, tc.arguments)
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    session.tool_calls_count = (session.tool_calls_count or 0) + 1
                    if error is not None:
                        self._step(
                            session, seq, "TOOL_CALL", tool_name=tc.name,
                            tool_args=tc.arguments if isinstance(tc.arguments, dict) else None,
                            status=status, explanation=error[:2000],
                            duration_ms=duration_ms,
                        )
                        conversation.tool(tc.call_id, json.dumps({"error": error}))
                    else:
                        capped = self._cap_payload(result)
                        self.index.observe(tc.name, result)
                        self._step(
                            session, seq, "TOOL_CALL", tool_name=tc.name,
                            tool_args=tc.arguments if isinstance(tc.arguments, dict) else None,
                            status="OK", result=capped, duration_ms=duration_ms,
                        )
                        conversation.tool(tc.call_id, json.dumps(capped, default=str))
                if stop_reason:
                    break
                continue

            # No tool calls -> the model must deliver the final assessment.
            content = turn.content
            if not content.strip() and turn.protocol == "json":
                content = ""
            t_val = time.monotonic()
            assessment, err, repairs = self._accept_final(content)
            val_ms = int((time.monotonic() - t_val) * 1000)
            if repairs:
                # Observability: every deterministic repair is persisted, even
                # when the assessment is ultimately accepted.
                seq += 1
                self._step(
                    session, seq, "VALIDATION", status="SCHEMA_REPAIR",
                    explanation="deterministic pre-validation repairs applied to the final answer",
                    result={"repairs": repairs, "model_round": iteration},
                    duration_ms=val_ms,
                )
            if assessment is None:
                invalid_final_retries += 1
                seq += 1
                self._step(
                    session, seq, "VALIDATION", status=STOP_INVALID_FINAL,
                    explanation=err[:2000],
                    result={
                        "attempt": invalid_final_retries,
                        "max_retries": MAX_FINAL_RETRIES,
                        "model_round": iteration,
                        "repairs": repairs,
                    },
                    duration_ms=val_ms,
                )
                if invalid_final_retries >= MAX_FINAL_RETRIES:
                    stop_reason = STOP_INVALID_FINAL
                    break
                conversation.assistant(content[:2000] if content else "(empty reply)")
                conversation.user(
                    "Your reply was REJECTED: " + err + "\nRe-read the final-answer contract and "
                    "reply with exactly one JSON object matching the schema (or call a tool first "
                    "if you still need evidence)."
                )
                continue

            t_val = time.monotonic()
            violations = validate_grounding(assessment, self.index)
            val_ms = int((time.monotonic() - t_val) * 1000)
            if violations:
                grounding_retries += 1
                seq += 1
                self._step(
                    session, seq, "VALIDATION", status=STOP_GROUNDING_FAILED,
                    explanation="grounding validator rejected the final assessment",
                    result={
                        "violations": violations,
                        "attempt": grounding_retries,
                        "max_retries": MAX_FINAL_RETRIES,
                        "model_round": iteration,
                    },
                    duration_ms=val_ms,
                )
                if grounding_retries >= MAX_FINAL_RETRIES:
                    stop_reason = STOP_GROUNDING_FAILED
                    break
                # Actionable feedback: every violation above already names the
                # expected identifier type and lists the canonical ids of that
                # type; restate the full canonical inventory by type so the
                # bounded retry can cite real ids instead of guessing.
                canonical = (
                    "\n\nCanonical identifiers actually returned by tools this session:\n"
                    f"- evidence refs (for facts[].evidence_refs / evidence_references[].ref_id "
                    f"/ basis_ref): {_available(self.index.refs)}\n"
                    f"- asset ids (for vehicle_classifications[].asset_id / "
                    f"recovery_candidates[].spare_asset_id): {_available(self.index.assets)}\n"
                    f"- route ids (for affected_routes / recovery_candidates[].route_id): "
                    f"{_available(self.index.routes)}\n"
                    f"- eligible spares: {_available(self.index.eligible_spares)}\n"
                    "Identifier fields must contain EXACT ids of the right type: an asset_id is "
                    "never an evidence reference, and route ids are exact values like R-101 - "
                    "never route names or composite strings."
                )
                conversation.assistant(content[:2000])
                conversation.user(
                    "Your final assessment was REJECTED by the deterministic grounding validator. "
                    "Ungrounded claims are never shown to users. Violations:\n- "
                    + "\n- ".join(violations)
                    + canonical
                    + "\nRe-emit the final assessment citing ONLY ids that tools actually "
                    "returned, using classifications exactly as the engine produced them. "
                    "Call tools again if you need to verify."
                )
                violations = []  # rejected draft must not leak into the session record
                continue

            # Grounded final answer accepted.
            seq += 1
            self._step(
                session, seq, "FINAL", status="OK",
                explanation=(
                    "final assessment accepted by schema + grounding validation"
                    + (f" after {len(repairs)} deterministic repair(s)" if repairs else "")
                ),
                duration_ms=val_ms,
            )
            final_result = assessment.model_dump()
            stop_reason = STOP_MODEL_STOPPED
            break

        if not stop_reason:
            stop_reason = STOP_MAX_ITERATIONS if final_result is None else STOP_MODEL_STOPPED
        return stop_reason, final_result, violations, seq


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_investigation(
    db: Session,
    *,
    incident_id: int,
    objective: str,
    requested_by: str,
    provider: ChatProvider | None = None,
    max_iterations: int | None = None,
    total_budget_seconds: float | None = None,
) -> models.InvestigationSession:
    """Create and run one bounded investigation synchronously.

    Writes ONLY InvestigationSession / InvestigationStep rows. The router
    launches it on a worker thread; tests may call it directly.
    """
    incident = db.get(models.Incident, incident_id)
    if incident is None:
        raise ValueError(f"Incident {incident_id} not found")

    provider = provider or VultrChatProvider()
    session = models.InvestigationSession(
        session_uid=f"INV-{uuid.uuid4().hex[:12].upper()}",
        incident_id=incident.id,
        objective=objective,
        requested_by=requested_by,
        status="INVESTIGATING",
    )
    db.add(session)
    db.commit()

    runner = InvestigationRunner(
        db, provider, max_iterations=max_iterations, total_budget_seconds=total_budget_seconds
    )
    return runner.run(session)


def launch_investigation(
    *,
    incident_id: int,
    objective: str,
    requested_by: str,
    provider: ChatProvider | None = None,
    db: Session | None = None,
) -> str:
    """Create the session row synchronously, then run the loop on a daemon
    thread. Returns the session_uid immediately; clients poll the GET
    endpoint. Writes ONLY InvestigationSession/InvestigationStep rows.
    """
    from ..db import SessionLocal

    own_db = db is None
    db = db or SessionLocal()
    try:
        incident = db.get(models.Incident, incident_id)
        if incident is None:
            raise ValueError(f"Incident {incident_id} not found")
        session = models.InvestigationSession(
            session_uid=f"INV-{uuid.uuid4().hex[:12].upper()}",
            incident_id=incident.id,
            objective=objective,
            requested_by=requested_by,
            status="INVESTIGATING",
        )
        db.add(session)
        db.commit()
        session_uid = session.session_uid
    finally:
        if own_db:
            db.close()

    def _work() -> None:
        thread_db = SessionLocal()
        try:
            session = thread_db.scalar(
                select(models.InvestigationSession).where(
                    models.InvestigationSession.session_uid == session_uid
                )
            )
            if session is None:  # pragma: no cover - row was committed above
                return
            runner = InvestigationRunner(thread_db, provider or VultrChatProvider())
            runner.run(session)
        except Exception:  # noqa: BLE001 - thread must never crash the app
            logger.exception("Background investigation %s failed", session_uid)
        finally:
            thread_db.close()

    threading.Thread(target=_work, name=f"agent-{session_uid}", daemon=True).start()
    return session_uid


def agent_status() -> dict:
    """Status payload for GET /api/agents/status (read-only, no LLM calls)."""
    settings = get_settings()
    provider = VultrChatProvider()
    configured = provider.configured()
    return {
        "configured": configured,
        "model": settings.vultr_inference_model or "",
        "base_url_host": _host_only(settings.vultr_inference_base_url),
        "max_iterations": min(settings.agent_max_iterations, HARD_MAX_ITERATIONS),
        "tools": sorted(agent_tools.REGISTRY),
        "write_tools_exposed": False,
        "note": (
            "Read-only investigation agent. Tool calls and outputs are persisted verbatim; "
            "the deterministic engine's classifications are authoritative; every operational "
            "change still requires manager approval."
            if configured
            else "Vultr Serverless Inference is not configured (VULTR_INFERENCE_API_KEY / "
            "VULTR_INFERENCE_MODEL). The agent cannot run and will report MODEL_NOT_CONFIGURED "
            "honestly - nothing is simulated."
        ),
    }


def probe_provider() -> dict:
    """Minimal live check of the inference endpoint + native tool calling.

    Costs one tiny completion. Never touches the database.
    """
    provider = VultrChatProvider()
    if not provider.configured():
        return {
            "configured": False,
            "tool_calling_supported": None,
            "model": "",
            "detail": "VULTR_INFERENCE_API_KEY / VULTR_INFERENCE_MODEL not set.",
            "duration_ms": 0,
        }
    probe_tool = [{
        "type": "function",
        "function": {
            "name": "get_recall_evidence",
            "description": "Retrieve recall evidence for an incident.",
            "parameters": {
                "type": "object",
                "properties": {"incident_id": {"type": "integer", "minimum": 1}},
                "required": ["incident_id"],
            },
        },
    }]
    messages = [
        {"role": "system", "content": "You are a connectivity probe. Use the provided tool exactly once."},
        {"role": "user", "content": "Call get_recall_evidence with incident_id 1."},
    ]
    started = time.monotonic()
    try:
        turn = provider.chat(messages, probe_tool)
    except ProviderToolingUnsupported as exc:
        return {
            "configured": True,
            "tool_calling_supported": False,
            "model": provider.model,
            "detail": f"Native tool calling rejected; strict JSON protocol will be used: {exc}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except ProviderError as exc:
        return {
            "configured": True,
            "tool_calling_supported": None,
            "model": provider.model,
            "detail": f"Provider error: {exc}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    supported = bool(turn.tool_calls)
    return {
        "configured": True,
        "tool_calling_supported": supported,
        "model": turn.model or provider.model,
        "detail": (
            "Native function calling works." if supported
            else "Endpoint accepted the request but the model replied without a tool call."
        ),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _host_only(url: str) -> str:
    match = re.match(r"^https?://([^/]+)", url or "")
    return match.group(1) if match else ""
