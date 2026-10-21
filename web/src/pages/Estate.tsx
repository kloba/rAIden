import { Bot, Hand, Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { Meter, Sparkline } from "../components/charts";
import { PLATFORM_CARDS } from "../components/serverform";
import { STAGE_TITLE } from "../components/incident";
import { Badge, Card, cn, EmptyState, Input, IssueChip, Loading, PageHeader, Segmented, Select, ServerTag, StatusLabel } from "../components/ui";
import { value as fmt } from "../lib/format";
import { useEstate, useMe, useMetricDefs } from "../lib/hooks";
import type { Env, EstateRow, ServerStatus } from "../lib/types";

const STATUS_ORDER: Record<ServerStatus, number> = { critical: 0, unreachable: 0, warning: 1, unknown: 2, healthy: 3, paused: 4 };

function MetricCell({ row, k, spark }: { row: EstateRow; k: string; spark?: string }) {
  const defOf = useMetricDefs();
  const def = defOf(k);
  const v = row.metrics[k];
  const hot = def && v != null && (def.lower_is_worse ? def.warn != null && v <= def.warn : def.warn != null && v >= def.warn);
  return (
    <div className="flex items-center gap-2">
      {spark && <Sparkline values={row.spark[spark] ?? []} width={46} height={22} warn={!!hot} />}
      <span className={cn("w-11 text-right text-[12.5px] font-semibold whitespace-nowrap num", hot ? "text-warn" : "text-ink")}>{v == null ? "—" : fmt(def, v)}</span>
    </div>
  );
}

export function EstatePage() {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const chips = params.get("chips") ?? "";
  const [q, setQ] = useState("");
  const [env, setEnv] = useState<"all" | Env>("all");
  const [platform, setPlatform] = useState("");
  const estate = useEstate();
  const me = useMe();
  const nav = useNavigate();
  const rows = estate.data ?? [];
  const setParam = (k: string, v: string) => setParams((p) => { if (v) p.set(k, v); else p.delete(k); return p; });

  const filtered = useMemo(() => rows.filter((r) =>
    (env === "all" || r.environment === env) &&
    (!platform || r.platform === platform) &&
    (!status || (status === "critical" ? r.status === "critical" || r.status === "unreachable" : r.status === status)) &&
    (!chips || (chips === "issues" ? r.chips.issues.length > 0 : r.chips.suggestions.length > 0)) &&
    (!q || `${r.name} ${r.host} ${r.platform_label} ${Object.values(r.tags).join(" ")} ${r.owner}`.toLowerCase().includes(q.toLowerCase())),
  ).sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || a.name.localeCompare(b.name)), [rows, env, platform, status, chips, q]);

  const count = (f: (r: EstateRow) => boolean) => rows.filter(f).length;
  // CPU and storage come only from Azure Monitor or the host agent: no empty columns for a fleet that sends neither
  const hasCpu = rows.some((r) => r.metrics.cpu_percent != null);
  const hasStorage = rows.some((r) => r.metrics.storage_percent != null);
  const platforms = [...new Map(rows.map((r) => [r.platform, r.platform_label])).entries()];
  const filters: { id: string; label: string; n: number; tone: string; on: boolean; set: () => void }[] = [
    { id: "all", label: "All servers", n: rows.length, tone: "", on: !status && !chips, set: () => setParams({}) },
    { id: "critical", label: "Critical", n: count((r) => r.status === "critical" || r.status === "unreachable"), tone: "text-crit", on: status === "critical", set: () => setParams({ status: "critical" }) },
    { id: "warning", label: "Warning", n: count((r) => r.status === "warning"), tone: "text-warn", on: status === "warning", set: () => setParams({ status: "warning" }) },
    { id: "healthy", label: "Healthy", n: count((r) => r.status === "healthy"), tone: "text-ok", on: status === "healthy", set: () => setParams({ status: "healthy" }) },
    { id: "issues", label: "With issues", n: count((r) => r.chips.issues.length > 0), tone: "text-crit", on: chips === "issues", set: () => setParams({ chips: "issues" }) },
    { id: "suggestions", label: "With suggestions", n: count((r) => r.chips.suggestions.length > 0), tone: "text-lamp-ink", on: chips === "suggestions", set: () => setParams({ chips: "suggestions" }) },
  ];

  if (estate.isLoading) return <Loading />;
  return (
    <div>
      <PageHeader title="Estate"
        sub="Every PostgreSQL server rAIden watches, wherever it runs: on-prem, virtual machines, Kubernetes and managed cloud services."
        actions={me.data?.role === "admin" && <Link to="/databases/new" className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-ink px-3.5 text-[13.5px] font-semibold text-surface hover:opacity-90"><Plus className="size-4" /> Add database</Link>} />

      <div className="mb-3 flex flex-wrap gap-2">
        {filters.map((f) => (
          <button key={f.id} onClick={f.set} aria-pressed={f.on}
            className={cn("flex items-baseline gap-2 rounded-lg border px-3 py-1.5 text-[13px] font-semibold transition-colors",
              f.on ? "border-ink/30 bg-surface text-ink shadow-card" : "border-line bg-surface-2 text-ink-2 hover:text-ink")}>
            {f.label}<span className={cn("num", f.tone || "text-ink-3")}>{f.n}</span>
          </button>
        ))}
      </div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-ink-3" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search name, host, tag, owner" className="w-64 pl-8" />
        </div>
        <Segmented value={env} onChange={setEnv} options={[{ id: "all", label: "All" }, { id: "production", label: "Production" }, { id: "staging", label: "Staging" }, { id: "development", label: "Development" }]} />
        <Select aria-label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value)} className="w-60">
          <option value="">All platforms</option>
          {platforms.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </Select>
        {(status || chips) && <button className="text-[12.5px] font-semibold text-arc-ink hover:underline" onClick={() => { setParam("status", ""); setParam("chips", ""); }}>Clear filter</button>}
      </div>

      <Card className="overflow-hidden">
        {filtered.length === 0 ? (
          <EmptyState title={rows.length ? "No server matches" : "No databases yet"}
            action={!rows.length && <Link to="/databases/new" className="inline-flex h-9 items-center rounded-lg bg-ink px-3.5 text-[13.5px] font-semibold text-surface">Add a database</Link>}>
            {rows.length ? "Try another filter or search." : "Connect the first PostgreSQL server: rAIden assesses it and starts monitoring right away."}
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left" style={{ minWidth: 1180 - (hasCpu ? 0 : 150) - (hasStorage ? 0 : 110) }}>
              <thead className="bg-surface-2 text-[11.5px] text-ink-3">
                <tr>
                  {["Server", "Platform", "Status", "Issues and suggestions", ...(hasCpu ? ["CPU"] : []), "Connections", "Transactions", ...(hasStorage ? ["Storage"] : []), "Gates"].map((h) => (
                    <th key={h} className="px-3 py-2 font-semibold whitespace-nowrap">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => {
                  const issues = r.chips.issues;
                  const sugg = r.chips.suggestions;
                  const shown = [...issues.slice(0, 2).map((c) => ({ ...c, kind: "issue" as const })), ...sugg.slice(0, issues.length >= 2 ? 0 : 1).map((c) => ({ ...c, kind: "suggestion" as const }))];
                  const more = issues.length + sugg.length - shown.length;
                  return (
                    <tr key={r.id} onClick={() => nav(`/servers/${r.id}`)} className="cursor-pointer border-t border-line align-top transition-colors hover:bg-surface-2">
                      <td className="max-w-[250px] px-3 py-2.5">
                        <div className="flex items-center gap-1.5">
                          <Link to={`/servers/${r.id}`} onClick={(e) => e.stopPropagation()} className="min-w-0 text-[13.5px] hover:underline"><ServerTag name={r.name} slot={r.color_slot} /></Link>
                          {r.environment !== "production" && <Badge tone={r.environment === "staging" ? "info" : "neutral"}>{r.environment === "staging" ? "Staging" : "Dev"}</Badge>}
                          {r.in_recovery && <Badge>Standby</Badge>}
                        </div>
                        <div className="mono mt-0.5 truncate pl-3.5 text-[11.5px] text-ink-3" title={`${r.host}:${r.port}`}>{r.host}:{r.port}</div>
                      </td>
                      <td className="max-w-[180px] px-3 py-2.5 text-[12.5px]">
                        <div className="truncate font-semibold text-ink" title={r.platform_label}>{PLATFORM_CARDS.find((p) => p.id === r.platform)?.title ?? r.platform_label}</div>
                        <div className="truncate text-ink-3" title={r.region}>{r.version ? `PG ${r.version.replace(/^PostgreSQL /, "").split(" ")[0]}` : "—"}{r.region && ` · ${r.region.split(" (")[0]}`}</div>
                      </td>
                      <td className="max-w-[200px] px-3 py-2.5">
                        <div className="flex items-center gap-2">
                          <StatusLabel status={r.status} />
                          {r.incidents.open > 0 && (
                            <Link to={r.incidents.ids.length === 1 ? `/incidents/${r.incidents.ids[0]}` : "/incidents?view=list"} onClick={(e) => e.stopPropagation()}
                              className="text-[11.5px] font-semibold whitespace-nowrap text-ink-2 hover:underline">{r.incidents.open} open</Link>
                          )}
                        </div>
                        {r.status_reason && r.status !== "healthy" && <div className="mt-0.5 line-clamp-2 text-[12px] text-ink-3" title={r.status_reason}>{r.status_reason}</div>}
                      </td>
                      <td className="max-w-[250px] px-3 py-2.5">
                        <div className="flex flex-wrap gap-1">
                          {shown.map((c, i) => <IssueChip key={i} title={c.title} kind={c.kind} />)}
                          {more > 0 && <span className="rounded-full border border-line px-2 py-[1px] text-[11.5px] font-semibold text-ink-3">+{more}</span>}
                          {!shown.length && <span className="text-[12px] text-ink-3">None</span>}
                        </div>
                      </td>
                      {hasCpu && <td className="px-3 py-2.5"><MetricCell row={r} k="cpu_percent" spark="cpu_percent" /></td>}
                      <td className="px-3 py-2.5"><MetricCell row={r} k="connections_percent" spark="connections_percent" /></td>
                      <td className="px-3 py-2.5"><MetricCell row={r} k="tps" spark="tps" /></td>
                      {hasStorage && (
                        <td className="w-[110px] px-3 py-2.5">
                          {r.metrics.storage_percent != null ? (
                            <Meter value={r.metrics.storage_percent} max={100} sub={`${Math.round(r.metrics.storage_percent)}%`} />
                          ) : <span className="text-[12px] text-ink-3">—</span>}
                        </td>
                      )}
                      <td className="px-3 py-2.5">
                        {r.supervised_stages.length === 0 ? (
                          <span className="inline-flex items-center gap-1 text-[12px] font-semibold whitespace-nowrap text-arc-ink" title="Every stage runs on autopilot (high-risk actions still wait for a person)"><Bot className="size-3.5" /> Auto</span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-[12px] font-semibold whitespace-nowrap text-lamp-ink" title={`A person approves: ${r.supervised_stages.map((s) => STAGE_TITLE[s]).join(", ")}`}>
                            <Hand className="size-3.5" /> {r.supervised_stages.length}
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <p className="mt-2 text-[12px] text-ink-3">Issues need a fix; suggestions improve tuning, observability or maintenance. Sparklines show the last hour. Gates: the stages where a person approves before rAIden carries on.
        {rows.length > 0 && (!hasCpu || !hasStorage) && ` ${!hasCpu && !hasStorage ? "CPU and storage come" : !hasCpu ? "CPU comes" : "Storage comes"} from Azure Monitor or the rAIden host agent; these servers do not send ${!hasCpu && !hasStorage ? "them" : "it"} yet.`}</p>
    </div>
  );
}
