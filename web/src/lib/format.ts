// Formatting shared by every page. Mirrors raiden/console/fmt.py so the UI and rAIden's notes read the same.
import type { MetricDef } from "./types";

export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return "—";
  const s = Math.round(seconds);
  if (s < 90) return `${s} s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m < 10 && r ? `${m} min ${r} s` : `${m} min`;
  }
  if (s < 86400 * 2) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h < 10 && m ? `${h} h ${m} min` : `${h} h`;
  }
  return `${(s / 86400).toFixed(1)} days`;
}

export function bytes(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let v = n;
  let i = 0;
  while (Math.abs(v) >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return i === 0 ? `${Math.round(v)} B` : `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

export function compact(n: number | null | undefined, digits = 1): string {
  if (n == null || !isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return `${(n / 1e9).toFixed(digits)}B`;
  if (a >= 1e6) return `${(n / 1e6).toFixed(digits)}M`;
  if (a >= 1e4) return `${(n / 1e3).toFixed(digits)}k`;
  if (a >= 100) return Math.round(n).toLocaleString("en-US");
  if (a >= 10) return n.toFixed(1).replace(/\.0$/, "");
  return n.toFixed(2).replace(/\.?0+$/, "") || "0";
}

export function money(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n === 0) return "$0";
  if (n < 0.01) return "<$0.01";
  if (n < 100) return `$${n.toFixed(2)}`;
  return `$${Math.round(n).toLocaleString("en-US")}`;
}

export function value(def: Pick<MetricDef, "unit"> | undefined, v: number | null | undefined): string {
  if (v == null || !isFinite(v)) return "—";
  switch (def?.unit) {
    case "percent":
      return v >= 10 ? `${Math.round(v)}%` : `${v.toFixed(1)}%`;
    case "seconds":
      return duration(v);
    case "bytes":
      return bytes(v);
    case "bytes_per_second":
      return `${bytes(v)}/s`;
    case "per_second":
      return `${compact(v)}/s`;
    case "count":
      return Math.abs(v) < 1e4 ? Math.round(v).toLocaleString("en-US") : compact(v);
    case "xids":
      return Math.abs(v) >= 1e9 ? `${(v / 1e9).toFixed(2)}B` : compact(v);
    default:
      return compact(v);
  }
}

export function axisValue(def: Pick<MetricDef, "unit"> | undefined, v: number): string {
  if (def?.unit === "percent") return `${Math.round(v)}%`;
  if (def?.unit === "bytes" || def?.unit === "bytes_per_second") return bytes(v).replace(".0 ", " ");
  if (def?.unit === "seconds") return v >= 3600 ? `${Math.round(v / 3600)}h` : v >= 120 ? `${Math.round(v / 60)}m` : `${Math.round(v)}s`;
  return compact(v, 0);
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  const s = Math.round((now - t) / 1000);
  if (s < 0) return `in ${duration(-s)}`;
  if (s < 10) return "just now";
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)} d ago`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export function until(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const s = Math.round((new Date(iso).getTime() - now) / 1000);
  if (s <= 0) return "now";
  return `in ${duration(s)}`;
}

export function clock(iso: string | number | null | undefined, withDate = false): string {
  if (iso == null) return "—";
  const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
  const time = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  return withDate ? `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} ${time}` : time;
}

export function since(a: string | null | undefined, b: string | null | undefined = null): number | null {
  if (!a) return null;
  return ((b ? new Date(b).getTime() : Date.now()) - new Date(a).getTime()) / 1000;
}

/** Where a server's chart starts: the range asked for, but not before the server was added (no empty hours on day one). */
export function chartStart(rangeStart: number | undefined, createdAt: string | null | undefined): number {
  const added = createdAt ? new Date(createdAt).getTime() / 1000 : 0;
  return Math.max(rangeStart ?? 0, added);
}

export function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, " ");
}

const CATEGORY: Record<string, string> = { cpu: "CPU", io: "I/O", wal: "WAL", xid: "Transaction IDs" };
/** A monitor or metric category as people say it. */
export const categoryLabel = (c: string) => CATEGORY[c] ?? titleCase(c);

export function initials(nameOrEmail: string): string {
  const base = nameOrEmail.includes("@") ? nameOrEmail.split("@")[0].replace(/[._-]/g, " ") : nameOrEmail;
  return base.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]!.toUpperCase()).join("") || "?";
}

export function personName(email: string | null | undefined): string {
  if (!email) return "someone";
  if (email === "raiden") return "rAIden";
  if (email === "autopilot") return "Autopilot";
  if (email === "system") return "System";
  if (!email.includes("@")) return email;
  const base = email.split("@")[0]!.replace(/[._-]/g, " ");
  return base.replace(/\b\w/g, (c) => c.toUpperCase());
}

const OPS: Record<string, string> = { ">": "above", ">=": "at or above", "<": "below", "<=": "at or below" };
const AGG: Record<string, string> = { avg: "average", max: "maximum", min: "minimum", last: "latest value" };

/** A monitor rule as a sentence: "CPU above 90% · average over 3 min". */
export function monitorRule(m: { metric: string; aggregation: string; operator: string; threshold: number; window_minutes: number; dimension?: string | null },
  def: Pick<MetricDef, "name" | "unit"> | undefined): string {
  // a 0/1 liveness metric reads as a sentence, not as "alive below 1"
  if (m.metric === "is_db_alive" && (m.operator === "<" || m.operator === "<=") && m.threshold <= 1) return `Does not answer · ${m.window_minutes} min`;
  return `${def?.name ?? m.metric}${m.dimension ? ` (${m.dimension})` : ""} ${OPS[m.operator] ?? m.operator} ${value(def, m.threshold)} · ${AGG[m.aggregation] ?? m.aggregation} over ${m.window_minutes} min`;
}
