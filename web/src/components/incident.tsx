import {
  AlertOctagon, Bot, Check, CircleDot, Clock, Eye, Hand, MessageSquare, Radar, ShieldCheck, Signal, Terminal, User as UserIcon, Wrench, X, Zap,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { duration, personName, titleCase, until } from "../lib/format";
import { useDecide } from "../lib/hooks";
import type { Approval, HardenItem, IncidentCard, IncidentEvent, Mode, PlanItem, Stage } from "../lib/types";
import { Badge, Button, cn, DataTable, Markdown, RiskBadge, Textarea, Time, useToast } from "./ui";

export const STAGES: { id: Stage; title: string }[] = [
  { id: "detected", title: "Detected" },
  { id: "triage", title: "Triage" },
  { id: "diagnose", title: "Diagnose" },
  { id: "remediate", title: "Remediate" },
  { id: "verify", title: "Verify" },
  { id: "harden", title: "Harden" },
  { id: "resolved", title: "Resolved" },
];
export const STAGE_TITLE = Object.fromEntries(STAGES.map((s) => [s.id, s.title])) as Record<Stage, string>;
export const STATE_HINT: Record<Stage, string> = {
  detected: "Nothing waiting to start.",
  triage: "Nothing being triaged.",
  diagnose: "No investigation running.",
  remediate: "No fix in progress.",
  verify: "Nothing under observation.",
  harden: "No protections being added.",
  resolved: "Nothing resolved in the last two days.",
};

export function StateLine({ inc, now = Date.now() }: { inc: IncidentCard; now?: number }) {
  if (inc.stage === "resolved") {
    const took = inc.resolved_at ? (new Date(inc.resolved_at).getTime() - new Date(inc.detected_at).getTime()) / 1000 : null;
    return <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-ok"><Check className="size-3.5" /> Resolved{took != null ? ` in ${duration(took)}` : ""}</span>;
  }
  if (inc.state === "waiting") {
    return (
      <span className="flex max-w-full min-w-0 items-center gap-1.5 text-[12px] font-semibold text-lamp-ink" title={inc.waiting_reason || undefined}>
        <span className="lamp-glow inline-block size-2 shrink-0 rounded-full bg-lamp" />
        <span className="truncate">{inc.waiting_reason || "Waiting for a person"}</span>
      </span>
    );
  }
  if (inc.state === "escalated") {
    return (
      <span className="flex max-w-full min-w-0 items-center gap-1.5 text-[12px] font-semibold text-crit">
        <UserIcon className="size-3.5 shrink-0" /><span className="truncate">With {inc.assignee === "on-call" ? "on-call" : personName(inc.assignee)}{inc.waiting_reason ? `: ${inc.waiting_reason}` : ""}</span>
      </span>
    );
  }
  if (inc.state === "running") {
    return <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-arc-ink"><span className="working inline-block size-2 rounded-full bg-arc" /> rAIden is working</span>;
  }
  if (inc.stage === "verify" && inc.verify_until) {
    return <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-arc-ink"><Eye className="size-3.5" /> Watching the metrics · done {until(inc.verify_until, now)}</span>;
  }
  if (inc.assignee === "host-agent") return <span className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-arc-ink"><Bot className="size-3.5" /> Host agent is on it</span>;
  return <span className="inline-flex items-center gap-1.5 text-[12px] font-medium text-ink-3"><Clock className="size-3.5" /> Queued for rAIden</span>;
}

export function PlanList({ items, compact }: { items: PlanItem[]; compact?: boolean }) {
  if (!items.length) return <div className="text-[12.5px] text-ink-3">No actions planned.</div>;
  return (
    <ol className="space-y-1.5">
      {items.map((p, i) => (
        <li key={p.id ?? i} className="rounded-lg border border-line bg-surface-2 px-3 py-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="flex size-5 items-center justify-center rounded-full bg-surface-3 text-[11px] font-bold text-ink-2">{i + 1}</span>
            <span className="text-[13px] font-semibold text-ink">{p.title}</span>
            <code className="mono rounded bg-surface-3 px-1.5 py-0.5 text-[11.5px] text-ink-2">{p.action}({Object.entries(p.args).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : v}`).join(", ")})</code>
            <RiskBadge risk={p.risk} />
            {p.status && p.status !== "proposed" && <PlanStatus status={p.status} />}
          </div>
          {!compact && p.rationale && <div className="mt-1 pl-7 text-[12.5px] text-ink-2">{p.rationale}</div>}
          {!compact && p.result && <div className="mt-1 pl-7 text-[12px] text-ink-3"><span className="font-semibold">Result:</span> {p.result}</div>}
        </li>
      ))}
    </ol>
  );
}

function PlanStatus({ status }: { status: string }) {
  const tone = status === "done" || status === "applied" ? "ok" : status === "failed" ? "crit" : status === "rejected" || status === "skipped" ? "neutral" : status === "approved" || status === "suggested" ? "arc" : "neutral";
  return <Badge tone={tone as any}>{titleCase(status)}</Badge>;
}

export function HardeningList({ items }: { items: HardenItem[] }) {
  if (!items.length) return <div className="text-[12.5px] text-ink-3">No protections proposed.</div>;
  const icon = { monitor: <Radar className="size-3.5" />, action: <Wrench className="size-3.5" />, routine: <Clock className="size-3.5" />, lesson: <MessageSquare className="size-3.5" />,
    recommendation: <UserIcon className="size-3.5" /> };
  return (
    <ul className="space-y-1.5">
      {items.map((h) => (
        <li key={h.id} className="rounded-lg border border-line bg-surface-2 px-3 py-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1 text-[11.5px] font-semibold text-ink-3 uppercase">{icon[h.kind]} {h.kind === "recommendation" ? "for a person" : h.kind}</span>
            <span className="text-[13px] font-semibold">{h.title}</span>
            {h.risk && h.risk !== "low" && <RiskBadge risk={h.risk} />}
            {h.status !== "proposed" && <PlanStatus status={h.status} />}
          </div>
          {h.detail && <div className="mt-1 text-[12.5px] text-ink-2">{h.detail}</div>}
          {h.kind === "recommendation" && typeof h.spec?.how === "string" && (
            <pre className="mono mt-1.5 overflow-x-auto rounded-md bg-surface-3 px-2.5 py-1.5 text-[11.5px] text-ink-2">{h.spec.how}</pre>
          )}
          {h.result && h.status !== "proposed" && <div className="mt-0.5 text-[12px] text-ink-3">{h.result}</div>}
        </li>
      ))}
    </ul>
  );
}

/** The decision box: what rAIden wants to do, how risky it is, and the two buttons. */
export function ApprovalBox({ approval, canDecide, compact }: { approval: Approval; canDecide: boolean; compact?: boolean }) {
  const decide = useDecide();
  const toast = useToast();
  const [comment, setComment] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const p = approval.payload || {};
  const go = (approve: boolean) =>
    decide.mutate({ id: approval.id, approve, comment }, {
      onSuccess: () => toast(approve ? `Approved: ${approval.title}` : `Rejected: ${approval.title}`),
      onError: (e) => toast((e as Error).message, "crit"),
    });
  const decided = approval.status !== "pending";
  return (
    <div className={cn("rounded-xl border p-3.5", decided ? "border-line bg-surface-2" : "border-lamp-line bg-lamp-soft/60")}>
      <div className="flex flex-wrap items-center gap-2">
        <Hand className={cn("size-4", decided ? "text-ink-3" : "text-lamp-ink")} />
        <span className="text-[13.5px] font-semibold text-ink">{approval.title}</span>
        <RiskBadge risk={approval.risk} />
        <span className="text-[12px] text-ink-3">· asked <Time iso={approval.requested_at} /></span>
      </div>
      {!compact && (
        <div className="mt-2.5 space-y-2.5">
          {p.root_cause && <div className="text-[13px] text-ink-2"><span className="font-semibold text-ink">Root cause: </span>{p.root_cause}</div>}
          {Array.isArray(p.plan) && <PlanList items={p.plan} />}
          {Array.isArray(p.items) && <HardeningList items={p.items} />}
          {p.action && (
            <div className="rounded-lg border border-line bg-surface px-3 py-2 text-[13px]">
              <code className="mono text-[12px]">{p.action}({Object.entries(p.args || {}).map(([k, v]) => `${k}=${v}`).join(", ")})</code>
              {p.rationale && <div className="mt-1 text-ink-2">{p.rationale}</div>}
              {p.reason && <div className="mt-1 text-[12px] text-ink-3">Needs a person because: {p.reason}.</div>}
            </div>
          )}
          {!p.plan && !p.items && !p.action && approval.summary && <Markdown className="text-[13px]">{approval.summary}</Markdown>}
        </div>
      )}
      {decided ? (
        <div className="mt-2.5 flex flex-wrap items-center gap-2 text-[12.5px] text-ink-3">
          <Badge tone={approval.status === "approved" ? "ok" : approval.status === "rejected" ? "crit" : "neutral"}>{titleCase(approval.status)}</Badge>
          <span>by {personName(approval.decided_by)} · <Time iso={approval.decided_at} /></span>
          {approval.comment && <span className="text-ink-2">“{approval.comment}”</span>}
        </div>
      ) : canDecide ? (
        <div className="mt-3">
          {rejecting && <Textarea rows={2} className="mb-2" placeholder="Why not? rAIden reads this and tries another route." value={comment} onChange={(e) => setComment(e.target.value)} autoFocus />}
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="lamp" size="sm" loading={decide.isPending && !rejecting} onClick={() => go(true)}><Check className="size-3.5" /> Approve</Button>
            {rejecting ? (
              <>
                <Button variant="danger" size="sm" loading={decide.isPending} onClick={() => go(false)}><X className="size-3.5" /> Reject</Button>
                <Button variant="ghost" size="sm" onClick={() => setRejecting(false)}>Cancel</Button>
              </>
            ) : (
              <Button variant="outline" size="sm" onClick={() => setRejecting(true)}>Reject…</Button>
            )}
          </div>
        </div>
      ) : (
        <div className="mt-2 text-[12px] text-ink-3">An operator or admin decides.</div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ the timeline
const KIND: Record<string, { icon: ReactNode; tone: string }> = {
  detected: { icon: <AlertOctagon className="size-3.5" />, tone: "text-crit bg-crit-soft border-crit/25" },
  stage: { icon: <CircleDot className="size-3.5" />, tone: "text-ink-2 bg-surface-3 border-line" },
  autopilot: { icon: <Zap className="size-3.5" />, tone: "text-arc-ink bg-arc-soft border-arc-line" },
  thought: { icon: <Bot className="size-3.5" />, tone: "text-arc-ink bg-arc-soft border-arc-line" },
  tool: { icon: <Terminal className="size-3.5" />, tone: "text-ink-2 bg-surface-2 border-line" },
  triage: { icon: <Radar className="size-3.5" />, tone: "text-arc-ink bg-arc-soft border-arc-line" },
  diagnosis: { icon: <Radar className="size-3.5" />, tone: "text-arc-ink bg-arc-soft border-arc-line" },
  approval: { icon: <Hand className="size-3.5" />, tone: "text-lamp-ink bg-lamp-soft border-lamp-line" },
  decision: { icon: <UserIcon className="size-3.5" />, tone: "text-lamp-ink bg-lamp-soft border-lamp-line" },
  action: { icon: <Wrench className="size-3.5" />, tone: "text-ink bg-surface-3 border-line-strong" },
  remediation: { icon: <Wrench className="size-3.5" />, tone: "text-arc-ink bg-arc-soft border-arc-line" },
  verify: { icon: <Eye className="size-3.5" />, tone: "text-ink-2 bg-surface-3 border-line" },
  verification: { icon: <Eye className="size-3.5" />, tone: "text-ok bg-ok-soft border-ok/25" },
  hardening: { icon: <ShieldCheck className="size-3.5" />, tone: "text-arc-ink bg-arc-soft border-arc-line" },
  hardened: { icon: <ShieldCheck className="size-3.5" />, tone: "text-ok bg-ok-soft border-ok/25" },
  report: { icon: <MessageSquare className="size-3.5" />, tone: "text-arc-ink bg-arc-soft border-arc-line" },
  escalated: { icon: <AlertOctagon className="size-3.5" />, tone: "text-crit bg-crit-soft border-crit/25" },
  signal: { icon: <Signal className="size-3.5" />, tone: "text-ink-2 bg-surface-3 border-line" },
  recurrence: { icon: <AlertOctagon className="size-3.5" />, tone: "text-warn bg-warn-soft border-warn/25" },
  comment: { icon: <MessageSquare className="size-3.5" />, tone: "text-ink-2 bg-surface-3 border-line" },
  takeover: { icon: <UserIcon className="size-3.5" />, tone: "text-lamp-ink bg-lamp-soft border-lamp-line" },
  handback: { icon: <Bot className="size-3.5" />, tone: "text-arc-ink bg-arc-soft border-arc-line" },
  error: { icon: <AlertOctagon className="size-3.5" />, tone: "text-crit bg-crit-soft border-crit/25" },
  denied: { icon: <AlertOctagon className="size-3.5" />, tone: "text-crit bg-crit-soft border-crit/25" },
};

export function EventIcon({ kind, className }: { kind: string; className?: string }) {
  const k = KIND[kind] ?? KIND.stage!;
  return <span className={cn("relative z-[1] flex size-7 shrink-0 items-center justify-center rounded-full border", k.tone, className)}>{k.icon}</span>;
}

export function Actor({ actor }: { actor: string }) {
  if (actor === "raiden") return <span className="font-semibold text-arc-ink">rAIden</span>;
  if (actor === "autopilot") return <span className="font-semibold text-arc-ink">Autopilot</span>;
  if (actor === "system") return <span className="font-semibold text-ink-3">System</span>;
  if (actor === "host-agent") return <span className="font-semibold text-arc-ink">Host agent</span>;
  return <span className="font-semibold text-lamp-ink">{personName(actor)}</span>;
}

function ToolEvent({ ev }: { ev: IncidentEvent }) {
  const [open, setOpen] = useState(false);
  const preview = ev.data?.preview;
  const hasTable = preview && typeof preview === "object" && Array.isArray(preview.rows);
  return (
    <div>
      <button onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-2 text-left text-[12.5px] text-ink-2 hover:text-ink" disabled={!preview}>
        <span className="mono truncate">{ev.title}</span>
        {ev.data?.ms != null && <span className="shrink-0 text-ink-3 num">{ev.data.ms} ms</span>}
        {preview && <span className="shrink-0 text-[11.5px] font-semibold text-arc-ink">{open ? "hide" : "show"}</span>}
      </button>
      {open && preview && (
        <div className="mt-1.5 max-h-72 overflow-auto rounded-lg border border-line bg-surface">
          {hasTable ? <DataTable data={preview} dense /> : <pre className="p-2 text-[11.5px] whitespace-pre-wrap">{JSON.stringify(preview, null, 2)}</pre>}
        </div>
      )}
    </div>
  );
}

export function Timeline({ events, autonomy }: { events: IncidentEvent[]; autonomy?: Record<string, Mode> }) {
  return (
    <ol className="relative">
      {events.map((ev, i) => {
        const k = KIND[ev.kind] ?? KIND.stage!;
        const isStage = ev.kind === "stage";
        const isTool = ev.kind === "tool";
        const last = i === events.length - 1;
        if (isStage) {
          const to = (ev.data?.to as Stage) ?? ev.stage;
          return (
            <li key={ev.id} className="relative flex items-center gap-3 py-2 pl-0">
              <div className="h-px flex-1 bg-line" />
              <span className="plate inline-flex items-center gap-1.5 text-[10.5px]">
                {STAGE_TITLE[to] ?? to}
                {autonomy?.[to] === "human" && <Hand className="size-3 text-lamp-ink" aria-label="needs a person" />}
              </span>
              <span className="text-[11.5px] text-ink-3 num"><Time iso={ev.ts} mode="clock" /></span>
              <div className="h-px flex-1 bg-line" />
            </li>
          );
        }
        return (
          <li key={ev.id} className={cn("relative flex gap-3 pb-3", isTool ? "pl-9" : "")}>
            {!last && <span className="absolute top-7 bottom-0 left-[13px] w-px bg-line" aria-hidden />}
            {!isTool && (
              <span className={cn("relative z-[1] mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border", k.tone)}>{k.icon}</span>
            )}
            <div className="min-w-0 flex-1">
              {isTool ? (
                <div className="flex items-start gap-2"><Terminal className="mt-0.5 size-3.5 shrink-0 text-ink-3" /><div className="min-w-0 flex-1"><ToolEvent ev={ev} /></div></div>
              ) : (
                <>
                  <div className="flex flex-wrap items-baseline gap-x-2 text-[12px] text-ink-3">
                    <Actor actor={ev.actor} />
                    <span className="num"><Time iso={ev.ts} mode="clock" /></span>
                  </div>
                  <div className={cn("mt-0.5 text-[13.5px] text-ink", ev.kind === "thought" ? "italic text-ink-2" : "font-semibold")}>{ev.kind === "thought" ? `“${ev.body || ev.title}”` : ev.title}</div>
                  {ev.body && ev.kind !== "thought" && <Markdown className="mt-1 text-[13px] text-ink-2">{ev.body}</Markdown>}
                  {ev.kind === "action" && ev.data?.approved_by && (
                    <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11.5px] text-ink-3">
                      <RiskBadge risk={ev.data.risk ?? "low"} /> {ev.data.ok === false ? <Badge tone="crit">Failed</Badge> : <Badge tone="ok">Done</Badge>}
                      <span>approved by {personName(ev.data.approved_by)}</span>
                      {ev.data.ms != null && <span className="num">· {duration(ev.data.ms / 1000)}</span>}
                    </div>
                  )}
                </>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
