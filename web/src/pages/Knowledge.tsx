import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Check, GraduationCap, Hand, ListChecks, Moon, MoonStar, Plus, RotateCcw, Scale, Search, SlidersHorizontal, Undo2, X } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import {
  Badge, Button, Card, CardHeader, cn, Drawer, EmptyState, ErrorNote, Field, Input, Loading, Markdown, PageHeader, RiskBadge, Segmented, Select, Tabs, Textarea, Time, useToast,
} from "../components/ui";
import { api } from "../lib/api";
import { compact, incidentKind, personName, titleCase, until } from "../lib/format";
import { useKnowledge, useMe, useServers } from "../lib/hooks";
import type { Dream, DreamChange, Env, Knowledge, KnowledgeKind, KnowledgePage as KnowledgeData, KnowledgeScope, LearnedApproval } from "../lib/types";

const KINDS: { id: KnowledgeKind; label: string; icon: ReactNode; hint: string; placeholder: string }[] = [
  { id: "rule", label: "Rule", icon: <Scale className="size-3.5" />, hint: "What rAIden does, or never does.", placeholder: "Never end sessions of the backup role" },
  { id: "fact", label: "Fact", icon: <BookOpen className="size-3.5" />, hint: "Something true about a server, an application or a role.", placeholder: "The migrator role deploys on weekdays at 14:00 UTC" },
  { id: "preference", label: "Preference", icon: <SlidersHorizontal className="size-3.5" />, hint: "How the team likes things done.", placeholder: "Put the exact SQL in every recommendation" },
  { id: "skill", label: "Playbook", icon: <ListChecks className="size-3.5" />, hint: "How to handle one kind of incident: what to look at first, the usual cause, the fix, the protections.", placeholder: "Lock queue behind a DDL" },
];
const KIND = Object.fromEntries(KINDS.map((k) => [k.id, k])) as Record<KnowledgeKind, (typeof KINDS)[number]>;
const ENVS: Env[] = ["production", "staging", "development"];

function source(k: Knowledge): ReactNode {
  if (k.source === "person") return <>Taught by <b className="font-semibold text-lamp-ink">{personName(k.author)}</b>{k.evidence.some((e) => e.type === "chat") && " in a chat"}</>;
  if (k.source === "incident") return <>Learned in <Link to={`/incidents/${k.author}`} className="font-semibold text-arc-ink hover:underline">{k.author}</Link></>;
  const words = k.evidence.find((e) => e.words && e.by);
  return <>Learned in {k.author}{words ? <> from {personName(words.by)}’s words</> : k.status === "proposed" ? " from what people did" : ""}</>;
}

function ScopeChips({ scope, servers }: { scope: KnowledgeScope; servers: Map<string, string> }) {
  const parts = [scope.server && servers.get(scope.server) ? servers.get(scope.server)! : scope.server, scope.environment && titleCase(scope.environment),
    scope.category && titleCase(incidentKind(scope.category))].filter(Boolean) as string[];
  if (!parts.length) return <Badge>Every server</Badge>;
  return <>{parts.map((p) => <Badge key={p} tone="info">{p}</Badge>)}</>;
}

function Evidence({ k }: { k: Knowledge }) {
  const incidents = [...new Set(k.evidence.filter((e) => e.type === "incident").map((e) => e.id))];
  const quote = k.evidence.find((e) => e.words && e.text && e.type !== "incident");
  return (
    <>
      {quote && <div className="mt-1 border-l-2 border-lamp-line pl-2 text-[12.5px] text-ink-2 italic">“{quote.text}”</div>}
      {incidents.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-x-2 text-[12px] text-ink-3">
          {incidents.map((id) => <Link key={id} to={`/incidents/${id}`} className="mono hover:text-ink hover:underline">{id}</Link>)}
        </div>
      )}
    </>
  );
}

// ------------------------------------------------------------------------------------------------ teach and edit
interface Draft { id?: number; kind: KnowledgeKind; title: string; body: string; server: string; environment: string; category: string; retention: string }
const blank: Draft = { kind: "rule", title: "", body: "", server: "", environment: "", category: "", retention: "durable" };

function Editor({ draft, item, page, onClose, canEdit }: { draft: Draft; item?: Knowledge; page: KnowledgeData; onClose: () => void; canEdit: boolean }) {
  const [d, setD] = useState(draft);
  const servers = useServers();
  const qc = useQueryClient();
  const toast = useToast();
  const set = (p: Partial<Draft>) => setD((x) => ({ ...x, ...p }));
  const save = useMutation({
    mutationFn: () => {
      const scope = { server: d.server || null, environment: d.environment || null, category: d.category || null };
      return d.id ? api.patch(`/knowledge/${d.id}`, { title: d.title, body: d.body, scope, retention: d.retention })
        : api.post("/knowledge", { kind: d.kind, title: d.title, body: d.body, scope, retention: d.retention });
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["knowledge"] }); toast(d.id ? "Saved. rAIden uses the new version from its next prompt." : "rAIden knows it now, from its next prompt on."); onClose(); },
  });
  const k = KIND[d.kind];
  const ok = d.title.trim().length >= 3 && (d.kind !== "skill" || (!!d.category && d.body.trim().length > 0));
  return (
    <Drawer open onClose={onClose} width={620} title={d.id ? `${k.label} #${d.id}` : "Teach rAIden"}
      sub={d.id && item ? <>{source(item)} · <Time iso={item.created_at} /> · in {compact(item.uses)} prompt{item.uses === 1 ? "" : "s"}</> : "It goes into every incident, chat and routine it applies to, for the whole team."}
      footer={canEdit ? <><Button variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" disabled={!ok} loading={save.isPending} onClick={() => save.mutate()}>{d.id ? "Save" : "Teach rAIden"}</Button></> : undefined}>
      <div className="space-y-4">
        {!d.id && (
          <Field label="What kind" hint={k.hint}>
            <Segmented value={d.kind} onChange={(v) => set({ kind: v })} options={KINDS.map((x) => ({ id: x.id, label: <span className="inline-flex items-center gap-1.5">{x.icon}{x.label}</span> }))} />
          </Field>
        )}
        <Field label={d.kind === "skill" ? "Name of the playbook" : "In one sentence"}><Input value={d.title} disabled={!canEdit} onChange={(e) => set({ title: e.target.value })} placeholder={k.placeholder} /></Field>
        <Field label={d.kind === "skill" ? "The playbook" : "Why, or the details"} hint={d.kind === "skill" ? "Markdown. What to look at first, the usual root cause, the plan people approve, the protections that work." : "Optional. rAIden reads it with the sentence."}>
          <Textarea rows={d.kind === "skill" ? 10 : 3} value={d.body} disabled={!canEdit} onChange={(e) => set({ body: e.target.value })} className={d.kind === "skill" ? "mono text-[12.5px]" : ""} />
        </Field>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Server">
            <Select value={d.server} disabled={!canEdit} onChange={(e) => set({ server: e.target.value })}>
              <option value="">Every server</option>
              {(servers.data ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>
          </Field>
          <Field label="Environment">
            <Select value={d.environment} disabled={!canEdit} onChange={(e) => set({ environment: e.target.value })}>
              <option value="">Any</option>
              {ENVS.map((e) => <option key={e} value={e}>{titleCase(e)}</option>)}
            </Select>
          </Field>
          <Field label="Kind of incident" error={d.kind === "skill" && !d.category ? "A playbook is for one kind" : null}>
            <Select value={d.category} disabled={!canEdit} onChange={(e) => set({ category: e.target.value })}>
              <option value="">{d.kind === "skill" ? "Choose…" : "Any"}</option>
              {page.categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Keep it">
          <Segmented value={d.retention} onChange={(v) => canEdit && set({ retention: v })} options={[{ id: "durable", label: "Until someone changes it" }, { id: "permanent", label: "Always" }, { id: "ephemeral", label: "For 30 days" }]} />
        </Field>
        {item && item.evidence.length > 0 && (
          <div>
            <div className="plate mb-1.5">Where it comes from</div>
            <ul className="space-y-1.5">
              {item.evidence.map((e, i) => (
                <li key={i} className="rounded-lg border border-line bg-surface-2 px-3 py-2 text-[12.5px]">
                  <span className="font-semibold">{e.type === "incident" ? <Link to={`/incidents/${e.id}`} className="hover:underline">{e.id}</Link> : `${titleCase(e.type)} #${e.id}`}</span>
                  {e.by && <span className="text-ink-3"> · {personName(e.by)}</span>}
                  {e.text && <div className="mt-0.5 text-ink-2">{e.words ? `“${e.text}”` : e.text}</div>}
                </li>
              ))}
            </ul>
          </div>
        )}
        {item && item.history.length > 0 && (
          <div>
            <div className="plate mb-1.5">Earlier versions</div>
            <ol className="space-y-1.5">
              {[...item.history].reverse().map((h, i) => (
                <li key={i} className="rounded-lg border border-line px-3 py-2 text-[12.5px]">
                  <div className="text-ink-3">Changed by <b className="font-semibold text-ink-2">{h.by.startsWith("dream") ? h.by : personName(h.by)}</b> · <Time iso={h.at} />{h.why && <> · {h.why}</>}</div>
                  <div className="mt-0.5 text-ink-2">{h.title} <span className="text-ink-3">({titleCase(h.status)}{Object.keys(h.scope || {}).length ? "" : ", every server"})</span></div>
                </li>
              ))}
            </ol>
          </div>
        )}
        {save.error && <ErrorNote error={save.error} />}
      </div>
    </Drawer>
  );
}

// ------------------------------------------------------------------------------------------------ waiting for a person
function ApprovalProposal({ la, isAdmin, canOperate, highlight }: { la: LearnedApproval; isAdmin: boolean; canOperate: boolean; highlight: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const act = useMutation({
    mutationFn: (verb: "accept" | "decline") => api.post(`/learned-approvals/${la.id}/${verb}`, {}),
    onSuccess: (_d, verb) => { qc.invalidateQueries({ queryKey: ["knowledge"] }); toast(verb === "accept" ? "Accepted. The next such plan goes ahead without asking; the next rejection takes it back." : "Declined. rAIden keeps asking."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const incidents = [...new Set(la.evidence.map((e) => e.incident_id).filter(Boolean))] as string[];
  const words = la.evidence.find((e) => e.comment && !e.approval_id);
  return (
    <li id={`approval-${la.id}`} className={cn("rounded-xl border border-lamp-line bg-surface px-4 py-3", highlight && "ring-2 ring-lamp")}>
      <div className="flex flex-wrap items-center gap-2">
        <Hand className="size-4 text-lamp-ink" />
        <span className="text-[14px] font-semibold">Don’t ask again: {la.action_title.toLowerCase()} for {incidentKind(la.category)} on {la.server_id ?? la.environment}</span>
        <RiskBadge risk={la.risk} />
      </div>
      <div className="mt-1 text-[12.5px] text-ink-2">
        {la.approvals ? <>Approved {la.approvals} times in a row by {la.approvers.map(personName).join(" and ")}, not rejected since.</> : words ? <>{personName(words.by)} asked: “{words.comment}”</> : la.reason}
        {incidents.length > 0 && <span className="text-ink-3"> · {incidents.map((id, i) => <span key={id}>{i ? ", " : ""}<Link to={`/incidents/${id}`} className="mono hover:text-ink hover:underline">{id}</Link></span>)}</span>}
      </div>
      <div className="mt-1 text-[12px] text-ink-3">
        Proposed <Time iso={la.created_at} />{la.proposed_by.startsWith("dream") ? ` in ${la.proposed_by}` : ", right after the last approval"}. Only this kind of plan goes ahead; high-risk steps still wait; the next rejection takes it back.
      </div>
      {canOperate && (
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          {isAdmin ? <Button size="sm" variant="lamp" loading={act.isPending && act.variables === "accept"} onClick={() => act.mutate("accept")}><Check className="size-3.5" /> Don’t ask again</Button>
            : <span className="text-[12px] text-ink-3">An admin accepts it.</span>}
          <Button size="sm" variant="outline" loading={act.isPending && act.variables === "decline"} onClick={() => act.mutate("decline")}><X className="size-3.5" /> Keep asking</Button>
        </div>
      )}
    </li>
  );
}

function InstructionProposal({ k, servers, canOperate, onOpen }: { k: Knowledge; servers: Map<string, string>; canOperate: boolean; onOpen: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const set = useMutation({
    mutationFn: (status: "active" | "retired") => api.patch(`/knowledge/${k.id}`, { status, why: status === "active" ? "confirmed" : "not true" }),
    onSuccess: (_d, status) => { qc.invalidateQueries({ queryKey: ["knowledge"] }); toast(status === "active" ? "Confirmed: rAIden follows it from now on." : "Retired: rAIden forgets it."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  return (
    <li className="rounded-xl border border-lamp-line bg-surface px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1 text-[11.5px] font-bold text-ink-3 uppercase">{KIND[k.kind].icon}{KIND[k.kind].label}</span>
        <button onClick={onOpen} className="text-left text-[14px] font-semibold hover:underline">{k.title}</button>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3"><ScopeChips scope={k.scope} servers={servers} /><span>{source(k)}: nobody said it in words, so it waits for you.</span></div>
      <Evidence k={k} />
      {canOperate && (
        <div className="mt-2.5 flex gap-2">
          <Button size="sm" variant="lamp" loading={set.isPending && set.variables === "active"} onClick={() => set.mutate("active")}><Check className="size-3.5" /> Confirm</Button>
          <Button size="sm" variant="outline" loading={set.isPending && set.variables === "retired"} onClick={() => set.mutate("retired")}><X className="size-3.5" /> Not true</Button>
        </div>
      )}
    </li>
  );
}

// ------------------------------------------------------------------------------------------------ what rAIden knows
function Row({ k, servers, onOpen, canOperate }: { k: Knowledge; servers: Map<string, string>; onOpen: () => void; canOperate: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const retire = useMutation({
    mutationFn: (status: "active" | "retired") => api.patch(`/knowledge/${k.id}`, { status, why: status === "retired" ? "retired from the Knowledge page" : "restored" }),
    onSuccess: (_d, status) => { qc.invalidateQueries({ queryKey: ["knowledge"] }); toast(status === "retired" ? "Retired. rAIden no longer uses it; it stays in the history." : "Restored."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const gone = k.status === "retired" || k.status === "expired";
  return (
    <li className="group flex gap-3 px-4 py-3">
      <span className={cn("mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg", k.kind === "rule" ? "bg-lamp-soft text-lamp-ink" : "bg-surface-3 text-ink-2")}>{KIND[k.kind].icon}</span>
      <div className="min-w-0 flex-1">
        <button onClick={onOpen} className={cn("text-left text-[13.5px] font-semibold text-ink hover:underline", gone && "text-ink-3 line-through decoration-ink-3/40")}>{k.title}</button>
        {k.body && k.kind !== "skill" && <div className="mt-0.5 text-[12.5px] text-ink-2">{k.body}</div>}
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3">
          <ScopeChips scope={k.scope} servers={servers} />
          <span>{source(k)} · <Time iso={k.created_at} /></span>
          {k.retention === "ephemeral" && k.expires_at && <Badge tone="warn">{k.status === "expired" ? "Expired" : `Until ${new Date(k.expires_at).toLocaleDateString("en-GB", { day: "numeric", month: "short" })}`}</Badge>}
          {k.retention === "permanent" && <Badge>Always</Badge>}
        </div>
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1">
        <span className="text-[12px] text-ink-3 num" title="How often it went into a prompt">{k.uses ? `in ${compact(k.uses)} prompt${k.uses === 1 ? "" : "s"}` : "not used yet"}</span>
        {canOperate && (gone ? <Button size="sm" variant="ghost" loading={retire.isPending} onClick={() => retire.mutate("active")}><RotateCcw className="size-3.5" /> Restore</Button>
          : <Button size="sm" variant="ghost" className="opacity-0 group-hover:opacity-100 focus:opacity-100" loading={retire.isPending} onClick={() => retire.mutate("retired")}>Retire</Button>)}
      </div>
    </li>
  );
}

function Playbook({ k, onOpen }: { k: Knowledge; onOpen: () => void }) {
  return (
    <Card>
      <div className="flex items-start justify-between gap-3 px-4 pt-3.5">
        <div className="min-w-0">
          <button onClick={onOpen} className="display text-left text-[17px] leading-tight hover:underline">{k.title}</button>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3"><Badge tone="info">{titleCase(incidentKind(k.scope.category ?? "other"))}</Badge><span>{source(k)} · updated <Time iso={k.updated_at} /></span></div>
        </div>
        <span className="shrink-0 text-[12px] text-ink-3 num">{k.uses ? `used in ${k.uses} diagnos${k.uses === 1 ? "is" : "es"}` : "not used yet"}</span>
      </div>
      <div className="px-4 pt-2 pb-3.5"><Markdown className="text-[13px]">{k.body}</Markdown></div>
    </Card>
  );
}

function NotAsked({ la, canOperate }: { la: LearnedApproval; canOperate: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const revoke = useMutation({
    mutationFn: () => api.post(`/learned-approvals/${la.id}/revoke`, { reason: "asked again from the Knowledge page" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["knowledge"] }); toast("rAIden asks again from the next plan on."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const live = la.status === "active";
  return (
    <li className="flex flex-wrap items-center gap-3 px-4 py-3">
      <span className={cn("flex size-7 shrink-0 items-center justify-center rounded-lg", live ? "bg-arc-soft text-arc-ink" : "bg-surface-3 text-ink-3")}><GraduationCap className="size-3.5" /></span>
      <div className="min-w-0 flex-1">
        <div className={cn("text-[13.5px] font-semibold", !live && "text-ink-3")}>{la.action_title} · {incidentKind(la.category)} · {la.server_id ?? titleCase(la.environment)}</div>
        <div className="text-[12px] text-ink-3">
          {live ? <>Accepted by {personName(la.decided_by)} <Time iso={la.decided_at} /> · {la.uses ? `went ahead ${la.uses} time${la.uses === 1 ? "" : "s"}${la.last_used_at ? `, last ${new Date(la.last_used_at).toLocaleDateString("en-GB", { day: "numeric", month: "short" })}` : ""}` : "not needed yet"}</>
            : <>{la.status === "declined" ? "Declined" : "Taken back"} by {personName(la.decided_by)} <Time iso={la.decided_at} />{la.reason && la.status === "revoked" ? `: ${la.reason}` : ""}</>}
        </div>
      </div>
      {live ? canOperate && <Button size="sm" variant="outline" loading={revoke.isPending} onClick={() => revoke.mutate()}>Ask me again</Button> : <Badge>{la.status === "declined" ? "Keeps asking" : "Asks again"}</Badge>}
    </li>
  );
}

// ------------------------------------------------------------------------------------------------ the dream journal
const OP: Record<DreamChange["op"], { sign: string; tone: string; verb: string }> = {
  add: { sign: "+", tone: "text-ok", verb: "learned" },
  update: { sign: "~", tone: "text-arc-ink", verb: "corrected" },
  retire: { sign: "−", tone: "text-ink-3", verb: "retired" },
  propose: { sign: "?", tone: "text-lamp-ink", verb: "proposes" },
};

function DreamEntry({ d, isAdmin, last }: { d: Dream; isAdmin: boolean; last: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const undo = useMutation({
    mutationFn: () => api.post<{ kept: string[] }>(`/dreams/${d.id}/undo`),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ["knowledge"] }); toast(r.kept.length ? `Undone, except what people changed since: ${r.kept.join("; ")}` : "Undone: everything it changed is back as it was."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const quiet = d.status === "done" && !d.changes.length;
  const started = new Date(d.started_at);
  return (
    <li className="relative flex gap-3 pb-4">
      {!last && <span className="absolute top-8 bottom-0 left-[13px] w-px bg-line" aria-hidden />}
      <span className={cn("relative z-[1] mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border",
        d.status === "running" ? "working border-arc-line bg-arc-soft text-arc-ink" : quiet ? "border-line bg-surface-2 text-ink-3" : "border-arc-line bg-arc-soft text-arc-ink")}>
        {quiet ? <Moon className="size-3.5" /> : <MoonStar className="size-3.5" />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 text-[12px] text-ink-3">
          <span className="font-semibold text-ink-2">{started.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })} · {started.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}</span>
          <span>{d.triggered_by === "schedule" ? "nightly" : `by ${personName(d.triggered_by)}`}</span>
          {d.tokens > 0 && <span className="num">· {compact(d.tokens)} tokens</span>}
          {d.status === "undone" && <Badge>Undone by {personName(d.undone_by)}</Badge>}
          {d.status === "error" && <Badge tone="crit">Stopped early</Badge>}
        </div>
        {d.status === "running" ? <div className="mt-0.5 text-[13px] font-semibold text-arc-ink">Dreaming: reading what happened since the last dream…</div> : (
          <>
            <div className={cn("mt-0.5 text-[13px]", quiet ? "text-ink-3" : "text-ink", d.status === "undone" && "line-through decoration-ink-3/40")}>{d.summary}</div>
            {!quiet && d.read_text !== "nothing new" && <div className="mt-0.5 text-[12px] text-ink-3">Read {d.read_text}.</div>}
            {d.changes.length > 0 && (
              <ul className="mt-1.5 space-y-0.5">
                {d.changes.map((c, i) => (
                  <li key={i} className="flex gap-1.5 text-[12.5px]">
                    <span className={cn("mono w-3 shrink-0 font-bold", OP[c.op].tone)} aria-label={OP[c.op].verb}>{OP[c.op].sign}</span>
                    <span className="min-w-0 text-ink-2">
                      {c.type === "approval" ? <>not to ask again: {c.title}</> : <><span className="text-ink-3">{c.kind === "skill" ? "playbook" : c.kind} #{c.id}</span> {c.title}</>}
                      {c.op === "update" && c.before && c.after && JSON.stringify(c.before.scope) !== JSON.stringify(c.after.scope) &&
                        <span className="text-ink-3"> (now {Object.keys(c.after.scope).length ? Object.values(c.after.scope).join(" · ") : "every server"})</span>}
                      {c.status === "proposed" && c.type === "knowledge" && <span className="text-lamp-ink"> · waits for you</span>}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {isAdmin && d.changes.length > 0 && d.status === "done" && (
              <Button size="sm" variant="ghost" className="mt-1 -ml-2.5" loading={undo.isPending} onClick={() => undo.mutate()}><Undo2 className="size-3.5" /> Undo this dream</Button>
            )}
          </>
        )}
      </div>
    </li>
  );
}

// ------------------------------------------------------------------------------------------------ the page
type Tab = "instructions" | "playbooks" | "approvals" | "retired";

export function KnowledgePage() {
  const page = useKnowledge();
  const me = useMe();
  const servers = useServers();
  const qc = useQueryClient();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const [tab, setTab] = useState<Tab>("instructions");
  const [kind, setKind] = useState<"all" | KnowledgeKind>("all");
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<{ draft: Draft; item?: Knowledge } | null>(null);
  const isAdmin = me.data?.role === "admin";
  const canOperate = !!me.data && me.data.role !== "viewer";
  const names = useMemo(() => new Map((servers.data ?? []).map((s) => [s.id, s.name])), [servers.data]);
  const dream = useMutation({
    mutationFn: () => api.post("/dreams"),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["knowledge"] }); toast("rAIden is dreaming: it reads what happened since its last dream."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const open = (k: Knowledge) => setEditing({ item: k, draft: { id: k.id, kind: k.kind, title: k.title, body: k.body, server: k.scope.server ?? "", environment: k.scope.environment ?? "",
    category: k.scope.category ?? "", retention: k.retention } });
  // a link from a chat or an incident opens what it points at
  const itemParam = params.get("item");
  useEffect(() => {
    const k = itemParam && page.data?.items.find((x) => x.id === Number(itemParam));
    if (k) { open(k); setParams({}, { replace: true }); }
  }, [itemParam, page.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const highlight = Number(params.get("approval") ?? 0);
  useEffect(() => { if (highlight && page.data) document.getElementById(`approval-${highlight}`)?.scrollIntoView({ block: "center" }); }, [highlight, page.data]);

  if (page.isLoading || !page.data) return <Loading />;
  const data = page.data;
  const items = data.items;
  const live = items.filter((k) => k.status === "active");
  const instructions = live.filter((k) => k.kind !== "skill");
  const playbooks = live.filter((k) => k.kind === "skill");
  const proposedItems = items.filter((k) => k.status === "proposed");
  const proposedApprovals = data.learned_approvals.filter((la) => la.status === "proposed");
  const settled = data.learned_approvals.filter((la) => la.status !== "proposed");
  const retired = items.filter((k) => k.status === "retired" || k.status === "expired");
  const needle = q.trim().toLowerCase();
  const shown = instructions.filter((k) => (kind === "all" || k.kind === kind) && (!needle || `${k.title} ${k.body} ${k.scope_text}`.toLowerCase().includes(needle)));
  const lastDream = data.dreams.find((d) => d.status !== "running");
  const waiting = proposedApprovals.length + proposedItems.length;
  const notAsked = settled.filter((la) => la.status === "active").length;

  return (
    <div>
      <PageHeader title="Knowledge"
        sub="What your team taught rAIden and what it learned from your decisions. It goes into every incident, chat and routine it applies to, so nobody has to say it twice; what rAIden only inferred waits for you."
        actions={canOperate && <>
          <Button onClick={() => dream.mutate()} loading={dream.isPending || data.dreaming} disabled={data.dreaming}><MoonStar className="size-4" /> {data.dreaming ? "Dreaming…" : "Dream now"}</Button>
          <Button variant="primary" onClick={() => setEditing({ draft: { ...blank } })}><Plus className="size-4" /> Teach rAIden</Button>
        </>} />

      <div className="mb-4 flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px] text-ink-2">
        <span><b className="text-ink num">{instructions.length}</b> instructions</span>
        <span><b className="text-ink num">{playbooks.length}</b> playbooks</span>
        {notAsked > 0 && <span><b className="text-ink num">{notAsked}</b> plan{notAsked === 1 ? "" : "s"} no longer asked about</span>}
        {waiting > 0 && <span className="font-semibold text-lamp-ink">{waiting} waiting for you</span>}
        <span className="text-ink-3">{data.policy.learn_approvals ? `rAIden offers not to ask again after ${data.policy.learn_after} approvals in a row` : "Learning from approvals is off (Autonomy)"}</span>
      </div>

      {waiting > 0 && (
        <Card className="mb-4 border-lamp-line bg-lamp-soft/40">
          <CardHeader title="Waiting for you" sub="rAIden proposes these from what people did. Nothing changes until a person says yes." />
          <ul className="space-y-2 px-4 pb-4">
            {proposedApprovals.map((la) => <ApprovalProposal key={la.id} la={la} isAdmin={isAdmin} canOperate={canOperate} highlight={la.id === highlight} />)}
            {proposedItems.map((k) => <InstructionProposal key={k.id} k={k} servers={names} canOperate={canOperate} onOpen={() => open(k)} />)}
          </ul>
        </Card>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_400px]">
        <div className="min-w-0">
          <Tabs value={tab} onChange={setTab} className="mb-3" tabs={[
            { id: "instructions", label: "Instructions", count: instructions.length },
            { id: "playbooks", label: "Playbooks", count: playbooks.length },
            { id: "approvals", label: "Not asked any more", count: notAsked },
            { id: "retired", label: "Retired", count: retired.length },
          ]} />
          {tab === "instructions" && (
            <Card>
              <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
                <Segmented size="sm" value={kind} onChange={setKind} options={[{ id: "all", label: "All" }, { id: "rule", label: "Rules" }, { id: "fact", label: "Facts" }, { id: "preference", label: "Preferences" }]} />
                <div className="relative ml-auto">
                  <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-ink-3" />
                  <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find" aria-label="Find an instruction" className="h-8 w-48 pl-8 text-[13px]" />
                </div>
              </div>
              {shown.length ? <ul className="divide-y divide-line">{shown.map((k) => <Row key={k.id} k={k} servers={names} onOpen={() => open(k)} canOperate={canOperate} />)}</ul>
                : <EmptyState icon={<GraduationCap className="size-5" />} title={instructions.length ? "Nothing matches" : "Nothing taught yet"}>
                  {instructions.length ? "Try another word or kind." : "Teach rAIden a rule, a fact or a preference, tell it in a chat, or let tonight's dream learn from your decisions."}
                </EmptyState>}
            </Card>
          )}
          {tab === "playbooks" && (playbooks.length ? <div className="space-y-4">{playbooks.map((k) => <Playbook key={k.id} k={k} onOpen={() => open(k)} />)}</div>
            : <Card><EmptyState icon={<ListChecks className="size-5" />} title="No playbooks yet">A dream writes one when the same kind of incident was fixed the same way twice. You can write one too.</EmptyState></Card>)}
          {tab === "approvals" && (
            <Card>
              <CardHeader title="Plans rAIden no longer asks about" sub="People approved them every time and accepted not to be asked. The plan must be the same kind, high-risk steps always wait, and the next rejection takes it back." />
              {settled.length ? <ul className="divide-y divide-line">{settled.map((la) => <NotAsked key={la.id} la={la} canOperate={canOperate} />)}</ul>
                : <EmptyState title="rAIden still asks about everything">After {data.policy.learn_after} approvals in a row of the same plan for the same kind of incident, it offers not to ask again.</EmptyState>}
            </Card>
          )}
          {tab === "retired" && (
            <Card>
              {retired.length ? <ul className="divide-y divide-line">{retired.map((k) => <Row key={k.id} k={k} servers={names} onOpen={() => open(k)} canOperate={canOperate} />)}</ul>
                : <EmptyState title="Nothing retired">Retired and expired instructions stay here with their history.</EmptyState>}
            </Card>
          )}
        </div>

        <Card className="self-start">
          <CardHeader title="Dreams" sub={<>Every night rAIden reads what the day brought and keeps what is worth keeping. Nothing new, no model call.
            {data.dream_routine && <> {data.dream_routine.enabled ? <>Next: {until(data.dream_routine.next_run_at)}.</> : <>Switched off on <Link to="/routines" className="font-semibold hover:underline">Routines</Link>.</>}</>}</>} />
          {data.dreams.length ? (
            <ol className="max-h-[920px] overflow-y-auto px-4 pt-1 pb-2">
              {data.dreams.map((d, i) => <DreamEntry key={d.id} d={d} isAdmin={isAdmin} last={i === data.dreams.length - 1} />)}
            </ol>
          ) : <EmptyState icon={<Moon className="size-5" />} title="No dreams yet">{lastDream ? "" : "The first one runs tonight, or now with Dream now."}</EmptyState>}
        </Card>
      </div>

      {editing && <Editor key={editing.draft.id ?? "new"} draft={editing.draft} item={editing.item} page={data} onClose={() => setEditing(null)} canEdit={canOperate} />}
    </div>
  );
}
