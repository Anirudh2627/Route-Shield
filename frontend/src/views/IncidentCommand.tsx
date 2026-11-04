import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, errorDetail, getToken } from "../api";
import type {
  ApprovalResponse,
  AuditEvent,
  BenchmarkRun,
  Evidence,
  FleetAssessmentSummary,
  Incident,
  OperationalPolicy,
  Proposal,
} from "../types";
import { Banner, Card, ClassificationBadge, Check, KV, Spinner, StatTile, StatusChip, fmtTime, shortHash } from "../components/ui";

const POLICY_INFO: Record<OperationalPolicy, { title: string; body: string; tone: string }> = {
  HOLD_PENDING_VERIFICATION: {
    title: "Option A — Hold pending verification",
    body:
      "Vehicles classified VERIFICATION_REQUIRED are excluded from the proposed dispatch allocation until further verification. This is a precautionary hold; it does not establish that the vehicle is affected.",
    tone: "border-indigo-300 bg-indigo-50/60",
  },
  MANAGER_REVIEW_ONLY: {
    title: "Option B — Manager review only",
    body:
      "Vehicles classified VERIFICATION_REQUIRED are flagged for your review WITHOUT automatically removing their existing assignment. Selecting this option does NOT establish that the vehicle is safe or free of recall exposure.",
    tone: "border-amber-300 bg-amber-50/60",
  },
};

export default function IncidentCommand({
  incidents,
  incident,
  authed,
  onIncidentChanged,
  onRequireAuth,
}: {
  incidents: Incident[];
  incident: Incident | null;
  authed: boolean;
  onIncidentChanged: () => void;
  onRequireAuth: () => void;
}) {
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [assessment, setAssessment] = useState<FleetAssessmentSummary | null>(null);
  const [plan, setPlan] = useState<Proposal | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [newAuditIds, setNewAuditIds] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [policy, setPolicy] = useState<OperationalPolicy>("HOLD_PENDING_VERIFICATION");
  const [ack, setAck] = useState(false);
  const [approval, setApproval] = useState<ApprovalResponse | null>(null);
  const approvalRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    if (!incident) return;
    setError("");
    try {
      const [ev, fa, aud, inc] = await Promise.all([
        api.evidence(incident.id),
        api.fleetAssessment(incident.id),
        api.audit(incident.id),
        api.incident(incident.id),
      ]);
      setEvidence(ev);
      setAssessment(fa);
      setAudit(aud);
      setPlan(inc.latest_proposal);
      if (inc.active_policy) setPolicy(inc.active_policy.policy);
    } catch (err) {
      setError(errorDetail(err));
    }
  }, [incident]);

  useEffect(() => {
    setApproval(null);
    load();
  }, [load]);

  async function act(label: string, fn: () => Promise<void>) {
    setBusy(label);
    setError("");
    setNotice("");
    try {
      await fn();
    } catch (err) {
      if ((err as { status?: number }).status === 401) {
        onRequireAuth();
      } else {
        setError(`${label} failed: ${errorDetail(err)}`);
      }
    } finally {
      setBusy("");
    }
  }

  const doRefresh = () =>
    act("Refresh", async () => {
      if (!incident) return;
      const res = await api.refreshRecall(incident.id);
      setNotice(res.message);
      await load();
      onIncidentChanged();
    });

  const doAssess = () =>
    act("Assessment", async () => {
      if (!incident) return;
      const fa = await api.assessFleet(incident.id);
      setAssessment(fa);
      setNotice(`Deterministic assessment complete: ${fa.total_vehicles} synthetic vehicles classified.`);
      await load();
    });

  const doSetPolicy = () =>
    act("Policy selection", async () => {
      if (!incident) return;
      await api.setPolicy(incident.id, policy, true);
      setNotice(`Operational policy recorded: ${policy}. The recovery plan will apply it exactly as selected.`);
      setPlan(null); // previous plan (if any) no longer reflects the new policy
      await load();
      onIncidentChanged();
    });

  const doGenerate = () =>
    act("Plan generation", async () => {
      if (!incident) return;
      const p = await api.generatePlan(incident.id);
      setPlan(p);
      setApproval(null);
      setNotice(
        `Recovery proposal ${p.proposal_uid} generated deterministically. No operational state has changed — manager approval required.`,
      );
      await load();
      onIncidentChanged();
    });

  const doApprove = () =>
    act("Approval", async () => {
      if (!incident || !plan) return;
      const res = await api.approvePlan(incident.id, plan.proposal_uid, plan.plan_hash, plan.policy);
      setApproval(res);
      setPlan(res.proposal);
      const freshIds = new Set(res.audit_events.slice(0, 12).map((e) => e.id));
      setAudit(res.audit_events);
      setNewAuditIds(freshIds);
      setTimeout(() => setNewAuditIds(new Set()), 2600);
      await load();
      onIncidentChanged();
      setTimeout(() => approvalRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }), 120);
    });

  const canWrite = authed && Boolean(getToken());
  const activePolicySel = incident?.active_policy ?? null;
  const planPending = plan?.status === "PENDING";
  const planApproved = plan?.status === "APPROVED" || approval !== null;

  const routeImpacts = useMemo(() => (plan ? plan.assignments : []), [plan]);

  if (!incident) {
    return (
      <Card title="Incident Command">
        <p className="text-ink-500">
          {incidents.length === 0 ? <Spinner label="Loading incidents…" /> : "No incident selected."}
        </p>
      </Card>
    );
  }

  const campaign = evidence?.campaign ?? incident.campaign;
  const version = evidence?.report_version ?? incident.active_report_version;

  return (
    <div className="space-y-5">
      {/* ---------------- A. Incident header ---------------- */}
      <div className="card px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-xl font-bold tracking-tight text-ink-950">
                NHTSA Campaign {incident.campaign_number}
              </h1>
              <StatusChip tone={incident.status === "PLAN_APPROVED" ? "green" : incident.status === "PLAN_PROPOSED" ? "amber" : "indigo"}>
                {incident.status.replaceAll("_", " ")}
              </StatusChip>
            </div>
            <p className="mt-1 text-[13px] text-ink-600">
              {campaign?.manufacturer || "…"} · {campaign?.make} {campaign?.model} ·{" "}
              MY {campaign?.model_years?.join(", ")} · {campaign?.potential_units_affected?.toLocaleString()} units potentially involved
              {campaign?.manufacturer_recall_number && <> · Mfr recall no. {campaign.manufacturer_recall_number}</>}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button className="btn-secondary" onClick={doRefresh} disabled={Boolean(busy)}>
              {busy === "Refresh" ? <Spinner label="Retrieving from NHTSA…" /> : "Refresh from NHTSA"}
            </button>
            <button className="btn-primary" onClick={doAssess} disabled={Boolean(busy)}>
              {busy === "Assessment" ? <Spinner label="Assessing…" /> : "Run fleet assessment"}
            </button>
          </div>
        </div>
        <div className="mt-3 grid gap-2 md:grid-cols-4 text-[12px]">
          <KV k="Report version" v={<span className="font-mono">{version ? shortHash(version.document_sha256, 16) : "—"}</span>} />
          <KV k="Retrieved" v={fmtTime(version?.retrieval_timestamp)} />
          <KV k="Review status" v={
            version?.review_status === "REVIEWED"
              ? <StatusChip tone="green">REVIEWED</StatusChip>
              : <StatusChip tone="amber">{version?.review_status ?? "UNKNOWN"}</StatusChip>
          } />
          <KV k="Provenance" v={<span className="font-mono text-[11px]">{version?.provenance ?? "—"}</span>} />
        </div>
      </div>

      {notice && <Banner tone="info">{notice}</Banner>}
      {error && <Banner tone="danger"><strong>Action failed.</strong> {error}</Banner>}

      {/* Evidence provenance banner - always explicit */}
      {evidence && (
        <Banner tone={evidence.using_reviewed_fixture ? "warn" : version?.review_status === "PENDING_REVIEW" ? "danger" : "success"}>
          <strong className="font-bold">
            {evidence.using_reviewed_fixture ? "Using manually reviewed campaign evidence. " : ""}
          </strong>
          {evidence.evidence_banner}
          {version && (
            <>
              {" "}
              <a className="underline font-medium" href={version.report_url} target="_blank" rel="noreferrer">
                Open the official Part 573 report (PDF)
              </a>
            </>
          )}
        </Banner>
      )}

      <div className="grid gap-5 xl:grid-cols-3">
        <div className="xl:col-span-2 space-y-5">
          {/* ---------------- B. Recall evidence ---------------- */}
          <Card
            title="Recall evidence"
            subtitle={campaign?.component ? `Component: ${campaign.component}` : undefined}
          >
            {campaign && (
              <div className="space-y-3">
                <div>
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-ink-500 mb-1">Defect summary</div>
                  <p className="text-[13px] text-ink-800">{campaign.summary}</p>
                </div>
                <div>
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-rose-600 mb-1">Safety consequence</div>
                  <p className="text-[13px] text-ink-800">{campaign.consequence}</p>
                </div>
                <div>
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-emerald-700 mb-1">Remedy</div>
                  <p className="text-[13px] text-ink-800">{campaign.remedy}</p>
                </div>
              </div>
            )}
            {evidence && (
              <div className="mt-4">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-ink-500 mb-2">
                  Extracted applicability criteria ({evidence.criteria.filter((c) => !["DEFECT_DESCRIPTION", "SAFETY_CONSEQUENCE", "REMEDY", "REMEDY_TYPE", "COMPONENT"].includes(c.criterion_type)).length})
                </div>
                <div className="overflow-x-auto rounded-lg border border-slate-200">
                  <table className="w-full text-[12.5px]">
                    <thead>
                      <tr>
                        <th className="table-th">Criterion</th>
                        <th className="table-th">Value</th>
                        <th className="table-th">Source</th>
                        <th className="table-th">Method</th>
                        <th className="table-th">Review</th>
                      </tr>
                    </thead>
                    <tbody>
                      {evidence.criteria
                        .filter((c) => !["DEFECT_DESCRIPTION", "SAFETY_CONSEQUENCE", "REMEDY", "REMEDY_TYPE", "COMPONENT", "MANUFACTURER"].includes(c.criterion_type))
                        .map((c) => (
                          <tr key={c.id} className="hover:bg-slate-50/60">
                            <td className="table-td font-medium text-ink-800 whitespace-nowrap">{c.label}</td>
                            <td className="table-td font-mono text-[11.5px] text-ink-700">{criterionValue(c.value)}</td>
                            <td className="table-td whitespace-nowrap">
                              {c.source_page ? <span className="font-mono">p.{c.source_page}</span> : "—"}
                              <span className="block text-[11px] text-ink-400 max-w-[240px] truncate" title={c.source_snippet}>
                                “{c.source_snippet}”
                              </span>
                            </td>
                            <td className="table-td whitespace-nowrap">
                              <span className="font-mono text-[11px]">{c.extraction_method}</span>
                              <span className="block text-[11px] text-ink-400">conf {(c.confidence * 100).toFixed(0)}%</span>
                            </td>
                            <td className="table-td">
                              <StatusChip tone={c.review_status === "REVIEWED" ? "green" : c.review_status === "NEEDS_REVIEW" ? "amber" : "slate"}>
                                {c.review_status.replaceAll("_", " ")}
                              </StatusChip>
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
                <p className="mt-2 text-[11px] text-ink-400">
                  Every extracted field preserves its source document, page number, verbatim snippet, extraction
                  method, confidence and review status. Only REVIEWED criteria back the deterministic matching.
                </p>
              </div>
            )}
          </Card>

          {/* ---------------- C. Fleet assessment ---------------- */}
          <Card
            title="Fleet assessment"
            subtitle={
              <>
                Deterministic Python matching against reviewed criteria — the LLM never classifies vehicles.{" "}
                {assessment?.assessed_at && <>Last run {fmtTime(assessment.assessed_at)}.</>}
              </>
            }
          >
            {assessment ? (
              <>
                <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
                  <StatTile label="Vehicles assessed" value={assessment.total_vehicles} hint="synthetic demo fleet" />
                  <StatTile label="Criteria match" value={assessment.counts.CAMPAIGN_CRITERIA_MATCH ?? 0} tone="red" hint="not VIN-confirmed" />
                  <StatTile label="Verification req." value={assessment.counts.VERIFICATION_REQUIRED ?? 0} tone="amber" hint="system abstains" />
                  <StatTile label="Outside report" value={assessment.counts.OUTSIDE_REPORT_VERSION ?? 0} tone="slate" hint="≠ safe" />
                  <StatTile
                    label="VIN verified"
                    value={assessment.vin_status_counts.VIN_RECALL_CHECK_DONE ?? 0}
                    tone="green"
                    hint={`${assessment.vin_status_counts.VIN_STATUS_UNAVAILABLE ?? 0} unavailable`}
                  />
                </div>
                <Banner tone="neutral">
                  <span className="font-semibold">{assessment.banner}.</span>{" "}
                  CAMPAIGN_CRITERIA_MATCH never implies VIN-level confirmation · OUTSIDE_REPORT_VERSION never implies SAFE ·
                  all {assessment.vin_status_counts.VIN_STATUS_UNAVAILABLE ?? 0} synthetic records honestly report VIN_STATUS_UNAVAILABLE.
                </Banner>
                {/* ---------------- D. Vehicle table ---------------- */}
                <div className="mt-4 overflow-x-auto rounded-lg border border-slate-200">
                  <table className="w-full text-[12.5px]">
                    <thead>
                      <tr>
                        <th className="table-th">Asset ID</th>
                        <th className="table-th">MY</th>
                        <th className="table-th">Production date</th>
                        <th className="table-th">Brake config</th>
                        <th className="table-th">Classification</th>
                        <th className="table-th">Explanation</th>
                        <th className="table-th">VIN evidence</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...assessment.assessments]
                        .sort((a, b) => rank(a.classification) - rank(b.classification))
                        .map((a) => (
                          <VehicleRow key={a.id} a={a} />
                        ))}
                    </tbody>
                  </table>
                </div>
              </>
            ) : (
              <Spinner label="Loading assessment…" />
            )}
          </Card>

          {/* ---------------- E. Route impact ---------------- */}
          <Card
            title="Route impact & recovery proposal"
            subtitle={
              plan
                ? `${plan.proposal_uid} · policy ${plan.policy.replaceAll("_", " ")} · generated ${fmtTime(plan.generated_at)}`
                : "No proposal generated yet. Select the operational policy, then generate the deterministic recovery plan."
            }
            right={plan && <StatusChip tone={plan.status === "APPROVED" ? "green" : plan.status === "PENDING" ? "amber" : "slate"}>{plan.status}</StatusChip>}
          >
            {routeImpacts.length === 0 ? (
              <p className="text-[13px] text-ink-500">
                No impacted routes have been computed yet. The engine inspects route assignments of
                criteria-match and verification-required vehicles under the policy you select.
              </p>
            ) : (
              <div className="overflow-x-auto rounded-lg border border-slate-200">
                <table className="w-full text-[12.5px]">
                  <thead>
                    <tr>
                      <th className="table-th">Route</th>
                      <th className="table-th">Original vehicle</th>
                      <th className="table-th">Reason</th>
                      <th className="table-th">Proposed replacement</th>
                      <th className="table-th">Capacity</th>
                      <th className="table-th">Checks</th>
                      <th className="table-th">Feasible</th>
                    </tr>
                  </thead>
                  <tbody>
                    {routeImpacts.map((a) => (
                      <tr key={a.id} className={a.status === "NOT_APPLICABLE" ? "bg-amber-50/40" : ""}>
                        <td className="table-td whitespace-nowrap">
                          <span className="font-semibold text-ink-800">{a.route?.route_id}</span>
                          <span className="block text-[11px] text-ink-400">{a.route?.name} · {a.route?.depot}</span>
                        </td>
                        <td className="table-td font-mono text-[11.5px] whitespace-nowrap">{a.original_vehicle?.asset_id}</td>
                        <td className="table-td max-w-[320px] text-[12px] text-ink-600">{a.disruption_reason}</td>
                        <td className="table-td font-mono text-[11.5px] whitespace-nowrap">
                          {a.replacement_vehicle ? (
                            <>
                              {a.replacement_vehicle.asset_id}
                              <span className="block font-sans text-[11px] text-ink-400">
                                {a.replacement_vehicle.passenger_capacity} seats · {a.replacement_vehicle.depot}
                              </span>
                            </>
                          ) : a.status === "NOT_APPLICABLE" ? (
                            <span className="font-sans text-[11.5px] text-amber-700">assignment retained · flagged</span>
                          ) : (
                            <span className="font-sans text-[11.5px] text-rose-600">none found</span>
                          )}
                        </td>
                        <td className="table-td whitespace-nowrap text-[12px]">
                          {a.capacity_offered != null ? (
                            <>
                              {a.capacity_offered} / {a.capacity_required} <Check ok={a.capacity_ok} />
                            </>
                          ) : (
                            <>req {a.capacity_required}</>
                          )}
                        </td>
                        <td className="table-td whitespace-nowrap">
                          {a.replacement_vehicle ? (
                            <span className="flex gap-2 text-[11px]">
                              <Check ok={a.depot_ok} /> depot
                              <Check ok={a.characteristics_ok} /> equip
                            </span>
                          ) : a.unresolved_issues.length > 0 ? (
                            <span className="text-[11px] text-rose-600">{a.unresolved_issues[0]}</span>
                          ) : "—"}
                        </td>
                        <td className="table-td">
                          {a.status === "NOT_APPLICABLE" ? (
                            <StatusChip tone="amber">FLAGGED</StatusChip>
                          ) : a.feasible ? (
                            <StatusChip tone={a.status === "APPLIED" ? "green" : "blue"}>{a.status === "APPLIED" ? "APPLIED" : "FEASIBLE"}</StatusChip>
                          ) : (
                            <StatusChip tone="red">NOT RECOVERABLE</StatusChip>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {plan && plan.status === "PENDING" && (
              <p className="mt-3 text-[11.5px] text-ink-500">
                This is a <strong>proposal</strong>. No route has been recovered and no operational state has
                changed — nothing is applied until a manager approves below.
              </p>
            )}
          </Card>
        </div>

        {/* ---------------- F. Manager decision panel ---------------- */}
        <div className="space-y-5">
          <Card title="Manager decision" subtitle="Operational policy for VERIFICATION_REQUIRED vehicles — never chosen automatically by the system.">
            {!canWrite ? (
              <Banner tone="warn">
                Manager sign-in required to select a policy, generate a plan or approve changes.{" "}
                <button className="underline font-semibold" onClick={onRequireAuth}>Sign in</button>
              </Banner>
            ) : (
              <div className="space-y-3">
                {(Object.keys(POLICY_INFO) as OperationalPolicy[]).map((p) => (
                  <label
                    key={p}
                    className={`block cursor-pointer rounded-lg border-2 px-3.5 py-3 transition-colors ${
                      policy === p ? POLICY_INFO[p].tone + " ring-1 ring-offset-1" : "border-slate-200 bg-white hover:border-slate-300"
                    }`}
                  >
                    <div className="flex items-center gap-2.5">
                      <input
                        type="radio"
                        name="policy"
                        className="h-4 w-4 accent-indigo-600"
                        checked={policy === p}
                        onChange={() => setPolicy(p)}
                        disabled={Boolean(busy)}
                      />
                      <span className="text-[13px] font-bold text-ink-900">{POLICY_INFO[p].title}</span>
                    </div>
                    <p className="mt-1.5 pl-7 text-[12px] leading-snug text-ink-600">{POLICY_INFO[p].body}</p>
                  </label>
                ))}

                <label className="flex items-start gap-2.5 rounded-lg bg-slate-50 border border-slate-200 px-3.5 py-3 cursor-pointer">
                  <input type="checkbox" className="mt-0.5 h-4 w-4 accent-indigo-600" checked={ack} onChange={(e) => setAck(e.target.checked)} />
                  <span className="text-[12px] text-ink-700">
                    I understand that selecting <strong>MANAGER_REVIEW_ONLY</strong> does not establish that any
                    vehicle is safe or free of recall exposure, and that no decision is applied until I explicitly
                    approve a generated plan.
                  </span>
                </label>

                <div className="flex gap-2">
                  <button className="btn-secondary flex-1" disabled={!ack || Boolean(busy)} onClick={doSetPolicy}>
                    {busy === "Policy selection" ? <Spinner /> : "Record policy selection"}
                  </button>
                  <button className="btn-primary flex-1" disabled={!ack || Boolean(busy) || !activePolicySel} onClick={doGenerate}>
                    {busy === "Plan generation" ? <Spinner /> : "Generate recovery plan"}
                  </button>
                </div>
                {activePolicySel ? (
                  <p className="text-[11.5px] text-ink-500">
                    Active policy: <strong>{activePolicySel.policy.replaceAll("_", " ")}</strong> — recorded by{" "}
                    {activePolicySel.selected_by} at {fmtTime(activePolicySel.selected_at)}.
                  </p>
                ) : (
                  <p className="text-[11.5px] text-ink-500">No policy recorded yet. Plan generation requires your explicit selection.</p>
                )}

                {/* Approval summary */}
                {plan && (
                  <div className="mt-2 rounded-lg border border-slate-200 bg-slate-50/70 p-3.5">
                    <div className="text-[11px] font-semibold uppercase tracking-wider text-ink-500 mb-2">Approval summary</div>
                    <dl className="text-[12.5px] space-y-1">
                      <div className="flex justify-between gap-2"><dt className="text-ink-500">Proposal</dt><dd className="font-mono">{plan.proposal_uid}</dd></div>
                      <div className="flex justify-between gap-2"><dt className="text-ink-500">Plan hash</dt><dd className="font-mono text-[11px]">{shortHash(plan.plan_hash, 16)}</dd></div>
                      <div className="flex justify-between gap-2"><dt className="text-ink-500">Policy</dt><dd className="font-semibold">{plan.policy.replaceAll("_", " ")}</dd></div>
                      <div className="flex justify-between gap-2"><dt className="text-ink-500">Routes disrupted</dt><dd>{plan.summary.routes_disrupted ?? 0}</dd></div>
                      <div className="flex justify-between gap-2"><dt className="text-ink-500">Recoverable</dt><dd className="text-emerald-700 font-semibold">{plan.summary.routes_recoverable ?? 0}</dd></div>
                      <div className="flex justify-between gap-2"><dt className="text-ink-500">Not recoverable</dt><dd className="text-rose-600 font-semibold">{plan.summary.routes_not_recoverable ?? 0}</dd></div>
                      <div className="flex justify-between gap-2"><dt className="text-ink-500">Flagged (review only)</dt><dd>{plan.summary.routes_flagged_review_only ?? 0}</dd></div>
                    </dl>
                    {plan.status === "PENDING" && (
                      <button className="btn-approve w-full mt-3 text-[15px] py-2.5" disabled={Boolean(busy)} onClick={doApprove}>
                        {busy === "Approval" ? <Spinner label="Committing transaction…" /> : "APPROVE RECOVERY PLAN"}
                      </button>
                    )}
                    {plan.status !== "PENDING" && (
                      <div className="mt-3 text-center">
                        <StatusChip tone={plan.status === "APPROVED" ? "green" : "slate"}>
                          {plan.status} {plan.decided_by && `by ${plan.decided_by}`}
                        </StatusChip>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </Card>

          {/* Approval success transition */}
          {approval && (
            <div ref={approvalRef} className="approve-flash card border-emerald-300 bg-emerald-50/60 p-5">
              <div className="flex items-center gap-2.5">
                <svg className="h-6 w-6 text-emerald-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="12" cy="12" r="10" /><path d="M8 12.5l2.5 2.5L16 9.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                <h3 className="text-[15px] font-bold text-emerald-900">Recovery plan approved & applied</h3>
              </div>
              <p className="mt-2 text-[12.5px] text-emerald-900/80">
                The backend validated the proposal, re-checked spare availability, applied {approval.approval.applied.length}{" "}
                operational change(s) in a single database transaction and wrote {approval.audit_events.filter((e) => e.result === "APPLIED" || e.result === "APPROVED").length} audit event(s).
                The LLM was not involved in executing this write.
              </p>
              <ul className="mt-2.5 space-y-1 text-[12px] text-emerald-950">
                {approval.approval.applied.map((c, i) => (
                  <li key={i} className="font-mono bg-white/70 rounded px-2 py-1 border border-emerald-200">
                    {String(c.route_id)}: {String(c.action).replaceAll("_", " ").toLowerCase()}
                    {c.replacement_vehicle ? ` → ${String(c.replacement_vehicle)}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* ---------------- Benchmark strip ---------------- */}
          <BenchmarkStrip />

          {/* ---------------- G. Recent audit history ---------------- */}
          <Card title="Recent audit history" subtitle="Append-only. No endpoint can modify or delete these records.">
            <div className="space-y-2 max-h-[420px] overflow-y-auto pr-1">
              {audit.length === 0 && <p className="text-[13px] text-ink-400">No audit events yet.</p>}
              {audit.slice(0, 25).map((e) => (
                <div
                  key={e.id}
                  className={`rounded-lg border px-3 py-2 ${newAuditIds.has(e.id) ? "audit-in border-emerald-300" : "border-slate-200 bg-white"}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[11px] font-bold text-ink-800">{e.action.replaceAll("_", " ")}</span>
                    <span className="text-[10.5px] text-ink-400 tabular-nums">{fmtTime(e.timestamp)}</span>
                  </div>
                  <div className="mt-0.5 text-[11.5px] text-ink-500">
                    {e.actor} · {e.entity_type} {e.entity_id} ·{" "}
                    <StatusChip tone={e.result === "APPROVED" || e.result === "APPLIED" || e.result === "OK" ? "green" : e.result === "REJECTED" ? "red" : "slate"}>
                      {e.result}
                    </StatusChip>
                  </div>
                  {e.detail && <p className="mt-1 text-[11.5px] text-ink-600 leading-snug">{e.detail}</p>}
                  {newAuditIds.has(e.id) && (
                    <div className="mt-1 text-[10.5px] font-bold uppercase tracking-wider text-emerald-600">new</div>
                  )}
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

function rank(c: string): number {
  return c === "CAMPAIGN_CRITERIA_MATCH" ? 0 : c === "VERIFICATION_REQUIRED" ? 1 : 2;
}

function BenchmarkStrip() {
  const [bench, setBench] = useState<BenchmarkRun | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.benchmarkLatest().then(setBench).catch(() => setBench(null));
  }, []);

  async function run() {
    setBusy(true);
    try {
      setBench(await api.benchmarkRun());
    } finally {
      setBusy(false);
    }
  }

  const m = bench?.metrics as Record<string, number | string> | undefined;
  const pctv = (v: unknown) => (typeof v === "number" ? `${(v * 100).toFixed(1)}%` : "—");

  return (
    <Card
      title="Classification benchmark"
      subtitle="Measured at runtime on synthetic ground-truth cases — never hardcoded."
      right={
        <button className="btn-secondary text-xs" onClick={run} disabled={busy}>
          {busy ? <Spinner /> : bench ? "Re-run" : "Run benchmark"}
        </button>
      }
    >
      {bench ? (
        <div className="space-y-2.5">
          <div className="text-[11px] font-bold uppercase tracking-wider text-amber-700">{bench.label}</div>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2.5 text-center">
            <div className="rounded-md border border-slate-200 px-2 py-2">
              <div className="text-[10.5px] text-ink-500 font-semibold">SAMPLES</div>
              <div className="text-lg font-bold tabular-nums">{bench.sample_count}</div>
            </div>
            <div className="rounded-md border border-slate-200 px-2 py-2">
              <div className="text-[10.5px] text-ink-500 font-semibold">MATCH ACC.</div>
              <div className="text-lg font-bold tabular-nums text-emerald-600">{pctv(m?.correct_match_rate)}</div>
            </div>
            <div className="rounded-md border border-slate-200 px-2 py-2">
              <div className="text-[10.5px] text-ink-500 font-semibold">ABSTENTION</div>
              <div className="text-lg font-bold tabular-nums text-emerald-600">{pctv(m?.correct_abstention_rate)}</div>
            </div>
            <div className="rounded-md border border-emerald-300 bg-emerald-50 px-2 py-2">
              <div className="text-[10.5px] text-emerald-700 font-semibold">FALSE-CONFIRM</div>
              <div className="text-lg font-bold tabular-nums text-emerald-700">{pctv(m?.false_confirmation_rate)}</div>
            </div>
            <div className="rounded-md border border-slate-200 px-2 py-2">
              <div className="text-[10.5px] text-ink-500 font-semibold">LAST RUN</div>
              <div className="text-[11px] font-semibold pt-1.5 leading-tight">{fmtTime(bench.run_at)}</div>
            </div>
          </div>
        </div>
      ) : (
        <p className="text-[12.5px] text-ink-500">
          No benchmark run recorded yet. Run it here or via{" "}
          <code className="font-mono bg-slate-100 rounded px-1">python scripts/run_benchmark.py</code>. Full
          metrics and the confusion matrix live under Evidence & Audit.
        </p>
      )}
    </Card>
  );
}

function criterionValue(v: Record<string, unknown>): string {
  if (v.make_model) return String(v.make_model);
  if (v.feature_code) return `${v.feature_code}${v.feature_description ? ` (${v.feature_description})` : ""}`;
  if (v.production_start) return `MY${v.model_year_start}: ${v.production_start} → ${v.production_end}${v.units ? ` · ${v.units} units` : ""}`;
  if (v.start) return `${v.start} → ${v.end}`;
  if (v.exclusion_statement) return String(v.exclusion_statement);
  if (v.total_units) return `${Number(v.total_units).toLocaleString()} units`;
  if (v.product_type) return String(v.product_type);
  return JSON.stringify(v);
}

function VehicleRow({ a }: { a: FleetAssessmentSummary["assessments"][number] }) {
  const [open, setOpen] = useState(false);
  const v = a.vehicle;
  return (
    <>
      <tr className="hover:bg-slate-50/60 cursor-pointer" onClick={() => setOpen(!open)}>
        <td className="table-td font-mono text-[11.5px] whitespace-nowrap">
          {v?.asset_id ?? a.vehicle_id}
          <span className="block font-sans text-[10.5px] text-ink-400">{v?.role === "SPARE" ? "spare" : `${v?.depot} depot`}</span>
        </td>
        <td className="table-td tabular-nums">{v?.model_year ?? "—"}</td>
        <td className="table-td tabular-nums whitespace-nowrap">{v?.build_date ?? <span className="text-amber-600 font-semibold">missing</span>}</td>
        <td className="table-td font-mono text-[11.5px] whitespace-nowrap">
          {v?.brake_feature_code ?? <span className="text-amber-600 font-semibold">not recorded</span>}
        </td>
        <td className="table-td"><ClassificationBadge c={a.classification} /></td>
        <td className="table-td max-w-[380px] text-[12px] text-ink-600">
          {a.reasons[0]}
          {a.reasons.length > 1 && (
            <button className="ml-1 text-indigo-600 font-semibold hover:underline" onClick={(e) => { e.stopPropagation(); setOpen(!open); }}>
              {open ? "less" : `+${a.reasons.length - 1} more`}
            </button>
          )}
        </td>
        <td className="table-td whitespace-nowrap">
          <StatusChip tone={a.vin_evidence_status === "VIN_STATUS_UNAVAILABLE" ? "slate" : "blue"}>
            {a.vin_evidence_status.replaceAll("_", " ")}
          </StatusChip>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={7} className="px-3 pb-3 pt-0 border-b border-slate-100 bg-slate-50/60">
            <div className="rounded-lg border border-slate-200 bg-white p-3.5">
              <div className="text-[11px] font-semibold uppercase tracking-wider text-ink-500 mb-1.5">Full explanation</div>
              <ul className="list-disc pl-4 space-y-1 text-[12.5px] text-ink-700">
                {a.reasons.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
              <div className="text-[11px] font-semibold uppercase tracking-wider text-ink-500 mt-3 mb-1.5">
                Per-criterion evidence
              </div>
              <table className="w-full text-[12px]">
                <thead>
                  <tr>
                    <th className="table-th">Criterion</th>
                    <th className="table-th">Required (source page)</th>
                    <th className="table-th">Observed</th>
                    <th className="table-th">Result</th>
                  </tr>
                </thead>
                <tbody>
                  {a.evidence_detail.map((c, i) => (
                    <tr key={i}>
                      <td className="table-td font-medium">{c.criterion_label}</td>
                      <td className="table-td font-mono text-[11px]">{c.required_value} {c.source_page ? <span className="text-ink-400">(p.{c.source_page})</span> : ""}</td>
                      <td className="table-td font-mono text-[11px]">{c.observed_value}</td>
                      <td className="table-td">
                        <StatusChip tone={c.result === "SATISFIED" ? "green" : c.result === "FAILED" ? "red" : "amber"}>{c.result}</StatusChip>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-2 text-[11.5px] text-ink-500"><strong>VIN evidence:</strong> {a.vin_evidence_note}</p>
              <p className="mt-1 text-[11.5px] text-ink-500"><strong>Category meaning:</strong> {a.classification_explanation}</p>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
