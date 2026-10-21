import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { TimeChart } from "../components/charts";
import { Card, CardHeader, cn, EmptyState, PageHeader, Segmented, Select } from "../components/ui";
import { value as fmt } from "../lib/format";
import { serverColor, useCatalog, useFleetMetric, useMetricDefs, useServers } from "../lib/hooks";
import type { Env } from "../lib/types";

const KEY = ["cpu_percent", "memory_percent", "connections_percent", "tps", "cache_hit_ratio", "blocked_sessions", "longest_transaction_time_sec",
  "physical_replication_delay_in_seconds", "maximum_used_transactionIDs", "storage_percent", "wal_bytes_per_second", "iops"];

function FleetChart({ metric, range, servers, height, onPick, active }: { metric: string; range: string; servers: string[]; height: number; onPick?: () => void; active?: boolean }) {
  const defOf = useMetricDefs();
  const def = defOf(metric);
  const q = useFleetMetric(metric, range, servers);
  const series = (q.data?.series ?? []).map((s) => ({ key: s.server_id, label: s.name, color: serverColor(s.color_slot), points: s.points }));
  if (q.data && series.length === 0 && onPick) return null;
  const worst = series.map((s) => ({ name: s.label, v: s.points.at(-1)?.[1] })).filter((x) => x.v != null)
    .sort((a, b) => (def?.lower_is_worse ? a.v! - b.v! : b.v! - a.v!))[0];
  return (
    <Card className={cn("min-w-0", onPick && "cursor-pointer transition-shadow hover:shadow-pop", active && "ring-2 ring-arc/40")} onClick={onPick}>
      <CardHeader title={def?.name ?? metric} sub={worst ? <>{def?.lower_is_worse ? "Lowest" : "Highest"} now: <b className="text-ink">{worst.name}</b> <span className="num">{fmt(def, worst.v)}</span></> : def?.description} />
      <div className="px-3 pb-3">
        <TimeChart series={series} def={def} start={q.data?.start ?? 0} end={q.data?.end ?? 0} height={height} area={false} loading={q.isFetching && !q.data} />
      </div>
    </Card>
  );
}

export function PerformancePage() {
  const [params, setParams] = useSearchParams();
  const metric = params.get("metric") || "cpu_percent";
  const [range, setRange] = useState("6h");
  const [env, setEnv] = useState<"all" | Env>("all");
  const [off, setOff] = useState<Set<string>>(new Set());
  const servers = useServers();
  const catalog = useCatalog();
  const inEnv = useMemo(() => (servers.data ?? []).filter((s) => env === "all" || s.environment === env), [servers.data, env]);
  const picked = inEnv.filter((s) => !off.has(s.id)).map((s) => s.id);
  const groups = useMemo(() => (catalog.data?.categories ?? []).map((c) => ({ ...c, metrics: (catalog.data?.metrics ?? []).filter((m) => m.category === c.id && !m.dimension) }))
    .filter((c) => c.metrics.length), [catalog.data]);
  const toggle = (id: string) => setOff((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  return (
    <div>
      <PageHeader title="Performance" sub="Compare servers on any metric. Each server keeps its color across the console."
        actions={<Segmented value={range} onChange={setRange} options={["1h", "6h", "24h", "7d", "30d"].map((r) => ({ id: r, label: r }))} />} />
      <Card className="mb-4 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Select aria-label="Metric" value={metric} onChange={(e) => setParams({ metric: e.target.value })} className="w-72">
            {groups.map((g) => (
              <optgroup key={g.id} label={g.name}>{g.metrics.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</optgroup>
            ))}
          </Select>
          <Segmented value={env} onChange={setEnv} options={[{ id: "all", label: "All" }, { id: "production", label: "Production" }, { id: "staging", label: "Staging" }, { id: "development", label: "Development" }]} />
        </div>
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {inEnv.map((s) => {
            const on = !off.has(s.id);
            return (
              <button key={s.id} onClick={() => toggle(s.id)} aria-pressed={on}
                className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[12.5px] font-semibold transition-colors",
                  on ? "border-line-strong bg-surface text-ink" : "border-line bg-surface-2 text-ink-3 line-through decoration-ink-3/60")}>
                <span className="size-2 rounded-full" style={{ background: on ? serverColor(s.color_slot) : "var(--line-strong)" }} />{s.name}
              </button>
            );
          })}
          {off.size > 0 && <button className="text-[12.5px] font-semibold text-arc-ink hover:underline" onClick={() => setOff(new Set())}>Show all</button>}
        </div>
      </Card>

      {servers.data && picked.length === 0 ? (
        <Card><EmptyState title="No server selected">Pick at least one server above.</EmptyState></Card>
      ) : (
        <>
          <FleetChart metric={metric} range={range} servers={picked} height={300} />
          <div className="mt-5 mb-2 plate">Key metrics</div>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {KEY.filter((k) => k !== metric).map((k) => (
              <FleetChart key={k} metric={k} range={range} servers={picked} height={150}
                onPick={() => { setParams({ metric: k }); document.querySelector("main")?.scrollTo({ top: 0, behavior: "smooth" }); }} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
