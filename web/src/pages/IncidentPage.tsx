import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Bot, Hand, Send, UserCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router";
import { TimeChart } from "../components/charts";
import { ApprovalBox, HardeningList, PlanList, STAGES, StateLine, Timeline } from "../components/incident";
import {
  Badge, Button, Card, CardHeader, cn, EmptyState, EnvBadge, KeyValue, Loading, Markdown, Modal, SeverityBadge, ServerTag, Textarea, Time, useToast,
} from "../components/ui";
import { api } from "../lib/api";
import { categoryLabel, compact, duration, monitorRule, money, personName, since, titleCase } from "../lib/format";
import { serverColor, useCatalog, useIncident, useMe, useMetricDefs, useServerMetrics, useServers } from "../lib/hooks";
import type { IncidentDetail, Stage } from "../lib/types";

/** This incident's path through the circuit: where it is, when it got there, which gates waited for a person. */
function Stepper({ inc }: { inc: IncidentDetail }) {
  const entered = new Map<string, string>();
  for (const e of inc.events) if (e.kind === "stage" && e.data?.to) entered.set(e.data.to, e.ts);
  entered.set("detected", inc.detected_at);
  const current = STAGES.findIndex((s) => s.id === inc.stage);
  return (
    <div className="overflow-x-auto">
      <ol className="flex min-w-[720px] items-start">
        {STAGES.map((s, i) => {
          const done = i < current || inc.stage === "resolved";
          const here = i === current && inc.stage !== "resolved";
          const human = inc.autonomy?.[s.id] === "human";
          return (
            <li key={s.id} className="relative flex flex-1 flex-col items-center text-center">
              {i > 0 && (
                <span className={cn("absolute top-[11px] right-1/2 h-[2px] w-full", i <= current ? "bg-arc" : "bg-line-strong")} aria-hidden />
              )}
              <span className={cn("relative z-[1] flex size-6 items-center justify-center rounded-full border-2",
                done ? "border-arc bg-arc text-on-arc" : here ? (inc.state === "waiting" ? "border-lamp bg-lamp-soft" : "border-arc bg-arc-soft") : "border-line-strong bg-surface")}>
                {here && <span className={cn("size-2 rounded-full", inc.state === "waiting" ? "lamp-glow bg-lamp" : "working bg-arc")} />}
                {done && <svg viewBox="0 0 12 12" className="size-3"><path d="M2.5 6.2 5 8.5l4.5-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>}
              </span>
              <span className={cn("mt-1.5 inline-flex items-center gap-1 text-[12px] font-semibold", here ? "text-ink" : done ? "text-ink-2" : "text-ink-3")}>
                {s.title}{human && s.id !== "detected" && <Hand className="size-3 text-lamp-ink" aria-label="a person approves this stage" />}
              </span>
              <span className="text-[11px] text-ink-3 num">{entered.has(s.id) ? <Time iso={entered.get(s.id)!} mode="clock" /> : " "}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function rangeFor(detected: string, resolved: string | null): string {
  const age = since(detected) ?? 0;
  if (age < 3000 && !resolved) return "1h";
  if (age < 5 * 3600) return "6h";
  if (age < 22 * 3600) return "24h";
  return "7d";
}

function Signal({ inc }: { inc: IncidentDetail }) {
  const defOf = useMetricDefs();
  const metric = inc.evidence?.metric as string | undefined;
  const range = rangeFor(inc.detected_at, inc.resolved_at);
  const servers = useServers();
  const slot = servers.data?.find((s) => s.id === inc.server_id)?.color_slot;
  const m = useServerMetrics(inc.server_id ?? undefined, metric ? [metric] : [], range);
  if (!metric || !inc.server_id) return null;
  const def = defOf(metric);
  // zoom to the incident: context before it (from the first data point of a newly added server), a little after it
  const all = m.data?.series[metric] ?? [];
  const t0 = new Date(inc.detected_at).getTime() / 1000;
  const t1 = inc.resolved_at ? new Date(inc.resolved_at).getTime() / 1000 : (m.data?.end ?? t0);
  const span = Math.max(t1 - t0, 60);
  let from = Math.max(m.data?.start ?? 0, all.length ? all[0][0] : 0, t0 - Math.max(1800, 2 * span));
  let to = Math.min(m.data?.end ?? t1, t1 + Math.max(600, span));
  let pts = all.filter(([t]) => t >= from && t <= to);
  if (pts.length < 4) [from, to, pts] = [m.data?.start ?? 0, m.data?.end ?? 0, all];  // an old incident: only coarse roll-ups are left
  const markers = [{ t: t0, label: "detected", color: "var(--crit)" }];
  if (inc.mitigated_at) markers.push({ t: new Date(inc.mitigated_at).getTime() / 1000, label: "fixed", color: "var(--arc)" });
  if (inc.resolved_at) markers.push({ t: new Date(inc.resolved_at).getTime() / 1000, label: "resolved", color: "var(--ok)" });
  return (
    <Card>
      <CardHeader title="The signal" sub={<>{def?.name ?? metric} on {inc.server_name} · {inc.evidence?.monitor ? `monitor “${inc.evidence.monitor}”` : inc.source}</>} />
      <div className="px-3 pb-3">
        <TimeChart series={[{ key: metric, label: def?.name ?? metric, color: serverColor(slot), points: pts }]} def={def}
          start={from} end={to > from ? to : m.data?.end ?? 0} height={170} loading={m.isFetching && !m.data} markers={markers} />
        {inc.evidence?.threshold != null && (
          <div className="mt-1.5 text-[12px] text-ink-3">
            Rule: {monitorRule({ metric, aggregation: inc.evidence.aggregation, operator: inc.evidence.operator, threshold: Number(inc.evidence.threshold),
              window_minutes: Number(inc.evidence.window_minutes) }, def)} · fired at <b className="text-ink-2">{inc.evidence.readable ?? inc.evidence.value}</b>
          </div>
        )}
      </div>
    </Card>
  );
}

export function IncidentPage() {
  const { id } = useParams();
  const { data: inc, isLoading } = useIncident(id);
  const me = useMe();
  const qc = useQueryClient();
  const toast = useToast();
  const [comment, setComment] = useState("");
  const [resolving, setResolving] = useState(false);
  const [resolveNote, setResolveNote] = useState("");
  const canOperate = me.data?.role !== "viewer";
  const servers = useServers();
  const catalog = useCatalog();
  const slot = servers.data?.find((s) => s.id === inc?.server_id)?.color_slot;
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["incident", id] });
    qc.invalidateQueries({ queryKey: ["incidents"] });
  };
  const act = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => api.post(`/incidents/${id}/${path}`, body ?? {}),
    onSuccess: (_d, v) => {
      refresh();
      toast({ "take-over": "You have the incident; rAIden stops working on it.", "hand-back": "Handed back to rAIden.", comment: "Comment added; rAIden reads it at its next step.", resolve: "Incident resolved." }[v.path] ?? "Done");
    },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const pending = useMemo(() => inc?.approvals.filter((a) => a.status === "pending") ?? [], [inc]);
  if (isLoading || !inc) return <Loading />;
  const open = inc.stage !== "resolved";
  const mtta = since(inc.detected_at, inc.acknowledged_at);
  const mttr = inc.resolved_at ? since(inc.detected_at, inc.resolved_at) : null;
  const ttm = inc.mitigated_at ? since(inc.detected_at, inc.mitigated_at) : null;
  return (
    <div>
      <Link to="/incidents" className="mb-3 inline-flex items-center gap-1.5 text-[13px] font-semibold text-ink-3 hover:text-ink"><ArrowLeft className="size-4" /> Incidents</Link>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-[13px]">
            <SeverityBadge severity={inc.severity} />
            <span className="mono text-ink-3">{inc.id}</span>
            {inc.server_name && <Link to={`/servers/${inc.server_id}`} className="hover:underline"><ServerTag name={inc.server_name} slot={slot} /></Link>}
            {inc.environment && <EnvBadge env={inc.environment} />}
            <Badge>{categoryLabel(inc.category)}</Badge>
            {inc.attempts > 0 && <Badge tone="warn">Came back {inc.attempts}×</Badge>}
          </div>
          <h1 className="display mt-1.5 text-[26px] leading-tight">{inc.title}</h1>
          <div className="mt-1"><StateLine inc={inc} /></div>
        </div>
        {canOperate && open && (
          <div className="flex flex-wrap gap-2">
            {inc.assignee === "raiden" || inc.assignee === "host-agent" ? (
              <Button onClick={() => act.mutate({ path: "take-over" })}><UserCheck className="size-4" /> Take over</Button>
            ) : (
              <Button variant="arc" onClick={() => act.mutate({ path: "hand-back", body: { text: "" } })}><Bot className="size-4" /> Hand back to rAIden</Button>
            )}
            <Button variant="ghost" onClick={() => setResolving(true)}>Resolve…</Button>
          </div>
        )}
      </div>

      <Card className="mb-4 px-4 py-4"><Stepper inc={inc} /></Card>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_400px]">
        <div className="min-w-0 space-y-4">
          {pending.map((a) => <ApprovalBox key={a.id} approval={a} canDecide={canOperate} />)}

          {/* a pending remediation gate already shows the root cause and the plan to decide on */}
          {(inc.root_cause || inc.hypothesis || inc.plan.length > 0) && !pending.some((a) => a.kind === "gate" && a.stage === "remediate") && (
            <Card>
              <CardHeader title={inc.root_cause ? "Root cause" : "Hypothesis"} />
              <div className="space-y-3 px-4 pb-4">
                <p className="text-[14px] text-ink">{inc.root_cause || inc.hypothesis}</p>
                {inc.plan.length > 0 && (
                  <div>
                    <div className="plate mb-1.5">Plan</div>
                    <PlanList items={inc.plan} />
                  </div>
                )}
              </div>
            </Card>
          )}

          <Card>
            <CardHeader title="Timeline" sub="Everything rAIden and people did, in order. Tool calls show their results." />
            <div className="px-4 pb-2">
              {inc.events.length ? <Timeline events={inc.events} autonomy={inc.autonomy} /> : <EmptyState title="Nothing yet" />}
            </div>
            {canOperate && open && (
              <div className="flex items-start gap-2 border-t border-line px-4 py-3">
                <Textarea rows={2} value={comment} onChange={(e) => setComment(e.target.value)} aria-label="Note"
                  placeholder="Add a note for the team and for rAIden (it reads notes at its next step)" />
                <Button variant="primary" disabled={!comment.trim()} loading={act.isPending && act.variables?.path === "comment"} aria-label="Add the note" title="Add the note"
                  onClick={() => act.mutate({ path: "comment", body: { text: comment } }, { onSuccess: () => setComment("") })}><Send className="size-4" /></Button>
              </div>
            )}
          </Card>

          {inc.report && (
            <Card>
              <CardHeader title="Incident report" sub="Written by rAIden from this timeline" />
              <div className="px-4 pb-4"><Markdown>{inc.report}</Markdown></div>
            </Card>
          )}
        </div>

        <div className="space-y-4">
          <Signal inc={inc} />
          <Card>
            <CardHeader title="Details" />
            <div className="px-4 pb-4">
              <KeyValue items={[
                ["Detected", <><Time iso={inc.detected_at} mode="date" /> by {inc.source === "monitor" ? `monitor “${inc.source_ref}”` : inc.source === "routine" ? `routine “${inc.source_ref}”` : inc.source}</>],
                ["Assigned to", inc.assignee === "raiden" ? <span className="font-semibold text-arc-ink">rAIden</span> : <span className="font-semibold text-lamp-ink">{personName(inc.assignee)}</span>],
                ["Acknowledged", mtta != null ? `after ${duration(mtta)}` : "—"],
                ["Fixed", ttm != null ? `after ${duration(ttm)}` : "—"],
                ["Resolved", mttr != null ? `after ${duration(mttr)}` : "—"],
                ["Platform", inc.platform ? catalog.data?.platforms.find((p) => p.id === inc.platform)?.name ?? titleCase(inc.platform) : "—"],
              ]} />
            </div>
          </Card>
          <Card>
            <CardHeader title="AI work" sub={inc.model ? `Model ${inc.model}` : "No model calls yet"} />
            <div className="grid grid-cols-3 gap-3 px-4 pb-4">
              <div><div className="text-[11.5px] text-ink-3">Tokens</div><div className="text-[17px] font-semibold num">{compact(inc.tokens_in + inc.tokens_out)}</div><div className="text-[11px] text-ink-3">{compact(inc.tokens_cached)} cached</div></div>
              <div><div className="text-[11.5px] text-ink-3">Cost</div><div className="text-[17px] font-semibold num">{money(inc.cost_usd)}</div></div>
              <div><div className="text-[11.5px] text-ink-3">Tool calls</div><div className="text-[17px] font-semibold num">{inc.tool_calls}</div><div className="text-[11px] text-ink-3">{inc.session?.turns ?? 0} turns</div></div>
            </div>
          </Card>
          {inc.hardening.length > 0 && (
            <Card>
              <CardHeader title="Protections" sub="So it cannot happen again, or is caught earlier" />
              <div className="px-4 pb-4"><HardeningList items={inc.hardening} /></div>
            </Card>
          )}
          {inc.approvals.filter((a) => a.status !== "pending").length > 0 && (
            <Card>
              <CardHeader title="Decisions" />
              <ul className="space-y-2 px-4 pb-4 text-[12.5px]">
                {inc.approvals.filter((a) => a.status !== "pending").map((a) => (
                  <li key={a.id} className="flex items-start gap-2">
                    <Badge tone={a.status === "approved" ? "ok" : a.status === "rejected" ? "crit" : "neutral"}>{titleCase(a.status)}</Badge>
                    <span className="min-w-0"><span className="font-semibold">{a.title}</span><span className="text-ink-3"> · {personName(a.decided_by)} · <Time iso={a.decided_at} /></span>
                      {a.comment && <span className="block text-ink-2">“{a.comment}”</span>}</span>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      </div>

      <Modal open={resolving} onClose={() => setResolving(false)} title={`Resolve ${inc.id}`}
        footer={<><Button variant="ghost" onClick={() => setResolving(false)}>Cancel</Button>
          <Button variant="primary" loading={act.isPending} onClick={() => act.mutate({ path: "resolve", body: { text: resolveNote } }, { onSuccess: () => setResolving(false) })}>Resolve incident</Button></>}>
        <p className="mb-3 text-[13.5px] text-ink-2">rAIden stops working on it and pending approvals are cancelled.</p>
        <Textarea rows={3} value={resolveNote} onChange={(e) => setResolveNote(e.target.value)} placeholder="What fixed it (shows as the summary)" />
      </Modal>
    </div>
  );
}

export type { Stage };
