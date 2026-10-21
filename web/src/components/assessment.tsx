import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, CheckCircle2, ClipboardCopy, Info, Lightbulb, Play, RotateCw, Wrench } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { api } from "../lib/api";
import { bytes, titleCase } from "../lib/format";
import { useCatalog } from "../lib/hooks";
import type { Assessment, Finding } from "../lib/types";
import { Badge, Button, Card, CardHeader, cn, Markdown, Modal, RiskBadge, Spinner, Time, useToast } from "./ui";

export const AREAS = ["configuration", "performance", "reliability", "security", "observability", "maintenance"] as const;
const AREA_TEXT: Record<string, string> = {
  configuration: "Memory, WAL and checkpoint sizing for this hardware",
  performance: "Cache, indexes, plans and query hot spots",
  reliability: "Durability, replication, backups and wraparound",
  security: "Logins, encryption, superusers and exposure",
  observability: "Statistics, logging and the extensions rAIden needs",
  maintenance: "Autovacuum, bloat, statistics and housekeeping",
};

export function scoreTone(score: number) {
  return score >= 85 ? "var(--ok)" : score >= 65 ? "var(--warn)" : "var(--crit)";
}

/** The overall score as a ring: the arc carries the band color, the number stays in ink. */
export function ScoreRing({ score, size = 104, label = "Health score" }: { score: number; size?: number; label?: string }) {
  const stroke = size < 60 ? 3.5 : 7;
  const r = size / 2 - stroke;
  const c = 2 * Math.PI * r;
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }} role="img" aria-label={`${label}: ${score} out of 100`}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-3)" strokeWidth={stroke} />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={scoreTone(score)} strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={`${(c * score) / 100} ${c}`} style={{ transition: "stroke-dasharray 600ms ease-out" }} />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className={cn("leading-none text-ink num", size < 60 ? "text-[11.5px] font-bold" : "display text-[30px]")}>{score}</span>
        {size >= 60 && <span className="mt-0.5 text-[10.5px] font-semibold tracking-wide text-ink-3 uppercase">of 100</span>}
      </div>
    </div>
  );
}

const SEV_ICON: Record<Finding["severity"], ReactNode> = {
  issue: <AlertTriangle className="size-4 text-crit" />,
  suggestion: <Lightbulb className="size-4 text-lamp" />,
  info: <Info className="size-4 text-info" />,
  ok: <CheckCircle2 className="size-4 text-ok" />,
};
const SEV_RANK = { issue: 0, suggestion: 1, info: 2, ok: 3 };

function CopyBlock({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <div className="group relative mt-1.5 rounded-lg border border-line bg-surface-2">
      <pre className="overflow-x-auto px-3 py-2 pr-10 font-mono text-[12px] whitespace-pre-wrap text-ink">{text}</pre>
      <button className="absolute top-1.5 right-1.5 inline-flex size-7 items-center justify-center rounded-md text-ink-3 hover:bg-surface-3 hover:text-ink"
        aria-label="Copy" title="Copy" onClick={() => { navigator.clipboard?.writeText(text); setDone(true); window.setTimeout(() => setDone(false), 1500); }}>
        {done ? <Check className="size-3.5 text-ok" /> : <ClipboardCopy className="size-3.5" />}
      </button>
    </div>
  );
}
export { CopyBlock };

type Apply = { title: string; action: string; args: Record<string, unknown>; key: string };

/** Fix of one finding: an action rAIden can run (a person clicks: they are the approval), SQL to run by hand, or provider steps. */
function FixLine({ fix, canApply, applied, onApply }: { fix: Finding["fix"]; canApply: boolean; applied: boolean; onApply: () => void }) {
  if (!fix || !fix.kind) return null;
  if (fix.kind === "action" && fix.action) {
    return (
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <code className="mono rounded bg-surface-3 px-1.5 py-0.5 text-[11.5px] text-ink-2">{fix.action}({Object.entries(fix.args ?? {}).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : v}`).join(", ")})</code>
        {applied ? <Badge tone="ok"><Check className="size-3" /> Applied</Badge>
          : canApply && <Button size="sm" variant="arc" onClick={onApply}><Wrench className="size-3.5" /> Apply</Button>}
      </div>
    );
  }
  return (
    <div className="mt-2">
      <div className="flex flex-wrap items-center gap-2 text-[12.5px] text-ink-2">
        <span className="font-semibold text-ink">{fix.kind === "provider" ? "In your cloud console:" : "Run by hand:"}</span>
        {fix.how && fix.kind === "provider" && <span>{fix.how}</span>}
        {fix.restart && <Badge tone="warn"><RotateCw className="size-3" /> Needs a restart</Badge>}
      </div>
      {fix.sql && <CopyBlock text={fix.sql} />}
      {!fix.sql && fix.how && fix.kind === "manual" && <CopyBlock text={fix.how} />}
    </div>
  );
}

export function AssessmentView({ assessment: a, serverId, serverName, canApply, onRerun, rerunning }: {
  assessment: Assessment; serverId: string; serverName: string; canApply: boolean; onRerun?: () => void; rerunning?: boolean;
}) {
  const [showOk, setShowOk] = useState(false);
  const [applied, setApplied] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<Apply | null>(null);
  const catalog = useCatalog();
  const qc = useQueryClient();
  const toast = useToast();
  const actionDef = (id: string) => catalog.data?.actions.find((x) => x.id === id);
  const run = useMutation({
    mutationFn: (x: Apply) => api.post<{ message: string }>(`/servers/${serverId}/actions`, { action: x.action, args: x.args, reason: `assessment #${a.id}: ${x.title}` }),
    onSuccess: (res, x) => {
      setApplied((s) => new Set(s).add(x.key));
      setConfirm(null);
      toast(`Applied on ${serverName}: ${res.message}`);
      for (const k of ["server", "estate", "live"]) qc.invalidateQueries({ queryKey: [k] });
    },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const findings = useMemo(() => [...(a.findings ?? [])].sort((x, y) => SEV_RANK[x.severity] - SEV_RANK[y.severity]), [a.findings]);
  // sizing findings are already rows of the sizing table: the area cards point there instead of repeating them
  const sized = (f: Finding) => f.id.startsWith("tune-") && (a.tuning ?? []).length > 0;
  const okCount = findings.filter((f) => f.severity === "ok").length;
  const facts = a.facts ?? {};
  const host = (facts.host ?? {}) as Record<string, any>;

  if (a.status === "running") {
    return (
      <Card className="px-6 py-8">
        <div className="flex items-center gap-3">
          <Spinner className="size-5 text-arc" />
          <div>
            <div className="text-[15px] font-semibold">Assessing {serverName}…</div>
            <div className="text-[13px] text-ink-3">Reading settings, statistics, roles, replication and maintenance history. Nothing is changed.</div>
          </div>
        </div>
        <div className="mt-5 grid gap-2 sm:grid-cols-3">
          {AREAS.map((area, i) => (
            <div key={area} className="rise flex items-center gap-2 rounded-lg border border-line bg-surface-2 px-3 py-2 text-[12.5px]" style={{ animationDelay: `${i * 120}ms` }}>
              <span className="working inline-block size-2 rounded-full bg-arc" style={{ animationDelay: `${i * 160}ms` }} />
              <span className="font-semibold">{titleCase(area)}</span>
            </div>
          ))}
        </div>
      </Card>
    );
  }
  if (a.status === "error") {
    return (
      <Card className="px-5 py-5">
        <div className="flex items-start gap-2 text-[13.5px] text-crit"><AlertTriangle className="mt-0.5 size-4" /> The assessment failed: {a.error}</div>
        {onRerun && <Button className="mt-3" onClick={onRerun} loading={rerunning}><Play className="size-4" /> Run again</Button>}
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <div className="flex flex-wrap items-center gap-6">
          <ScoreRing score={a.score} />
          <div className="min-w-[240px] flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="display text-[20px]">{a.score >= 85 ? "In good shape" : a.score >= 65 ? "Needs some work" : "Needs attention"}</span>
              <Badge tone="crit">{a.counts.issue} issue{a.counts.issue === 1 ? "" : "s"}</Badge>
              <Badge tone="lamp">{a.counts.suggestion} suggestion{a.counts.suggestion === 1 ? "" : "s"}</Badge>
            </div>
            <div className="mt-1 text-[12.5px] text-ink-3">
              {titleCase(a.trigger)} assessment by {a.triggered_by || "rAIden"} · <Time iso={a.finished_at ?? a.created_at} mode="date" />
              {facts.version && <> · {String(facts.version).split(" ").slice(0, 2).join(" ")}</>}
              {host.ram_gb && <> · sized for {host.ram_gb} GB RAM{host.vcpus ? `, ${host.vcpus} vCPU` : ""}{host.storage ? `, ${String(host.storage).toUpperCase()}` : ""}{host.workload ? `, ${String(host.workload).toUpperCase()}` : ""}</>}
            </div>
            {onRerun && <Button className="mt-3" size="sm" onClick={onRerun} loading={rerunning}><RotateCw className="size-3.5" /> Assess again</Button>}
          </div>
          <div className="grid min-w-[300px] flex-1 grid-cols-2 gap-x-5 gap-y-2.5">
            {AREAS.map((area) => {
              const v = a.scores[area] ?? 100;
              return (
                <div key={area} title={AREA_TEXT[area]}>
                  <div className="flex items-baseline justify-between text-[12.5px]"><span className="font-semibold text-ink-2">{titleCase(area)}</span><span className="font-semibold num">{v}</span></div>
                  <div className="mt-1 h-1.5 rounded-full bg-surface-3"><div className="h-full rounded-full" style={{ width: `${v}%`, background: scoreTone(v) }} /></div>
                </div>
              );
            })}
          </div>
        </div>
      </Card>

      {a.summary && (
        <Card>
          <CardHeader title="rAIden's read" sub="What to do first, and what can wait" />
          <div className="px-4 pb-4"><Markdown>{a.summary}</Markdown></div>
        </Card>
      )}

      {(a.tuning ?? []).length > 0 && (
        <Card id="sizing" className="scroll-mt-4">
          <CardHeader title="Sizing for this server" sub="PGTune-style starting points from the host profile and workload. Reloadable settings apply directly; the rest wait for a restart." />
          <div className="overflow-x-auto px-1 pb-2">
            <table className="w-full text-left text-[12.5px]">
              <thead className="text-[11.5px] text-ink-3"><tr>{["Setting", "Now", "Recommended", "Why", ""].map((h) => <th key={h} className="px-3 py-1.5 font-semibold">{h}</th>)}</tr></thead>
              <tbody>
                {a.tuning!.map((t) => {
                  const key = `tune:${t.name}`;
                  return (
                    <tr key={t.name} className="border-t border-line align-top">
                      <td className="px-3 py-2 font-mono text-[12px] font-semibold">{t.name}</td>
                      <td className="px-3 py-2 text-ink-3 num">{t.current}</td>
                      <td className="px-3 py-2 font-semibold text-ink num">{t.recommended}</td>
                      <td className="px-3 py-2 text-ink-2">{t.reason}{t.restart && <Badge tone="warn" className="ml-2"><RotateCw className="size-3" /> restart</Badge>}</td>
                      <td className="px-3 py-2 text-right whitespace-nowrap">
                        {t.fix?.kind === "action" && t.fix.action ? (
                          applied.has(key) ? <Badge tone="ok"><Check className="size-3" /> Applied</Badge>
                            : canApply && <Button size="sm" variant="arc" onClick={() => setConfirm({ title: `${t.name} = ${t.recommended}`, action: t.fix.action!, args: t.fix.args ?? {}, key })}>Apply</Button>
                        ) : t.fix?.how ? <span className="text-[11.5px] text-ink-3" title={t.fix.how}>{t.fix.kind === "provider" ? "Cloud console" : "By hand"}</span> : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {AREAS.map((area) => {
        const list = findings.filter((f) => f.area === area && !sized(f) && (showOk || f.severity !== "ok"));
        const tuned = findings.filter((f) => f.area === area && sized(f)).length;
        if (!list.length && !tuned) return null;
        return (
          <Card key={area}>
            <CardHeader title={titleCase(area)} sub={AREA_TEXT[area]} actions={<span className="text-[13px] font-semibold num" style={{ color: scoreTone(a.scores[area] ?? 100) }}>{a.scores[area] ?? 100}</span>} />
            <ul className="divide-y divide-line px-4 pb-2">
              {list.map((f) => (
                <li key={f.id} className="flex gap-3 py-3">
                  <span className="mt-0.5 shrink-0">{SEV_ICON[f.severity]}</span>
                  <div className="min-w-0 flex-1">
                    <div className={cn("text-[13.5px] font-semibold", f.severity === "ok" ? "text-ink-2" : "text-ink")}>{f.title}</div>
                    {f.detail && <div className="mt-0.5 text-[13px] text-ink-2">{f.detail}</div>}
                    {f.recommendation && f.severity !== "ok" && <div className="mt-1 text-[13px]"><span className="font-semibold">Recommendation: </span><span className="text-ink-2">{f.recommendation}</span></div>}
                    {f.evidence && <div className="mt-1 font-mono text-[11.5px] break-words text-ink-3">{f.evidence}</div>}
                    {f.severity !== "ok" && (
                      <FixLine fix={f.fix} canApply={canApply} applied={applied.has(f.id)}
                        onApply={() => setConfirm({ title: f.title, action: f.fix.action!, args: f.fix.args ?? {}, key: f.id })} />
                    )}
                  </div>
                </li>
              ))}
            </ul>
            {tuned > 0 && (
              <div className={cn("px-4 py-2.5 text-[12.5px] text-ink-3", list.length > 0 && "border-t border-line")}>
                {tuned} sizing change{tuned === 1 ? "" : "s"} for this area{" "}
                <button className="font-semibold text-arc-ink hover:underline" onClick={() => document.getElementById("sizing")?.scrollIntoView({ behavior: "smooth", block: "start" })}>
                  in the sizing table</button>.
              </div>
            )}
          </Card>
        );
      })}
      {okCount > 0 && (
        <button className="text-[12.5px] font-semibold text-arc-ink hover:underline" onClick={() => setShowOk((v) => !v)}>
          {showOk ? "Hide passed checks" : `Show ${okCount} passed check${okCount === 1 ? "" : "s"}`}
        </button>
      )}
      {facts.databases && Array.isArray(facts.databases) && facts.databases.length > 0 && (
        <p className="text-[12px] text-ink-3">Databases: {facts.databases.map((d: any) => `${d.name} (${bytes(d.size_bytes)})`).join(", ")}</p>
      )}

      <Modal open={!!confirm} onClose={() => setConfirm(null)} title="Apply this fix?"
        footer={<><Button variant="ghost" onClick={() => setConfirm(null)}>Cancel</Button><Button variant="arc" loading={run.isPending} onClick={() => confirm && run.mutate(confirm)}>Apply on {serverName}</Button></>}>
        {confirm && (
          <div className="space-y-3 text-[13.5px]">
            <div className="font-semibold">{confirm.title}</div>
            <div className="rounded-lg border border-line bg-surface-2 px-3 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{actionDef(confirm.action)?.title ?? confirm.action}</span>
                {actionDef(confirm.action) && <RiskBadge risk={actionDef(confirm.action)!.risk} />}
              </div>
              <code className="mono mt-1 block text-[12px] text-ink-2">{confirm.action}({Object.entries(confirm.args).map(([k, v]) => `${k}=${v}`).join(", ")})</code>
              {actionDef(confirm.action)?.description && <div className="mt-1 text-[12.5px] text-ink-3">{actionDef(confirm.action)!.description}</div>}
            </div>
            <p className="text-[12.5px] text-ink-3">It runs now with rAIden's login on {serverName}. You are the approval: the change is recorded under your name in the audit log.</p>
          </div>
        )}
      </Modal>
    </div>
  );
}
