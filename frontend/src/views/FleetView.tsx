import { useEffect, useMemo, useState } from "react";
import { api, errorDetail } from "../api";
import type { FleetAssessmentSummary, Incident, Vehicle, VinDecodeResult } from "../types";
import { Banner, Card, Spinner, StatusChip, fmtTime } from "../components/ui";

const AVAILABILITY_TONE: Record<string, "green" | "amber" | "red" | "slate" | "blue"> = {
  AVAILABLE: "green",
  ASSIGNED_TO_ROUTE: "blue",
  IN_MAINTENANCE: "slate",
  GROUNDED_RECALL: "red",
  HELD_PENDING_VERIFICATION: "amber",
  FLAGGED_FOR_MANAGER_REVIEW: "amber",
};

type SortKey = "asset_id" | "model_year" | "depot" | "capacity" | "availability";

export default function FleetView({
  incident,
  authed,
  onRequireAuth,
}: {
  incident: Incident | null;
  authed: boolean;
  onRequireAuth: () => void;
}) {
  const [vehicles, setVehicles] = useState<Vehicle[]>([]);
  const [assessment, setAssessment] = useState<FleetAssessmentSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [vin, setVin] = useState("");
  const [vinYear, setVinYear] = useState("");
  const [decode, setDecode] = useState<VinDecodeResult | null>(null);
  const [decodeBusy, setDecodeBusy] = useState(false);
  const [decodeError, setDecodeError] = useState("");

  useEffect(() => {
    api.vehicles()
      .then(setVehicles)
      .catch((e) => setError(errorDetail(e)))
      .finally(() => setLoading(false));
  }, [incident]);

  // Classification context (if an incident assessment exists) colors the register.
  useEffect(() => {
    if (!incident) return;
    api.fleetAssessment(incident.id).then(setAssessment).catch(() => setAssessment(null));
  }, [incident]);

  async function doDecode() {
    setDecodeBusy(true);
    setDecodeError("");
    setDecode(null);
    try {
      const res = await api.decodeVin(vin.trim(), vinYear.trim() || undefined);
      setDecode(res);
    } catch (err) {
      setDecodeError(errorDetail(err));
      if ((err as { status?: number }).status === 401) onRequireAuth();
    } finally {
      setDecodeBusy(false);
    }
  }

  const buses = vehicles.filter((v) => v.role === "ROUTE_BUS");
  const spares = vehicles.filter((v) => v.role === "SPARE");

  return (
    <div className="space-y-5">
      <Banner tone="warn">
        <strong>{vehicles[0]?.synthetic_label ?? "SYNTHETIC DEMONSTRATION DATA — NOT REAL VEHICLES"}.</strong>{" "}
        Asset IDs and VIN-like identifiers below are synthetic and are never submitted to NHTSA or vPIC.
        Operational state is persisted in the backend database — nothing here relies on browser storage.
      </Banner>
      {error && <Banner tone="danger">{error}</Banner>}

      <div className="grid gap-5 xl:grid-cols-3">
        <div className="xl:col-span-2 space-y-5">
          <Card title={`Route buses (${buses.length})`} subtitle={loading ? undefined : "Fleet register with live operational state — click a row for the full record"}>
            {loading ? <Spinner label="Loading fleet…" /> : <VehicleTable vehicles={buses} assessment={assessment} />}
          </Card>
          <Card title={`Spare vehicles (${spares.length})`} subtitle="Replacement pool considered by the recovery engine">
            {loading ? <Spinner /> : <VehicleTable vehicles={spares} assessment={assessment} />}
          </Card>
        </div>

        {/* VIN decoder - manual, one at a time, human-triggered */}
        <Card
          title="VIN decode (vPIC)"
          subtitle="Single, human-triggered lookups only. NHTSA's APIs are not for bulk third-party VIN use."
        >
          {!authed ? (
            <Banner tone="warn">
              Manager sign-in required for VIN decoding.{" "}
              <button className="underline font-semibold" onClick={onRequireAuth}>Sign in</button>
            </Banner>
          ) : (
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-semibold text-ink-600 mb-1">Real VIN (17 characters)</label>
                <input className="input font-mono uppercase" maxLength={17} value={vin}
                       onChange={(e) => setVin(e.target.value.toUpperCase())} placeholder="e.g. 1HGCM82633A004352" />
              </div>
              <div>
                <label className="block text-xs font-semibold text-ink-600 mb-1">Model year hint (optional)</label>
                <input className="input font-mono" maxLength={4} value={vinYear}
                       onChange={(e) => setVinYear(e.target.value.replace(/\D/g, ""))} placeholder="2025" />
              </div>
              <button className="btn-primary w-full" disabled={decodeBusy || vin.trim().length !== 17} onClick={doDecode}>
                {decodeBusy ? <Spinner label="Contacting vPIC…" /> : "Decode this VIN"}
              </button>
              <p className="text-[11.5px] leading-snug text-ink-500">
                One VIN at a time, per request. Results are cached. Synthetic identifiers are refused
                before any outbound call. A decode is <strong>never</strong> converted into recall confirmation,
                and fields vPIC does not return stay UNKNOWN — brake configuration is never inferred.
              </p>

              {decodeError && <Banner tone="danger">{decodeError}</Banner>}
              {decode && (
                <div className="rounded-lg border border-slate-200 bg-slate-50/70 p-3.5 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[12px] font-bold">{decode.vin}</span>
                    <StatusChip tone={decode.cached ? "slate" : "green"}>{decode.cached ? "CACHED RESULT" : "LIVE DECODE"}</StatusChip>
                  </div>
                  <div className="text-[11px] text-ink-500">
                    {decode.source} · {fmtTime(decode.decoded_at)} · by {decode.requested_by}
                  </div>
                  <div className="max-h-56 overflow-y-auto rounded border border-slate-200 bg-white">
                    <table className="w-full text-[11.5px]">
                      <tbody>
                        {Object.entries(decode.decoded_values).map(([k, v]) => (
                          <tr key={k} className="border-b border-slate-50 last:border-0">
                            <td className="px-2 py-1 text-ink-500 whitespace-nowrap">{k}</td>
                            <td className="px-2 py-1 font-mono text-ink-800">{String(v)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="text-[11px] text-ink-500">
                    {decode.unavailable_fields.length} field(s) unavailable (shown as unknown, never inferred),
                    including brake configuration{decode.unavailable_fields.includes("BrakeSystemType") ? " (BrakeSystemType)" : ""}.
                  </div>
                  <Banner tone="info">{decode.limitations}</Banner>
                </div>
              )}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function VehicleTable({
  vehicles,
  assessment,
}: {
  vehicles: Vehicle[];
  assessment: FleetAssessmentSummary | null;
}) {
  const [query, setQuery] = useState("");
  const [avail, setAvail] = useState("ALL");
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortAsc, setSortAsc] = useState(true);
  const [openId, setOpenId] = useState<number | null>(null);

  const classificationOf = useMemo(() => {
    const m: Record<number, string> = {};
    for (const a of assessment?.assessments ?? []) m[a.vehicle_id] = a.classification;
    return m;
  }, [assessment]);

  const availabilities = useMemo(
    () => Array.from(new Set(vehicles.map((v) => v.availability))).sort(),
    [vehicles],
  );

  const rows = useMemo(() => {
    let out = vehicles.filter((v) => {
      if (avail !== "ALL" && v.availability !== avail) return false;
      if (!query.trim()) return true;
      const hay = `${v.asset_id} ${v.make} ${v.model} ${v.depot} ${v.notes}`.toLowerCase();
      return hay.includes(query.trim().toLowerCase());
    });
    if (sortKey) {
      out = [...out].sort((x, y) => {
        let cmp = 0;
        if (sortKey === "asset_id") cmp = x.asset_id.localeCompare(y.asset_id);
        else if (sortKey === "model_year") cmp = (x.model_year ?? 0) - (y.model_year ?? 0);
        else if (sortKey === "depot") cmp = x.depot.localeCompare(y.depot);
        else if (sortKey === "capacity") cmp = x.passenger_capacity - y.passenger_capacity;
        else if (sortKey === "availability") cmp = x.availability.localeCompare(y.availability);
        return sortAsc ? cmp : -cmp;
      });
    }
    return out;
  }, [vehicles, avail, query, sortKey, sortAsc]);

  function th(label: string, key?: SortKey) {
    const active = key && sortKey === key;
    return (
      <th className={`table-th ${key ? "cursor-pointer select-none hover:text-ink-900" : ""}`}
          onClick={key ? () => { if (active) setSortAsc(!sortAsc); else { setSortKey(key); setSortAsc(true); } } : undefined}>
        {label}
        {active && <span className="ml-1 text-[9px]">{sortAsc ? "▲" : "▼"}</span>}
      </th>
    );
  }

  return (
    <div className="space-y-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <input
          className="input h-8 w-52 text-[12.5px]"
          placeholder="Search asset, make, depot…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select className="input h-8 w-52 text-[12.5px]" value={avail} onChange={(e) => setAvail(e.target.value)}>
          <option value="ALL">All availabilities</option>
          {availabilities.map((a) => (
            <option key={a} value={a}>{a.replaceAll("_", " ")}</option>
          ))}
        </select>
        <span className="ml-auto text-[11px] text-ink-400">{rows.length} of {vehicles.length} shown</span>
      </div>
      <div className="overflow-x-auto rounded-lg border border-slate-200">
        <table className="w-full text-[12.5px]">
          <thead>
            <tr>
              {th("Asset ID", "asset_id")}
              {th("Vehicle")}
              {th("MY", "model_year")}
              {th("Built")}
              {th("Brake code")}
              {th("Depot", "depot")}
              {th("Seats", "capacity")}
              {th("Availability", "availability")}
              {th("Recall classification")}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={9} className="table-td text-center text-ink-400">No vehicles match the current filter.</td></tr>
            )}
            {rows.map((v) => (
              <VehicleRegisterRow
                key={v.id}
                v={v}
                classification={classificationOf[v.id]}
                open={openId === v.id}
                onToggle={() => setOpenId(openId === v.id ? null : v.id)}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function VehicleRegisterRow({
  v,
  classification,
  open,
  onToggle,
}: {
  v: Vehicle;
  classification: string | undefined;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr className={`cursor-pointer ${open ? "bg-indigo-50/50" : "hover:bg-slate-50/60"}`} onClick={onToggle}>
        <td className="table-td font-mono text-[11.5px] whitespace-nowrap">
          {v.asset_id}
          {v.notes && <span className="block font-sans text-[10.5px] text-ink-400 max-w-[180px] truncate" title={v.notes}>{v.notes}</span>}
        </td>
        <td className="table-td whitespace-nowrap">{v.make} {v.model}</td>
        <td className="table-td tabular-nums">{v.model_year ?? "—"}</td>
        <td className="table-td tabular-nums whitespace-nowrap">{v.build_date ?? <span className="text-amber-600 font-semibold">missing</span>}</td>
        <td className="table-td font-mono text-[11.5px]">{v.brake_feature_code ?? <span className="text-amber-600 font-semibold">n/r</span>}</td>
        <td className="table-td">{v.depot}</td>
        <td className="table-td tabular-nums">{v.passenger_capacity}</td>
        <td className="table-td whitespace-nowrap">
          <StatusChip tone={AVAILABILITY_TONE[v.availability] ?? "slate"}>
            {v.availability.replaceAll("_", " ")}
          </StatusChip>
          {v.operational_status !== "IN_SERVICE" && (
            <span className="block text-[10.5px] text-ink-400 mt-0.5">{v.operational_status.replaceAll("_", " ").toLowerCase()}</span>
          )}
        </td>
        <td className="table-td whitespace-nowrap">
          {classification ? (
            <span className={`rounded-md border px-2 py-0.5 text-[10.5px] font-bold ${
              classification === "CAMPAIGN_CRITERIA_MATCH" ? "border-rose-200 bg-rose-50 text-rose-700"
              : classification === "VERIFICATION_REQUIRED" ? "border-amber-300 bg-amber-50 text-amber-800"
              : "border-slate-300 bg-slate-100 text-ink-600"
            }`}>
              {classification.replaceAll("_", " ")}
            </span>
          ) : (
            <span className="text-[11px] text-ink-400">not assessed</span>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={9} className="px-3 pb-3 pt-0 border-b border-slate-100 bg-slate-50/60">
            <div className="grid gap-3 rounded-lg border border-slate-200 bg-white p-3.5 md:grid-cols-2">
              <dl className="space-y-1.5 text-[12px]">
                <RegisterKV k="Synthetic data" v={v.synthetic ? "YES — demonstration record" : "no"} />
                <RegisterKV k="VIN status" v={v.vin_status.replaceAll("_", " ").toLowerCase()} />
                <RegisterKV k="VIN-like id" v={v.vin ?? "—"} mono />
                <RegisterKV k="Operational status" v={v.operational_status.replaceAll("_", " ").toLowerCase()} />
                <RegisterKV k="Role" v={v.role === "SPARE" ? "Spare" : "Route bus"} />
              </dl>
              <dl className="space-y-1.5 text-[12px]">
                <RegisterKV k="Equipment features" v={v.features.length ? v.features.map((f) => String(f).replaceAll("_", " ")).join(", ") : "none recorded"} />
                <RegisterKV k="Passenger capacity" v={String(v.passenger_capacity)} />
                <RegisterKV k="Depot" v={v.depot} />
                {v.notes && <RegisterKV k="Notes" v={v.notes} />}
              </dl>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function RegisterKV({ k, v, mono = false }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-3 border-b border-slate-50 pb-1 last:border-0">
      <dt className="text-ink-500">{k}</dt>
      <dd className={`text-right text-ink-800 font-medium ${mono ? "font-mono text-[11px]" : ""}`}>{v}</dd>
    </div>
  );
}
