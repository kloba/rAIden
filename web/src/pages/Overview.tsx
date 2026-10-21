import { AlertOctagon, AlertTriangle, ArrowRight, CheckCircle2, Lightbulb } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { BarList } from "../components/charts";
import { Actor, EventIcon } from "../components/incident";
import { ShiftTimeline } from "../components/shift";
import { Card, CardHeader, cn, EmptyState, Loading, PageHeader, RiskBadge, Segmented, ServerTag, SeverityBadge, Stat, Time } from "../components/ui";
import { compact, duration, money } from "../lib/format";
import { serverColor, useMetricDefs, useOverview, useSettings } from "../lib/hooks";
import type { Approval, Overview } from "../lib/types";

function useClock(ms = 30_000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

/** Status of the estate as one bar: a segment per state, 2px surface gaps between segments. */
function StatusBar({ k }: { k: Overview["kpis"] }) {
  const parts = [
    { n: k.healthy, color: "var(--ok)", label: "healthy" },
    { n: k.warning, color: "var(--warn)", label: "warning" },
    { n: k.critical + k.unreachable, color: "var(--crit)", label: "critical" },
    { n: k.paused + k.unknown, color: "var(--line-strong)", label: "paused or waiting for data" },
  ].filter((p) => p.n > 0);
  return (
    <div className="mt-1.5 flex h-1.5 gap-[2px] overflow-hidden rounded-full" role="img" aria-label={parts.map((p) => `${p.n} ${p.label}`).join(", ")}>
      {parts.map((p) => <span key={p.label} className="h-full first:rounded-l-full last:rounded-r-full" style={{ flex: p.n, background: p.color }} title={`${p.n} ${p.label}`} />)}
    </div>
  );
}

function NeedRow({ a, slot }: { a: Approval; slot: number | undefined }) {
  const to = a.incident_id ? `/incidents/${a.incident_id}` : a.conversation_id ? `/chat/${a.conversation_id}` : "/approvals";
  return (
    <Link to={to} className="group block rounded-lg border border-lamp-line bg-lamp-soft/45 px-3 py-2.5 transition-colors hover:bg-lamp-soft">
      <div className="flex items-center gap-2 text-[12px]">
        {a.severity && <SeverityBadge severity={a.severity} />}
        <span className="mono text-ink-3">{a.incident_id ?? "Investigation"}</span>
        {a.server_name && <ServerTag name={a.server_name} slot={slot} className="min-w-0 text-[12px]" />}
        <span className="ml-auto shrink-0 text-ink-3"><Time iso={a.requested_at} /></span>
      </div>
      <div className="mt-1 text-[13.5px] leading-snug font-semibold text-ink">{a.title}</div>
      {a.incident_title && <div className="line-clamp-1 text-[12px] text-ink-3">{a.incident_title}</div>}
      <div className="mt-1.5 flex items-center gap-2">
        <RiskBadge risk={a.risk} />
        <span className="ml-auto inline-flex items-center gap-1 text-[12px] font-semibold text-lamp-ink">Review <ArrowRight className="size-3.5 transition-transform group-hover:translate-x-0.5" /></span>
      </div>
    </Link>
  );
}

const ATTENTION = {
  critical: { icon: <AlertOctagon className="size-4" />, cls: "text-crit bg-crit-soft" },
  warning: { icon: <AlertTriangle className="size-4" />, cls: "text-warn bg-warn-soft" },
  issues: { icon: <AlertTriangle className="size-4" />, cls: "text-crit bg-crit-soft" },
  suggestions: { icon: <Lightbulb className="size-4" />, cls: "text-lamp-ink bg-lamp-soft" },
} as Record<string, { icon: React.ReactNode; cls: string }>;

const FLEET: { key: string; title: string }[] = [
  { key: "cpu_percent", title: "CPU" },
  { key: "memory_percent", title: "Memory" },
  { key: "storage_percent", title: "Storage" },
  { key: "connections_percent", title: "Connections used" },
];

export function OverviewPage() {
  const [range, setRange] = useState("24h");
  const { data, isLoading } = useOverview(range);
  const settings = useSettings();
  const defOf = useMetricDefs();
  const now = useClock();
  const slots = useMemo(() => new Map((data?.timeline.servers ?? []).map((s) => [s.id, s.color_slot])), [data]);
  const names = useMemo(() => new Map((data?.timeline.servers ?? []).map((s) => [s.id, s.name])), [data]);
  if (isLoading || !data) return <Loading />;
  const k = data.kpis;
  const org = settings.data?.general.organization;
  const rangeLabel = { "6h": "6 hours", "24h": "24 hours", "7d": "7 days" }[range];

  if (k.servers === 0) {
    return (
      <div>
        <PageHeader title="Estate overview" />
        <Card>
          <EmptyState title="No databases yet" action={<Link to="/databases/new" className="inline-flex h-9 items-center rounded-lg bg-ink px-3.5 text-[13.5px] font-semibold text-surface">Add a database</Link>}>
            Connect a PostgreSQL server (on-prem, a VM, Kubernetes or any cloud) and rAIden starts watching it within a minute.
          </EmptyState>
        </Card>
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        eyebrow={org || undefined}
        title="Estate overview"
        sub={<>{k.servers} PostgreSQL server{k.servers > 1 ? "s" : ""}, watched around the clock. Showing the last {rangeLabel}.</>}
        actions={<Segmented value={range} onChange={setRange} options={[{ id: "6h", label: "6 h" }, { id: "24h", label: "24 h" }, { id: "7d", label: "7 days" }]} />}
      />

      <Card className="mb-4 grid grid-cols-2 divide-line md:grid-cols-3 xl:grid-cols-6 xl:divide-x">
        <div className="px-4 py-3.5">
          <Stat label="Servers" value={<span className="num">{k.servers}</span>}
            sub={[k.healthy && `${k.healthy} healthy`, k.warning && `${k.warning} warning`, (k.critical + k.unreachable) && `${k.critical + k.unreachable} critical`].filter(Boolean).join(" · ")} />
          <StatusBar k={k} />
        </div>
        <div className="px-4 py-3.5">
          <Stat label="Open incidents" value={<span className="num">{k.open_incidents}</span>}
            sub={k.escalated ? `${k.escalated} with on-call` : k.open_incidents ? "rAIden is on all of them" : "Quiet shift"} tone={k.escalated ? "crit" : undefined} />
        </div>
        <div className="px-4 py-3.5">
          <Stat label="Waiting for you" value={<span className="num">{k.waiting_for_people}</span>} tone={k.waiting_for_people ? "lamp" : undefined}
            sub={k.waiting_for_people ? "Decisions only a person can make" : "Nothing needs a decision"} />
        </div>
        <div className="px-4 py-3.5">
          <Stat label="Resolved on autopilot" value={<span className="num">{k.autopilot_resolved_30d}<span className="text-[17px] text-ink-3">/{k.resolved_30d}</span></span>} tone="arc"
            sub="Last 30 days, no person needed" />
        </div>
        <div className="px-4 py-3.5">
          <Stat label="Median time to resolve" value={<span className="num">{duration(k.mttr_median_s)}</span>} sub="Detected to resolved, 30 days" />
        </div>
        <div className="px-4 py-3.5">
          <Stat label="AI spend this month" value={<span className="num">{money(k.cost_month)}</span>} sub={`${compact(k.tokens)} tokens in ${rangeLabel}`} />
        </div>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="min-w-0 space-y-4">
          <Card className="min-w-0">
            <CardHeader title="The shift" sub="Every incident from detection to resolution, and who made each call." actions={<Link to="/incidents" className="text-[12.5px] font-semibold text-arc-ink hover:underline">Incident board</Link>} />
            <div className="px-4 pb-4"><ShiftTimeline data={data} now={now} /></div>
          </Card>
          <Card>
            <CardHeader title="Activity" sub={`What rAIden and the team did in the last ${rangeLabel}`} />
            {data.activity.length === 0 ? <div className="px-4 pb-4 text-[13px] text-ink-3">No activity in this range.</div> : (
              <ul className="grid gap-x-6 px-4 pb-3 lg:grid-cols-2">
                {data.activity.map((e, i) => (
                  <li key={i} className="flex items-start gap-3 border-b border-line/70 py-2 last:border-0 lg:[&:nth-last-child(2)]:border-0">
                    <EventIcon kind={e.kind} className="size-6 [&_svg]:size-3" />
                    <div className="min-w-0 flex-1">
                      <div className="line-clamp-1 text-[13px] font-semibold text-ink">{e.title}</div>
                      <div className="text-[12px] text-ink-3">
                        <Actor actor={e.actor} /> · <Link to={`/incidents/${e.incident_id}`} className="mono hover:underline">{e.incident_id}</Link>
                        {e.server_id && <> · {names.get(e.server_id) ?? e.server_id}</>}
                      </div>
                    </div>
                    <span className="shrink-0 text-[12px] text-ink-3 num"><Time iso={e.ts} /></span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader title={<span className="inline-flex items-center gap-2">Needs you {data.needs_you.length > 0 && <span className="rounded-full bg-lamp px-1.5 text-[11px] font-bold text-[#2a1c00]">{data.needs_you.length}</span>}</span>}
              sub={data.needs_you.length ? "rAIden stopped at a gate and waits for a decision." : undefined}
              actions={data.needs_you.length > 3 ? <Link to="/approvals" className="text-[12.5px] font-semibold text-lamp-ink hover:underline">All {data.needs_you.length}</Link> : undefined} />
            <div className="space-y-2 px-4 pb-4">
              {data.needs_you.length === 0 ? (
                <div className="flex items-center gap-2.5 rounded-lg border border-arc-line bg-arc-soft/60 px-3 py-3 text-[13px] text-arc-ink">
                  <CheckCircle2 className="size-4 shrink-0" /> Nothing is waiting for a person. rAIden handles what the autonomy policy allows.
                </div>
              ) : data.needs_you.slice(0, 3).map((a) => <NeedRow key={a.id} a={a} slot={slots.get(a.server_id ?? "")} />)}
            </div>
          </Card>

          <Card>
            <CardHeader title="What needs attention" />
            <ul className="px-2 pb-2">
              {data.attention.length === 0 && <li className="px-2 pb-3 text-[13px] text-ink-3">Every server is healthy and there are no open findings.</li>}
              {data.attention.map((a) => {
                const look = ATTENTION[a.kind] ?? ATTENTION.warning!;
                return (
                  <li key={a.kind}>
                    <Link to={a.link} className="flex items-start gap-3 rounded-lg px-2 py-2 hover:bg-surface-2">
                      <span className={cn("mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg", look.cls)}>{look.icon}</span>
                      <span className="min-w-0">
                        <span className="block text-[13.5px] font-semibold text-ink">{a.title}</span>
                        <span className="line-clamp-2 text-[12.5px] text-ink-3">{a.detail}</span>
                        {a.servers.length > 0 && (
                          <span className="mt-1 flex flex-wrap gap-x-2.5 gap-y-0.5 text-[12px]">
                            {a.servers.slice(0, 4).map((sid) => <ServerTag key={sid} name={names.get(sid) ?? sid} slot={slots.get(sid)} />)}
                          </span>
                        )}
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </Card>
        </div>
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {FLEET.map((f) => {
          const items = [...(data.fleet[f.key] ?? [])].sort((a, b) => b.value - a.value);
          return (
            <Card key={f.key}>
              <CardHeader title={f.title} sub="Right now, highest first" actions={<Link to={`/performance?metric=${f.key}`} className="text-[12px] font-semibold text-arc-ink hover:underline">Trend</Link>} />
              <div className="px-4 pb-4">
                {items.length ? (
                  <BarList def={defOf(f.key)} items={items.map((it) => ({ key: it.server_id, value: it.value, color: serverColor(it.color_slot),
                    label: <Link to={`/servers/${it.server_id}`} className="hover:underline"><ServerTag name={it.name} slot={it.color_slot} className="text-[12.5px] font-medium" /></Link> }))} />
                ) : <div className="text-[12.5px] text-ink-3">No server reports this metric yet.</div>}
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
