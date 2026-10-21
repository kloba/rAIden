import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, CalendarClock, ListChecks, MoonStar, Play, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import {
  Badge, Button, Card, cn, Drawer, EmptyState, ErrorNote, Field, Input, Loading, Markdown, PageHeader, Segmented, Select, ServerTag, Switch, Textarea, Time, useToast,
} from "../components/ui";
import { api } from "../lib/api";
import { compact, duration, titleCase, until } from "../lib/format";
import { useMe, useRoutines, useRun, useServers } from "../lib/hooks";
import type { Env, Finding, Routine, RoutineRun } from "../lib/types";

const RUN_TONE: Record<string, string> = { ok: "var(--ok)", warn: "var(--warn)", crit: "var(--crit)", error: "var(--crit)", running: "var(--arc)" };
const RUN_LABEL: Record<string, string> = { ok: "All clear", warn: "Findings", crit: "Critical findings", error: "Failed", running: "Running" };
const ON_FINDING: Record<string, string> = { suggest: "Findings become suggestions on the server", incident: "Critical findings open an incident", report: "Writes a report" };
const KIND_LABEL: Record<Routine["kind"], string> = { agent: "AI review", check: "Checks", dream: "Learning" };

function scopeText(r: Routine) {
  if (r.kind === "dream") return "The whole fleet";
  const envs = r.scope.environments ?? [];
  const servers = r.scope.servers ?? [];
  if (servers.length) return `${servers.length} server${servers.length > 1 ? "s" : ""}`;
  if (envs.length) return `${envs.map(titleCase).join(" and ")} servers`;
  return "Every server";
}

function RunStrip({ runs, onOpen }: { runs: RoutineRun[]; onOpen: (id: number) => void }) {
  const list = [...runs].reverse();
  return (
    <div role="group" className="flex items-end gap-[3px]" aria-label="Recent runs, oldest first">
      {list.map((r) => (
        <button key={r.id} onClick={() => onOpen(r.id)} title={`${RUN_LABEL[r.status] ?? r.status} · ${new Date(r.started_at).toLocaleString("en-GB")}${r.summary ? `\n${r.summary}` : ""}`}
          className={cn("h-5 w-2.5 rounded-[3px] transition-transform hover:scale-y-110", r.status === "running" && "working")}
          style={{ background: RUN_TONE[r.status] ?? "var(--line-strong)", opacity: r.status === "ok" ? 0.55 : 1 }} />
      ))}
      {Array.from({ length: Math.max(0, 12 - list.length) }, (_, i) => <span key={`e${i}`} className="h-5 w-2.5 rounded-[3px] bg-surface-3" />)}
    </div>
  );
}

function FindingRow({ f }: { f: Finding }) {
  return (
    <li className="flex gap-2 py-1.5 text-[12.5px]">
      <Badge tone={f.severity === "issue" ? "crit" : f.severity === "suggestion" ? "lamp" : "neutral"}>{f.severity}</Badge>
      <span className="min-w-0"><span className="font-semibold">{f.title}</span>{f.detail && <span className="text-ink-3"> · {f.detail}</span>}</span>
    </li>
  );
}

function RunDrawer({ id, routine, onClose }: { id: number | null; routine?: Routine; onClose: () => void }) {
  const run = useRun(id);
  const servers = useServers();
  const byId = new Map((servers.data ?? []).map((s) => [s.id, s]));
  const r = run.data;
  return (
    <Drawer open={id != null} onClose={onClose} width={640} title={routine?.name ?? "Run"} sub={r ? <>{RUN_LABEL[r.status] ?? r.status} · started <Time iso={r.started_at} mode="date" /> by {r.triggered_by}{r.finished_at && ` · took ${duration((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000)}`}{r.tokens ? ` · ${compact(r.tokens)} tokens` : ""}</> : undefined}>
      {!r ? <Loading /> : (
        <div className="space-y-4">
          {r.summary && <div className="rounded-lg bg-surface-2 px-3 py-2 text-[13px]">{r.summary}</div>}
          {r.report && <Markdown>{r.report}</Markdown>}
          {(r.results ?? []).map((x) => {
            const s = byId.get(x.server_id);
            return (
              <div key={x.server_id} className="rounded-xl border border-line">
                <div className="flex items-center gap-2 border-b border-line px-3 py-2">
                  {s ? <Link to={`/servers/${s.id}`} className="hover:underline"><ServerTag name={s.name} slot={s.color_slot} /></Link> : <span className="font-semibold">{x.server_id}</span>}
                  <span className="ml-auto inline-flex items-center gap-1.5 text-[12px] font-semibold" style={{ color: RUN_TONE[x.status] ?? "var(--ink-3)" }}>
                    <span className="size-2 rounded-full" style={{ background: RUN_TONE[x.status] ?? "var(--ink-3)" }} />{RUN_LABEL[x.status] ?? x.status}
                  </span>
                </div>
                <div className="px-3 py-2">
                  {x.summary && <Markdown className="text-[13px]">{x.summary}</Markdown>}
                  {x.findings.length > 0 && <ul className="mt-1 divide-y divide-line">{x.findings.map((f, i) => <FindingRow key={i} f={f} />)}</ul>}
                  {x.assessment_id && <Link to={`/servers/${x.server_id}?tab=assessment`} className="mt-1 inline-block text-[12.5px] font-semibold text-arc-ink hover:underline">Open the assessment</Link>}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Drawer>
  );
}

const CHECK_OPTIONS: { id: string; label: string }[] = [
  { id: "metrics", label: "Key metrics against their thresholds" },
  { id: "area:configuration", label: "Configuration and sizing" },
  { id: "area:performance", label: "Performance" },
  { id: "area:reliability", label: "Reliability (durability, replication, backups)" },
  { id: "area:security", label: "Security posture" },
  { id: "area:observability", label: "Observability" },
  { id: "area:maintenance", label: "Maintenance (vacuum, bloat, statistics)" },
  { id: "assessment", label: "The full assessment, with sizing" },
];

interface Draft { id?: number; name: string; description: string; kind: Routine["kind"]; checks: string[]; prompt: string; cron: string; timezone: string; environments: Env[]; on_finding: string; enabled: boolean }
const blank: Draft = { name: "", description: "", kind: "agent", checks: ["metrics"], prompt: "", cron: "0 7 * * *", timezone: "UTC", environments: [], on_finding: "suggest", enabled: true };

function Editor({ draft, onClose }: { draft: Draft | null; onClose: () => void }) {
  const [d, setD] = useState<Draft>(draft ?? blank);
  const templates = useQuery({ queryKey: ["routine-templates"], queryFn: () => api.get<(Routine & { key: string })[]>("/routines/templates"), enabled: !!draft && !draft.id });
  const qc = useQueryClient();
  const toast = useToast();
  const save = useMutation({
    mutationFn: () => {
      const body = d.kind === "dream" ? { name: d.name, description: d.description, cron: d.cron.trim(), timezone: d.timezone, enabled: d.enabled } : {
        name: d.name, description: d.description, kind: d.kind, checks: d.kind === "check" ? d.checks : [], prompt: d.kind === "agent" ? d.prompt : "", cron: d.cron.trim(),
        timezone: d.timezone, scope: d.environments.length ? { environments: d.environments } : {}, on_finding: d.on_finding, enabled: d.enabled };
      return d.id ? api.patch(`/routines/${d.id}`, body) : api.post("/routines", body);
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["routines"] }); toast(d.id ? "Routine saved." : "Routine created."); onClose(); },
  });
  const set = (p: Partial<Draft>) => setD((x) => ({ ...x, ...p }));
  return (
    <Drawer open={!!draft} onClose={onClose} width={600} title={d.id ? `Edit ${d.name}` : "New routine"} sub="Runs on a schedule against the servers in scope."
      footer={<><Button variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" loading={save.isPending} disabled={d.name.length < 2 || (d.kind === "agent" && d.prompt.length < 10)} onClick={() => save.mutate()}>{d.id ? "Save routine" : "Create routine"}</Button></>}>
      <div className="space-y-4">
        {!d.id && (templates.data ?? []).length > 0 && (
          <div>
            <div className="plate mb-1.5">Start from a template</div>
            <div className="flex flex-wrap gap-1.5">
              {templates.data!.map((t) => (
                <button key={t.key} className="rounded-full border border-line bg-surface-2 px-2.5 py-1 text-[12px] font-semibold text-ink-2 hover:border-line-strong hover:text-ink"
                  onClick={() => setD({ ...blank, name: `${t.name} (copy)`, description: t.description, kind: t.kind, checks: t.checks ?? [], prompt: t.prompt ?? "", cron: t.cron, on_finding: t.on_finding })}>
                  {t.name}
                </button>
              ))}
            </div>
          </div>
        )}
        <Field label="Name"><Input value={d.name} onChange={(e) => set({ name: e.target.value })} placeholder="Nightly connection review" /></Field>
        <Field label="Description"><Textarea rows={2} value={d.description} onChange={(e) => set({ description: e.target.value })} /></Field>
        {d.kind !== "dream" && <Field label="Kind">
          <Segmented value={d.kind} onChange={(v) => set({ kind: v })} options={[{ id: "check", label: <span className="inline-flex items-center gap-1.5"><ListChecks className="size-3.5" />Checks (no tokens)</span> }, { id: "agent", label: <span className="inline-flex items-center gap-1.5"><Bot className="size-3.5" />AI review</span> }]} />
        </Field>}
        {d.kind === "dream" ? (
          <div className="rounded-lg bg-surface-2 px-3 py-2 text-[12.5px] text-ink-2">The dream reads what people decided, wrote and said since the last one, for the whole fleet, and keeps what is worth keeping on the <Link to="/knowledge" className="font-semibold text-arc-ink hover:underline">Knowledge</Link> page. Only its schedule changes here.</div>
        ) : d.kind === "check" ? (
          <Field label="What to check">
            <div className="space-y-1.5 rounded-lg border border-line p-2.5">
              {CHECK_OPTIONS.map((c) => (
                <label key={c.id} className="flex items-center gap-2 text-[13px]">
                  <input type="checkbox" className="size-4 accent-[var(--arc)]" checked={d.checks.includes(c.id)} onChange={(e) => set({ checks: e.target.checked ? [...d.checks, c.id] : d.checks.filter((x) => x !== c.id) })} />
                  {c.label}
                </label>
              ))}
              {d.checks.filter((c) => !CHECK_OPTIONS.some((o) => o.id === c)).map((c) => <Badge key={c} className="mr-1 font-mono">{c}</Badge>)}
            </div>
          </Field>
        ) : (
          <Field label="Instructions for rAIden" hint="It runs with read-only tools per server and ends with findings. Be specific about what matters.">
            <Textarea rows={5} value={d.prompt} onChange={(e) => set({ prompt: e.target.value })} placeholder="Review connection usage per role over the last day…" />
          </Field>
        )}
        <div className="grid gap-3 sm:grid-cols-[1fr_160px]">
          <Field label="Schedule (cron)" hint="minute hour day-of-month month day-of-week, e.g. 0 7 * * 1-5"><Input value={d.cron} onChange={(e) => set({ cron: e.target.value })} className="font-mono" /></Field>
          <Field label="Time zone"><Input value={d.timezone} onChange={(e) => set({ timezone: e.target.value })} /></Field>
        </div>
        {d.kind !== "dream" && <Field label="Servers">
          <div className="flex flex-wrap gap-3">
            {(["production", "staging", "development"] as Env[]).map((e) => (
              <label key={e} className="flex items-center gap-1.5 text-[13px]">
                <input type="checkbox" className="size-4 accent-[var(--arc)]" checked={d.environments.includes(e)} onChange={(ev) => set({ environments: ev.target.checked ? [...d.environments, e] : d.environments.filter((x) => x !== e) })} />
                {titleCase(e)}
              </label>
            ))}
            <span className="text-[12px] text-ink-3">{d.environments.length ? "" : "None ticked: every server"}</span>
          </div>
        </Field>}
        {d.kind !== "dream" && <Field label="When it finds something">
          <Select value={d.on_finding} onChange={(e) => set({ on_finding: e.target.value })}>
            <option value="suggest">Add issues and suggestions to the server</option>
            <option value="incident">Open an incident for critical findings</option>
            <option value="report">Only write a report</option>
          </Select>
        </Field>}
        {save.error && <ErrorNote error={save.error} />}
      </div>
    </Drawer>
  );
}

export function RoutinesPage() {
  const routines = useRoutines();
  const me = useMe();
  const qc = useQueryClient();
  const toast = useToast();
  const [runId, setRunId] = useState<number | null>(null);
  const [runRoutine, setRunRoutine] = useState<Routine | undefined>();
  const [editing, setEditing] = useState<Draft | null>(null);
  const isAdmin = me.data?.role === "admin";
  const canOperate = me.data?.role !== "viewer";
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) => api.patch(`/routines/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["routines"] }),
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const run = useMutation({
    mutationFn: (r: Routine) => api.post<{ run_id: number }>(`/routines/${r.id}/run`).then((x) => ({ ...x, r })),
    onSuccess: ({ run_id, r }) => { qc.invalidateQueries({ queryKey: ["routines"] }); setRunRoutine(r); setRunId(run_id); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/routines/${id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["routines"] }); toast("Routine deleted."); },
  });
  const list = routines.data ?? [];
  const sorted = useMemo(() => [...list].sort((a, b) => Number(b.enabled) - Number(a.enabled) || (a.next_run_at ?? "").localeCompare(b.next_run_at ?? "")), [list]);

  return (
    <div>
      <PageHeader title="Routines" sub="The regular work of a DBA, on a schedule: deterministic checks that cost no tokens, and AI reviews that read the server like a person would."
        actions={isAdmin && <Button variant="primary" onClick={() => setEditing({ ...blank })}><Plus className="size-4" /> New routine</Button>} />
      {routines.isLoading ? <Loading /> : list.length === 0 ? <Card><EmptyState title="No routines">Create one from a template.</EmptyState></Card> : (
        <div className="grid gap-4 lg:grid-cols-2">
          {sorted.map((r) => {
            const last = r.recent_runs[0];
            return (
              <Card key={r.id} className={cn("flex flex-col", !r.enabled && "border-dashed bg-surface-2")}>
                <div className="flex items-start gap-3 px-4 pt-4">
                  <span className={cn("mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg", r.kind === "check" ? "bg-surface-3 text-ink-2" : "bg-arc-soft text-arc-ink")}>
                    {r.kind === "agent" ? <Bot className="size-4" /> : r.kind === "dream" ? <MoonStar className="size-4" /> : <ListChecks className="size-4" />}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[15px] font-semibold">{r.name}</span>
                      <Badge tone={r.kind === "check" ? "neutral" : "arc"}>{KIND_LABEL[r.kind]}</Badge>
                      {!r.builtin && <Badge>Custom</Badge>}
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-[12.5px] text-ink-2">{r.description}</p>
                  </div>
                  <Switch checked={r.enabled} disabled={!isAdmin} onChange={(v) => patch.mutate({ id: r.id, body: { enabled: v } })} label={`${r.name} enabled`} />
                </div>
                <div className="mt-3 mb-3 grid grid-cols-2 gap-x-4 gap-y-1 px-4 text-[12.5px]">
                  <div className="flex items-center gap-1.5 text-ink-2"><CalendarClock className="size-3.5 text-ink-3" />{r.schedule}{r.timezone !== "UTC" && ` (${r.timezone})`}</div>
                  <div className="text-right text-ink-3">{r.enabled && r.next_run_at ? <>Next {until(r.next_run_at)}</> : "Off"}</div>
                  <div className="text-ink-3">{scopeText(r)}</div>
                  <div className="text-right text-ink-3">{r.kind === "dream" ? <Link to="/knowledge" className="hover:text-ink hover:underline">Learns into Knowledge</Link> : ON_FINDING[r.on_finding]}</div>
                </div>
                <div className="mt-auto flex items-center gap-3 border-t border-line px-4 py-2.5">
                  <RunStrip runs={r.recent_runs} onOpen={(id) => { setRunRoutine(r); setRunId(id); }} />
                  <div className="min-w-0 flex-1 truncate text-[12px] text-ink-3">
                    {last ? <><span className="font-semibold" style={{ color: RUN_TONE[last.status] }}>{RUN_LABEL[last.status] ?? last.status}</span> · <Time iso={last.started_at} />{last.summary && ` · ${last.summary}`}</> : "Not run yet"}
                  </div>
                  {isAdmin && !r.builtin && <Button size="sm" variant="ghost" onClick={() => remove.mutate(r.id)} aria-label="Delete"><Trash2 className="size-3.5" /></Button>}
                  {isAdmin && <Button size="sm" variant="ghost" onClick={() => setEditing({ id: r.id, name: r.name, description: r.description, kind: r.kind, checks: r.checks, prompt: r.prompt, cron: r.cron, timezone: r.timezone, environments: r.scope.environments ?? [], on_finding: r.on_finding, enabled: r.enabled })}>Edit</Button>}
                  {canOperate && <Button size="sm" onClick={() => run.mutate(r)} loading={run.isPending && run.variables?.id === r.id}><Play className="size-3.5" /> Run now</Button>}
                </div>
              </Card>
            );
          })}
        </div>
      )}
      <RunDrawer id={runId} routine={runRoutine} onClose={() => setRunId(null)} />
      {editing && <Editor key={editing.id ?? "new"} draft={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}
