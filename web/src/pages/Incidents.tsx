import { DndContext, PointerSensor, useDraggable, useDroppable, useSensor, useSensors, type DragEndEvent } from "@dnd-kit/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Columns3, List, Plus, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { CircuitHeader, type CircuitStage } from "../components/circuit";
import { STAGES, STATE_HINT, StateLine } from "../components/incident";
import { Button, Card, cn, EmptyState, Field, Input, Modal, PageHeader, Segmented, Select, SeverityBadge, ServerTag, Textarea, Time, useToast } from "../components/ui";
import { api } from "../lib/api";
import { compact, money } from "../lib/format";
import { useIncidents, useMe, usePolicy, useServers } from "../lib/hooks";
import type { Env, IncidentCard, Mode, Stage } from "../lib/types";

function useNow(ms = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

export function Card_({ inc, slot, now, env }: { inc: IncidentCard; slot: number | undefined; now: number; env?: string }) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({ id: inc.id, data: { stage: inc.stage }, disabled: inc.stage === "resolved" });
  const nav = useNavigate();
  const waiting = inc.state === "waiting";
  const style = transform ? { transform: `translate3d(${transform.x}px, ${transform.y}px, 0)` } : undefined;
  return (
    <div ref={setNodeRef} style={style} {...attributes} {...listeners}
      onClick={() => !isDragging && nav(`/incidents/${inc.id}`)}
      className={cn("rise group relative cursor-pointer rounded-xl border bg-surface p-3 shadow-card transition-shadow hover:shadow-pop",
        waiting ? "border-lamp-line" : inc.state === "escalated" ? "border-crit/35" : "border-line",
        isDragging && "z-20 opacity-90 shadow-pop", inc.stage === "resolved" && "opacity-80")}>
      {waiting && (
        <span className="absolute -top-px right-3 rounded-b-md bg-lamp px-1.5 py-[1px] text-[10px] font-bold tracking-wider text-[#2a1c00] uppercase">Needs you</span>
      )}
      <div className="flex items-center gap-2">
        <SeverityBadge severity={inc.severity} />
        <span className="mono text-[11.5px] whitespace-nowrap text-ink-3">{inc.id}</span>
      </div>
      {inc.server_name && (
        <div className="mt-1.5 flex min-w-0 items-center gap-1.5 text-[12.5px]">
          <ServerTag name={inc.server_name} slot={slot} />
          {env && env !== "production" && !inc.server_name.includes(env === "staging" ? "stag" : "dev") && (
            <span className="shrink-0 text-[11px] font-semibold text-ink-3">{env === "staging" ? "staging" : "dev"}</span>
          )}
        </div>
      )}
      <div className="mt-1 line-clamp-2 text-[13.5px] leading-snug font-semibold text-ink">{inc.title}</div>
      {inc.root_cause && inc.stage !== "resolved" && inc.stage !== "detected" && inc.stage !== "triage" && (
        <div className="mt-1 line-clamp-2 text-[12px] leading-snug text-ink-3">{inc.root_cause}</div>
      )}
      <div className="mt-2.5 flex items-end justify-between gap-2 border-t border-line pt-2" title={inc.tokens ? `${inc.tokens.toLocaleString()} tokens · ${money(inc.cost_usd)}` : undefined}>
        <div className="min-w-0 flex-1 [&_.truncate]:line-clamp-2 [&_.truncate]:whitespace-normal"><StateLine inc={inc} now={now} /></div>
        <span className="shrink-0 text-[11px] whitespace-nowrap text-ink-3"><Time iso={inc.detected_at} /></span>
      </div>
    </div>
  );
}

function Column({ stage, children, empty }: { stage: Stage; children: React.ReactNode; empty: boolean }) {
  const { setNodeRef, isOver } = useDroppable({ id: stage });
  return (
    <div ref={setNodeRef} className={cn("min-h-[420px] space-y-2.5 rounded-xl p-1.5 transition-colors", isOver ? "bg-arc-soft/70 outline-2 outline-dashed outline-arc-line" : "bg-surface-3/45")}>
      {children}
      {empty && <div className="px-2 py-6 text-center text-[12px] text-ink-3">{STATE_HINT[stage]}</div>}
    </div>
  );
}

function NewIncident({ open, onClose }: { open: boolean; onClose: () => void }) {
  const servers = useServers();
  const qc = useQueryClient();
  const toast = useToast();
  const nav = useNavigate();
  const [form, setForm] = useState({ server_id: "", title: "", severity: "sev3", summary: "" });
  const create = useMutation({
    mutationFn: () => api.post<IncidentCard>("/incidents", { ...form, server_id: form.server_id || null }),
    onSuccess: (inc) => {
      qc.invalidateQueries({ queryKey: ["incidents"] });
      toast(`${inc.id} opened; rAIden starts according to the autonomy policy`);
      onClose();
      nav(`/incidents/${inc.id}`);
    },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  return (
    <Modal open={open} onClose={onClose} title="Open an incident"
      footer={<><Button variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={form.title.length < 3} onClick={() => create.mutate()}>Open incident</Button></>}>
      <div className="space-y-3">
        <Field label="Server">
          <Select value={form.server_id} onChange={(e) => setForm({ ...form, server_id: e.target.value })}>
            <option value="">Fleet-wide (no single server)</option>
            {servers.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
        </Field>
        <Field label="What is wrong"><Input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="Checkout is slow since 14:05" autoFocus /></Field>
        <Field label="Severity">
          <Segmented value={form.severity} onChange={(v) => setForm({ ...form, severity: v })} options={[{ id: "sev1", label: "SEV1" }, { id: "sev2", label: "SEV2" }, { id: "sev3", label: "SEV3" }, { id: "sev4", label: "SEV4" }]} />
        </Field>
        <Field label="What you know so far" hint="rAIden reads this first."><Textarea rows={3} value={form.summary} onChange={(e) => setForm({ ...form, summary: e.target.value })} /></Field>
      </div>
    </Modal>
  );
}

export function IncidentsPage() {
  const [params, setParams] = useSearchParams();
  const view = (params.get("view") as "board" | "list") || "board";
  const [env, setEnv] = useState<Env>((params.get("env") as Env) || "production");
  const [q, setQ] = useState("");
  const [server, setServer] = useState("");
  const [creating, setCreating] = useState(false);
  const me = useMe();
  const canEdit = me.data?.role === "admin";
  const canOperate = me.data?.role !== "viewer";
  const board = useIncidents(view === "board" ? "board" : "all");
  const policy = usePolicy();
  const servers = useServers();
  const qc = useQueryClient();
  const toast = useToast();
  const now = useNow(1000);
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }));
  const slots = useMemo(() => new Map((servers.data ?? []).map((s) => [s.id, s.color_slot])), [servers.data]);
  const envOf = useMemo(() => new Map((servers.data ?? []).map((s) => [s.id, s.environment])), [servers.data]);

  const items = useMemo(() => (board.data ?? []).filter((i) =>
    (!server || i.server_id === server) && (!q || `${i.id} ${i.title} ${i.server_name}`.toLowerCase().includes(q.toLowerCase()))), [board.data, q, server]);

  const toggle = useMutation({
    mutationFn: ({ stage, mode }: { stage: Stage; mode: Mode }) => api.put("/autonomy", { environments: { [env]: { [stage]: mode } } }),
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: ["autonomy"] });
      toast(`${env[0]!.toUpperCase() + env.slice(1)} · ${v.stage}: ${v.mode === "auto" ? "autopilot" : "a person approves first"}`);
    },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const move = useMutation({
    mutationFn: ({ id, stage }: { id: string; stage: Stage }) => api.post(`/incidents/${id}/move`, { stage }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["incidents"] }),
    onError: (e) => toast((e as Error).message, "crit"),
  });

  const envPolicy = policy.data?.policy.environments[env];
  const info = new Map((policy.data?.stages ?? []).map((s) => [s.id, s]));
  const circuit: CircuitStage[] = STAGES.map((s) => {
    const col = items.filter((i) => i.stage === s.id);
    return {
      id: s.id, title: s.title, mode: s.id === "detected" ? undefined : (envPolicy?.[s.id] as Mode | undefined) ?? "auto",
      count: col.length, waiting: col.filter((i) => i.state === "waiting").length, autoText: info.get(s.id)?.auto, humanText: info.get(s.id)?.human,
    };
  });
  const onDragEnd = (e: DragEndEvent) => {
    const to = e.over?.id as Stage | undefined;
    const from = e.active.data.current?.stage as Stage | undefined;
    if (!to || !from || to === from || !canOperate || to === "detected") return;
    move.mutate({ id: String(e.active.id), stage: to });
    toast(`${e.active.id} moved to ${to}${STAGES.findIndex((s) => s.id === to) > STAGES.findIndex((s) => s.id === from) ? " (counts as the approval of every gate on the way)" : ""}`, "info");
  };
  const waitingCount = items.filter((i) => i.state === "waiting").length;

  return (
    <div>
      <PageHeader
        title="Incidents"
        sub={<>Every incident runs through the same circuit. A closed breaker lets rAIden carry on by itself; an open one stops it until a person approves. {waitingCount > 0 && <b className="text-lamp-ink">{waitingCount} waiting for you.</b>}</>}
        actions={<>
          <Segmented value={view} onChange={(v) => setParams((p) => { p.set("view", v); return p; })} options={[{ id: "board", label: <span className="inline-flex items-center gap-1.5"><Columns3 className="size-3.5" />Board</span> }, { id: "list", label: <span className="inline-flex items-center gap-1.5"><List className="size-3.5" />List</span> }]} />
          {canOperate && <Button variant="primary" onClick={() => setCreating(true)}><Plus className="size-4" /> Open incident</Button>}
        </>}
      />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-ink-3" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search incidents" className="w-56 pl-8" />
        </div>
        <Select aria-label="Server" value={server} onChange={(e) => setServer(e.target.value)} className="w-48">
          <option value="">All servers</option>
          {servers.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </Select>
        {view === "board" && (
          <div className="ml-auto flex items-center gap-2">
            <span className="text-[12.5px] text-ink-3">Breakers for</span>
            <Segmented value={env} onChange={setEnv} options={[{ id: "production", label: "Production" }, { id: "staging", label: "Staging" }, { id: "development", label: "Development" }]} />
          </div>
        )}
      </div>

      {view === "board" ? (
        <DndContext sensors={sensors} onDragEnd={onDragEnd}>
          <div className="-mx-1 overflow-x-auto px-1 pb-4">
            <div style={{ minWidth: 7 * 176 + 6 * 12 }}>
              <CircuitHeader stages={circuit} canEdit={canEdit} envLabel={env[0]!.toUpperCase() + env.slice(1)} onToggle={(stage, mode) => toggle.mutate({ stage, mode })} />
              <div className="mt-3 grid gap-3" style={{ gridTemplateColumns: `repeat(7, minmax(176px, 1fr))` }}>
                {STAGES.map((s) => {
                  const col = items.filter((i) => i.stage === s.id).sort((a, b) => Number(b.state === "waiting") - Number(a.state === "waiting") || b.number - a.number);
                  return (
                    <Column key={s.id} stage={s.id} empty={col.length === 0}>
                      {col.map((inc) => (
                        <Card_ key={inc.id} inc={inc} slot={slots.get(inc.server_id ?? "")} now={now} env={envOf.get(inc.server_id ?? "")} />
                      ))}
                    </Column>
                  );
                })}
              </div>
            </div>
          </div>
          <p className="mt-1 text-[12px] text-ink-3">The breakers show the {env} policy; staging and development cards are labelled, and servers can pin their own gates. Drag a card to move it by hand.</p>
        </DndContext>
      ) : (
        <Card className="overflow-hidden">
          {items.length === 0 ? <EmptyState title="No incidents match">Try another server or search.</EmptyState> : (
            <table className="w-full text-left text-[13px]">
              <thead className="bg-surface-2 text-[11.5px] text-ink-3">
                <tr>{["Incident", "Server", "Title", "Stage", "State", "Detected", "Tokens", "Cost"].map((h) => <th key={h} className="px-3 py-2 font-semibold">{h}</th>)}</tr>
              </thead>
              <tbody>
                {items.map((i) => (
                  <tr key={i.id} className="border-t border-line hover:bg-surface-2">
                    <td className="px-3 py-2 whitespace-nowrap"><Link to={`/incidents/${i.id}`} className="inline-flex items-center gap-2 font-semibold"><SeverityBadge severity={i.severity} /><span className="mono text-[12px]">{i.id}</span></Link></td>
                    <td className="px-3 py-2">{i.server_name && <ServerTag name={i.server_name} slot={slots.get(i.server_id ?? "")} />}</td>
                    <td className="max-w-[420px] px-3 py-2"><Link to={`/incidents/${i.id}`} className="line-clamp-1 hover:underline">{i.title}</Link></td>
                    <td className="px-3 py-2 capitalize">{i.stage}</td>
                    <td className="px-3 py-2"><StateLine inc={i} now={now} /></td>
                    <td className="px-3 py-2 whitespace-nowrap text-ink-2"><Time iso={i.detected_at} /></td>
                    <td className="px-3 py-2 text-ink-2 num">{compact(i.tokens)}</td>
                    <td className="px-3 py-2 text-ink-2 num">{money(i.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}
      <NewIncident open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}
