import { useEffect, useState } from "react";
import { api, errorDetail } from "../api";
import type { Incident, Vehicle, VinDecodeResult } from "../types";
import { Banner, Card, Spinner, StatusChip, fmtTime } from "../components/ui";

const AVAILABILITY_TONE: Record<string, "green" | "amber" | "red" | "slate" | "blue"> = {
  AVAILABLE: "green",
  ASSIGNED_TO_ROUTE: "blue",
  IN_MAINTENANCE: "slate",
  GROUNDED_RECALL: "red",
  HELD_PENDING_VERIFICATION: "amber",
};

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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [vin, setVin] = useState("");
  const [vinYear, setVinYear] = useState("");
  const [decode, setDecode] = useState<VinDecodeResult | null>(null);
  const [decodeBusy, setDecodeBusy] = useState(false);
  const [decodeError, setDecodeError] = useState("");

  useEffect(() => {
    api.vehicles().then(setVehicles).catch((e) => setError(errorDetail(e))).finally(() => setLoading(false));
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
          <Card title={`Route buses (${buses.length})`} subtitle={loading ? undefined : "Fleet register with live operational state"}>
            {loading ? <Spinner label="Loading fleet…" /> : <VehicleTable vehicles={buses} />}
          </Card>
          <Card title={`Spare vehicles (${spares.length})`} subtitle="Replacement pool considered by the recovery engine">
            {loading ? <Spinner /> : <VehicleTable vehicles={spares} />}
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

function VehicleTable({ vehicles }: { vehicles: Vehicle[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200">
      <table className="w-full text-[12.5px]">
        <thead>
          <tr>
            <th className="table-th">Asset ID</th>
            <th className="table-th">Vehicle</th>
            <th className="table-th">MY</th>
            <th className="table-th">Built</th>
            <th className="table-th">Brake code</th>
            <th className="table-th">Depot</th>
            <th className="table-th">Seats</th>
            <th className="table-th">Availability</th>
            <th className="table-th">VIN status</th>
          </tr>
        </thead>
        <tbody>
          {vehicles.map((v) => (
            <tr key={v.id} className="hover:bg-slate-50/60">
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
              <td className="table-td whitespace-nowrap text-[11px] text-ink-500">
                {v.vin_status.replaceAll("_", " ").toLowerCase()}
                {v.vin && <span className="block font-mono text-[10px] text-amber-600" title="Synthetic VIN-like identifier — never submitted to external APIs">{v.vin}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
