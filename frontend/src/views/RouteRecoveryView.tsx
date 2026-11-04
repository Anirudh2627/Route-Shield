import { useCallback, useEffect, useState } from "react";
import { api, errorDetail } from "../api";
import type { Incident, Proposal, Route, Vehicle } from "../types";
import { Banner, Card, Check, Spinner, StatTile, StatusChip, fmtTime } from "../components/ui";

export default function RouteRecoveryView({
  incident,
  authed,
  onIncidentChanged,
  onRequireAuth,
}: {
  incident: Incident | null;
  authed: boolean;
  onIncidentChanged: () => void;
  onRequireAuth: () => void;
}) {
  const [routes, setRoutes] = useState<Route[]>([]);
  const [vehicles, setVehicles] = useState<Vehicle[]>([]);
  const [plan, setPlan] = useState<Proposal | null>(null);
  const [planError, setPlanError] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    if (!incident) return;
    try {
      const [rt, vh, inc] = await Promise.all([api.routes(), api.vehicles(), api.incident(incident.id)]);
      setRoutes(rt);
      setVehicles(vh);
      setPlan(inc.latest_proposal);
      setPlanError("");
    } catch (err) {
      setError(errorDetail(err));
    }
  }, [incident]);

  useEffect(() => { load(); }, [load]);

  // A 404 on the plan endpoint is fine (none generated yet).
  useEffect(() => {
    if (!incident) return;
    api.recoveryPlan(incident.id).then(setPlan).catch(() => setPlan(null));
  }, [incident]);

  const byId = new Map(vehicles.map((v) => [v.id, v]));

  async function generate() {
    if (!incident) return;
    setBusy("generate");
    setError("");
    try {
      const p = await api.generatePlan(incident.id);
      setPlan(p);
      setNotice(`Proposal ${p.proposal_uid} generated. Nothing is applied until a manager approves it.`);
      onIncidentChanged();
    } catch (err) {
      if ((err as { status?: number }).status === 401) onRequireAuth();
      else setError(errorDetail(err));
    } finally {
      setBusy("");
    }
  }

  async function approve() {
    if (!incident || !plan) return;
    setBusy("approve");
    setError("");
    try {
      const res = await api.approvePlan(incident.id, plan.proposal_uid, plan.plan_hash, plan.policy);
      setPlan(res.proposal);
      setNotice(
        `Plan approved by ${res.approval.applied.length ? "applying " + res.approval.applied.length + " change(s)" : ""} — state persisted, audit events written.`,
      );
      await load();
      onIncidentChanged();
    } catch (err) {
      if ((err as { status?: number }).status === 401) onRequireAuth();
      else setError(errorDetail(err));
    } finally {
      setBusy("");
    }
  }

  if (!incident) return <Card title="Route Recovery"><Spinner label="Loading…" /></Card>;

  return (
    <div className="space-y-5">
      <div className="card px-5 py-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-bold text-ink-950">Route Recovery</h1>
          <p className="text-[12.5px] text-ink-500">
            Deterministic impact analysis and spare allocation for campaign {incident.campaign_number}.
            The engine proposes; only your approval applies.
          </p>
        </div>
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={generate} disabled={Boolean(busy) || !authed}>
            {busy === "generate" ? <Spinner /> : "Regenerate plan"}
          </button>
          {plan?.status === "PENDING" && (
            <button className="btn-approve" onClick={approve} disabled={Boolean(busy) || !authed}>
              {busy === "approve" ? <Spinner label="Committing…" /> : "APPROVE RECOVERY PLAN"}
            </button>
          )}
        </div>
      </div>

      {!authed && (
        <Banner tone="warn">
          Manager sign-in required to generate or approve plans. <button className="underline font-semibold" onClick={onRequireAuth}>Sign in</button>
        </Banner>
      )}
      {notice && <Banner tone="success">{notice}</Banner>}
      {error && <Banner tone="danger">{error}</Banner>}

      <div className="grid gap-5 lg:grid-cols-3">
        <Card title="Route register" className="lg:col-span-2" subtitle="Live persisted assignments (synthetic demonstration data)">
          <div className="overflow-x-auto rounded-lg border border-slate-200">
            <table className="w-full text-[12.5px]">
              <thead>
                <tr>
                  <th className="table-th">Route</th>
                  <th className="table-th">Depot</th>
                  <th className="table-th">Required</th>
                  <th className="table-th">Assigned vehicle</th>
                  <th className="table-th">Availability</th>
                  <th className="table-th">Status</th>
                </tr>
              </thead>
              <tbody>
                {routes.map((r) => {
                  const v = r.assigned_vehicle_id != null ? byId.get(r.assigned_vehicle_id) : undefined;
                  return (
                    <tr key={r.id} className="hover:bg-slate-50/60">
                      <td className="table-td whitespace-nowrap">
                        <span className="font-semibold">{r.route_id}</span>
                        <span className="block text-[11px] text-ink-400">{r.name}</span>
                      </td>
                      <td className="table-td">{r.depot}</td>
                      <td className="table-td whitespace-nowrap text-[12px]">
                        {r.required_capacity} seats
                        {r.required_characteristics.length > 0 && (
                          <span className="block text-[10.5px] text-ink-400">{r.required_characteristics.join(", ")}</span>
                        )}
                      </td>
                      <td className="table-td font-mono text-[11.5px] whitespace-nowrap">
                        {v ? v.asset_id : <span className="font-sans text-rose-600 font-semibold">UNASSIGNED</span>}
                        {v && <span className="block font-sans text-[10.5px] text-ink-400">{v.make} {v.model} · {v.passenger_capacity} seats</span>}
                      </td>
                      <td className="table-td text-[11px]">{v ? v.availability.replaceAll("_", " ").toLowerCase() : "—"}</td>
                      <td className="table-td"><StatusChip tone={r.is_active ? "green" : "slate"}>{r.is_active ? "ACTIVE" : "INACTIVE"}</StatusChip></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="Spare pool" subtitle="Vehicles the recovery engine may allocate">
          <div className="space-y-2.5">
            {vehicles.filter((v) => v.role === "SPARE").map((s) => (
              <div key={s.id} className="rounded-lg border border-slate-200 bg-white p-3">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[12px] font-bold">{s.asset_id}</span>
                  <StatusChip tone={s.availability === "AVAILABLE" ? "green" : "slate"}>
                    {s.availability.replaceAll("_", " ")}
                  </StatusChip>
                </div>
                <div className="mt-1 text-[12px] text-ink-600">
                  {s.make} {s.model} MY{s.model_year} · {s.passenger_capacity} seats · {s.depot} depot
                </div>
                <div className="mt-0.5 text-[11px] text-ink-400">
                  features: {s.features.length ? s.features.join(", ") : "none"} · brake code {s.brake_feature_code ?? "n/r"}
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>

      {plan && (
        <Card
          title={`Recovery proposal ${plan.proposal_uid}`}
          subtitle={`Policy: ${plan.policy.replaceAll("_", " ")} · generated ${fmtTime(plan.generated_at)} · plan hash ${plan.plan_hash.slice(0, 16)}…`}
          right={<StatusChip tone={plan.status === "APPROVED" ? "green" : plan.status === "PENDING" ? "amber" : "slate"}>{plan.status}</StatusChip>}
        >
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
            <StatTile label="Routes disrupted" value={plan.summary.routes_disrupted ?? 0} tone="red" />
            <StatTile label="Recoverable" value={plan.summary.routes_recoverable ?? 0} tone="green" />
            <StatTile label="Not recoverable" value={plan.summary.routes_not_recoverable ?? 0} tone="amber" />
            <StatTile label="Flagged review-only" value={plan.summary.routes_flagged_review_only ?? 0} tone="slate" />
          </div>
          <div className="overflow-x-auto rounded-lg border border-slate-200">
            <table className="w-full text-[12.5px]">
              <thead>
                <tr>
                  <th className="table-th">Route</th>
                  <th className="table-th">Original</th>
                  <th className="table-th">Replacement</th>
                  <th className="table-th">Capacity</th>
                  <th className="table-th">Depot</th>
                  <th className="table-th">Equipment</th>
                  <th className="table-th">Unresolved issues</th>
                  <th className="table-th">State</th>
                </tr>
              </thead>
              <tbody>
                {plan.assignments.map((a) => (
                  <tr key={a.id}>
                    <td className="table-td whitespace-nowrap font-semibold">{a.route?.route_id}</td>
                    <td className="table-td font-mono text-[11.5px]">{a.original_vehicle?.asset_id}</td>
                    <td className="table-td font-mono text-[11.5px]">
                      {a.replacement_vehicle?.asset_id ?? (a.status === "NOT_APPLICABLE" ? "retained (flagged)" : "—")}
                    </td>
                    <td className="table-td whitespace-nowrap">
                      {a.capacity_offered != null ? <><Check ok={a.capacity_ok} /> {a.capacity_offered}/{a.capacity_required}</> : `req ${a.capacity_required}`}
                    </td>
                    <td className="table-td"><Check ok={a.depot_ok} /></td>
                    <td className="table-td"><Check ok={a.characteristics_ok} /></td>
                    <td className="table-td max-w-[320px] text-[11.5px] text-rose-600">
                      {a.unresolved_issues.length ? a.unresolved_issues.join(" ") : "—"}
                    </td>
                    <td className="table-td">
                      {a.status === "APPLIED" ? <StatusChip tone="green">APPLIED</StatusChip>
                        : a.status === "NOT_APPLICABLE" ? <StatusChip tone="amber">FLAG ONLY</StatusChip>
                        : a.feasible ? <StatusChip tone="blue">PROPOSED</StatusChip>
                        : <StatusChip tone="red">INFEASIBLE</StatusChip>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {plan.status === "PENDING" && (
            <Banner tone="info">
              No route has been recovered yet. Approval re-validates the plan hash, re-checks spare availability,
              then applies every change in one database transaction with audit events.
            </Banner>
          )}
          {plan.status === "APPROVED" && (
            <Banner tone="success">
              Approved by {plan.decided_by} at {fmtTime(plan.decided_at)}. The changes above are persisted;
              see Evidence & Audit for the append-only record.
            </Banner>
          )}
        </Card>
      )}
      {planError && <Banner tone="neutral">{planError}</Banner>}
    </div>
  );
}
