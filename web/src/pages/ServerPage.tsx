import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, MessageSquareText, Stethoscope } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { AssessmentView, ScoreRing } from "../components/assessment";
import { StateLine } from "../components/incident";
import { Badge, Button, Card, CardHeader, cn, EmptyState, EnvBadge, Loading, Segmented, SeverityBadge, ServerTag, StatusLabel, Tabs, Time, useToast } from "../components/ui";
import { api } from "../lib/api";
import { duration } from "../lib/format";
import { useAssessment, useAssessments, useMe, useServer } from "../lib/hooks";
import type { Conversation, ServerDetail } from "../lib/types";
import { ConfigurationTab, QueriesTab, ReplicationTab, SessionsTab, TablesTab } from "./server/LiveTabs";
import { OverviewTab } from "./server/OverviewTab";
import { PerformanceTab } from "./server/PerformanceTab";
import { SettingsTab } from "./server/SettingsTab";

type Tab = "overview" | "performance" | "sessions" | "queries" | "tables" | "replication" | "configuration" | "assessment" | "incidents" | "settings";

function AssessmentTab({ server, canOperate }: { server: ServerDetail; canOperate: boolean }) {
  const history = useAssessments(server.id);
  const [picked, setPicked] = useState<number | null>(null);
  const id = picked ?? server.assessment?.id ?? history.data?.[0]?.id ?? null;
  const a = useAssessment(id);
  const qc = useQueryClient();
  const run = useMutation({
    mutationFn: () => api.post<{ id: number }>(`/servers/${server.id}/assess`),
    onSuccess: (r) => { setPicked(r.id); qc.invalidateQueries({ queryKey: ["assessments", server.id] }); },
  });
  if (!id) {
    return (
      <Card>
        <EmptyState icon={<Stethoscope className="size-7" />} title="Not assessed yet"
          action={canOperate && <Button variant="primary" onClick={() => run.mutate()} loading={run.isPending}>Assess {server.name}</Button>}>
          A read-only review of configuration and sizing, performance, reliability, security, observability and maintenance, with fixes you can apply.
        </EmptyState>
      </Card>
    );
  }
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_260px]">
      <div className="min-w-0">{a.data ? <AssessmentView assessment={a.data} serverId={server.id} serverName={server.name} canApply={canOperate} onRerun={canOperate ? () => run.mutate() : undefined} rerunning={run.isPending} /> : <Loading />}</div>
      <Card className="self-start">
        <CardHeader title="History" />
        <ul className="px-2 pb-2">
          {(history.data ?? []).map((h) => (
            <li key={h.id}>
              <button onClick={() => setPicked(h.id)} className={cn("flex w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left hover:bg-surface-2", h.id === id && "bg-surface-3")}>
                {h.status === "done" ? <ScoreRing score={h.score} size={34} /> : <span className="flex size-[34px] items-center justify-center text-[11px] text-ink-3">{h.status}</span>}
                <span className="min-w-0 text-[12.5px]">
                  <span className="block font-semibold"><Time iso={h.created_at} mode="date" /></span>
                  <span className="block text-ink-3">{h.trigger} · {h.counts.issue} issues · {h.counts.suggestion} suggestions</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

export function ServerPage() {
  const { id } = useParams();
  const { data: server, isLoading, error } = useServer(id);
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) || "overview";
  const [range, setRange] = useState("6h");
  const me = useMe();
  const nav = useNavigate();
  const toast = useToast();
  const canOperate = me.data?.role !== "viewer";
  const isAdmin = me.data?.role === "admin";
  const investigate = useMutation({
    mutationFn: () => api.post<Conversation>("/chat/conversations", { server_id: id, title: `${server?.name}: investigation` }),
    onSuccess: (c) => nav(`/chat/${c.id}`),
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const assess = useMutation({
    mutationFn: () => api.post<{ id: number }>(`/servers/${id}/assess`),
    onSuccess: () => setParams({ tab: "assessment" }),
  });
  if (isLoading) return <Loading />;
  if (error || !server) return <EmptyState title="No such server">It may have been removed. <Link to="/estate" className="font-semibold text-arc-ink">Back to the estate</Link></EmptyState>;

  const open = server.incidents.filter((i) => i.stage !== "resolved");
  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "overview", label: "Overview" },
    { id: "performance", label: "Metrics" },
    { id: "sessions", label: "Sessions and locks" },
    { id: "queries", label: "Queries" },
    { id: "tables", label: "Tables and vacuum" },
    { id: "replication", label: "Replication" },
    { id: "configuration", label: "Configuration" },
    { id: "assessment", label: "Assessment", count: server.assessment ? server.assessment.counts.issue + server.assessment.counts.suggestion : undefined },
    { id: "incidents", label: "Incidents", count: open.length || undefined },
    { id: "settings", label: "Settings" },
  ];

  return (
    <div>
      <Link to="/estate" className="mb-3 inline-flex items-center gap-1.5 text-[13px] font-semibold text-ink-3 hover:text-ink"><ArrowLeft className="size-4" /> Estate</Link>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="display text-[27px] leading-tight"><ServerTag name={server.name} slot={server.color_slot} className="[&>span:first-child]:size-3 [&>span:first-child]:rounded-[4px]" /></h1>
            <StatusLabel status={server.status} />
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[12.5px]">
            <EnvBadge env={server.environment} />
            <Badge>{server.platform_label}</Badge>
            {server.version && <Badge>{server.version.split(" ").slice(0, 2).join(" ")}</Badge>}
            {server.in_recovery && <Badge>Standby</Badge>}
            {server.simulated && <Badge className="border-dashed">Simulated</Badge>}
            {!server.enabled && <Badge tone="warn">Monitoring paused</Badge>}
            <span className="ml-1 inline-flex min-w-0 items-center gap-1 text-ink-3">
              <span className="mono max-w-[360px] truncate" title={`${server.host}:${server.port}`}>{server.host}:{server.port}</span>
              <span className="whitespace-nowrap">{server.uptime_s ? `· up ${duration(server.uptime_s)} ` : ""}· collected <Time iso={server.last_seen_at} /></span>
            </span>
          </div>
          {/* an open incident already has its own strip below */}
          {server.status !== "healthy" && server.status_reason && !open.some((i) => server.status_reason.startsWith(i.id)) && (
            <div className="mt-1.5 text-[13px] text-ink-2">{server.status_reason}</div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canOperate && <Button onClick={() => assess.mutate()} loading={assess.isPending}><Stethoscope className="size-4" /> Assess</Button>}
          {canOperate && <Button variant="arc" onClick={() => investigate.mutate()} loading={investigate.isPending}><MessageSquareText className="size-4" /> Investigate with rAIden</Button>}
        </div>
      </div>

      {open.length > 0 && tab !== "incidents" && (
        <div className="mb-4 flex flex-wrap gap-2">
          {open.slice(0, 3).map((i) => (
            <Link key={i.id} to={`/incidents/${i.id}`} className={cn("flex min-w-[280px] flex-1 items-center gap-3 rounded-xl border bg-surface px-3 py-2 shadow-card hover:shadow-pop",
              i.state === "waiting" ? "border-lamp-line" : "border-line")}>
              <SeverityBadge severity={i.severity} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-semibold">{i.title}</span>
                <StateLine inc={i} />
              </span>
              <span className="mono text-[11.5px] text-ink-3">{i.id}</span>
            </Link>
          ))}
        </div>
      )}

      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <Tabs value={tab} onChange={(t) => setParams({ tab: t })} tabs={tabs} className="min-w-0 flex-1" />
        {(tab === "overview" || tab === "performance") && (
          <Segmented size="sm" value={range} onChange={setRange} options={["1h", "6h", "24h", "7d", "30d"].map((r) => ({ id: r, label: r }))} />
        )}
      </div>

      {tab === "overview" && <OverviewTab server={server} range={range} canOperate={canOperate} />}
      {tab === "performance" && <PerformanceTab server={server} range={range} />}
      {tab === "sessions" && <SessionsTab server={server} canOperate={canOperate} />}
      {tab === "queries" && <QueriesTab server={server} />}
      {tab === "tables" && <TablesTab server={server} canOperate={canOperate} />}
      {tab === "replication" && <ReplicationTab server={server} canOperate={canOperate} />}
      {tab === "configuration" && <ConfigurationTab server={server} />}
      {tab === "assessment" && <AssessmentTab server={server} canOperate={canOperate} />}
      {tab === "incidents" && (
        <Card className="overflow-hidden">
          {server.incidents.length === 0 ? <EmptyState title="No incidents on this server" /> : (
            <table className="w-full text-left text-[13px]">
              <thead className="bg-surface-2 text-[11.5px] text-ink-3"><tr>{["Incident", "Title", "Stage", "State", "Detected"].map((h) => <th key={h} className="px-3 py-2 font-semibold">{h}</th>)}</tr></thead>
              <tbody>
                {server.incidents.map((i) => (
                  <tr key={i.id} className="border-t border-line hover:bg-surface-2">
                    <td className="px-3 py-2 whitespace-nowrap"><Link to={`/incidents/${i.id}`} className="inline-flex items-center gap-2"><SeverityBadge severity={i.severity} /><span className="mono text-[12px]">{i.id}</span></Link></td>
                    <td className="px-3 py-2"><Link to={`/incidents/${i.id}`} className="hover:underline">{i.title}</Link></td>
                    <td className="px-3 py-2 capitalize">{i.stage}</td>
                    <td className="px-3 py-2"><StateLine inc={i} /></td>
                    <td className="px-3 py-2 whitespace-nowrap text-ink-2"><Time iso={i.detected_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}
      {tab === "settings" && <SettingsTab key={server.id} server={server} isAdmin={isAdmin} />}
    </div>
  );
}
