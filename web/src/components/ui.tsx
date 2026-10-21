import clsx, { type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import { AlertTriangle, CheckCircle2, Info, Lightbulb, Loader2, X, XCircle } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ago, bytes, clock, compact, duration, initials, titleCase } from "../lib/format";
import { serverColor } from "../lib/hooks";
import type { Env, ServerStatus, Severity, Table } from "../lib/types";

/** Class names with Tailwind conflicts resolved: a caller's `w-40` replaces the component's `w-full`. */
export const cn = (...classes: ClassValue[]) => twMerge(clsx(classes));

// ------------------------------------------------------------------------------------------------ buttons
type Variant = "primary" | "arc" | "lamp" | "ghost" | "outline" | "danger";
export function Button({ variant = "outline", size = "md", loading, className, children, ...rest }:
  ComponentProps<"button"> & { variant?: Variant; size?: "sm" | "md"; loading?: boolean }) {
  return (
    <button
      {...rest}
      disabled={rest.disabled || loading}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-lg font-semibold transition-colors select-none whitespace-nowrap disabled:opacity-55",
        size === "sm" ? "h-7 px-2.5 text-[12.5px]" : "h-9 px-3.5 text-[13.5px]",
        variant === "primary" && "bg-ink text-surface hover:opacity-90",
        variant === "arc" && "bg-arc text-on-arc hover:bg-arc-ink",
        variant === "lamp" && "bg-lamp text-[#2a1c00] hover:brightness-95",
        variant === "ghost" && "text-ink-2 hover:bg-surface-3 hover:text-ink",
        variant === "outline" && "border border-line-strong bg-surface text-ink hover:bg-surface-2",
        variant === "danger" && "border border-crit/40 bg-surface text-crit hover:bg-crit-soft",
        className,
      )}
    >
      {loading && <Loader2 className="size-3.5 animate-spin" />}
      {children}
    </button>
  );
}

export function IconButton({ label, className, children, ...rest }: ComponentProps<"button"> & { label: string }) {
  return (
    <button {...rest} aria-label={label} title={label}
      className={cn("inline-flex size-8 items-center justify-center rounded-lg text-ink-2 hover:bg-surface-3 hover:text-ink", className)}>
      {children}
    </button>
  );
}

// ------------------------------------------------------------------------------------------------ surfaces
export function Card({ className, children, ...rest }: ComponentProps<"div">) {
  return <div {...rest} className={cn("rounded-xl border border-line bg-surface shadow-card", className)}>{children}</div>;
}

export function CardHeader({ title, sub, actions, className }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex items-start justify-between gap-3 px-4 pt-3.5 pb-2", className)}>
      <div className="min-w-0">
        <div className="plate">{title}</div>
        {sub && <div className="mt-0.5 text-[12.5px] text-ink-3">{sub}</div>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
    </div>
  );
}

export function Plate({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("plate", className)}>{children}</div>;
}

export function PageHeader({ title, sub, actions, eyebrow }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        {eyebrow && <div className="plate mb-1">{eyebrow}</div>}
        <h1 className="display text-[27px] leading-tight text-ink">{title}</h1>
        {sub && <p className="mt-1 max-w-3xl text-[13.5px] text-ink-2">{sub}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ badges
type Tone = "neutral" | "arc" | "lamp" | "ok" | "warn" | "crit" | "info";
const TONES: Record<Tone, string> = {
  neutral: "bg-surface-3 text-ink-2 border-line",
  arc: "bg-arc-soft text-arc-ink border-arc-line",
  lamp: "bg-lamp-soft text-lamp-ink border-lamp-line",
  ok: "bg-ok-soft text-ok border-ok/25",
  warn: "bg-warn-soft text-warn border-warn/25",
  crit: "bg-crit-soft text-crit border-crit/25",
  info: "bg-info-soft text-info border-info/25",
};
export function Badge({ tone = "neutral", children, className, title }: { tone?: Tone; children: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={cn("inline-flex items-center gap-1 rounded-md border px-1.5 py-[1px] text-[11.5px] font-semibold whitespace-nowrap", TONES[tone], className)}>
      {children}
    </span>
  );
}

const SEV: Record<Severity, { tone: Tone; label: string }> = {
  sev1: { tone: "crit", label: "SEV1" },
  sev2: { tone: "warn", label: "SEV2" },
  sev3: { tone: "info", label: "SEV3" },
  sev4: { tone: "neutral", label: "SEV4" },
};
export function SeverityBadge({ severity }: { severity: Severity }) {
  const s = SEV[severity] ?? SEV.sev4;
  return <Badge tone={s.tone} className="font-mono tracking-wide" title={`Severity ${s.label.slice(3)}`}>{s.label}</Badge>;
}

const STATUS: Record<ServerStatus, { color: string; label: string }> = {
  healthy: { color: "var(--ok)", label: "Healthy" },
  warning: { color: "var(--warn)", label: "Warning" },
  critical: { color: "var(--crit)", label: "Critical" },
  unreachable: { color: "var(--crit)", label: "Unreachable" },
  paused: { color: "var(--ink-3)", label: "Paused" },
  unknown: { color: "var(--ink-3)", label: "Waiting for data" },
};
export function StatusDot({ status, className }: { status: ServerStatus; className?: string }) {
  const s = STATUS[status] ?? STATUS.unknown;
  return (
    <span role="img" className={cn("relative inline-flex size-2.5 shrink-0", className)} title={s.label} aria-label={s.label}>
      {(status === "critical" || status === "unreachable") && <span className="absolute inset-0 rounded-full opacity-40 motion-safe:animate-ping" style={{ background: s.color }} />}
      <span className="relative inline-block size-2.5 rounded-full" style={{ background: s.color }} />
    </span>
  );
}
export function StatusLabel({ status }: { status: ServerStatus }) {
  const s = STATUS[status] ?? STATUS.unknown;
  return (
    <span className="inline-flex items-center gap-1.5 text-[12.5px] font-semibold" style={{ color: status === "healthy" ? "var(--ok)" : status === "paused" || status === "unknown" ? "var(--ink-3)" : s.color }}>
      <StatusDot status={status} />
      {s.label}
    </span>
  );
}

export function EnvBadge({ env }: { env: Env }) {
  const map: Record<Env, { tone: Tone; label: string }> = { production: { tone: "neutral", label: "Production" }, staging: { tone: "info", label: "Staging" }, development: { tone: "neutral", label: "Development" } };
  const e = map[env] ?? map.production;
  return <Badge tone={e.tone} className={env === "production" ? "border-ink/15 bg-ink/[0.06] text-ink" : undefined}>{e.label}</Badge>;
}

export function RiskBadge({ risk }: { risk: string }) {
  return <Badge tone={risk === "high" ? "crit" : risk === "medium" ? "warn" : "neutral"}>{titleCase(risk)} risk</Badge>;
}

export function ServerTag({ name, slot, className }: { name: string; slot: number | undefined; className?: string }) {
  return (
    <span className={cn("inline-flex min-w-0 items-center gap-1.5 font-semibold text-ink", className)}>
      <span className="size-2 shrink-0 rounded-[3px]" style={{ background: serverColor(slot) }} />
      <span className="truncate">{name}</span>
    </span>
  );
}

export function IssueChip({ title, kind }: { title: string; kind: "issue" | "suggestion" }) {
  return kind === "issue" ? (
    <span className="inline-flex max-w-full items-center gap-1 rounded-full border border-crit/25 bg-crit-soft px-2 py-[1px] text-[11.5px] font-semibold text-crit" title={title}>
      <AlertTriangle className="size-3 shrink-0" />
      <span className="truncate">{title}</span>
    </span>
  ) : (
    <span className="inline-flex max-w-full items-center gap-1 rounded-full border border-line bg-surface-2 px-2 py-[1px] text-[11.5px] font-medium text-ink-2" title={title}>
      <Lightbulb className="size-3 shrink-0 text-lamp" />
      <span className="truncate">{title}</span>
    </span>
  );
}

export function Avatar({ name, size = 26, tone = "neutral" }: { name: string; size?: number; tone?: "neutral" | "arc" }) {
  return (
    <span
      className={cn("inline-flex shrink-0 items-center justify-center rounded-full font-semibold", tone === "arc" ? "bg-arc text-on-arc" : "bg-surface-3 text-ink-2 border border-line")}
      style={{ width: size, height: size, fontSize: size * 0.4 }}
      title={name}
    >
      {initials(name)}
    </span>
  );
}

// ------------------------------------------------------------------------------------------------ inputs
export function Switch({ checked, onChange, label, disabled, size = "md" }: { checked: boolean; onChange: (v: boolean) => void; label?: string; disabled?: boolean; size?: "sm" | "md" }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn("relative inline-flex shrink-0 items-center rounded-full transition-colors disabled:opacity-50", size === "sm" ? "h-4 w-7" : "h-5 w-9",
        checked ? "bg-arc" : "bg-line-strong")}
    >
      <span className={cn("inline-block rounded-full bg-white shadow transition-transform", size === "sm" ? "size-3" : "size-4", checked ? (size === "sm" ? "translate-x-3.5" : "translate-x-[18px]") : "translate-x-0.5")} />
    </button>
  );
}

export function Field({ label, hint, error, children, className }: { label: ReactNode; hint?: ReactNode; error?: string | null; children: ReactNode; className?: string }) {
  return (
    <label className={cn("block", className)}>
      <span className="mb-1 block text-[12.5px] font-semibold text-ink-2">{label}</span>
      {children}
      {hint && !error && <span className="mt-1 block text-[12px] text-ink-3">{hint}</span>}
      {error && <span className="mt-1 block text-[12px] font-medium text-crit">{error}</span>}
    </label>
  );
}

const inputCls = "w-full rounded-lg border border-line-strong bg-surface px-3 py-2 text-[13.5px] text-ink placeholder:text-ink-3 focus:border-arc focus:outline-none focus:ring-2 focus:ring-arc/20";
export const Input = (p: ComponentProps<"input">) => <input {...p} className={cn(inputCls, "h-9 py-0", p.className)} />;
export const Textarea = (p: ComponentProps<"textarea">) => <textarea {...p} className={cn(inputCls, p.className)} />;
export const Select = (p: ComponentProps<"select">) => <select {...p} className={cn(inputCls, "h-9 py-0 pr-8", p.className)} />;

export function Tabs<T extends string>({ value, onChange, tabs, className }: { value: T; onChange: (v: T) => void; tabs: { id: T; label: ReactNode; count?: number }[]; className?: string }) {
  return (
    <div role="tablist" className={cn("flex gap-1 overflow-x-auto border-b border-line", className)}>
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)}
          className={cn("-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-[13.5px] font-semibold whitespace-nowrap transition-colors",
            value === t.id ? "border-arc text-ink" : "border-transparent text-ink-3 hover:text-ink-2")}>
          {t.label}
          {t.count != null && <span className="rounded-full bg-surface-3 px-1.5 text-[11px] text-ink-2">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Segmented<T extends string>({ value, onChange, options, size = "md" }: { value: T; onChange: (v: T) => void; options: { id: T; label: ReactNode }[]; size?: "sm" | "md" }) {
  return (
    <div className="inline-flex rounded-lg border border-line bg-surface-2 p-0.5">
      {options.map((o) => (
        <button key={o.id} onClick={() => onChange(o.id)} aria-pressed={value === o.id}
          className={cn("rounded-md font-semibold transition-colors", size === "sm" ? "px-2 py-0.5 text-[12px]" : "px-3 py-1 text-[13px]",
            value === o.id ? "bg-surface text-ink shadow-card" : "text-ink-3 hover:text-ink-2")}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ overlays
export function Drawer({ open, onClose, title, sub, children, footer, width = 560 }: { open: boolean; onClose: () => void; title: ReactNode; sub?: ReactNode; children: ReactNode; footer?: ReactNode; width?: number }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-ink/25 backdrop-blur-[1px]" onClick={onClose} />
      <aside className="rise relative flex h-full flex-col border-l border-line bg-surface shadow-pop" style={{ width: `min(${width}px, 100vw)` }} role="dialog" aria-modal="true">
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <div className="display text-[19px] leading-tight">{title}</div>
            {sub && <div className="mt-0.5 text-[12.5px] text-ink-3">{sub}</div>}
          </div>
          <IconButton label="Close" onClick={onClose}><X className="size-4" /></IconButton>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </aside>
    </div>,
    document.body,
  );
}

export function Modal({ open, onClose, title, children, footer, width = 480 }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; footer?: ReactNode; width?: number }) {
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-ink/30 backdrop-blur-[1px]" onClick={onClose} />
      <div className="rise relative w-full rounded-xl border border-line bg-surface shadow-pop" style={{ maxWidth: width }} role="dialog" aria-modal="true">
        <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <div className="display text-[18px]">{title}</div>
          <IconButton label="Close" onClick={onClose}><X className="size-4" /></IconButton>
        </div>
        <div className="px-5 py-4">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

// ------------------------------------------------------------------------------------------------ feedback
type Toast = { id: number; tone: "ok" | "crit" | "info"; text: string };
const ToastCtx = createContext<(text: string, tone?: Toast["tone"]) => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const n = useRef(0);
  const push = useCallback((text: string, tone: Toast["tone"] = "ok") => {
    const id = ++n.current;
    setItems((x) => [...x, { id, tone, text }]);
    window.setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), 4200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed right-4 bottom-4 z-[60] flex w-[360px] flex-col gap-2" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className="rise pointer-events-auto flex items-start gap-2.5 rounded-xl border border-line bg-surface px-3.5 py-3 text-[13.5px] shadow-pop">
            {t.tone === "ok" ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ok" /> : t.tone === "crit" ? <XCircle className="mt-0.5 size-4 shrink-0 text-crit" /> : <Info className="mt-0.5 size-4 shrink-0 text-info" />}
            <span className="text-ink">{t.text}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-4 animate-spin text-ink-3", className)} />;
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 px-4 py-10 text-[13px] text-ink-3">
      <Spinner /> {label}
    </div>
  );
}

export function EmptyState({ icon, title, children, action }: { icon?: ReactNode; title: ReactNode; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      {icon && <div className="mb-3 text-ink-3">{icon}</div>}
      <div className="text-[14.5px] font-semibold text-ink">{title}</div>
      {children && <div className="mt-1 max-w-md text-[13px] text-ink-3">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div className="flex items-start gap-2 rounded-lg border border-crit/25 bg-crit-soft px-3 py-2 text-[13px] text-crit">
      <XCircle className="mt-0.5 size-4 shrink-0" /> {(error as Error).message ?? String(error)}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ text
export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("md", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}

export function Time({ iso, mode = "ago" }: { iso: string | null | undefined; mode?: "ago" | "clock" | "date" }) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (mode !== "ago") return;
    const t = window.setInterval(() => tick((x) => x + 1), 15_000);
    return () => window.clearInterval(t);
  }, [mode]);
  if (!iso) return <span className="text-ink-3">—</span>;
  const full = new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "medium" });
  return <time dateTime={iso} title={full}>{mode === "ago" ? ago(iso) : clock(iso, mode === "date")}</time>;
}

export function Stat({ label, value, sub, tone, icon }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "lamp" | "arc" | "crit" | "ok"; icon?: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1.5 text-[12.5px] font-medium text-ink-3">{icon}{label}</div>
      <div className={cn("mt-0.5 text-[26px] leading-tight font-semibold tracking-tight",
        tone === "lamp" ? "text-lamp-ink" : tone === "arc" ? "text-arc-ink" : tone === "crit" ? "text-crit" : tone === "ok" ? "text-ok" : "text-ink")}>{value}</div>
      {sub && <div className="mt-0.5 truncate text-[12px] text-ink-3">{sub}</div>}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ data tables (tool output)
function cell(col: string, v: any): ReactNode {
  if (v == null) return <span className="text-ink-3">—</span>;
  if (Array.isArray(v)) return v.length ? v.join(", ") : <span className="text-ink-3">—</span>;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") {
    if (/bytes$/.test(col) || col === "size_bytes" || col === "retained_bytes") return bytes(v);
    if (/_s$/.test(col) || col.endsWith("_age_s")) return duration(v);
    if (col.endsWith("_ms")) return `${compact(v)} ms`;
    if (col.endsWith("_pct") || col === "pct_of_limit") return `${v}%`;
    if (col === "pid") return v;
    return compact(v);
  }
  const s = String(v);
  if (/^\d{4}-\d\d-\d\dT/.test(s)) return <Time iso={s} />;
  return s;
}

/** Friendlier headers for the columns the PostgreSQL views return (pg_stat_activity, pg_stat_statements, pg_stat_user_tables...). */
const COLUMN_LABELS: Record<string, string> = {
  usename: "Role", datname: "Database", application_name: "Application", client_addr: "Client", wait_event_type: "Wait type", wait_event: "Wait event",
  backend_age_s: "Connected for", xact_age_s: "Transaction age", query_age_s: "Query age", blocked_by: "Blocked by", blocks: "Blocks",
  size_bytes: "Size", numbackends: "Connections", xid_age: "XID age", mxid_age: "MultiXact age", xact_commit: "Commits", xact_rollback: "Rollbacks",
  temp_bytes: "Temp files", hit_pct: "Cache hit", pct_of_limit: "Of the limit", table_name: "Table", n_live_tup: "Live rows", n_dead_tup: "Dead rows",
  dead_pct: "Dead", total_bytes: "Size", last_vacuum: "Last vacuum", last_analyze: "Last analyze", n_mod_since_analyze: "Changed since analyze",
  seq_scan: "Seq scans", idx_scan: "Index scans", queryid: "Query ID", total_ms: "Total time", mean_ms: "Mean time", temp_blks_written: "Temp blocks",
  slot_name: "Slot", slot_type: "Type", wal_status: "WAL status", retained_bytes: "WAL retained", logical_lag_bytes: "Logical lag", sync_state: "Sync",
  send_lag_bytes: "Send lag", replay_lag_bytes: "Replay lag", replay_lag_s: "Replay delay", sender_host: "Primary", sender_port: "Port",
  last_msg_receipt_time: "Last message", latest_end_lsn: "Received up to", pending_restart: "Waits for restart", sourcefile: "File", sourceline: "Line",
  short_desc: "Description", xmin_age: "Xmin age", age_s: "Age", pid: "PID",
};
const colLabel = (c: string) => COLUMN_LABELS[c] ?? titleCase(c);

export function DataTable({ data, maxRows = 200, dense, highlight, rowActions, hide }: {
  data: Table | undefined; maxRows?: number; dense?: boolean; highlight?: (row: any[]) => boolean; rowActions?: (row: any[]) => ReactNode; hide?: string[];
}) {
  const cols = data?.columns ?? [];
  const rows = useMemo(() => (data?.rows ?? []).slice(0, maxRows), [data, maxRows]);
  if (!data) return null;
  if (!cols.length) return <div className="px-3 py-4 text-[13px] text-ink-3">{data.note || "No rows."}</div>;
  return (
    <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="Table">
      <table className="w-full border-collapse text-left">
        <thead>
          <tr>
            {cols.map((c) => !hide?.includes(c) && (
              <th key={c} className="sticky top-0 border-b border-line bg-surface-2 px-3 py-1.5 text-[11.5px] font-semibold whitespace-nowrap text-ink-3">{colLabel(c)}</th>
            ))}
            {rowActions && <th className="sticky top-0 border-b border-line bg-surface-2 px-3 py-1.5" />}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr><td colSpan={cols.length + 1} className="px-3 py-3 text-[12.5px] text-ink-3">{data.note || "Nothing to show."}</td></tr>
          )}
          {rows.map((r, i) => (
            <tr key={i} className={cn("border-b border-line/70 last:border-0", highlight?.(r) && "bg-lamp-soft/60")}>
              {r.map((v, j) => !hide?.includes(cols[j]!) && (
                <td key={j} className={cn("px-3 align-top text-[12.5px]", dense ? "py-1" : "py-1.5",
                  cols[j] === "query" || cols[j] === "detail" ? "max-w-[520px] font-mono text-[11.5px] text-ink-2" : "whitespace-nowrap num")}>
                  <span className={cols[j] === "query" ? "line-clamp-2" : undefined}>{cell(cols[j]!, v)}</span>
                </td>
              ))}
              {rowActions && <td className={cn("px-3 text-right align-top whitespace-nowrap", dense ? "py-1" : "py-1")}>{rowActions(r)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      {data.note && rows.length > 0 && <div className="px-3 py-2 text-[12px] text-ink-3">{data.note}</div>}
    </div>
  );
}

export function KeyValue({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(110px,auto)_1fr] gap-x-4 gap-y-1.5 text-[13px]">
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-ink-3">{k}</dt>
          <dd className="min-w-0 break-words text-ink">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
