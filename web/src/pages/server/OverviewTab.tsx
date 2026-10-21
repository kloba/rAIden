import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, BellRing, Lightbulb, X } from "lucide-react";
import { Link } from "react-router";
import { TimeChart, type LineSeries } from "../../components/charts";
import { StateLine } from "../../components/incident";
import { Badge, Card, CardHeader, cn, IconButton, KeyValue, Markdown, SeverityBadge, Time, useToast } from "../../components/ui";
import { api } from "../../lib/api";
import { categoryLabel, chartStart, duration, value as fmt } from "../../lib/format";
import { serverColor, useMetricDefs, useServerMetrics } from "../../lib/hooks";
import type { MetricDef, ServerDetail } from "../../lib/types";

const TILES: { key: string; label: string }[] = [
  { key: "cpu_percent", label: "CPU" },
  { key: "memory_percent", label: "Memory" },
  { key: "storage_percent", label: "Storage" },
  { key: "connections_percent", label: "Connections used" },
  { key: "tps", label: "Transactions" },
  { key: "cache_hit_ratio", label: "Cache hit ratio" },
  { key: "longest_transaction_time_sec", label: "Oldest transaction" },
  { key: "blocked_sessions", label: "Blocked sessions" },
  { key: "physical_replication_delay_in_seconds", label: "Replica lag" },
  { key: "maximum_used_transactionIDs", label: "Transaction ID age" },
];

const CHARTS: { title: string; keys: string[]; labels?: string[] }[] = [
  { title: "CPU and memory", keys: ["cpu_percent", "memory_percent"], labels: ["CPU", "Memory"] },
  { title: "Connections", keys: ["active_connections", "idle_in_transaction"], labels: ["Active", "Idle in transaction"] },
  { title: "Transactions per second", keys: ["tps"] },
  { title: "Blocked sessions", keys: ["blocked_sessions"] },
  { title: "Oldest transaction and query", keys: ["longest_transaction_time_sec", "longest_query_time_sec"], labels: ["Transaction", "Query"] },
  { title: "Cache hit ratio", keys: ["cache_hit_ratio"] },
  { title: "Read and write IOPS", keys: ["read_iops", "write_iops"], labels: ["Read", "Write"] },
  { title: "Storage", keys: ["storage_percent"] },
  { title: "Replica lag", keys: ["physical_replication_delay_in_seconds"] },
  { title: "WAL retained by replication slots", keys: ["replication_slot_retained_bytes"] },
  { title: "Transaction ID age", keys: ["maximum_used_transactionIDs"] },
  { title: "WAL generated", keys: ["wal_bytes_per_second"] },
];

function health(def: MetricDef | undefined, v: number | undefined): "ok" | "warn" | "crit" {
  if (!def || v == null) return "ok";
  if (def.lower_is_worse) return def.crit != null && v <= def.crit ? "crit" : def.warn != null && v <= def.warn ? "warn" : "ok";
  return def.crit != null && v >= def.crit ? "crit" : def.warn != null && v >= def.warn ? "warn" : "ok";
}

export function OverviewTab({ server, range, canOperate }: { server: ServerDetail; range: string; canOperate: boolean }) {
  const defOf = useMetricDefs();
  const allKeys = [...new Set(CHARTS.flatMap((c) => c.keys))].filter((k) => server.metrics[k] != null);
  const m = useServerMetrics(server.id, allKeys, range);
  const qc = useQueryClient();
  const toast = useToast();
  const dismiss = useMutation({
    mutationFn: (id: number) => api.post(`/suggestions/${id}/dismiss`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["server", server.id] }); qc.invalidateQueries({ queryKey: ["estate"] }); toast("Dismissed: it comes back only if the check finds it again."); },
  });
  const tiles = TILES.filter((t) => server.metrics[t.key] != null).slice(0, 8);
  const charts = CHARTS.filter((c) => c.keys.some((k) => server.metrics[k] != null));
  const color = serverColor(server.color_slot);
  const issues = server.suggestions.filter((x) => x.kind === "issue");
  const suggestions = server.suggestions.filter((x) => x.kind === "suggestion");

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
      <div className="min-w-0 space-y-4">
        <Card className="overflow-hidden">
          <div className="grid grid-cols-2 gap-px bg-line sm:grid-cols-4">
            {tiles.map((t) => {
              const def = defOf(t.key);
              const v = server.metrics[t.key];
              const h = health(def, v);
              const extra = t.key === "connections_percent" && server.metrics.active_connections != null
                ? `${Math.round(server.metrics.active_connections)} of ${Math.round(server.metrics.max_connections ?? 0)}`
                : t.key === "maximum_used_transactionIDs" && v != null ? `${((100 * v) / 2147483648).toFixed(1)}% of the wraparound limit` : def?.description.split(".")[0];
              return (
                <div key={t.key} className="bg-surface px-4 py-3">
                  <div className="flex items-center gap-1.5 text-[12.5px] font-medium text-ink-3">
                    {t.label}
                    {h !== "ok" && <span role="img" className={cn("size-1.5 rounded-full", h === "crit" ? "bg-crit" : "bg-warn")} aria-label={h === "crit" ? "critical" : "warning"} />}
                  </div>
                  <div className={cn("mt-0.5 text-[23px] leading-tight font-semibold tracking-tight num", h === "crit" ? "text-crit" : h === "warn" ? "text-warn" : "text-ink")}>{fmt(def, v)}</div>
                  <div className="truncate text-[11.5px] text-ink-3" title={extra}>{extra}</div>
                </div>
              );
            })}
            {Array.from({ length: (4 - (tiles.length % 4)) % 4 }, (_, i) => <div key={`pad${i}`} className="hidden bg-surface sm:block" />)}
          </div>
        </Card>

        <div className="grid gap-4 lg:grid-cols-2">
          {charts.map((c) => {
            const keys = c.keys.filter((k) => server.metrics[k] != null);
            const def = defOf(keys[0]!);
            const series: LineSeries[] = keys.map((k, i) => ({
              key: k, label: c.labels?.[c.keys.indexOf(k)] ?? defOf(k)?.name ?? k, color: keys.length === 1 ? color : `var(--s${i + 1})`,
              points: m.data?.series[k] ?? [],
            }));
            return (
              <Card key={c.title} className="min-w-0">
                <CardHeader title={c.title} sub={keys.length === 1 ? <>Now <b className="text-ink num">{fmt(def, server.metrics[keys[0]!])}</b></> : undefined} />
                <div className="px-3 pb-3">
                  <TimeChart series={series} def={def} start={chartStart(m.data?.start, server.created_at)} end={m.data?.end ?? 0} height={150} loading={m.isFetching && !m.data} />
                </div>
              </Card>
            );
          })}
        </div>
      </div>

      <div className="space-y-4">
        {(server.firing.length > 0 || issues.length > 0 || suggestions.length > 0) && (
          <Card>
            <CardHeader title="Issues and suggestions" sub="From monitors, routines and assessments" />
            <ul className="divide-y divide-line px-4 pb-2">
              {server.firing.map((f, i) => (
                <li key={`f${i}`} className="flex gap-2.5 py-2.5">
                  <BellRing className="mt-0.5 size-4 shrink-0 text-crit" />
                  <div className="min-w-0 flex-1">
                    <div className="text-[13px] font-semibold">{f.monitor}</div>
                    <div className="text-[12px] text-ink-3">Firing since <Time iso={f.since} /> <SeverityBadge severity={f.severity} /></div>
                  </div>
                </li>
              ))}
              {[...issues, ...suggestions].map((x) => (
                <li key={x.id} className="group flex gap-2.5 py-2.5">
                  {x.kind === "issue" ? <AlertTriangle className="mt-0.5 size-4 shrink-0 text-crit" /> : <Lightbulb className="mt-0.5 size-4 shrink-0 text-lamp" />}
                  <div className="min-w-0 flex-1">
                    <div className="text-[13px] font-semibold">{x.title}</div>
                    <Markdown className="line-clamp-3 text-[12.5px] text-ink-2">{x.detail.split("\n\n")[0] ?? ""}</Markdown>
                    <div className="mt-0.5 text-[11.5px] text-ink-3">{categoryLabel(x.category)} · {x.source} · <Time iso={x.updated_at} /></div>
                  </div>
                  {canOperate && <IconButton label="Dismiss" className="size-7 opacity-0 group-hover:opacity-100 focus:opacity-100" onClick={() => dismiss.mutate(x.id)}><X className="size-3.5" /></IconButton>}
                </li>
              ))}
            </ul>
          </Card>
        )}

        <Card>
          <CardHeader title="About" />
          <div className="px-4 pb-4">
            <KeyValue items={[
              ["Platform", <>{server.platform_label}{server.region && <span className="text-ink-3"> · {server.region}</span>}</>],
              ["Version", server.version || "—"],
              ...(server.sku ? [["Size", server.sku] as [string, string]] : []),
              ["Address", <span className="mono break-all">{server.host}:{server.port}/{server.dbname}</span>],
              ["Role", server.in_recovery ? <>Standby{server.replica_of && <> of <Link className="font-semibold hover:underline" to={`/servers/${server.replica_of}`}>{server.replica_of}</Link></>}</> : "Primary"],
              ["Up for", server.uptime_s ? duration(server.uptime_s) : "—"],
              ["Data from", <span className="flex flex-wrap gap-1">{server.sources.map((s) => <Badge key={s}>{s === "sql" ? "SQL" : s === "azure" ? "Azure Monitor" : "Host agent"}</Badge>)}{server.host_agent && !server.sources.includes("host") && <Badge>Host agent</Badge>}</span>],
              ["Collected", <>every {duration(server.collect_interval_s)} · last <Time iso={server.last_seen_at} /></>],
              ...(server.owner ? [["Owner", server.owner] as [string, string]] : []),
              ...(server.maintenance_window ? [["Maintenance", server.maintenance_window] as [string, string]] : []),
              ...(Object.keys(server.tags).length ? [["Tags", <span className="flex flex-wrap gap-1">{Object.entries(server.tags).map(([k, v]) => <Badge key={k}>{k}={v}</Badge>)}</span>] as [string, React.ReactNode]] : []),
            ]} />
            {server.extensions.length > 0 && <div className="mt-3 text-[12px] text-ink-3"><span className="font-semibold text-ink-2">Extensions: </span>{server.extensions.join(", ")}</div>}
            {server.last_error && <div className="mt-3 rounded-lg bg-crit-soft px-3 py-2 text-[12px] text-crit">{server.last_error}</div>}
          </div>
        </Card>

        <Card>
          <CardHeader title="Recent incidents" actions={<Link to={`/incidents?view=list`} className="text-[12px] font-semibold text-arc-ink hover:underline">All</Link>} />
          {server.incidents.length === 0 ? <div className="px-4 pb-4 text-[13px] text-ink-3">No incidents on this server.</div> : (
            <ul className="divide-y divide-line px-4 pb-2">
              {server.incidents.slice(0, 6).map((i) => (
                <li key={i.id} className="py-2">
                  <Link to={`/incidents/${i.id}`} className="block hover:underline-offset-2">
                    <div className="flex items-center gap-2 text-[12px]"><SeverityBadge severity={i.severity} /><span className="mono text-ink-3">{i.id}</span><span className="ml-auto text-ink-3"><Time iso={i.detected_at} /></span></div>
                    <div className="mt-0.5 line-clamp-1 text-[13px] font-semibold hover:underline">{i.title}</div>
                  </Link>
                  <div className="mt-0.5"><StateLine inc={i} /></div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
