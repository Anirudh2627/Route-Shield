import { useCallback, useEffect, useState } from "react";
import { api, clearSession, errorDetail, getToken, getUser, setSession } from "./api";
import type { Health, Incident } from "./types";
import IncidentCommand from "./views/IncidentCommand";
import FleetView from "./views/FleetView";
import RouteRecoveryView from "./views/RouteRecoveryView";
import EvidenceAuditView from "./views/EvidenceAuditView";
import { Spinner } from "./components/ui";

type View = "incident" | "fleet" | "recovery" | "evidence";

const NAV: Array<{ id: View; label: string; icon: JSX.Element }> = [
  {
    id: "incident",
    label: "Incident Command",
    icon: (
      <svg className="h-[18px] w-[18px]" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
        <path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z" strokeLinejoin="round" />
        <path d="M12 8v5M12 16h.01" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    id: "fleet",
    label: "Fleet",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
        <rect x="3" y="6" width="14" height="10" rx="2" />
        <path d="M17 9h2.5L22 12v4h-2M6 16v2M18 16v2" strokeLinecap="round" />
        <circle cx="7.5" cy="18.5" r="1.5" /><circle cx="17.5" cy="18.5" r="1.5" />
      </svg>
    ),
  },
  {
    id: "recovery",
    label: "Route Recovery",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
        <circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="18" r="2.5" />
        <path d="M8.5 6H14a3.5 3.5 0 010 7H9a3.5 3.5 0 000 5h6.5" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    id: "evidence",
    label: "Evidence & Audit",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
        <path d="M7 3h7l5 5v13a1 1 0 01-1 1H7a1 1 0 01-1-1V4a1 1 0 011-1z" strokeLinejoin="round" />
        <path d="M14 3v5h5M9 13h6M9 17h6" strokeLinecap="round" />
      </svg>
    ),
  },
];

function LoginScreen({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("fleet.manager");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await api.login(username, password);
      setSession(res.token, { username: res.username, display_name: res.display_name, role: res.role });
      onLogin();
    } catch (err) {
      setError(errorDetail(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-b from-slate-100 to-slate-200 p-6">
      <form onSubmit={submit} className="card w-full max-w-md p-8">
        <div className="flex items-center gap-3 mb-1">
          <div className="h-10 w-10 rounded-lg bg-indigo-600 text-white flex items-center justify-center font-bold text-lg">R</div>
          <div>
            <h1 className="text-lg font-bold text-ink-900">RouteShield</h1>
            <p className="text-xs text-ink-500">Recall Response & Operational Recovery</p>
          </div>
        </div>
        <p className="mt-4 mb-6 text-[13px] text-ink-600">
          Sign in with the fleet manager account to access operational decisions. Read-only
          incident data does not require a session.
        </p>
        <label className="block text-xs font-semibold text-ink-600 mb-1">Username</label>
        <input className="input mb-4" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
        <label className="block text-xs font-semibold text-ink-600 mb-1">Password</label>
        <input className="input mb-6" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
               placeholder="Set via ROUTESHIELD_MANAGER_PASSWORD" />
        {error && <p className="mb-4 rounded-md bg-rose-50 border border-rose-200 px-3 py-2 text-[13px] text-rose-700">{error}</p>}
        <button className="btn-primary w-full" disabled={busy}>
          {busy ? <Spinner label="Signing in…" /> : "Sign in as Fleet Manager"}
        </button>
        <button type="button" className="btn-ghost w-full mt-2" onClick={onLogin}>
          Continue in read-only mode
        </button>
      </form>
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState<boolean>(Boolean(getToken()));
  const [showLogin, setShowLogin] = useState(false);
  const [view, setView] = useState<View>("incident");
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [incident, setIncident] = useState<Incident | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [loadError, setLoadError] = useState("");

  const reloadIncidents = useCallback(async () => {
    try {
      const list = await api.incidents();
      setIncidents(list);
      setIncident((prev) => (prev ? list.find((i) => i.id === prev.id) ?? list[0] ?? null : list[0] ?? null));
      setLoadError("");
    } catch (err) {
      setLoadError(errorDetail(err));
    }
  }, []);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    reloadIncidents();
    const t = setInterval(() => api.health().then(setHealth).catch(() => {}), 30000);
    return () => clearInterval(t);
  }, [reloadIncidents]);

  // Global 401 handling: any ApiError 401 clears the session.
  useEffect(() => {
    const handler = () => {
      clearSession();
      setAuthed(false);
      setShowLogin(true);
    };
    window.addEventListener("routeshield:unauthorized", handler);
    return () => window.removeEventListener("routeshield:unauthorized", handler);
  }, []);

  const user = getUser();

  if (showLogin && !authed) {
    return (
      <LoginScreen
        onLogin={() => {
          setShowLogin(false);
          setAuthed(Boolean(getToken()));
          reloadIncidents();
        }}
      />
    );
  }

  return (
    <div className="min-h-screen flex">
      {/* Sidebar */}
      <aside className="w-60 shrink-0 bg-ink-950 text-slate-300 flex flex-col sticky top-0 h-screen">
        <div className="flex items-center gap-2.5 px-5 py-5 border-b border-white/10">
          <div className="h-8 w-8 rounded-md bg-indigo-500 text-white flex items-center justify-center font-bold">R</div>
          <div>
            <div className="text-white font-bold leading-tight">RouteShield</div>
            <div className="text-[10px] text-slate-400 tracking-wide">FLEET SAFETY · RECALL OPS</div>
          </div>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-1">
          {NAV.map((n) => (
            <button
              key={n.id}
              onClick={() => setView(n.id)}
              className={`w-full flex items-center gap-3 rounded-lg px-3 py-2.5 text-[13px] font-medium transition-colors
                ${view === n.id ? "bg-indigo-600/90 text-white" : "text-slate-300 hover:bg-white/5 hover:text-white"}`}
            >
              {n.icon}
              {n.label}
            </button>
          ))}
        </nav>
        <div className="px-5 py-4 border-t border-white/10 text-[11px] space-y-2">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${health?.status === "ok" ? "bg-emerald-400" : "bg-rose-400"}`} />
            <span className="text-slate-400">
              API {health ? health.status : "…"}
              {health && <span className="text-slate-500"> · v{health.version}</span>}
            </span>
          </div>
          <div className="text-slate-500">
            LLM: {health ? (health.llm_configured ? "configured (extraction only)" : "not configured") : "…"}
          </div>
          {user ? (
            <div className="flex items-center justify-between gap-2 pt-1">
              <span className="text-slate-300 font-medium truncate">{user.username}</span>
              <button className="text-slate-400 hover:text-white underline underline-offset-2"
                      onClick={() => { clearSession(); setAuthed(false); setShowLogin(true); }}>
                sign out
              </button>
            </div>
          ) : (
            <button className="text-indigo-300 hover:text-indigo-200 underline underline-offset-2"
                    onClick={() => setShowLogin(true)}>
              manager sign in
            </button>
          )}
        </div>
      </aside>

      {/* Main */}
      <div className="flex-1 min-w-0">
        <div className="border-b border-amber-300 bg-amber-100/90 px-6 py-1.5 text-center text-[11.5px] font-bold tracking-wide text-amber-900">
          SYNTHETIC DEMONSTRATION DATA — NOT REAL VEHICLES · HACKATHON MVP · NEVER OPERATE A REAL FLEET ON THIS DATA
        </div>
        {loadError && (
          <div className="border-b border-rose-200 bg-rose-50 px-6 py-2 text-[13px] text-rose-700">
            Backend unreachable: {loadDetail(loadError)} — start the API server (see README).
          </div>
        )}
        <main className="p-6 max-w-[1500px] mx-auto">
          {view === "incident" && (
            <IncidentCommand
              incidents={incidents}
              incident={incident}
              authed={authed}
              onIncidentChanged={reloadIncidents}
              onRequireAuth={() => setShowLogin(true)}
            />
          )}
          {view === "fleet" && <FleetView incident={incident} authed={authed} onRequireAuth={() => setShowLogin(true)} />}
          {view === "recovery" && (
            <RouteRecoveryView
              incident={incident}
              authed={authed}
              onIncidentChanged={reloadIncidents}
              onRequireAuth={() => setShowLogin(true)}
            />
          )}
          {view === "evidence" && <EvidenceAuditView incident={incident} />}
        </main>
      </div>
    </div>
  );
}

function loadDetail(e: string): string {
  return e.length > 120 ? e.slice(0, 120) + "…" : e;
}
