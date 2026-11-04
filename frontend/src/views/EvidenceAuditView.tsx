import { useCallback, useEffect, useState } from "react";
import { api, errorDetail } from "../api";
import type { AuditEvent, BenchmarkRun, Evidence, Incident } from "../types";
import { Banner, Card, Spinner, StatTile, StatusChip, fmtTime, shortHash } from "../components/ui";

export default function EvidenceAuditView({ incident }: { incident: Incident | null }) {
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [bench, setBench] = useState<BenchmarkRun | null>(null);
  const [bench404, setBench404] = useState(false);
  const [benchBusy, setBenchBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!incident) return;
    setError("");
    try {
      const [ev, aud] = await Promise.all([api.evidence(incident.id), api.audit(incident.id)]);
      setEvidence(ev);
      setAudit(aud);
    } catch (err) {
      setError(errorDetail(err));
    }
    try {
      setBench(await api.benchmarkLatest());
      setBench404(false);
    } catch {
      setBench404(true);
    }
  }, [incident]);

  useEffect(() => { load(); }, [load]);

  async function runBenchmark() {
    setBenchBusy(true);
    try {
      setBench(await api.benchmarkRun());
      setBench404(false);
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setBenchBusy(false);
    }
  }

  if (!incident) return <Card title="Evidence & Audit"><Spinner /></Card>;

  const version = evidence?.report_version;
  const m = bench?.metrics as Record<string, number | string> | undefined;

  return (
    <div className="space-y-5">
      {error && <Banner tone="danger">{error}</Banner>}

      {/* Report version */}
      <Card title="Report version & provenance" subtitle="Immutable version records — a changed document creates a NEW version marked for review; reviewed criteria are never silently replaced.">
        {version ? (
          <div className="grid gap-x-8 md:grid-cols-2">
            <dl>
              <Row k="Campaign" v={incident.campaign_number} />
              <Row k="Report URL" v={<a className="underline text-indigo-600" href={version.report_url} target="_blank" rel="noreferrer">{version.report_url}</a>} />
              <Row k="Document SHA-256" v={<span className="font-mono text-[11px] break-all">{version.document_sha256}</span>} />
              <Row k="Pages" v={version.page_count ?? "—"} />
              <Row k="Submission date" v={version.submission_date || "—"} />
            </dl>
            <dl>
              <Row k="Retrieval timestamp" v={fmtTime(version.retrieval_timestamp)} />
              <Row k="Extraction timestamp" v={fmtTime(version.extraction_timestamp)} />
              <Row k="Provenance" v={<span className="font-mono text-[11px]">{version.provenance}</span>} />
              <Row k="Amendment / revision" v={version.amendment_info || "none recorded"} />
              <Row k="Review status" v={
                <StatusChip tone={version.review_status === "REVIEWED" ? "green" : "amber"}>{version.review_status.replaceAll("_", " ")}</StatusChip>
              } />
            </dl>
            {version.notes && <p className="md:col-span-2 mt-2 text-[12px] text-ink-500">{version.notes}</p>}
          </div>
        ) : (
          <Spinner label="Loading version info…" />
        )}
        {evidence && (
          <div className="mt-3">
            <Banner tone={evidence.using_reviewed_fixture ? "warn" : "success"}>{evidence.evidence_banner}</Banner>
          </div>
        )}
      </Card>

      {/* Full criteria provenance */}
      <Card title="Extracted criteria — full provenance" subtitle="Every field: value · source document · page · verbatim snippet · method · confidence · review status.">
        {evidence ? (
          <div className="overflow-x-auto rounded-lg border border-slate-200">
            <table className="w-full text-[12px]">
              <thead>
                <tr>
                  <th className="table-th">Type</th>
                  <th className="table-th">Label</th>
                  <th className="table-th">Value</th>
                  <th className="table-th">Page</th>
                  <th className="table-th">Snippet</th>
                  <th className="table-th">Method</th>
                  <th className="table-th">Conf.</th>
                  <th className="table-th">Review</th>
                </tr>
              </thead>
              <tbody>
                {evidence.criteria.map((c) => (
                  <tr key={c.id} className="hover:bg-slate-50/60">
                    <td className="table-td font-mono text-[10.5px] whitespace-nowrap">{c.criterion_type}</td>
                    <td className="table-td whitespace-nowrap font-medium">{c.label}</td>
                    <td className="table-td font-mono text-[11px] max-w-[280px]"><Json v={c.value} /></td>
                    <td className="table-td">{c.source_page ?? "—"}</td>
                    <td className="table-td max-w-[320px] text-ink-500 italic">“{c.source_snippet}”</td>
                    <td className="table-td font-mono text-[10.5px] whitespace-nowrap">{c.extraction_method}</td>
                    <td className="table-td tabular-nums">{(c.confidence * 100).toFixed(0)}%</td>
                    <td className="table-td whitespace-nowrap">
                      <StatusChip tone={c.review_status === "REVIEWED" ? "green" : c.review_status === "NEEDS_REVIEW" ? "amber" : "slate"}>
                        {c.review_status.replaceAll("_", " ")}
                      </StatusChip>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Spinner />
        )}
      </Card>

      {/* Benchmark */}
      <Card
        title="Classification benchmark"
        subtitle="Measured at runtime by scripts/run_benchmark.py and POST /api/benchmarks/run — never hardcoded."
        right={
          <button className="btn-secondary" onClick={runBenchmark} disabled={benchBusy}>
            {benchBusy ? <Spinner label="Running…" /> : "Run benchmark now"}
          </button>
        }
      >
        {bench404 && !bench && (
          <p className="text-[13px] text-ink-500">No benchmark run recorded yet — run it now or execute <code className="font-mono bg-slate-100 px-1 rounded">python scripts/run_benchmark.py</code>.</p>
        )}
        {bench && (
          <div className="space-y-4">
            <Banner tone="warn"><strong>{bench.label}</strong></Banner>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <StatTile label="Samples" value={bench.sample_count} />
              <StatTile label="Match accuracy" value={pct(m?.correct_match_rate)} tone="green" hint={String(m?.correct_match_count ?? "")} />
              <StatTile label="Abstention" value={pct(m?.correct_abstention_rate)} tone="green" hint={String(m?.correct_abstention_count ?? "")} />
              <StatTile label="False-confirmation" value={pct(m?.false_confirmation_rate)} tone={(m?.false_confirmation_rate as number) === 0 ? "green" : "red"} hint={String(m?.false_confirmation_count ?? "")} />
              <StatTile label="Outside accuracy" value={pct(m?.outside_accuracy)} tone="green" hint={String(m?.outside_count ?? "")} />
            </div>
            <div className="grid md:grid-cols-2 gap-4">
              <div>
                <div className="text-[11px] font-semibold uppercase tracking-wider text-ink-500 mb-2">Confusion matrix (expected ↓ / predicted →)</div>
                <ConfusionMatrix matrix={bench.confusion_matrix} />
              </div>
              <div className="text-[12px] text-ink-600 space-y-1">
                <p><strong>Engine version:</strong> <span className="font-mono">{bench.engine_version}</span></p>
                <p><strong>Campaign:</strong> {bench.campaign_number} (reviewed criteria)</p>
                <p><strong>Last execution:</strong> {fmtTime(bench.run_at)} ({bench.duration_ms} ms)</p>
                <p className="pt-2 text-[11.5px] text-ink-500 leading-snug">
                  False-confirmation rate = incomplete-evidence cases classified as CAMPAIGN_CRITERIA_MATCH ÷ all
                  incomplete-evidence cases. A value above 0 would mean the engine confirmed a recall match on
                  incomplete evidence — the exact failure mode RouteShield is built to prevent.
                </p>
              </div>
            </div>
          </div>
        )}
      </Card>

      {/* Audit log */}
      <Card title="Audit trail" subtitle="Append-only by design: no endpoint in RouteShield can modify or delete audit events.">
        <div className="overflow-x-auto rounded-lg border border-slate-200">
          <table className="w-full text-[12px]">
            <thead>
              <tr>
                <th className="table-th">Event ID</th>
                <th className="table-th">Timestamp</th>
                <th className="table-th">Actor</th>
                <th className="table-th">Action</th>
                <th className="table-th">Entity</th>
                <th className="table-th">Policy</th>
                <th className="table-th">Proposal</th>
                <th className="table-th">Result</th>
                <th className="table-th">State change</th>
              </tr>
            </thead>
            <tbody>
              {audit.map((e) => (
                <tr key={e.id} className="hover:bg-slate-50/60">
                  <td className="table-td font-mono text-[10.5px] whitespace-nowrap">{e.event_uid}</td>
                  <td className="table-td whitespace-nowrap tabular-nums">{fmtTime(e.timestamp)}</td>
                  <td className="table-td whitespace-nowrap font-medium">{e.actor}</td>
                  <td className="table-td font-mono text-[10.5px] whitespace-nowrap">{e.action}</td>
                  <td className="table-td whitespace-nowrap text-[11px]">{e.entity_type} <span className="font-mono">{e.entity_id}</span></td>
                  <td className="table-td text-[11px]">{e.policy?.replaceAll("_", " ").toLowerCase() ?? "—"}</td>
                  <td className="table-td font-mono text-[10.5px]">{e.proposal_id ?? "—"}</td>
                  <td className="table-td whitespace-nowrap">
                    <StatusChip tone={e.result === "APPROVED" || e.result === "APPLIED" || e.result === "OK" ? "green" : e.result === "REJECTED" ? "red" : "slate"}>{e.result}</StatusChip>
                  </td>
                  <td className="table-td max-w-[360px]">
                    {e.previous_state && <div className="text-[10.5px] font-mono text-ink-500 truncate" title={JSON.stringify(e.previous_state)}>prev: {JSON.stringify(e.previous_state)}</div>}
                    {e.new_state && <div className="text-[10.5px] font-mono text-ink-700 truncate" title={JSON.stringify(e.new_state)}>new: {JSON.stringify(e.new_state)}</div>}
                    {e.detail && <div className="text-[11px] text-ink-500 mt-0.5">{e.detail}</div>}
                  </td>
                </tr>
              ))}
              {audit.length === 0 && (
                <tr><td colSpan={9} className="table-td text-ink-400">No events recorded yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex gap-3 py-1.5 border-b border-slate-50">
      <dt className="w-44 shrink-0 text-xs font-medium text-ink-500">{k}</dt>
      <dd className="text-[12.5px] text-ink-800 min-w-0">{v}</dd>
    </div>
  );
}

function Json({ v }: { v: Record<string, unknown> }) {
  const text = Object.entries(v)
    .filter(([k]) => !k.startsWith("$"))
    .map(([k, val]) => `${k}=${typeof val === "object" ? JSON.stringify(val) : String(val)}`)
    .join(" · ");
  return <span title={text}>{text}</span>;
}

function ConfusionMatrix({ matrix }: { matrix: Record<string, Record<string, number>> }) {
  const classes = Object.keys(matrix);
  const short = (c: string) => c.replace("CAMPAIGN_CRITERIA_MATCH", "MATCH").replace("VERIFICATION_REQUIRED", "VERIF_REQ").replace("OUTSIDE_REPORT_VERSION", "OUTSIDE");
  return (
    <table className="text-[11.5px] border border-slate-200 rounded-lg overflow-hidden">
      <thead>
        <tr className="bg-slate-50">
          <th className="px-2 py-1.5 border-b border-r border-slate-200" />
          {classes.map((c) => <th key={c} className="px-2 py-1.5 font-semibold border-b border-r border-slate-200 last:border-r-0 text-center">{short(c)}</th>)}
        </tr>
      </thead>
      <tbody>
        {classes.map((exp) => (
          <tr key={exp}>
            <td className="px-2 py-1.5 font-semibold border-b border-r border-slate-200 bg-slate-50 whitespace-nowrap">{short(exp)}</td>
            {classes.map((pred) => {
              const n = matrix[exp][pred] ?? 0;
              const diag = exp === pred;
              return (
                <td key={pred} className={`px-3 py-1.5 text-center tabular-nums border-b border-r border-slate-100 last:border-r-0 ${diag ? (n > 0 ? "bg-emerald-50 font-bold text-emerald-700" : "bg-rose-50 text-rose-600 font-bold") : n > 0 ? "bg-rose-50 text-rose-600 font-bold" : "text-ink-400"}`}>
                  {n}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function pct(v: unknown): string {
  if (typeof v === "number") return `${(v * 100).toFixed(1)}%`;
  return "—";
}
