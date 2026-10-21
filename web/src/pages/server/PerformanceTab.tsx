import { useQuery } from "@tanstack/react-query";
import { Info } from "lucide-react";
import { useMemo, useState } from "react";
import { TimeChart, type LineSeries } from "../../components/charts";
import { Card, CardHeader, cn, Loading } from "../../components/ui";
import { api } from "../../lib/api";
import { chartStart, value as fmt } from "../../lib/format";
import { serverColor, useCatalog, useServerMetrics } from "../../lib/hooks";
import type { ServerDetail } from "../../lib/types";

const dimOf = (k: string) => /\[(.*)\]$/.exec(k)?.[1] ?? "";

/** Every metric this server reports, by category, the way Azure Monitor groups them. */
export function PerformanceTab({ server, range }: { server: ServerDetail; range: string }) {
  const catalog = useCatalog();
  const keys = useQuery({ queryKey: ["metric-keys", server.id], queryFn: () => api.get<string[]>(`/servers/${server.id}/metric-keys`), staleTime: 60_000 });
  const available = useMemo(() => new Set((keys.data ?? []).map((k) => k.split("[")[0]!)), [keys.data]);
  const cats = useMemo(() => (catalog.data?.categories ?? []).map((c) => ({ ...c, metrics: (catalog.data?.metrics ?? []).filter((m) => m.category === c.id && available.has(m.id)) }))
    .filter((c) => c.metrics.length > 0), [catalog.data, available]);
  const [cat, setCat] = useState<string | null>(null);
  const current = cats.find((c) => c.id === cat) ?? cats[0];
  const wanted = (current?.metrics ?? []).map((m) => (m.dimension && (keys.data ?? []).some((k) => k.startsWith(`${m.id}[`)) ? `${m.id}[*]` : m.id));
  const series = useServerMetrics(server.id, wanted, range);
  if (keys.isLoading || catalog.isLoading) return <Loading />;
  if (!current) return <Card className="p-6 text-[13.5px] text-ink-3">No metrics collected yet. The first collection runs within a minute of adding the server.</Card>;
  return (
    <div className="grid gap-4 lg:grid-cols-[200px_minmax(0,1fr)]">
      <nav className="flex gap-1 overflow-x-auto lg:flex-col" aria-label="Metric categories">
        {cats.map((c) => (
          <button key={c.id} onClick={() => setCat(c.id)} aria-current={current.id === c.id}
            className={cn("flex items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-[13px] font-semibold whitespace-nowrap",
              current.id === c.id ? "bg-surface-3 text-ink" : "text-ink-2 hover:bg-surface-2")}>
            {c.name}<span className="text-[11.5px] font-medium text-ink-3 num">{c.metrics.length}</span>
          </button>
        ))}
      </nav>
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        {current.metrics.map((m) => {
          const all = Object.entries(series.data?.series ?? {}).filter(([k]) => k === m.id || k.startsWith(`${m.id}[`));
          const dimensional = all.some(([k]) => k.includes("["));
          // the top 8 by current value, colored in name order so a series keeps its color while the ranking moves
          const lines: LineSeries[] = (dimensional ? all.filter(([k]) => k.includes("[")) : all.filter(([k]) => k === m.id))
            .sort((a, b) => (b[1].at(-1)?.[1] ?? 0) - (a[1].at(-1)?.[1] ?? 0)).slice(0, 8)
            .sort((a, b) => a[0].localeCompare(b[0]))
            .map(([k, pts], i) => ({ key: k, label: dimensional ? dimOf(k) : m.name, color: dimensional ? `var(--s${i + 1})` : serverColor(server.color_slot), points: pts }));
          const latest = series.data?.latest[m.id] ?? server.metrics[m.id];
          return (
            <Card key={m.id} className="min-w-0">
              <CardHeader
                title={<span className="inline-flex items-center gap-1.5">{m.name}<span title={`${m.description}${m.azure ? ` (Azure Monitor: ${m.azure})` : ""}`}><Info className="size-3.5 text-ink-3" /></span></span>}
                sub={dimensional ? `By ${m.dimension}, top ${Math.min(lines.length, 8)}` : <>Now <b className="text-ink num">{fmt(m, latest)}</b></>} />
              <div className="px-3 pb-3">
                <TimeChart series={lines} def={m} start={chartStart(series.data?.start, server.created_at)} end={series.data?.end ?? 0} height={160} loading={series.isFetching && !series.data} />
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
