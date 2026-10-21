import { useMutation } from "@tanstack/react-query";
import { MessageSquareText, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { Badge, Button, Card, CardHeader, DataTable, ErrorNote, Input, KeyValue, Loading, Segmented, Select, Switch } from "../../components/ui";
import { api } from "../../lib/api";
import { bytes, duration } from "../../lib/format";
import { useLive } from "../../lib/hooks";
import type { Conversation, ServerDetail, Table } from "../../lib/types";
import { useActionDialog } from "./actions";

/** Table rows as objects keyed by column. */
export function rowsOf(t: Table | undefined): Record<string, any>[] {
  if (!t) return [];
  return t.rows.map((r) => Object.fromEntries(t.columns.map((c, i) => [c, r[i]])));
}
const col = (t: Table | undefined, row: any[], name: string) => (t ? row[t.columns.indexOf(name)] : undefined);

function useAsk(server: ServerDetail) {
  const nav = useNavigate();
  return useMutation({
    mutationFn: (q: string) => api.post<Conversation>("/chat/conversations", { server_id: server.id, title: q.slice(0, 80) }).then((c) => ({ c, q })),
    onSuccess: ({ c, q }) => nav(`/chat/${c.id}?q=${encodeURIComponent(q)}`),
  });
}

// ------------------------------------------------------------------------------------------------ sessions & locks
export function SessionsTab({ server, canOperate }: { server: ServerDetail; canOperate: boolean }) {
  const [state, setState] = useState("");
  const [role, setRole] = useState("");
  const params = new URLSearchParams();
  if (state) params.set("state", state);
  if (role) params.set("role", role);
  const sessions = useLive(server.id, "sessions", params.toString());
  const locks = useLive(server.id, "locks");
  const [act, dialog] = useActionDialog(server.id, server.name);
  const ask = useAsk(server);
  const lockRows = rowsOf(locks.data);
  const roots = new Set<number>((locks.data?.roots as number[] | undefined) ?? []);
  // everyone a root holds up, directly or through the sessions queued behind it
  const behind = useMemo(() => {
    const waiters = new Map<number, number[]>();
    for (const [w, b] of (locks.data?.edges as [number, number][] | undefined) ?? []) waiters.set(b, [...(waiters.get(b) ?? []), w]);
    const count = (root: number) => {
      const seen = new Set<number>();
      const todo = [root];
      while (todo.length) for (const w of waiters.get(todo.pop()!) ?? []) if (!seen.has(w)) { seen.add(w); todo.push(w); }
      return seen.size;
    };
    return new Map([...roots].map((r) => [r, count(r)]));
  }, [locks.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const waiting = (locks.data?.waiting_total as number | undefined) ?? lockRows.filter((r) => (r.blocked_by ?? []).length).length;
  const byRole = (sessions.data?.by_role_state as [string, string, number][] | undefined) ?? [];
  const actions = (row: any[], t: Table | undefined) => {
    if (!canOperate) return null;
    const pid = col(t, row, "pid");
    const st = col(t, row, "state");
    const who = `${col(t, row, "usename")} · ${col(t, row, "application_name") || "no application name"}`;
    return (
      <span className="inline-flex gap-1">
        {st === "active" && <Button size="sm" variant="ghost" onClick={() => act({ action: "cancel_backend", args: { pid }, title: `Cancel the query of ${pid}?`, detail: <div className="text-[12.5px] text-ink-2">{who}</div> })}>Cancel</Button>}
        <Button size="sm" variant="ghost" className="text-crit" onClick={() => act({ action: "terminate_backend", args: { pid }, title: `End session ${pid}?`, detail: <div className="text-[12.5px] text-ink-2">{who}{col(t, row, "xact_age_s") ? ` · transaction open for ${duration(col(t, row, "xact_age_s"))}` : ""}</div> })}>End</Button>
      </span>
    );
  };
  return (
    <div className="space-y-4">
      {lockRows.length > 0 && (
        <Card className="border-lamp-line">
          <CardHeader title="Lock waits" sub={`${waiting} session${waiting === 1 ? "" : "s"} waiting. The sessions at the head of the queue hold the locks everyone else waits for.`}
            actions={<Button size="sm" variant="ghost" onClick={() => ask.mutate("Who is blocking whom right now, and what is the safest way to clear it?")} loading={ask.isPending}><MessageSquareText className="size-3.5" /> Ask rAIden</Button>} />
          <div className="space-y-2 px-4 pb-3">
            {lockRows.filter((r) => roots.has(r.pid)).map((r) => (
              <div key={r.pid} className="flex flex-wrap items-start gap-3 rounded-lg border border-lamp-line bg-lamp-soft/50 px-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2 text-[13px]">
                    <span className="font-semibold">Head of the queue: pid {r.pid}</span>
                    <Badge tone="lamp">{roots.size === 1 ? Math.max(behind.get(r.pid) ?? 0, waiting) : behind.get(r.pid) ?? r.blocks} waiting behind it</Badge>
                    <span className="text-ink-3">{r.usename} · {r.application_name || "no application name"} · {r.state}{r.xact_age_s ? ` · transaction open ${duration(r.xact_age_s)}` : ""}</span>
                  </div>
                  <div className="mt-1 line-clamp-2 font-mono text-[11.5px] text-ink-2">{r.query}</div>
                </div>
                {canOperate && (
                  <div className="flex gap-1.5">
                    {r.state === "active" && <Button size="sm" onClick={() => act({ action: "cancel_backend", args: { pid: r.pid }, title: `Cancel the query of ${r.pid}?` })}>Cancel query</Button>}
                    <Button size="sm" variant="danger" onClick={() => act({ action: "terminate_backend", args: { pid: r.pid }, title: `End the blocking session ${r.pid}?`, detail: <div className="text-[12.5px] text-ink-2">{roots.size === 1 ? waiting : behind.get(r.pid) ?? r.blocks} sessions wait behind it. Its open transaction is rolled back.</div> })}>End session</Button>
                  </div>
                )}
              </div>
            ))}
          </div>
          <div className="max-h-80 overflow-auto border-t border-line">
            <DataTable data={locks.data} dense hide={["datname", "wait_event_type"]} highlight={(row) => roots.has(col(locks.data, row, "pid"))} />
          </div>
        </Card>
      )}

      <Card>
        <CardHeader title="Sessions" sub="Client backends, oldest transaction first. Refreshes every 10 seconds." />
        {byRole.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-4 pb-3">
            {byRole.slice(0, 10).map(([u, s, n]) => (
              <button key={`${u}-${s}`} onClick={() => { setRole(u); setState(s === "idle in transaction" || s === "active" || s === "idle" ? s : ""); }}
                className="rounded-md border border-line bg-surface-2 px-2 py-0.5 text-[12px] hover:border-line-strong">
                <span className="font-semibold">{u}</span> <span className="text-ink-3">{s}</span> <span className="font-semibold num">{n}</span>
              </button>
            ))}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2 px-4 pb-3">
          <Segmented size="sm" value={state} onChange={setState} options={[{ id: "", label: "All" }, { id: "active", label: "Active" }, { id: "idle in transaction", label: "Idle in transaction" }, { id: "idle", label: "Idle" }]} />
          <Input value={role} onChange={(e) => setRole(e.target.value)} placeholder="Role" className="h-8 w-40" />
          {(state || role) && <button className="text-[12.5px] font-semibold text-arc-ink hover:underline" onClick={() => { setState(""); setRole(""); }}>Clear</button>}
        </div>
        {sessions.error ? <div className="px-4 pb-4"><ErrorNote error={sessions.error} /></div> : sessions.isLoading ? <Loading /> : (
          <div className="max-h-[560px] overflow-auto border-t border-line">
            <DataTable data={sessions.data} dense hide={["client_addr", "backend_age_s"]} rowActions={(row) => actions(row, sessions.data)}
              highlight={(row) => (col(sessions.data, row, "blocked_by") ?? []).length > 0 || (col(sessions.data, row, "state") === "idle in transaction" && (col(sessions.data, row, "xact_age_s") ?? 0) > 60)} />
          </div>
        )}
      </Card>
      {dialog}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ queries
export function QueriesTab({ server }: { server: ServerDetail }) {
  const [order, setOrder] = useState("total_time");
  const q = useLive(server.id, "queries", `order_by=${order}`);
  const ask = useAsk(server);
  return (
    <Card>
      <CardHeader title="Top statements" sub="From pg_stat_statements since its last reset." actions={
        <Select value={order} onChange={(e) => setOrder(e.target.value)} className="h-8 w-48">
          <option value="total_time">Total time</option><option value="mean_time">Mean time</option><option value="calls">Calls</option><option value="io">Cache misses</option><option value="temp">Temp files</option>
        </Select>} />
      {q.error ? <div className="px-4 pb-4"><ErrorNote error={q.error} /></div> : q.isLoading ? <Loading /> : (
        <div className="border-t border-line">
          <DataTable data={q.data} hide={["queryid"]} rowActions={(row) => (
            <Button size="sm" variant="ghost" loading={ask.isPending && ask.variables?.includes(String(col(q.data, row, "queryid")))}
              onClick={() => ask.mutate(`Why is this statement expensive and how do I make it cheaper? (queryid ${col(q.data, row, "queryid")})\n\n${col(q.data, row, "query")}`)}>
              <MessageSquareText className="size-3.5" /> Ask
            </Button>
          )} />
        </div>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------------------------------------ tables, databases, wraparound
export function TablesTab({ server, canOperate }: { server: ServerDetail; canOperate: boolean }) {
  const dbs = useLive(server.id, "databases");
  const dbNames = useMemo(() => rowsOf(dbs.data).map((r) => r.datname as string), [dbs.data]);
  const [db, setDb] = useState("");
  const database = db || server.databases[0] || dbNames[0] || "";
  const tables = useLive(server.id, "tables", database ? `database=${encodeURIComponent(database)}` : "", !!database);
  const wrap = useLive(server.id, "wraparound");
  const [act, dialog] = useActionDialog(server.id, server.name);
  const holders = wrap.data?.xmin_holders as Table | undefined;
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader title="Databases" />
        <div className="border-t border-line"><DataTable data={dbs.data} dense /></div>
      </Card>
      <Card>
        <CardHeader title="Tables" sub="Most dead rows first: what autovacuum is behind on." actions={
          <Select value={database} onChange={(e) => setDb(e.target.value)} className="h-8 w-48">
            {(dbNames.length ? dbNames : server.databases).map((d) => <option key={d} value={d}>{d}</option>)}
          </Select>} />
        {tables.error ? <div className="px-4 pb-4"><ErrorNote error={tables.error} /></div> : (
          <div className="border-t border-line">
            <DataTable data={tables.data} dense highlight={(row) => (col(tables.data, row, "dead_pct") ?? 0) >= 20}
              rowActions={canOperate ? (row) => {
                const table = col(tables.data, row, "table_name");
                return (
                  <span className="inline-flex gap-1">
                    <Button size="sm" variant="ghost" onClick={() => act({ action: "vacuum_table", args: { database, table }, title: `Vacuum ${table}?` })}>Vacuum</Button>
                    <Button size="sm" variant="ghost" onClick={() => act({ action: "analyze_table", args: { database, table }, title: `Analyze ${table}?` })}>Analyze</Button>
                  </span>
                );
              } : undefined} />
          </div>
        )}
      </Card>
      <Card>
        <CardHeader title="Transaction ID wraparound" sub={`Autovacuum freezes at ${((wrap.data?.autovacuum_freeze_max_age as number | undefined) ?? 200_000_000).toLocaleString("en-US")} transactions; PostgreSQL stops writes near 2.1 billion.`} />
        <div className="border-t border-line"><DataTable data={wrap.data} dense highlight={(row) => (col(wrap.data, row, "pct_of_limit") ?? 0) >= 50} /></div>
        {holders && holders.rows.length > 0 && (
          <div className="border-t border-line">
            <div className="px-4 pt-3 pb-1 text-[12.5px] font-semibold text-ink-2">What holds the oldest snapshot back</div>
            <DataTable data={holders} dense />
          </div>
        )}
      </Card>
      {dialog}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ replication
export function ReplicationTab({ server, canOperate }: { server: ServerDetail; canOperate: boolean }) {
  const rep = useLive(server.id, "replication");
  const [act, dialog] = useActionDialog(server.id, server.name);
  if (rep.error) return <ErrorNote error={rep.error} />;
  if (!rep.data) return <Loading />;
  const slots = rep.data.slots as Table | undefined;
  if (rep.data.role === "standby") {
    const r = rowsOf(rep.data)[0] ?? {};
    return (
      <Card>
        <CardHeader title="Standby" sub="This server replays WAL from its primary." />
        <div className="px-4 pb-4">
          <KeyValue items={[
            ["Status", r.status ?? "—"],
            ["Primary", r.sender_host ? <span className="mono">{r.sender_host}:{r.sender_port}</span> : "—"],
            ["Slot", r.slot_name ?? "—"],
            ["Replay lag", rep.data.replay_lag_s != null ? duration(rep.data.replay_lag_s as number) : "—"],
            ["Replay", String(rep.data.replay_pause_state ?? "—")],
            ...(rep.data.replay_waiting_for ? [["Waiting for", <span className="text-warn">{String(rep.data.replay_waiting_for)}</span>] as [string, React.ReactNode]] : []),
          ]} />
        </div>
      </Card>
    );
  }
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader title="Standbys" sub="Connected replicas and how far behind they are." />
        <div className="border-t border-line"><DataTable data={rep.data} /></div>
      </Card>
      {slots && (
        <Card>
          <CardHeader title="Replication slots" sub="An inactive slot keeps WAL forever: it can fill the disk." />
          <div className="border-t border-line">
            <DataTable data={slots} highlight={(row) => col(slots, row, "active") === false}
              rowActions={canOperate ? (row) => col(slots, row, "active") === false ? (
                <Button size="sm" variant="danger" onClick={() => act({ action: "drop_replication_slot", args: { slot: col(slots, row, "slot_name") }, title: `Drop the slot ${col(slots, row, "slot_name")}?`,
                  detail: <div className="text-[12.5px] text-ink-2">Frees {bytes(col(slots, row, "retained_bytes"))} of WAL. The consumer of this slot must be set up again.</div> })}>Drop</Button>
              ) : null : undefined} />
          </div>
        </Card>
      )}
      {dialog}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ configuration
export function ConfigurationTab({ server }: { server: ServerDetail }) {
  const [pattern, setPattern] = useState("");
  const [pending, setPending] = useState(false);
  const params = new URLSearchParams();
  if (pattern) params.set("pattern", pattern);
  if (pending) params.set("pending", "true");
  const st = useLive(server.id, "settings", params.toString());
  const pendingCount = rowsOf(st.data).filter((r) => r.pending_restart).length;
  return (
    <Card>
      <CardHeader title="Server settings" sub={pendingCount ? <span className="text-lamp-ink">{pendingCount} setting{pendingCount === 1 ? "" : "s"} changed in a file but not active until a restart.</span> : "Current values and where they come from."} />
      <div className="flex flex-wrap items-center gap-3 px-4 pb-3">
        <div className="relative">
          <Search className="pointer-events-none absolute top-2 left-2.5 size-4 text-ink-3" />
          <Input value={pattern} onChange={(e) => setPattern(e.target.value)} placeholder="Filter settings" className="h-8 w-56 pl-8" />
        </div>
        <label className="flex items-center gap-2 text-[12.5px] font-semibold text-ink-2"><Switch size="sm" checked={pending} onChange={setPending} /> Only waiting for a restart</label>
      </div>
      {st.error ? <div className="px-4 pb-4"><ErrorNote error={st.error} /></div> : (
        <div className="max-h-[620px] overflow-auto border-t border-line">
          <DataTable data={st.data} dense maxRows={500} hide={["short_desc", "sourceline"]} highlight={(row) => !!col(st.data, row, "pending_restart")} />
        </div>
      )}
    </Card>
  );
}
