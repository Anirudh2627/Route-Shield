/* Shared UI primitives - consistent enterprise look, no external assets. */

import type { ReactNode } from "react";
import type { Classification } from "../types";

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-ink-500">
      <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
      </svg>
      {label}
    </span>
  );
}

export function Card({
  title,
  subtitle,
  right,
  children,
  className = "",
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || right) && (
        <header className="flex items-start justify-between gap-4 px-5 pt-4 pb-3 border-b border-slate-100">
          <div>
            {title && <h2 className="panel-title">{title}</h2>}
            {subtitle && <p className="mt-1 text-xs text-ink-500 max-w-3xl">{subtitle}</p>}
          </div>
          {right && <div className="shrink-0">{right}</div>}
        </header>
      )}
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

export function Banner({
  tone,
  children,
  icon,
}: {
  tone: "info" | "warn" | "danger" | "success" | "neutral";
  children: ReactNode;
  icon?: ReactNode;
}) {
  const tones = {
    info: "bg-sky-50 border-sky-200 text-sky-900",
    warn: "bg-amber-50 border-amber-300 text-amber-900",
    danger: "bg-rose-50 border-rose-200 text-rose-900",
    success: "bg-emerald-50 border-emerald-200 text-emerald-900",
    neutral: "bg-slate-50 border-slate-200 text-ink-700",
  } as const;
  return (
    <div className={`flex items-start gap-2.5 rounded-lg border px-3.5 py-2.5 text-[13px] leading-snug ${tones[tone]}`}>
      {icon && <span className="mt-px shrink-0">{icon}</span>}
      <div>{children}</div>
    </div>
  );
}

const CLASSIFICATION_STYLES: Record<Classification, { cls: string; label: string; title: string }> = {
  CAMPAIGN_CRITERIA_MATCH: {
    cls: "bg-rose-50 text-rose-700 border-rose-200",
    label: "CAMPAIGN CRITERIA MATCH",
    title:
      "All available required campaign criteria match. NOT a VIN-level confirmation of an open recall.",
  },
  VERIFICATION_REQUIRED: {
    cls: "bg-amber-50 text-amber-800 border-amber-300",
    label: "VERIFICATION REQUIRED",
    title: "Critical evidence missing or ambiguous — the system abstains. Absence of evidence is not evidence of safety.",
  },
  OUTSIDE_REPORT_VERSION: {
    cls: "bg-slate-100 text-ink-600 border-slate-300",
    label: "OUTSIDE REPORT VERSION",
    title: "Outside this report version's criteria. This does NOT mean the vehicle is safe.",
  },
};

export function ClassificationBadge({ c }: { c: Classification }) {
  const s = CLASSIFICATION_STYLES[c];
  return (
    <span
      title={s.title}
      className={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[10.5px] font-bold tracking-wide whitespace-nowrap ${s.cls}`}
    >
      {c === "CAMPAIGN_CRITERIA_MATCH" && <span className="h-1.5 w-1.5 rounded-full bg-rose-500 pulse-dot" />}
      {c === "VERIFICATION_REQUIRED" && <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />}
      {c === "OUTSIDE_REPORT_VERSION" && <span className="h-1.5 w-1.5 rounded-full bg-slate-400" />}
      {s.label}
    </span>
  );
}

export function StatusChip({
  tone = "slate",
  children,
}: {
  tone?: "slate" | "green" | "amber" | "red" | "blue" | "indigo";
  children: ReactNode;
}) {
  const tones = {
    slate: "bg-slate-100 text-ink-600 border-slate-200",
    green: "bg-emerald-50 text-emerald-700 border-emerald-200",
    amber: "bg-amber-50 text-amber-800 border-amber-200",
    red: "bg-rose-50 text-rose-700 border-rose-200",
    blue: "bg-sky-50 text-sky-700 border-sky-200",
    indigo: "bg-indigo-50 text-indigo-700 border-indigo-200",
  } as const;
  return (
    <span className={`inline-flex items-center rounded-md border px-2 py-0.5 text-[11px] font-semibold ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function StatTile({
  label,
  value,
  tone = "neutral",
  hint,
}: {
  label: string;
  value: ReactNode;
  tone?: "neutral" | "red" | "amber" | "slate" | "green";
  hint?: string;
}) {
  const tones = {
    neutral: "text-ink-900",
    red: "text-rose-600",
    amber: "text-amber-600",
    slate: "text-ink-500",
    green: "text-emerald-600",
  } as const;
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-4 py-3">
      <div className="text-[11px] font-semibold uppercase tracking-wider text-ink-500">{label}</div>
      <div className={`mt-1 text-2xl font-bold tabular-nums ${tones[tone]}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-ink-400">{hint}</div>}
    </div>
  );
}

export function KV({ k, v, mono }: { k: string; v: ReactNode; mono?: boolean }) {
  return (
    <div className="flex gap-3 py-1.5 border-b border-slate-50 last:border-0">
      <dt className="w-52 shrink-0 text-xs font-medium text-ink-500">{k}</dt>
      <dd className={`text-[13px] text-ink-800 ${mono ? "font-mono text-xs" : ""}`}>{v}</dd>
    </div>
  );
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export function shortHash(h: string | null | undefined, n = 12): string {
  return h ? `${h.slice(0, n)}…` : "—";
}

export function Check({ ok }: { ok: boolean | null }) {
  if (ok === null || ok === undefined) return <span className="text-ink-400">—</span>;
  return ok ? (
    <span className="inline-flex items-center gap-1 text-emerald-600 font-semibold">
      <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M16.7 5.3a1 1 0 010 1.4l-8 8a1 1 0 01-1.4 0l-4-4a1 1 0 111.4-1.4L8 12.6l7.3-7.3a1 1 0 011.4 0z" clipRule="evenodd" />
      </svg>
      pass
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-rose-600 font-semibold">
      <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M4.3 4.3a1 1 0 011.4 0L10 8.6l4.3-4.3a1 1 0 111.4 1.4L11.4 10l4.3 4.3a1 1 0 01-1.4 1.4L10 11.4l-4.3 4.3a1 1 0 01-1.4-1.4L8.6 10 4.3 5.7a1 1 0 010-1.4z" clipRule="evenodd" />
      </svg>
      fail
    </span>
  );
}
