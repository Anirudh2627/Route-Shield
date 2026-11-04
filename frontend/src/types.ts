/* API types mirroring the FastAPI schemas (single source of truth: backend/app/schemas.py) */

export type Classification =
  | "CAMPAIGN_CRITERIA_MATCH"
  | "VERIFICATION_REQUIRED"
  | "OUTSIDE_REPORT_VERSION";

export type OperationalPolicy = "HOLD_PENDING_VERIFICATION" | "MANAGER_REVIEW_ONLY";

export interface Campaign {
  id: number;
  campaign_number: string;
  manufacturer: string;
  make: string;
  model: string;
  model_years: number[];
  component: string;
  summary: string;
  consequence: string;
  remedy: string;
  potential_units_affected: number | null;
  manufacturer_recall_number: string;
  report_received_date: string;
  api_retrieved_at: string | null;
}

export interface ReportVersion {
  id: number;
  report_url: string;
  document_sha256: string;
  page_count: number | null;
  submission_date: string;
  amendment_info: string;
  retrieval_timestamp: string;
  extraction_timestamp: string | null;
  provenance: string;
  review_status: "REVIEWED" | "PENDING_REVIEW";
  notes: string;
}

export interface Criterion {
  id: number;
  criterion_type: string;
  label: string;
  value: Record<string, unknown>;
  source_document: string;
  source_page: number | null;
  source_snippet: string;
  extraction_method: string;
  confidence: number;
  review_status: string;
}

export interface Evidence {
  incident_id: number;
  campaign: Campaign | null;
  report_version: ReportVersion | null;
  criteria: Criterion[];
  evidence_source: string;
  evidence_banner: string;
  using_reviewed_fixture: boolean;
  live_retrieval_error?: string | null;
}

export interface Vehicle {
  id: number;
  asset_id: string;
  synthetic: boolean;
  synthetic_label: string;
  manufacturer: string;
  make: string;
  model: string;
  model_year: number | null;
  build_date: string | null;
  configuration: Record<string, unknown> | null;
  brake_feature_code: string | null;
  vin: string | null;
  vin_status: string;
  depot: string;
  passenger_capacity: number;
  role: "ROUTE_BUS" | "SPARE";
  availability: string;
  operational_status: string;
  features: string[];
  notes: string;
}

export interface Route {
  id: number;
  route_id: string;
  name: string;
  depot: string;
  required_capacity: number;
  required_characteristics: string[];
  assigned_vehicle_id: number | null;
  is_active: boolean;
  synthetic: boolean;
}

export interface EvidenceCheck {
  criterion_type: string;
  criterion_label: string;
  source_page: number | null;
  required_value: string;
  observed_value: string;
  result: "SATISFIED" | "FAILED" | "UNKNOWN" | "CONFLICT";
}

export interface Assessment {
  id: number;
  vehicle_id: number;
  classification: Classification;
  classification_explanation: string;
  reasons: string[];
  evidence_detail: EvidenceCheck[];
  vin_evidence_status: string;
  vin_evidence_note: string;
  assessed_at: string;
  vehicle?: Vehicle | null;
}

export interface FleetAssessmentSummary {
  incident_id: number;
  assessed_at: string | null;
  total_vehicles: number;
  counts: Record<Classification, number>;
  vin_status_counts: Record<string, number>;
  assessments: Assessment[];
  banner: string;
}

export interface PolicySelection {
  id: number;
  policy: OperationalPolicy;
  policy_explanation: string;
  selected_by: string;
  selected_at: string;
  superseded: boolean;
  acknowledgement_text: string;
}

export interface Assignment {
  id: number;
  route_id: number;
  route: Route | null;
  original_vehicle_id: number | null;
  original_vehicle: Vehicle | null;
  replacement_vehicle_id: number | null;
  replacement_vehicle: Vehicle | null;
  disruption_reason: string;
  capacity_required: number;
  capacity_offered: number | null;
  capacity_ok: boolean | null;
  depot_ok: boolean | null;
  characteristics_ok: boolean | null;
  feasible: boolean;
  unresolved_issues: string[];
  status: "PROPOSED" | "APPLIED" | "NOT_APPLICABLE";
}

export interface Proposal {
  id: number;
  proposal_uid: string;
  incident_id: number;
  policy: OperationalPolicy;
  policy_explanation: string;
  status: "PENDING" | "APPROVED" | "REJECTED" | "SUPERSEDED";
  plan_hash: string;
  summary: {
    routes_disrupted?: number;
    routes_flagged_review_only?: number;
    routes_recoverable?: number;
    routes_not_recoverable?: number;
    spares_considered?: number;
  };
  generated_at: string;
  decided_at: string | null;
  decided_by: string | null;
  assignments: Assignment[];
}

export interface Incident {
  id: number;
  campaign_number: string;
  status: string;
  evidence_source: string;
  created_at: string;
  updated_at: string;
  campaign: Campaign | null;
  active_report_version: ReportVersion | null;
  active_policy: PolicySelection | null;
  latest_proposal: Proposal | null;
}

export interface AuditEvent {
  id: number;
  event_uid: string;
  timestamp: string;
  actor: string;
  incident_id: number | null;
  action: string;
  entity_type: string;
  entity_id: string;
  previous_state: Record<string, unknown> | null;
  new_state: Record<string, unknown> | null;
  policy: string | null;
  proposal_id: string | null;
  result: string;
  detail: string;
}

export interface BenchmarkRun {
  id: number | null;
  run_at: string;
  engine_version: string;
  campaign_number: string;
  sample_count: number;
  duration_ms: number;
  metrics: Record<string, number | string | null>;
  confusion_matrix: Record<string, Record<string, number>>;
  cases: Array<Record<string, unknown>>;
  label: string;
}

export interface ApprovalResponse {
  approval: {
    approval_id: number;
    proposal_uid: string;
    policy: OperationalPolicy;
    applied: Array<Record<string, unknown>>;
    previous_state: Record<string, unknown>;
    new_state: Record<string, unknown>;
  };
  proposal: Proposal;
  incident: Incident;
  audit_events: AuditEvent[];
}

export interface Health {
  status: string;
  app: string;
  version: string;
  environment: string;
  database: string;
  utc_time: string;
  llm_configured: boolean;
}

export interface VinDecodeResult {
  vin: string;
  requested_model_year: string;
  decoded_values: Record<string, string | number>;
  unavailable_fields: string[];
  decoded_at: string;
  source: string;
  requested_by: string;
  cached: boolean;
  limitations: string;
}

// ---------------------------------------------------------------------------
// Incident Response Agent (bounded, read-only investigation)
// ---------------------------------------------------------------------------

export interface InvestigationStep {
  id: number;
  seq: number;
  kind: "MODEL_MESSAGE" | "TOOL_CALL" | "VALIDATION" | "FINAL" | "ERROR";
  tool_name: string;
  tool_args: Record<string, unknown> | null;
  status: string;
  explanation: string;
  result: Record<string, unknown> | null;
  duration_ms: number;
  created_at: string;
}

export interface Investigation {
  id: number;
  session_uid: string;
  incident_id: number;
  objective: string;
  requested_by: string;
  status: "INVESTIGATING" | "COMPLETED" | "FAILED";
  stop_reason: string;
  model: string;
  iterations_used: number;
  tool_calls_count: number;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  provider_error: string;
  final_result: AgentFinalResult | null;
  grounding_violations: string[];
  steps: InvestigationStep[];
}

export interface AgentFinalResult {
  incident_summary: string;
  objective_interpretation: string;
  facts: Array<{ statement: string; evidence_refs: string[] }>;
  inferences: Array<{ statement: string; basis: string }>;
  vehicle_classifications: Array<{
    asset_id: string;
    classification: Classification;
    basis_ref: string;
  }>;
  affected_routes: string[];
  recovery_candidates: Array<{
    route_id: string;
    spare_asset_id: string | null;
    feasibility_note: string;
  }>;
  unresolved_constraints: string[];
  unknowns_and_missing_evidence: string[];
  proposed_next_actions: Array<{
    action: string;
    rationale: string;
    requires_human_approval: boolean;
  }>;
  uncertainty_notes: string;
  evidence_references: Array<{
    ref_id: string;
    description: string;
    source: string;
  }>;
  human_approval_required: boolean;
}

export interface AgentStatus {
  configured: boolean;
  model: string;
  base_url_host: string;
  max_iterations: number;
  tools: string[];
  write_tools_exposed: false;
  note: string;
}

export interface AgentProbe {
  configured: boolean;
  tool_calling_supported: boolean | null;
  model: string;
  detail: string;
  duration_ms: number;
}
