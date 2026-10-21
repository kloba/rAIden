import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, ChevronRight, MessageSquarePlus, Terminal, Trash2, Zap } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { ApprovalBox } from "../components/incident";
import { Badge, Card, cn, DataTable, EmptyState, IconButton, Loading, Markdown, Select, ServerTag, Spinner, Time, useToast } from "../components/ui";
import { api, postStream } from "../lib/api";
import { compact, money } from "../lib/format";
import { useConversation, useConversations, useMe, useServers } from "../lib/hooks";
import type { ChatMessage, Conversation, Server } from "../lib/types";

const SUGGESTIONS = (server: Server | undefined) => server ? [
  `Who is blocking whom on ${server.name} right now?`,
  `What changed on ${server.name} in the last hour?`,
  "Which statements got slower today, and why?",
  "Is autovacuum keeping up? Which tables are behind?",
  "How far is each database from transaction ID wraparound?",
] : [
  "Which server needs attention first, and why?",
  "Compare connection usage across production",
  "Where is replication lagging?",
  "Which servers have settings waiting for a restart?",
];

function argsText(args: Record<string, unknown> | undefined) {
  return Object.entries(args ?? {}).map(([k, v]) => `${k}=${typeof v === "string" ? (v.length > 60 ? `${v.slice(0, 60)}…` : v) : JSON.stringify(v)}`).join(", ");
}

function RaidenMark({ busy }: { busy?: boolean }) {
  return (
    <span className={cn("flex size-7 shrink-0 items-center justify-center rounded-full bg-ink", busy && "working")}>
      <svg viewBox="0 0 32 32" className="size-4" aria-hidden><path d="M18.5 3 8 18h7l-2 11 11-16h-7l1.5-10z" fill="var(--arc)" /></svg>
    </span>
  );
}

function ToolRow({ m }: { m: ChatMessage }) {
  const [open, setOpen] = useState(false);
  const d = m.data ?? {};
  const preview = d.preview;
  const isTable = preview && typeof preview === "object" && Array.isArray(preview.rows);
  if (d.name === "decision") {
    return <div className={cn("rounded-lg px-3 py-1.5 text-[12.5px] font-semibold", d.ok ? "bg-ok-soft text-ok" : "bg-crit-soft text-crit")}>{preview?.decision}</div>;
  }
  if (d.name === "open_incident" && preview?.incident_id) {
    return <Link to={`/incidents/${preview.incident_id}`} className="block rounded-lg border border-arc-line bg-arc-soft px-3 py-1.5 text-[12.5px] font-semibold text-arc-ink hover:underline">Opened {preview.incident_id}: the incident pipeline takes over</Link>;
  }
  return (
    <div className="rounded-lg border border-line bg-surface">
      <button onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[12px]" aria-expanded={open}>
        <ChevronRight className={cn("size-3.5 shrink-0 text-ink-3 transition-transform", open && "rotate-90")} />
        <Terminal className="size-3.5 shrink-0 text-ink-3" />
        <span className="mono shrink-0 font-semibold text-ink">{d.name}</span>
        <span className="mono min-w-0 truncate text-ink-3">{argsText(d.args)}</span>
        <span className="ml-auto shrink-0 text-ink-3 num">{d.ms != null ? `${d.ms} ms` : ""}</span>
        {d.ok === false && <Badge tone="crit">failed</Badge>}
      </button>
      {open && preview != null && (
        <div className="max-h-72 overflow-auto border-t border-line">
          {isTable ? <DataTable data={preview} dense /> : <pre className="p-2.5 text-[11.5px] whitespace-pre-wrap">{typeof preview === "string" ? preview : JSON.stringify(preview, null, 2)}</pre>}
        </div>
      )}
    </div>
  );
}

type Block = { kind: "user" | "assistant" | "error" | "approval"; m: ChatMessage } | { kind: "tools"; items: ChatMessage[] };

function blocks(messages: ChatMessage[]): Block[] {
  const out: Block[] = [];
  for (const m of messages) {
    if (m.role === "tool") {
      const last = out[out.length - 1];
      if (last && last.kind === "tools") last.items.push(m);
      else out.push({ kind: "tools", items: [m] });
    } else out.push({ kind: m.role === "user" ? "user" : m.role === "approval" ? "approval" : m.role === "error" ? "error" : "assistant", m });
  }
  return out;
}

function Thread({ conv, extra, live, canDecide }: { conv: Conversation; extra: ChatMessage[]; live: { name: string; args: Record<string, unknown> }[] | null; canDecide: boolean }) {
  const ids = new Set((conv.messages ?? []).map((m) => m.id));
  const all = [...(conv.messages ?? []), ...extra.filter((m) => !ids.has(m.id))];
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [all.length, live?.length]);
  return (
    <div className="space-y-4">
      {blocks(all).map((b, i) => {
        if (b.kind === "tools") {
          return (
            <div key={`t${b.items[0]!.id}`} className="ml-10 space-y-1">
              <div className="text-[11.5px] font-semibold text-ink-3">{b.items.length === 1 ? "1 step" : `${b.items.length} steps`}</div>
              {b.items.map((m) => <ToolRow key={m.id} m={m} />)}
            </div>
          );
        }
        const m = b.m;
        if (b.kind === "user") {
          return (
            <div key={m.id} className="flex justify-end">
              <div className="max-w-[78%] rounded-2xl rounded-br-md bg-ink px-4 py-2.5 text-[14px] whitespace-pre-wrap text-surface">{m.content}</div>
            </div>
          );
        }
        if (b.kind === "approval") {
          const a = conv.approvals?.[String(m.data?.approval_id)];
          return <div key={m.id} className="ml-10">{a ? <ApprovalBox approval={a} canDecide={canDecide} /> : <div className="text-[12.5px] text-ink-3">{m.content}</div>}</div>;
        }
        if (b.kind === "error") return <div key={m.id} className="ml-10 rounded-lg border border-crit/25 bg-crit-soft px-3 py-2 text-[13px] text-crit">{m.content}</div>;
        const u = m.data ?? {};
        return (
          <div key={m.id ?? i} className="flex gap-3">
            <RaidenMark />
            <div className="min-w-0 flex-1">
              <Markdown className="text-[14px]">{m.content}</Markdown>
              {(u.tokens_in || u.model) && (
                <div className="mt-1.5 text-[11.5px] text-ink-3 num">
                  {u.model}{u.simulated ? " (simulated)" : ""} · {compact((u.tokens_in ?? 0) + (u.tokens_out ?? 0))} tokens · {money(u.cost_usd)}
                </div>
              )}
            </div>
          </div>
        );
      })}
      {(live || conv.busy) && (
        <div className="flex gap-3">
          <RaidenMark busy />
          <div className="min-w-0 flex-1 pt-1">
            <div className="flex items-center gap-2 text-[13px] font-semibold text-arc-ink"><Spinner className="size-3.5 text-arc" /> {live?.length ? "Looking into it" : "Thinking"}</div>
            {live && live.length > 0 && (
              <ul className="mt-1.5 space-y-0.5">
                {live.map((s, i) => <li key={i} className="mono truncate text-[12px] text-ink-3">{s.name}({argsText(s.args)})</li>)}
              </ul>
            )}
          </div>
        </div>
      )}
      <div ref={end} />
    </div>
  );
}

function Composer({ onSend, disabled, initial, placeholder }: { onSend: (text: string) => void; disabled?: boolean; initial?: string; placeholder: string }) {
  const [text, setText] = useState(initial ?? "");
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (initial) setText(initial); }, [initial]);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [text]);
  const send = () => {
    const t = text.trim();
    if (!t || disabled) return;
    onSend(t);
    setText("");
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };
  return (
    <div className="flex items-end gap-2 rounded-2xl border border-line-strong bg-surface p-2 shadow-card focus-within:border-arc focus-within:ring-2 focus-within:ring-arc/20">
      <textarea ref={ref} rows={1} value={text} onChange={(e) => setText(e.target.value)} onKeyDown={onKey} placeholder={placeholder}
        className="max-h-[180px] min-h-[38px] flex-1 resize-none bg-transparent px-2 py-2 text-[14px] text-ink placeholder:text-ink-3 focus:outline-none" />
      <button onClick={send} disabled={disabled || !text.trim()} aria-label="Send"
        className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-arc text-on-arc transition-opacity hover:bg-arc-ink disabled:opacity-40">
        <ArrowUp className="size-4" />
      </button>
    </div>
  );
}

export function ChatPage() {
  const { id } = useParams();
  const [params, setParams] = useSearchParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const me = useMe();
  const canOperate = me.data?.role !== "viewer";
  const list = useConversations();
  const conv = useConversation(id);
  const servers = useServers();
  const [focus, setFocus] = useState("");
  const [extra, setExtra] = useState<ChatMessage[]>([]);
  const [live, setLive] = useState<{ name: string; args: Record<string, unknown> }[] | null>(null);
  const sentInitial = useRef<string | null>(null);
  const serverOf = useMemo(() => new Map((servers.data ?? []).map((s) => [s.id, s])), [servers.data]);
  const current = conv.data;
  const focusServer = current ? serverOf.get(current.server_id ?? "") : serverOf.get(focus);

  const send = async (text: string, convId = id) => {
    if (!convId) return;
    setLive([]);
    setExtra([]);
    let n = -1;
    try {
      await postStream(`/chat/conversations/${convId}/messages`, { content: text }, (e) => {
        if (e.event === "message") setExtra((x) => [...x, { id: e.data.id, role: e.data.role, content: e.data.content, data: e.data.usage ?? {}, created_at: new Date().toISOString() }]);
        else if (e.event === "step") setLive((x) => [...(x ?? []), { name: e.data.name, args: e.data.args }]);
        else if (e.event === "tool") {
          setLive((x) => (x ?? []).slice(1));
          setExtra((x) => [...x, { id: e.data.id ?? n--, role: "tool", content: "", data: e.data, created_at: new Date().toISOString() }]);
        } else if (e.event === "approval" || e.event === "incident") qc.invalidateQueries({ queryKey: ["conversation", convId] });
        else if (e.event === "error") toast(e.data.message, "crit");
      });
    } catch (err) {
      toast((err as Error).message, "crit");
    } finally {
      setLive(null);
      await qc.invalidateQueries({ queryKey: ["conversation", convId] });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      setExtra([]);
    }
  };

  const create = useMutation({
    mutationFn: (first: string) => api.post<Conversation>("/chat/conversations", { server_id: focus || null, title: first.slice(0, 80) }).then((c) => ({ c, first })),
    onSuccess: ({ c, first }) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      nav(`/chat/${c.id}?send=${encodeURIComponent(first)}`);
    },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const remove = useMutation({
    mutationFn: (cid: string) => api.del(`/chat/conversations/${cid}`),
    onSuccess: (_d, cid) => { qc.invalidateQueries({ queryKey: ["conversations"] }); if (cid === id) nav("/chat"); },
  });

  // a question handed over from another page: ?send= sends it, ?q= only fills the box
  useEffect(() => {
    const s = params.get("send");
    if (id && s && current && sentInitial.current !== id) {
      sentInitial.current = id;
      setParams({}, { replace: true });
      if (!(current.messages ?? []).length) void send(s, id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, current]);

  const busy = live !== null || !!current?.busy;
  const initialQ = params.get("q") ?? undefined;

  return (
    <div className="-my-2 grid h-[calc(100vh-7rem)] min-h-[520px] gap-4 lg:grid-cols-[290px_minmax(0,1fr)]">
      <Card className="hidden min-h-0 flex-col lg:flex">
        <div className="flex items-center justify-between border-b border-line px-3 py-2.5">
          <span className="plate">Investigations</span>
          <IconButton label="New investigation" onClick={() => nav("/chat")}><MessageSquarePlus className="size-4" /></IconButton>
        </div>
        <ul className="min-h-0 flex-1 overflow-y-auto p-1.5">
          {(list.data ?? []).map((c) => {
            const s = serverOf.get(c.server_id ?? "");
            return (
              <li key={c.id} className="group relative">
                <Link to={`/chat/${c.id}`} className={cn("block rounded-lg px-2.5 py-2 pr-8", c.id === id ? "bg-surface-3" : "hover:bg-surface-2")}>
                  <div className="line-clamp-1 text-[13px] font-semibold text-ink">{c.title}</div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-ink-3">
                    {s ? <ServerTag name={s.name} slot={s.color_slot} className="font-medium text-ink-2" /> : <span>Whole estate</span>}
                    <span>·</span><Time iso={c.updated_at} />
                  </div>
                </Link>
                {canOperate && (
                  <IconButton label="Delete" className="absolute top-2 right-1 size-7 opacity-0 group-hover:opacity-100 focus:opacity-100" onClick={() => remove.mutate(c.id)}>
                    <Trash2 className="size-3.5" />
                  </IconButton>
                )}
              </li>
            );
          })}
          {list.data?.length === 0 && <li className="px-3 py-6 text-center text-[12.5px] text-ink-3">No investigations yet.</li>}
        </ul>
      </Card>

      <Card className="flex min-h-0 flex-col">
        {!id ? (
          <div className="flex flex-1 flex-col items-center justify-center overflow-y-auto px-6 py-10">
            <div className="w-full max-w-[680px]">
              <div className="mb-1 flex items-center gap-2.5"><RaidenMark /><span className="display text-[24px]">What should rAIden look into?</span></div>
              <p className="mb-5 text-[13.5px] text-ink-2">It reads live statistics, sessions, locks, plans and history with read-only tools. If it wants to change something, it asks you first.</p>
              <div className="mb-3 flex items-center gap-2">
                <span className="text-[12.5px] font-semibold text-ink-2">Focus</span>
                <Select aria-label="Server in focus" value={focus} onChange={(e) => setFocus(e.target.value)} className="h-8 w-64">
                  <option value="">The whole estate</option>
                  {servers.data?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </Select>
              </div>
              <Composer onSend={(t) => create.mutate(t)} disabled={!canOperate || create.isPending} initial={initialQ} placeholder={canOperate ? "Ask about a server, a query, a symptom…" : "Viewers can read investigations but not start them"} />
              <div className="mt-4 flex flex-wrap gap-2">
                {SUGGESTIONS(serverOf.get(focus)).map((s) => (
                  <button key={s} disabled={!canOperate} onClick={() => create.mutate(s)} className="rounded-full border border-line bg-surface-2 px-3 py-1.5 text-left text-[12.5px] text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50">{s}</button>
                ))}
              </div>
            </div>
          </div>
        ) : conv.isLoading || !current ? <Loading /> : (
          <>
            <div className="flex items-center gap-3 border-b border-line px-4 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="truncate text-[14px] font-semibold">{current.title}</div>
                <div className="flex items-center gap-1.5 text-[12px] text-ink-3">
                  {focusServer ? <>Focus <Link to={`/servers/${focusServer.id}`} className="hover:underline"><ServerTag name={focusServer.name} slot={focusServer.color_slot} /></Link></> : "Whole estate"}
                  {current.incident_id && <> · <Link to={`/incidents/${current.incident_id}`} className="font-semibold text-arc-ink hover:underline">{current.incident_id}</Link></>}
                  <span>· started by {current.user}</span>
                </div>
              </div>
              {busy && <Badge tone="arc"><Zap className="size-3" /> Working</Badge>}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 lg:px-8">
              {(current.messages ?? []).length === 0 && !live && extra.length === 0 ? (
                <EmptyState title="Ask the first question">
                  <span className="mt-3 flex flex-wrap justify-center gap-2">
                    {SUGGESTIONS(focusServer).slice(0, 4).map((s) => (
                      <button key={s} disabled={!canOperate} onClick={() => send(s)} className="rounded-full border border-line bg-surface-2 px-3 py-1.5 text-[12.5px] text-ink-2 hover:text-ink">{s}</button>
                    ))}
                  </span>
                </EmptyState>
              ) : <Thread conv={current} extra={extra} live={live} canDecide={canOperate} />}
            </div>
            <div className="border-t border-line p-3">
              <Composer onSend={(t) => send(t)} disabled={busy || !canOperate} initial={initialQ} placeholder={busy ? "rAIden is answering…" : "Ask a follow-up"} />
              <div className="mt-1.5 px-1 text-[11.5px] text-ink-3">Enter to send · Shift+Enter for a new line · read-only tools; changes need your approval</div>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}

