import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorDetail, getToken } from "../api";
import type { AgentProbe, AgentStatus, Incident, Investigation, InvestigationStep } from "../types";
import { Banner, Card, Spinner, StatusChip, fmtTime } from "../components/ui";

const OBJECTIVE_EXAMPLES = [
  "Assess which vehicles are affected by this recall and which routes are disrupted.",
  "Which vehicles can be verified, and what evidence is missing for the rest?",
  "Investigate recovery options for every impacted route and what still needs a decision.",
  "Why was the vehicle on the most-affected route grounded, and what would change its classification?",
];

const STOP_REASON_HELP: Record<string, string> = {
  MODEL_NOT_CONFIGURED:
    "Vultr Serverless Inference is not configured (VULTR_INFERENCE_API_KEY / VULTR_INFERENCE_MODEL). The agent refuses to simulate results — configure the key and retry.",
  PROVIDER_ERROR:
    "The inference provider call failed. The sanitized provider error is shown below; the investigation made no operational changes.",
  MAX_ITERATIONS:
    "The model used all 8 investigation rounds without delivering a grounded final assessment. The loop stopped exactly at the boundary.",
  INVALID_FINAL_OUTPUT:
    "The model repeatedly failed to produce output matching the strict final schema. Nothing unvalidated was shown as fact.",
  GROUNDING_VALIDATION_FAILED:
    "The deterministic grounding validator rejected the model's final assessment (fabricated references or classifications contradicting the engine). Ungrounded output is never displayed.",
  TIME_BUDGET_EXCEEDED:
    "The wall-clock budget for this investigation ran out. The partial tool trace is preserved below.",
};

export default function AgentConsoleView({
  incident,
  authed,
  onRequireAuth,
}: {
  incident: Incident | null;
  authed: boolean;
  onRequireAuth: () => void;
}) {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [probe, setProbe] = useState<AgentProbe | null>(null);
  const [probeBusy, setProbeBusy] = useState(false);
  const [investigations, setInvestigations] = useState<Investigation[]>([]);
  const [activeUid, setActiveUid] = useState<string | null>(null);
  const [active, setActive] = useState<Investigation | null>(null);
  const [objective, setObjective] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const reloadList = useCallback(async () => {
    if (!incident) return;
    try {
      const list = await api.investigations(incident.id);
      setInvestigations(list);
      return list;
    } catch {
      return [];
    }
  }, [incident]);

  useEffect(() => {
    api.agentStatus().then(setStatus).catch(() => setStatus(null));
  }, []);

  useEffect(() => {
    setActiveUid(null);
    setActive(null);
    reloadList();
  }, [incident, reloadList]);

  // Poll the active investigation while it is running.
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    if (!activeUid) return;
    let stopped = false;
    const tick = async () => {
      try {
        const inv = await api.investigation(activeUid);
        if (stopped) return;
        setActive(inv);
        if (inv.status !== "INVESTIGATING") {
          if (pollRef.current) clearInterval(pollRef.current);
          reloadList();
        }
      } catch {
        /* transient */
      }
    };
    tick();
    pollRef.current = setInterval(tick, 1500);
    return () => {
      stopped = true;
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [activeUid, reloadList]);

  async function start() {
    if (!incident || !objective.trim()) return;
    setBusy(true);
    setError("");
    try {
      const inv = await api.startInvestigation(incident.id, objective.trim());
      setActiveUid(inv.session_uid);
      setActive(inv);
    } catch (err) {
      if ((err as { status?: number }).status === 401) onRequireAuth();
      else setError(errorDetail(err));
    } finally {
      setBusy(false);
    }
  }

  async function runProbe() {
    setProbeBusy(true);
    setProbe(null);
    try {
      setProbe(await api.agentProbe());
    } catch (err) {
      if ((err as { status?: number }).status === 401) onRequireAuth();
      else setError(errorDetail(err));
    } finally {
      setProbeBusy(false);
    }
  }

  if (!incident) {
    return (
      <Card title="Agent Console">
        <p className="text-ink-500"><Spinner label="Loading incident…" /></p>
      </Card>
    );
  }

  const running = active?.status === "INVESTIGATING";

  return (
    <div className="space-y-5">
      {/* Agent readiness */}
      <div className="card px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-xl font-bold tracking-tight text-ink-950">Incident Response Agent</h1>
              {status && (
                <StatusChip tone={status.configured ? "green" : "amber"}>
                  {status.configured ? "INFERENCE CONFIGURED" : "NOT CONFIGURED"}
                </StatusChip>
              )}
            </div>
            <p className="mt-1 text-[13px] text-ink-600">
              A bounded, read-only investigation agent. It queries the same evidence and deterministic
              engines you use, then prepares a grounded briefing — every operational decision still
              requires <strong>your</strong> approval in Incident Command.
            </p>
          </div>
          <button className="btn-secondary" onClick={runProbe} disabled={probeBusy || !authed}
                  title={authed ? "Send a minimal live probe to the inference endpoint" : "Manager sign-in required"}>
            {probeBusy ? <Spinner label="Probing…" /> : "Probe inference endpoint"}
          </button>
        </div>
        {status && (
          <div className="mt-3 flex flex-wrap gap-x-8 gap-y-2">
            <Meta k="Model" v={status.model || "—"} mono />
            <Meta k="Endpoint" v={status.base_url_host} mono />
            <Meta k="Max rounds" v={String(status.max_iterations)} />
            <Meta k="Tools exposed" v={`${status.tools.length} (read-only)`} />
            <Meta k="Write tools" v="NONE" />
          </div>
        )}
        {status && !status.configured && (
          <Banner tone="warn">
            <strong>The agent will refuse to fabricate.</strong> {status.note} Set the environment
            variables on the backend and restart to enable live investigations.
          </Banner>
        )}
        {probe && (
          <Banner tone={probe.configured && probe.tool_calling_supported ? "success" : probe.configured ? "warn" : "danger"}>
            <strong>Probe:</strong> {probe.detail} ({probe.duration_ms} ms)
            {probe.model && <> · model <span className="font-mono">{probe.model}</span></>}
          </Banner>
        )}
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        {/* Composer + history */}
        <div className="space-y-5">
          <Card title="New investigation" subtitle={`Incident ${incident.campaign_number} · the agent investigates, you decide.`}>
            {!authed ? (
              <Banner tone="warn">
                Manager sign-in required to launch an investigation (it consumes inference credits).
                Browsing past investigations is open.{" "}
                <button className="underline font-semibold" onClick={onRequireAuth}>Sign in</button>
              </Banner>
            ) : (
              <div className="space-y-3">
                <textarea
                  className="input min-h-[90px] text-[13px] leading-snug"
                  placeholder="What should the agent investigate? e.g. Assess recall exposure for every vehicle and prepare recovery options for disrupted routes."
                  value={objective}
                  maxLength={2000}
                  onChange={(e) => setObjective(e.target.value)}
                  disabled={Boolean(running)}
                />
                <div className="flex flex-wrap gap-1.5">
                  {OBJECTIVE_EXAMPLES.map((ex) => (
                    <button key={ex}
                      className="rounded-full border border-slate-300 bg-white px-2.5 py-1 text-[11px] text-ink-600 hover:border-indigo-400 hover:text-indigo-700"
                      onClick={() => setObjective(ex)} disabled={Boolean(running)}>
                      {ex.length > 52 ? ex.slice(0, 52) + "…" : ex}
                    </button>
                  ))}
                </div>
                <button className="btn-primary w-full" disabled={busy || running || !objective.trim()} onClick={start}>
                  {running ? <Spinner label="Investigation in progress…" /> : "Launch investigation"}
                </button>
                <p className="text-[11px] leading-snug text-ink-500">
                  Max 8 model rounds · read-only tools only · every tool call, argument and result is
                  persisted verbatim. If the model is unavailable the investigation fails honestly —
                  nothing is simulated.
                </p>
                {error && <Banner tone="danger">{error}</Banner>}
              </div>
            )}
          </Card>

          <Card title="Investigation history" subtitle="Persisted server-side — refresh-safe and auditable.">
            {investigations.length === 0 ? (
              <p className="text-[13px] text-ink-400">No investigations for this incident yet.</p>
            ) : (
              <div className="space-y-2 max-h-[420px] overflow-y-auto pr-1">
                {investigations.map((inv) => (
                  <button key={inv.session_uid}
                    onClick={() => setActiveUid(inv.session_uid)}
                    className={`w-full rounded-lg border px-3 py-2.5 text-left transition-colors ${
                      activeUid === inv.session_uid ? "border-indigo-400 bg-indigo-50/60" : "border-slate-200 bg-white hover:border-slate-300"
                    }`}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-[11px] font-bold text-ink-800">{inv.session_uid}</span>
                      <StatusChip tone={inv.status === "COMPLETED" ? "green" : inv.status === "INVESTIGATING" ? "blue" : "red"}>
                        {inv.status}
                      </StatusChip>
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-[11.5px] text-ink-600">{inv.objective}</p>
                    <div className="mt-1 text-[10.5px] text-ink-400">
                      {fmtTime(inv.started_at)} · {inv.tool_calls_count} tool calls · {inv.iterations_used} rounds
                      {inv.duration_ms != null && ` · ${(inv.duration_ms / 1000).toFixed(1)}s`}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </Card>
        </div>

        {/* Trace + result */}
        <div className="xl:col-span-2 space-y-5">
          {active ? (
            <>
              <Card
                title={`Trace ${active.session_uid}`}
                subtitle={`Objective: ${active.objective}`}
                right={
                  <StatusChip tone={active.status === "COMPLETED" ? "green" : active.status === "INVESTIGATING" ? "blue" : "red"}>
                    {active.status === "INVESTIGATING" ? "INVESTIGATING…" : active.status}
                  </StatusChip>
                }
              >
                <div className="grid gap-2 text-[11.5px] sm:grid-cols-4">
                  <Meta k="Model label" v={active.model} mono small />
                  <Meta k="Rounds used" v={`${active.iterations_used} / ${status?.max_iterations ?? 8}`} small />
                  <Meta k="Tool calls" v={String(active.tool_calls_count)} small />
                  <Meta k="Duration" v={active.duration_ms != null ? `${(active.duration_ms / 1000).toFixed(1)}s` : "—"} small />
                </div>

                {active.status === "FAILED" && (
                  <Banner tone="danger">
                    <strong>Investigation failed ({active.stop_reason || "ERROR"}).</strong>{" "}
                    {STOP_REASON_HELP[active.stop_reason] ?? active.provider_error}
                    {active.provider_error && (
                      <span className="mt-1 block font-mono text-[11px] break-words">provider: {active.provider_error}</span>
                    )}
                    {active.grounding_violations.length > 0 && (
                      <span className="mt-1 block">
                        Grounding violations detected (never displayed as facts):
                        <ul className="mt-0.5 list-disc pl-4">
                          {active.grounding_violations.slice(0, 6).map((v, i) => <li key={i} className="text-[11.5px]">{v}</li>)}
                        </ul>
                      </span>
                    )}
                  </Banner>
                )}
                {active.status === "COMPLETED" && (
                  <Banner tone="success">
                    <strong>Grounded briefing ready.</strong> Every claim below was checked by the
                    deterministic grounding validator against the persisted tool outputs. The agent
                    changed nothing — decisions happen in Incident Command.
                  </Banner>
                )}

                {/* Step trace */}
                <div className="mt-4 space-y-2">
                  {active.steps.map((s) => <StepRow key={s.id} s={s} />)}
                  {running && (
                    <div className="rounded-lg border border-sky-200 bg-sky-50/60 px-3.5 py-2.5 text-[12.5px] text-sky-800 flex items-center gap-2">
                      <Spinner /> Agent is working — the trace updates live from the server.
                    </div>
                  )}
                  {active.steps.length === 0 && !running && (
                    <p className="text-[13px] text-ink-400">No steps were recorded for this investigation.</p>
                  )}
                </div>
              </Card>

              {active.status === "COMPLETED" && active.final_result && (
                <FinalResultCard result={active.final_result} />
              )}
            </>
          ) : (
            <Card title="Investigation trace">
              <p className="text-[13px] text-ink-500">
                Launch a new investigation or pick one from the history. The trace shows the model's
                narration, every read-only tool call with its real arguments and results, validation
                outcomes and the grounded final assessment.
              </p>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

function Meta({ k, v, mono = false, small = false }: { k: string; v: string; mono?: boolean; small?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-ink-400">{k}</div>
      <div className={`${small ? "text-[11.5px]" : "text-[12.5px]"} font-semibold text-ink-800 truncate ${mono ? "font-mono" : ""}`} title={v}>
        {v}
      </div>
    </div>
  );
}

function StepRow({ s }: { s: InvestigationStep }) {
  const [openDetail, setOpenDetail] = useState(false);
  if (s.kind === "MODEL_MESSAGE") {
    return (
      <div className="rounded-lg border border-slate-200 bg-slate-50/70 px-3.5 py-2.5">
        <div className="flex items-center gap-2 text-[10.5px] font-bold uppercase tracking-wider text-ink-400">
          <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></svg>
          Model narration · step {s.seq}
        </div>
        <p className="mt-1 text-[12.5px] text-ink-700">{s.explanation}</p>
      </div>
    );
  }
  if (s.kind === "TOOL_CALL") {
    const ok = s.status === "OK";
    return (
      <div className={`rounded-lg border px-3.5 py-2.5 ${ok ? "border-slate-200 bg-white" : "border-amber-300 bg-amber-50/60"}`}>
        <div className="flex flex-wrap items-center gap-2">
          <span className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-bold font-mono ${ok ? "border-sky-200 bg-sky-50 text-sky-800" : "border-amber-300 bg-amber-100 text-amber-900"}`}>
            <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"><path d="M13 10V3L4 14h7v7l9-11h-7z" strokeLinejoin="round" /></svg>
            {s.tool_name}
          </span>
          <StatusChip tone={ok ? "green" : s.status === "TIMEOUT" ? "amber" : "red"}>{s.status}</StatusChip>
          {s.duration_ms > 0 && <span className="text-[10.5px] text-ink-400 tabular-nums">{s.duration_ms} ms</span>}
          <button className="ml-auto text-[11px] font-semibold text-indigo-600 hover:underline" onClick={() => setOpenDetail(!openDetail)}>
            {openDetail ? "hide details" : "args & result"}
          </button>
        </div>
        {s.tool_args && !openDetail && (
          <div className="mt-1 font-mono text-[11px] text-ink-500 truncate">{JSON.stringify(s.tool_args)}</div>
        )}
        {!ok && s.explanation && <p className="mt-1 text-[12px] text-amber-900">{s.explanation}</p>}
        {openDetail && (
          <div className="mt-2 space-y-2">
            <JsonBlock label="Arguments (as sent by the model)" value={s.tool_args} />
            {s.result && <JsonBlock label="Result (persisted verbatim)" value={s.result} />}
            {!s.result && <div className="rounded border border-slate-200 bg-slate-50 px-2.5 py-2 text-[11.5px] text-ink-500">No result payload ({s.status}).</div>}
          </div>
        )}
      </div>
    );
  }
  if (s.kind === "VALIDATION") {
    return (
      <div className="rounded-lg border border-amber-300 bg-amber-50/70 px-3.5 py-2.5">
        <div className="text-[10.5px] font-bold uppercase tracking-wider text-amber-700">Grounding / schema validation · step {s.seq}</div>
        <p className="mt-1 text-[12.5px] text-amber-900">{s.explanation}</p>
        {Array.isArray((s.result as { violations?: string[] })?.violations) && (
          <ul className="mt-1 list-disc pl-4 text-[11.5px] text-amber-900">
            {((s.result as { violations: string[] }).violations).slice(0, 8).map((v, i) => <li key={i}>{v}</li>)}
          </ul>
        )}
      </div>
    );
  }
  if (s.kind === "FINAL") {
    return (
      <div className="rounded-lg border border-emerald-300 bg-emerald-50/70 px-3.5 py-2.5">
        <div className="text-[10.5px] font-bold uppercase tracking-wider text-emerald-700">Final assessment accepted · step {s.seq}</div>
        <p className="mt-1 text-[12.5px] text-emerald-900">{s.explanation}</p>
      </div>
    );
  }
  return null;
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <div className="text-[10.5px] font-semibold uppercase tracking-wider text-ink-400 mb-1">{label}</div>
      <pre className="max-h-64 overflow-auto rounded-lg border border-slate-200 bg-slate-950 p-3 text-[11px] leading-snug text-slate-100 whitespace-pre-wrap break-words">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

function RefChip({ refId }: { refId: string }) {
  return (
    <span className="inline-flex items-center rounded border border-indigo-200 bg-indigo-50 px-1.5 py-px font-mono text-[10.5px] font-bold text-indigo-700 whitespace-nowrap" title="Reference id observed in a persisted tool result">
      {refId}
    </span>
  );
}

function FinalResultCard({ result }: { result: NonNullable<Investigation["final_result"]> }) {
  const [section, setSection] = useState<"briefing" | "evidence">("briefing");
  return (
    <Card
      title="Grounded briefing"
      subtitle="AI-prepared, deterministically grounded. Classifications are the engine's own — the agent repeated them, it did not decide them."
      right={
        <div className="flex gap-1">
          <button className={`rounded-md border px-2.5 py-1 text-[11px] font-semibold ${section === "briefing" ? "border-indigo-500 bg-indigo-600 text-white" : "border-slate-300 bg-white text-ink-600"}`} onClick={() => setSection("briefing")}>Briefing</button>
          <button className={`rounded-md border px-2.5 py-1 text-[11px] font-semibold ${section === "evidence" ? "border-indigo-500 bg-indigo-600 text-white" : "border-slate-300 bg-white text-ink-600"}`} onClick={() => setSection("evidence")}>
            Evidence refs ({result.evidence_references.length})
          </button>
        </div>
      }
    >
      {section === "briefing" ? (
        <div className="space-y-4">
          <div>
            <SectionLabel>Incident summary</SectionLabel>
            <p className="text-[13px] leading-relaxed text-ink-800">{result.incident_summary}</p>
          </div>
          <div>
            <SectionLabel>Objective interpretation</SectionLabel>
            <p className="text-[13px] leading-relaxed text-ink-800">{result.objective_interpretation}</p>
          </div>

          {result.facts.length > 0 && (
            <div>
              <SectionLabel>Facts (each carries verified evidence refs)</SectionLabel>
              <ul className="space-y-1.5">
                {result.facts.map((f, i) => (
                  <li key={i} className="rounded-lg border border-slate-200 bg-slate-50/60 px-3 py-2 text-[12.5px] text-ink-800">
                    <span className="mr-1 font-bold text-ink-400">F{i + 1}.</span>{f.statement}
                    {f.evidence_refs.length > 0 && (
                      <span className="ml-2 inline-flex flex-wrap gap-1 align-middle">
                        {f.evidence_refs.map((r) => <RefChip key={r} refId={r} />)}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {result.inferences.length > 0 && (
            <div>
              <SectionLabel>Inferences (agent reasoning — not facts)</SectionLabel>
              <ul className="space-y-1.5">
                {result.inferences.map((inf, i) => (
                  <li key={i} className="rounded-lg border border-dashed border-sky-300 bg-sky-50/50 px-3 py-2 text-[12.5px] text-sky-900">
                    <span className="mr-1 font-bold text-sky-500">I{i + 1}.</span>{inf.statement}
                    {inf.basis && <span className="block pl-6 text-[11.5px] text-sky-700/80">basis: {inf.basis}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {result.vehicle_classifications.length > 0 && (
            <div>
              <SectionLabel>Vehicle classifications (deterministic engine values)</SectionLabel>
              <div className="overflow-x-auto rounded-lg border border-slate-200">
                <table className="w-full text-[12px]">
                  <thead>
                    <tr><th className="table-th">Asset</th><th className="table-th">Classification</th><th className="table-th">Basis ref</th></tr>
                  </thead>
                  <tbody>
                    {result.vehicle_classifications.map((c, i) => (
                      <tr key={i}>
                        <td className="table-td font-mono text-[11.5px]">{c.asset_id}</td>
                        <td className="table-td">
                          <span className={`rounded border px-1.5 py-0.5 text-[10.5px] font-bold ${
                            c.classification === "CAMPAIGN_CRITERIA_MATCH" ? "border-rose-200 bg-rose-50 text-rose-700"
                            : c.classification === "VERIFICATION_REQUIRED" ? "border-amber-300 bg-amber-50 text-amber-800"
                            : "border-slate-300 bg-slate-100 text-ink-600"}`}>
                            {c.classification.replaceAll("_", " ")}
                          </span>
                        </td>
                        <td className="table-td">{c.basis_ref ? <RefChip refId={c.basis_ref} /> : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div className="grid gap-4 md:grid-cols-2">
            {result.affected_routes.length > 0 && (
              <div>
                <SectionLabel>Affected routes</SectionLabel>
                <div className="flex flex-wrap gap-1.5">
                  {result.affected_routes.map((r) => <RefChip key={r} refId={r} />)}
                </div>
              </div>
            )}
            {result.recovery_candidates.length > 0 && (
              <div>
                <SectionLabel>Recovery candidates (previews only — nothing applied)</SectionLabel>
                <ul className="space-y-1 text-[12px] text-ink-700">
                  {result.recovery_candidates.map((c, i) => (
                    <li key={i} className="rounded border border-slate-200 bg-white px-2 py-1.5">
                      <RefChip refId={c.route_id} />
                      {c.spare_asset_id ? <> → <RefChip refId={c.spare_asset_id} /></> : <> → <span className="text-[11px] font-semibold text-rose-600">no eligible spare</span></>}
                      {c.feasibility_note && <span className="block pl-1 text-[11px] text-ink-500">{c.feasibility_note}</span>}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          {(result.unresolved_constraints.length > 0 || result.unknowns_and_missing_evidence.length > 0) && (
            <div className="grid gap-4 md:grid-cols-2">
              {result.unresolved_constraints.length > 0 && (
                <div>
                  <SectionLabel>Unresolved constraints</SectionLabel>
                  <ul className="list-disc pl-4 text-[12.5px] text-ink-700 space-y-1">
                    {result.unresolved_constraints.map((u, i) => <li key={i}>{u}</li>)}
                  </ul>
                </div>
              )}
              {result.unknowns_and_missing_evidence.length > 0 && (
                <div>
                  <SectionLabel>Unknowns & missing evidence</SectionLabel>
                  <ul className="list-disc pl-4 text-[12.5px] text-ink-700 space-y-1">
                    {result.unknowns_and_missing_evidence.map((u, i) => <li key={i}>{u}</li>)}
                  </ul>
                </div>
              )}
            </div>
          )}

          {result.proposed_next_actions.length > 0 && (
            <div>
              <SectionLabel>Proposed next actions — manager decisions</SectionLabel>
              <ol className="space-y-1.5">
                {result.proposed_next_actions.map((a, i) => (
                  <li key={i} className="rounded-lg border border-indigo-200 bg-indigo-50/50 px-3 py-2 text-[12.5px] text-ink-800">
                    <div className="flex items-start gap-2">
                      <span className="font-bold text-indigo-500">{i + 1}.</span>
                      <div>
                        {a.action}
                        {a.rationale && <span className="block text-[11.5px] text-ink-500">{a.rationale}</span>}
                      </div>
                      <span className="ml-auto shrink-0 rounded border border-indigo-300 bg-white px-1.5 py-px text-[10px] font-bold text-indigo-700">REQUIRES MANAGER APPROVAL</span>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
          )}

          {result.uncertainty_notes && (
            <Banner tone="warn"><strong>Uncertainty notes:</strong> {result.uncertainty_notes}</Banner>
          )}
        </div>
      ) : (
        <div className="space-y-1.5">
          <p className="mb-2 text-[12px] text-ink-500">
            Every reference below appeared verbatim in a persisted tool result — the grounding validator
            rejects citations it has not seen.
          </p>
          {result.evidence_references.map((r, i) => (
            <div key={i} className="flex items-start gap-2.5 rounded-lg border border-slate-200 bg-white px-3 py-2">
              <RefChip refId={r.ref_id} />
              <span className="text-[12px] text-ink-700">{r.description || "—"}</span>
              {r.source && <span className="ml-auto shrink-0 font-mono text-[10.5px] text-ink-400 truncate max-w-[280px]" title={r.source}>{r.source}</span>}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-ink-500">{children}</div>
  );
}
