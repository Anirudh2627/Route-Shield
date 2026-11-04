/* Thin API client. The token lives in sessionStorage for demo convenience;
   ALL authoritative application state lives in the backend database. */

import type {
  ApprovalResponse,
  AuditEvent,
  BenchmarkRun,
  Evidence,
  FleetAssessmentSummary,
  Health,
  Incident,
  PolicySelection,
  Proposal,
  Route,
  Vehicle,
  VinDecodeResult,
} from "./types";

const TOKEN_KEY = "routeshield.token";
const USER_KEY = "routeshield.user";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message?: string) {
    super(message || `API error ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}
export function getUser(): { username: string; display_name: string; role: string } | null {
  const raw = sessionStorage.getItem(USER_KEY);
  return raw ? JSON.parse(raw) : null;
}
export function setSession(token: string, user: { username: string; display_name: string; role: string }) {
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(USER_KEY, JSON.stringify(user));
}
export function clearSession() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch(`/api${path}`, { ...options, headers });
  if (!resp.ok) {
    let detail: unknown = resp.statusText;
    try {
      detail = await resp.json();
    } catch {
      /* keep statusText */
    }
    if (resp.status === 401 && !path.startsWith("/auth/login")) {
      window.dispatchEvent(new Event("routeshield:unauthorized"));
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const api = {
  health: () => request<Health>("/health"),
  login: (username: string, password: string) =>
    request<{ token: string; username: string; display_name: string; role: string }>(
      "/auth/login",
      { method: "POST", body: JSON.stringify({ username, password }) },
    ),

  incidents: () => request<Incident[]>("/incidents"),
  incident: (id: number) => request<Incident>(`/incidents/${id}`),
  refreshRecall: (id: number) =>
    request<{ incident_id: number; evidence: Evidence; message: string }>(
      `/incidents/${id}/refresh-recall`,
      { method: "POST" },
    ),
  evidence: (id: number) => request<Evidence>(`/incidents/${id}/evidence`),
  assessFleet: (id: number) =>
    request<FleetAssessmentSummary>(`/incidents/${id}/assess-fleet`, { method: "POST" }),
  fleetAssessment: (id: number) =>
    request<FleetAssessmentSummary>(`/incidents/${id}/fleet-assessment`),
  setPolicy: (id: number, policy: string, acknowledgement: boolean) =>
    request<PolicySelection>(`/incidents/${id}/set-operational-policy`, {
      method: "POST",
      body: JSON.stringify({ policy, acknowledgement }),
    }),
  generatePlan: (id: number) =>
    request<Proposal>(`/incidents/${id}/generate-recovery-plan`, { method: "POST" }),
  recoveryPlan: (id: number) => request<Proposal>(`/incidents/${id}/recovery-plan`),
  approvePlan: (id: number, proposal_uid: string, plan_hash: string, policy: string) =>
    request<ApprovalResponse>(`/incidents/${id}/approve-plan`, {
      method: "POST",
      body: JSON.stringify({ proposal_uid, plan_hash, policy }),
    }),
  audit: (id: number) => request<AuditEvent[]>(`/incidents/${id}/audit`),

  vehicles: () => request<Vehicle[]>("/fleet/vehicles"),
  routes: () => request<Route[]>("/fleet/routes"),

  benchmarkLatest: () => request<BenchmarkRun>("/benchmarks/latest"),
  benchmarkRun: () => request<BenchmarkRun>("/benchmarks/run", { method: "POST" }),

  decodeVin: (vin: string, model_year?: string) =>
    request<VinDecodeResult>("/vpic/decode-single-vin", {
      method: "POST",
      body: JSON.stringify({ vin, model_year: model_year || null }),
    }),
  recallCheckVin: (vin: string) =>
    request<{ vin: string; results: unknown[]; note: string }>(
      "/vpic/recall-check-single-vin",
      { method: "POST", body: JSON.stringify({ vin, model_year: null }) },
    ),
};

export function errorDetail(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { detail?: { reason?: string } | string } | undefined;
    if (typeof d === "object" && d && typeof d.detail === "object" && d.detail) {
      return String((d.detail as { reason?: string }).reason ?? JSON.stringify(d.detail));
    }
    if (typeof d === "object" && d && typeof d.detail === "string") return d.detail;
    if (Array.isArray(d?.detail)) {
      return (d!.detail as Array<{ msg?: string }>).map((x) => x.msg).join("; ");
    }
    return String(d ?? err.message);
  }
  return err instanceof Error ? err.message : String(err);
}
